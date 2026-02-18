# PPTX 坐标转换器：工程实现 Spec（含解析模板）

目标：解析 `ppt/slides/slideN.xml`，为每个可见元素产出其在渲染 PNG 中的像素级轴对齐包围框（AABB），并生成 overlay 验证图。

---

## 0. 输入 / 输出

### 输入
- `pptx_path`: `.pptx`（zip）
- `slide_index`: N（从 1 开始）
- `png_path`: 该 slide 渲染出的 PNG（或 `png_width/png_height`）

### 输出
- `out.json`：元素列表与 bbox（像素坐标）
- `debug_overlay.png`：在 PNG 上绘制 bbox 的调试图（用于验证）

---

## 1. 坐标系与单位

- XML 坐标单位：EMU
- PNG 坐标单位：px
- Slide 尺寸：`ppt/presentation.xml` 的 `<p:sldSz cx cy>`（EMU）
- PNG 尺寸：从 PNG header 读取（W_px, H_px）

### 1.1 EMU → PX 映射（强制使用比例映射法）
- `sx = W_px / slide_cx_emu`
- `sy = H_px / slide_cy_emu`
- `x_px = x_emu * sx`
- `y_px = y_emu * sy`

> 禁止写死 DPI；仅当需要物理尺寸才用 `px = emu * dpi / 914400`。

---

## 2. 需要支持的元素类型（MVP）

遍历 `ppt/slides/slideN.xml`：
- `p:sld/p:cSld/p:spTree` 下递归：
  - `p:sp` (shape / textbox)
  - `p:pic` (picture)
  - `p:grpSp` (group, 递归)
  - `p:graphicFrame` (table/chart，MVP：只按自身 xfrm 做 bbox)

每个元素变换主要来自 `a:xfrm`：
- `a:off @x @y` （位置）
- `a:ext @cx @cy`（尺寸）
- 可选：`a:xfrm @rot @flipH @flipV`
- group 专用：`a:chOff @x @y`、`a:chExt @cx @cy`

---

## 3. 统一几何模型：仿射矩阵 + 四点 AABB

### 3.1 仿射矩阵（2x3）
```
x' = a*x + c*y + e
y' = b*x + d*y + f
```

- 局部矩形四点：`(0,0),(w,0),(w,h),(0,h)`
- 变换后取 min/max 得 AABB（EMU），再按 `sx/sy` 转 px

### 3.2 Rotation / Flip（绕元素中心）
- `rot` 单位：1/60000 度；`rad = (rot/60000) * π/180`
- PPT 旋转绕元素中心 `(w/2,h/2)`；flip 也按中心处理
- 坐标系按图像一致：x 向右、y 向下；正 rot 视为“顺时针”

---

## 4. Group 处理（必须支持）

`p:grpSp/a:xfrm` 含义：
- `off/ext`：group 在父坐标系的位置/大小
- `chOff/chExt`：group 子坐标系（虚拟画布）的原点/大小

规范矩阵链：
- 先归一化子坐标：`T(-chOff)`  
- 再缩放：`S(ext/chExt)`  
- 再做 group 自身 rot/flip（绕 group ext 中心）  
- 最后平移到 `off`

```
M_group = T(off) * AroundCenter(ext_w, ext_h, R/Flip) * S(ext/chExt) * T(-chOff)
```

递归：
- `M_total(child) = M_parent * M_group * M_child`

---

## 5. 输出 JSON（建议字段）

每个元素：
- `id`: `cNvPr/@id` 或稳定 hash
- `type`: `sp|pic|grpSp|graphicFrame`
- `name`: `cNvPr/@name`（可空）
- `z_index`: 元素在 XML 遍历中的顺序（保留遮挡分析价值）
- `bbox_emu`: `[min_x, min_y, max_x, max_y]`
- `bbox_px`: `[min_x, min_y, max_x, max_y]`
- `transform`: `off/ext/rot/flipH/flipV` + group 的 `chOff/chExt`
- `parent_id`: 可空
- `skipped_reason`: 可空

顶层：
- `slide_index`
- `png_size`: `[W_px, H_px]`
- `slide_size_emu`: `[cx, cy]`
- `sx/sy`

---

# 附录 A：2D 仿射矩阵实现（Python，可直接用）

> 说明：`self @ other` 表示“先应用 other，再应用 self”，即矩阵右乘链。

```python
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Iterable, Tuple, List

Point = Tuple[float, float]
Rect  = Tuple[float, float, float, float]  # (minx, miny, maxx, maxy)

@dataclass(frozen=True)
class Affine2D:
    # x' = a*x + c*y + e
    # y' = b*x + d*y + f
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    @staticmethod
    def identity() -> "Affine2D":
        return Affine2D()

    @staticmethod
    def translate(tx: float, ty: float) -> "Affine2D":
        return Affine2D(1, 0, 0, 1, tx, ty)

    @staticmethod
    def scale(sx: float, sy: float) -> "Affine2D":
        return Affine2D(sx, 0, 0, sy, 0, 0)

    @staticmethod
    def rotate_cw(rad: float) -> "Affine2D":
        # screen coords (y down): clockwise positive
        # Matrix [[cos, -sin], [sin, cos]] → a=cos, b=sin, c=-sin, d=cos
        cos_t = math.cos(rad)
        sin_t = math.sin(rad)
        return Affine2D(cos_t, sin_t, -sin_t, cos_t, 0, 0)

    @staticmethod
    def flip_h() -> "Affine2D":
        return Affine2D.scale(-1.0, 1.0)

    @staticmethod
    def flip_v() -> "Affine2D":
        return Affine2D.scale(1.0, -1.0)

    def __matmul__(self, other: "Affine2D") -> "Affine2D":
        a1,b1,c1,d1,e1,f1 = self.a,self.b,self.c,self.d,self.e,self.f
        a2,b2,c2,d2,e2,f2 = other.a,other.b,other.c,other.d,other.e,other.f
        return Affine2D(
            a=a1*a2 + c1*b2,
            b=b1*a2 + d1*b2,
            c=a1*c2 + c1*d2,
            d=b1*c2 + d1*d2,
            e=a1*e2 + c1*f2 + e1,
            f=b1*e2 + d1*f2 + f1
        )

    def apply_point(self, p: Point) -> Point:
        x,y = p
        return (self.a*x + self.c*y + self.e,
                self.b*x + self.d*y + self.f)

    def apply_points(self, pts: Iterable[Point]) -> List[Point]:
        return [self.apply_point(p) for p in pts]

    def apply_rect_aabb(self, w: float, h: float) -> Rect:
        corners = [(0.0,0.0),(w,0.0),(w,h),(0.0,h)]
        pts = self.apply_points(corners)
        xs = [x for x,_ in pts]
        ys = [y for _,y in pts]
        return (min(xs), min(ys), max(xs), max(ys))

def around_center(w: float, h: float, inner: Affine2D) -> Affine2D:
    cx, cy = w/2.0, h/2.0
    return Affine2D.translate(cx,cy) @ inner @ Affine2D.translate(-cx,-cy)

def element_matrix(off_x: float, off_y: float, w: float, h: float,
                   rot_60000: int = 0, flipH: bool = False, flipV: bool = False) -> Affine2D:
    rad = (rot_60000/60000.0) * (math.pi/180.0)
    inner = Affine2D.identity()
    if flipH: inner = Affine2D.flip_h() @ inner
    if flipV: inner = Affine2D.flip_v() @ inner
    if rot_60000: inner = Affine2D.rotate_cw(rad) @ inner
    return Affine2D.translate(off_x, off_y) @ around_center(w, h, inner)

def group_matrix(off_x: float, off_y: float, ext_w: float, ext_h: float,
                 chOff_x: float, chOff_y: float, chExt_w: float, chExt_h: float,
                 rot_60000: int = 0, flipH: bool = False, flipV: bool = False) -> Affine2D:
    sx = (ext_w/chExt_w) if chExt_w else 1.0
    sy = (ext_h/chExt_h) if chExt_h else 1.0

    base = Affine2D.scale(sx, sy) @ Affine2D.translate(-chOff_x, -chOff_y)

    rad = (rot_60000/60000.0) * (math.pi/180.0)
    inner = Affine2D.identity()
    if flipH: inner = Affine2D.flip_h() @ inner
    if flipV: inner = Affine2D.flip_v() @ inner
    if rot_60000: inner = Affine2D.rotate_cw(rad) @ inner

    return Affine2D.translate(off_x, off_y) @ around_center(ext_w, ext_h, inner) @ base
```

---

# 附录 B：Namespace-safe 解析模板（PPTX → Slide 元素 → xfrm）

```python
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Tuple

NS = {
    "p":  "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a":  "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r":  "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

def _get_int_attr(el: Optional[ET.Element], name: str, default: int = 0) -> int:
    if el is None: return default
    v = el.get(name)
    return int(v) if v is not None else default

def _get_bool_attr(el: Optional[ET.Element], name: str, default: bool = False) -> bool:
    if el is None: return default
    v = el.get(name)
    if v is None: return default
    return v in ("1","true","True")

@dataclass
class Xfrm:
    off_x: int = 0
    off_y: int = 0
    ext_w: int = 0
    ext_h: int = 0
    rot_60000: int = 0
    flipH: bool = False
    flipV: bool = False
    chOff_x: int = 0
    chOff_y: int = 0
    chExt_w: int = 0
    chExt_h: int = 0

def parse_xfrm(xfrm_el: Optional[ET.Element]) -> Optional[Xfrm]:
    if xfrm_el is None:
        return None

    off = xfrm_el.find("a:off", NS)
    ext = xfrm_el.find("a:ext", NS)
    if off is None or ext is None:
        return None

    x = Xfrm(
        off_x=_get_int_attr(off, "x", 0),
        off_y=_get_int_attr(off, "y", 0),
        ext_w=_get_int_attr(ext, "cx", 0),
        ext_h=_get_int_attr(ext, "cy", 0),
        rot_60000=_get_int_attr(xfrm_el, "rot", 0),
        flipH=_get_bool_attr(xfrm_el, "flipH", False),
        flipV=_get_bool_attr(xfrm_el, "flipV", False),
    )

    # group-only fields
    chOff = xfrm_el.find("a:chOff", NS)
    chExt = xfrm_el.find("a:chExt", NS)
    if chOff is not None:
        x.chOff_x = _get_int_attr(chOff, "x", 0)
        x.chOff_y = _get_int_attr(chOff, "y", 0)
    if chExt is not None:
        x.chExt_w = _get_int_attr(chExt, "cx", 0)
        x.chExt_h = _get_int_attr(chExt, "cy", 0)

    return x

def read_slide_size_emu(zf: zipfile.ZipFile) -> Tuple[int,int]:
    xml = zf.read("ppt/presentation.xml")
    root = ET.fromstring(xml)
    sldSz = root.find(".//p:sldSz", NS)
    if sldSz is None:
        raise ValueError("presentation.xml missing p:sldSz")
    cx = int(sldSz.get("cx"))
    cy = int(sldSz.get("cy"))
    return cx, cy

def read_slide_xml_root(zf: zipfile.ZipFile, slide_index: int) -> ET.Element:
    path = f"ppt/slides/slide{slide_index}.xml"
    xml = zf.read(path)
    return ET.fromstring(xml)

def find_spTree(slide_root: ET.Element) -> ET.Element:
    spTree = slide_root.find(".//p:cSld/p:spTree", NS)
    if spTree is None:
        raise ValueError("slide missing p:spTree")
    return spTree

def tag_localname(el: ET.Element) -> str:
    t = el.tag
    return t.split("}")[-1] if "}" in t else t

def get_cNvPr(node: ET.Element):
    """返回 (id, name, hidden)。hidden=True 表示元素不可见。"""
    # p:sp: p:nvSpPr/p:cNvPr
    # p:pic: p:nvPicPr/p:cNvPr
    # p:grpSp: p:nvGrpSpPr/p:cNvPr
    # p:graphicFrame: p:nvGraphicFramePr/p:cNvPr
    for path in (
        "p:nvSpPr/p:cNvPr",
        "p:nvPicPr/p:cNvPr",
        "p:nvGrpSpPr/p:cNvPr",
        "p:nvGraphicFramePr/p:cNvPr",
    ):
        el = node.find(path, NS)
        if el is not None:
            hidden = _get_bool_attr(el, "hidden", False)
            return el.get("id"), el.get("name"), hidden
    return None, None, False

def get_node_xfrm(node: ET.Element) -> Optional[Xfrm]:
    # group: p:grpSpPr/a:xfrm
    grpSpPr = node.find("p:grpSpPr", NS)
    if grpSpPr is not None:
        return parse_xfrm(grpSpPr.find("a:xfrm", NS))

    # common: p:spPr/a:xfrm
    spPr = node.find("p:spPr", NS)
    if spPr is not None:
        x = parse_xfrm(spPr.find("a:xfrm", NS))
        if x is not None:
            return x

    # graphicFrame: p:xfrm
    gf_xfrm = node.find("p:xfrm", NS)
    if gf_xfrm is not None:
        return parse_xfrm(gf_xfrm)

    return None
```

---

# 附录 C：递归遍历 + bbox 计算骨架

```python
from typing import Any, Dict, List, Optional

def compute_bboxes_for_slide(pptx_path: str, slide_index: int,
                            png_w: int, png_h: int) -> Dict[str, Any]:
    import zipfile

    with zipfile.ZipFile(pptx_path, "r") as zf:
        slide_cx, slide_cy = read_slide_size_emu(zf)
        slide_root = read_slide_xml_root(zf, slide_index)
        spTree = find_spTree(slide_root)

    sx = png_w / slide_cx
    sy = png_h / slide_cy

    out_elems: List[Dict[str, Any]] = []
    z_counter = 0

    def emit(node, M_parent, parent_id: Optional[str]):
        nonlocal z_counter

        node_type = tag_localname(node)  # sp, pic, grpSp, graphicFrame, ...
        x = get_node_xfrm(node)
        el_id, el_name, hidden = get_cNvPr(node)

        if hidden:
            out_elems.append({
                "id": el_id, "name": el_name, "type": node_type,
                "parent_id": parent_id, "z_index": z_counter,
                "skipped_reason": "hidden"
            })
            z_counter += 1
            return

        # group
        if node_type == "grpSp":
            if x is None:
                out_elems.append({
                    "id": el_id, "name": el_name, "type": "grpSp",
                    "parent_id": parent_id, "z_index": z_counter,
                    "skipped_reason": "missing_xfrm"
                })
                z_counter += 1
                return

            chExt_w = x.chExt_w or x.ext_w
            chExt_h = x.chExt_h or x.ext_h

            M_g = group_matrix(
                off_x=x.off_x, off_y=x.off_y,
                ext_w=x.ext_w, ext_h=x.ext_h,
                chOff_x=x.chOff_x, chOff_y=x.chOff_y,
                chExt_w=chExt_w, chExt_h=chExt_h,
                rot_60000=x.rot_60000, flipH=x.flipH, flipV=x.flipV
            )
            M_here = M_parent @ M_g

            out_elems.append({
                "id": el_id, "name": el_name, "type": "grpSp",
                "parent_id": parent_id, "z_index": z_counter,
                "transform": x.__dict__,
            })
            z_counter += 1

            # recurse: group children mixed; skip nv* and grpSpPr
            for gc in list(node):
                if tag_localname(gc) in ("nvGrpSpPr", "grpSpPr"):
                    continue
                emit(gc, M_here, el_id)
            return

        # normal element
        if x is None or x.ext_w <= 0 or x.ext_h <= 0:
            out_elems.append({
                "id": el_id, "name": el_name, "type": node_type,
                "parent_id": parent_id, "z_index": z_counter,
                "skipped_reason": "missing_or_invalid_xfrm"
            })
            z_counter += 1
            return

        M_e = element_matrix(x.off_x, x.off_y, x.ext_w, x.ext_h,
                             x.rot_60000, x.flipH, x.flipV)
        M = M_parent @ M_e
        minx, miny, maxx, maxy = M.apply_rect_aabb(x.ext_w, x.ext_h)

        bbox_px = [minx*sx, miny*sy, maxx*sx, maxy*sy]

        out_elems.append({
            "id": el_id, "name": el_name, "type": node_type,
            "parent_id": parent_id, "z_index": z_counter,
            "transform": x.__dict__,
            "bbox_emu": [minx, miny, maxx, maxy],
            "bbox_px": bbox_px,
        })
        z_counter += 1

    KNOWN_ELEMENTS = {"sp", "pic", "grpSp", "graphicFrame", "cxnSp"}

    M0 = Affine2D.identity()
    for child in list(spTree):
        if tag_localname(child) not in KNOWN_ELEMENTS:
            continue
        emit(child, M0, None)

    return {
        "slide_index": slide_index,
        "png_size": [png_w, png_h],
        "slide_size_emu": [slide_cx, slide_cy],
        "sx": sx, "sy": sy,
        "elements": out_elems,
    }
```

---

## 验收（Checklist）

- overlay 框在视觉上与 PNG 元素边缘对齐，无系统性偏移
- 至少覆盖：
  - ≥3 个 group 内元素
  - ≥3 个旋转元素
  - ≥20 个元素抽检
- JSON 必须保留 `z_index`

---

# 附录 D：Pillow Debug Overlay（补全验证闭环）

> 功能：读取 `elements[].bbox_px`，在 PNG 上绘制包围框（可选标签），输出 `debug_overlay.png` 以肉眼验证坐标对齐。

```python
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _safe_int(v: float) -> int:
    # Pillow 绘制用 int 更稳
    return int(round(v))

def draw_overlay(
    png_path: str,
    elements: List[Dict[str, Any]],
    out_path: str,
    *,
    draw_labels: bool = True,
    label_fields: Tuple[str, ...] = ("id", "type", "name"),
    line_width: int = 2,
    z_order: str = "asc",  # "asc"=z_index小先画（底），"desc"=大先画（顶）
    max_labels: Optional[int] = None,
) -> None:
    '''
    在 PNG 上画 bbox overlay。

    elements[i] 期望包含：
      - bbox_px: [minx, miny, maxx, maxy]
      - z_index (optional)
      - id/type/name (optional)
    '''
    img = Image.open(png_path).convert("RGBA")
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Optional font
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    def get_z(e: Dict[str, Any]) -> int:
        z = e.get("z_index")
        if isinstance(z, int):
            return z
        if isinstance(z, float):
            return int(z)
        if isinstance(z, str) and z.isdigit():
            return int(z)
        return 0

    elems = [e for e in elements if isinstance(e, dict) and e.get("bbox_px")]
    elems.sort(key=get_z, reverse=(z_order == "desc"))

    label_count = 0

    for e in elems:
        bbox = e.get("bbox_px")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            continue

        x1, y1, x2, y2 = map(float, bbox)

        # Handle inverted boxes
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1

        # Clamp to image bounds
        x1 = _clamp(x1, 0, W - 1)
        y1 = _clamp(y1, 0, H - 1)
        x2 = _clamp(x2, 0, W - 1)
        y2 = _clamp(y2, 0, H - 1)

        # Skip degenerate
        if (x2 - x1) < 1 or (y2 - y1) < 1:
            continue

        # Deterministic color by (id/type/z) — 便于多次对比
        key = str(e.get("id", "")) + "|" + str(e.get("type", "")) + "|" + str(get_z(e))
        h = 0
        for ch in key:
            h = (h * 131 + ord(ch)) & 0xFFFFFFFF
        r = 60 + (h & 0x7F)
        g = 60 + ((h >> 8) & 0x7F)
        b = 60 + ((h >> 16) & 0x7F)
        color = (r, g, b, 255)

        x1i, y1i, x2i, y2i = map(_safe_int, (x1, y1, x2, y2))
        for k in range(line_width):
            draw.rectangle([x1i - k, y1i - k, x2i + k, y2i + k], outline=color)

        # Label
        if draw_labels and (max_labels is None or label_count < max_labels):
            parts = []
            for f in label_fields:
                v = e.get(f)
                if v is None:
                    continue
                s = str(v).strip()
                if s:
                    parts.append(s)
            if parts:
                text = " | ".join(parts)

                tx, ty = x1i + 2, max(0, y1i - 12)

                if font is not None:
                    # textbbox -> (x0,y0,x1,y1)
                    x0, y0, x1b, y1b = draw.textbbox((tx, ty), text, font=font)
                    bw, bh = x1b - x0, y1b - y0
                else:
                    bw, bh = (len(text) * 6, 10)

                draw.rectangle([tx - 2, ty - 2, tx + bw + 2, ty + bh + 2], fill=(0, 0, 0, 140))
                draw.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)
                label_count += 1

    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(out_path)

def draw_overlay_from_json(
    png_path: str,
    json_path: str,
    out_path: str,
    **kwargs,
) -> None:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    elements = data.get("elements", [])
    if not isinstance(elements, list):
        raise ValueError("JSON missing 'elements' list")
    draw_overlay(png_path, elements, out_path, **kwargs)
```

### 用法示例
```python
# 从 out.json 画 overlay
draw_overlay_from_json(
    png_path="slide1.png",
    json_path="out.json",
    out_path="debug_overlay.png",
    draw_labels=True,
    z_order="asc",
    line_width=2,
    max_labels=200,
)

# 如果你已经在内存里有 elements
# draw_overlay("slide1.png", elements, "debug_overlay.png", draw_labels=False)
```

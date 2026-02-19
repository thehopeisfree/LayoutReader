"""PPTX Coordinate Transformer.

Parse ppt/slides/slideN.xml, compute pixel-level AABB for each visible
element, and optionally draw a debug overlay on the rendered PNG.

Usage:
    python pptx_bbox.py <pptx_path> <slide_index> <png_path> [--out-json out.json] [--overlay debug_overlay.png]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Point = Tuple[float, float]
Rect = Tuple[float, float, float, float]  # (minx, miny, maxx, maxy)

# ---------------------------------------------------------------------------
# XML namespaces
# ---------------------------------------------------------------------------

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}

# Element tags we recognise inside spTree / grpSp
KNOWN_ELEMENTS = {"sp", "pic", "grpSp", "graphicFrame", "cxnSp"}

# ---------------------------------------------------------------------------
# Affine 2-D (2×3 matrix)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Affine2D:
    """2-D affine transform.

    x' = a*x + c*y + e
    y' = b*x + d*y + f
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    # -- factories -----------------------------------------------------------

    @staticmethod
    def identity() -> Affine2D:
        return Affine2D()

    @staticmethod
    def translate(tx: float, ty: float) -> Affine2D:
        return Affine2D(1, 0, 0, 1, tx, ty)

    @staticmethod
    def scale(sx: float, sy: float) -> Affine2D:
        return Affine2D(sx, 0, 0, sy, 0, 0)

    @staticmethod
    def rotate_cw(rad: float) -> Affine2D:
        """Clockwise rotation in screen coords (y-down).

        Matrix [[cos, -sin], [sin, cos]]  →  a=cos, b=sin, c=-sin, d=cos.
        """
        cos_t = math.cos(rad)
        sin_t = math.sin(rad)
        return Affine2D(cos_t, sin_t, -sin_t, cos_t, 0, 0)

    @staticmethod
    def flip_h() -> Affine2D:
        return Affine2D.scale(-1.0, 1.0)

    @staticmethod
    def flip_v() -> Affine2D:
        return Affine2D.scale(1.0, -1.0)

    # -- composition ---------------------------------------------------------

    def __matmul__(self, other: Affine2D) -> Affine2D:
        """``self @ other`` means *first* apply ``other``, *then* ``self``."""
        a1, b1, c1, d1, e1, f1 = self.a, self.b, self.c, self.d, self.e, self.f
        a2, b2, c2, d2, e2, f2 = other.a, other.b, other.c, other.d, other.e, other.f
        return Affine2D(
            a=a1 * a2 + c1 * b2,
            b=b1 * a2 + d1 * b2,
            c=a1 * c2 + c1 * d2,
            d=b1 * c2 + d1 * d2,
            e=a1 * e2 + c1 * f2 + e1,
            f=b1 * e2 + d1 * f2 + f1,
        )

    # -- application ---------------------------------------------------------

    def apply_point(self, p: Point) -> Point:
        x, y = p
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def apply_points(self, pts: Iterable[Point]) -> List[Point]:
        return [self.apply_point(p) for p in pts]

    def apply_rect_aabb(self, w: float, h: float) -> Rect:
        corners = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
        pts = self.apply_points(corners)
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# Transform helpers
# ---------------------------------------------------------------------------


def _around_center(w: float, h: float, inner: Affine2D) -> Affine2D:
    cx, cy = w / 2.0, h / 2.0
    return Affine2D.translate(cx, cy) @ inner @ Affine2D.translate(-cx, -cy)


def _build_rot_flip(
    w: float,
    h: float,
    rot_60000: int,
    flipH: bool,
    flipV: bool,
) -> Affine2D:
    """Build rotation+flip transform applied around element centre."""
    rad = (rot_60000 / 60000.0) * (math.pi / 180.0)
    inner = Affine2D.identity()
    if flipH:
        inner = Affine2D.flip_h() @ inner
    if flipV:
        inner = Affine2D.flip_v() @ inner
    if rot_60000:
        inner = Affine2D.rotate_cw(rad) @ inner
    return _around_center(w, h, inner)


def element_matrix(
    off_x: float,
    off_y: float,
    w: float,
    h: float,
    rot_60000: int = 0,
    flipH: bool = False,
    flipV: bool = False,
) -> Affine2D:
    return Affine2D.translate(off_x, off_y) @ _build_rot_flip(w, h, rot_60000, flipH, flipV)


def group_matrix(
    off_x: float,
    off_y: float,
    ext_w: float,
    ext_h: float,
    chOff_x: float,
    chOff_y: float,
    chExt_w: float,
    chExt_h: float,
    rot_60000: int = 0,
    flipH: bool = False,
    flipV: bool = False,
) -> Affine2D:
    sx = (ext_w / chExt_w) if chExt_w else 1.0
    sy = (ext_h / chExt_h) if chExt_h else 1.0
    base = Affine2D.scale(sx, sy) @ Affine2D.translate(-chOff_x, -chOff_y)
    return (
        Affine2D.translate(off_x, off_y)
        @ _build_rot_flip(ext_w, ext_h, rot_60000, flipH, flipV)
        @ base
    )


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _get_int_attr(el: Optional[ET.Element], name: str, default: int = 0) -> int:
    if el is None:
        return default
    v = el.get(name)
    return int(v) if v is not None else default


def _get_bool_attr(el: Optional[ET.Element], name: str, default: bool = False) -> bool:
    if el is None:
        return default
    v = el.get(name)
    if v is None:
        return default
    return v in ("1", "true", "True")


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


def _parse_xfrm(xfrm_el: Optional[ET.Element]) -> Optional[Xfrm]:
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

    chOff = xfrm_el.find("a:chOff", NS)
    chExt = xfrm_el.find("a:chExt", NS)
    if chOff is not None:
        x.chOff_x = _get_int_attr(chOff, "x", 0)
        x.chOff_y = _get_int_attr(chOff, "y", 0)
    if chExt is not None:
        x.chExt_w = _get_int_attr(chExt, "cx", 0)
        x.chExt_h = _get_int_attr(chExt, "cy", 0)

    return x


def _tag_localname(el: ET.Element) -> str:
    t = el.tag
    return t.split("}")[-1] if "}" in t else t


def _get_cNvPr(node: ET.Element) -> Tuple[Optional[str], Optional[str], bool]:
    """Return ``(id, name, hidden)``."""
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


def _get_node_xfrm(node: ET.Element) -> Optional[Xfrm]:
    # group: p:grpSpPr/a:xfrm
    grpSpPr = node.find("p:grpSpPr", NS)
    if grpSpPr is not None:
        return _parse_xfrm(grpSpPr.find("a:xfrm", NS))

    # common shape: p:spPr/a:xfrm
    spPr = node.find("p:spPr", NS)
    if spPr is not None:
        x = _parse_xfrm(spPr.find("a:xfrm", NS))
        if x is not None:
            return x

    # graphicFrame: p:xfrm
    gf_xfrm = node.find("p:xfrm", NS)
    if gf_xfrm is not None:
        return _parse_xfrm(gf_xfrm)

    return None


# ---------------------------------------------------------------------------
# Semantic classification (kind / subtype)
# ---------------------------------------------------------------------------


def _extract_text_content(sp_node: ET.Element, max_len: int = 400) -> str:
    ts = [t.text for t in sp_node.findall(".//p:txBody//a:t", NS) if t.text]
    s = "".join(ts).strip()
    if max_len and len(s) > max_len:
        return s[:max_len]
    return s


def _get_placeholder_info(sp_node: ET.Element) -> Tuple[bool, Optional[str]]:
    ph = sp_node.find("p:nvSpPr/p:nvPr/p:ph", NS)
    if ph is None:
        return False, None
    return True, ph.get("type")


def _get_prst_geom(sp_node: ET.Element) -> Optional[str]:
    prst = sp_node.find("p:spPr/a:prstGeom", NS)
    if prst is None:
        return None
    return prst.get("prst")


def _get_connector_logic(node: ET.Element) -> Dict[str, Any]:
    """Extract connector start/end shape references from cxnSp."""
    cxn_pr = node.find("p:nvCxnSpPr/p:cNvCxnSpPr", NS)
    logic: Dict[str, Any] = {"start_id": None, "end_id": None}
    if cxn_pr is not None:
        st = cxn_pr.find("a:stCxn", NS)
        en = cxn_pr.find("a:endCxn", NS)
        if st is not None:
            logic["start_id"] = st.get("id")
        if en is not None:
            logic["end_id"] = en.get("id")
    return logic


def _get_graphicframe_subtype(node: ET.Element) -> str:
    """Detect graphicFrame content type via graphicData URI, with fallback."""
    data = node.find(".//a:graphicData", NS)
    if data is not None:
        uri = data.get("uri", "")
        if "table" in uri:
            return "table"
        if "chart" in uri:
            return "chart"
        if "diagram" in uri:
            return "smartart"
    # fallback: probe child elements directly
    if node.find(".//a:tbl", NS) is not None:
        return "table"
    if node.find(".//c:chart", NS) is not None:
        return "chart"
    return "unknown"


def _classify_element_semantics(
    node: ET.Element,
    node_type: str,
    bbox_px: Optional[Tuple[float, float, float, float]],
    slide_px: Tuple[int, int],
) -> Dict[str, Any]:
    """Return semantic fields to merge into out_elem. Non-invasive."""
    W, H = slide_px
    meta: Dict[str, Any] = {}

    if node_type == "sp":
        text = _extract_text_content(node, max_len=400)
        is_ph, ph_type = _get_placeholder_info(node)
        geom = _get_prst_geom(node)

        if text or is_ph:
            meta["kind"] = "text"
            if text:
                meta["text_content"] = text
                meta["text_len"] = len(text)
            meta["has_text_body"] = node.find(".//p:txBody", NS) is not None
            if is_ph:
                meta["is_placeholder"] = True
            if ph_type:
                meta["ph_type"] = ph_type
        else:
            meta["kind"] = "shape"
            if geom:
                meta["geom_prst"] = geom
        return meta

    if node_type == "pic":
        meta["kind"] = "image"
        if bbox_px is not None and W > 0 and H > 0:
            x1, y1, x2, y2 = bbox_px
            w = max(0.0, x2 - x1)
            h = max(0.0, y2 - y1)
            area_ratio = (w * h) / float(W * H) if W * H else 0.0
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            is_centered = (abs(cx - W / 2.0) < (W * 0.05)) and (abs(cy - H / 2.0) < (H * 0.05))

            if area_ratio > 0.9 and is_centered:
                meta["subtype"] = "background"
            else:
                meta["subtype"] = "inline"

            meta["area_ratio"] = round(area_ratio, 4)
        return meta

    if node_type == "cxnSp":
        meta["kind"] = "connector"
        endpoints = _get_connector_logic(node)
        if endpoints["start_id"] or endpoints["end_id"]:
            meta["linked_endpoints"] = endpoints
        return meta

    if node_type == "graphicFrame":
        meta["kind"] = "container"
        meta["is_container"] = True
        meta["subtype"] = _get_graphicframe_subtype(node)
        return meta

    if node_type == "grpSp":
        meta["kind"] = "container"
        meta["subtype"] = "group"
        meta["is_container"] = True
        return meta

    return meta


# ---------------------------------------------------------------------------
# Slide / presentation readers
# ---------------------------------------------------------------------------


def _read_slide_size_emu(zf: zipfile.ZipFile) -> Tuple[int, int]:
    xml = zf.read("ppt/presentation.xml")
    root = ET.fromstring(xml)
    sldSz = root.find(".//p:sldSz", NS)
    if sldSz is None:
        raise ValueError("presentation.xml missing p:sldSz")
    cx = int(sldSz.get("cx"))  # type: ignore[arg-type]
    cy = int(sldSz.get("cy"))  # type: ignore[arg-type]
    return cx, cy


def _read_slide_xml_root(zf: zipfile.ZipFile, slide_index: int) -> ET.Element:
    path = f"ppt/slides/slide{slide_index}.xml"
    xml = zf.read(path)
    return ET.fromstring(xml)


def _find_spTree(slide_root: ET.Element) -> ET.Element:
    spTree = slide_root.find(".//p:cSld/p:spTree", NS)
    if spTree is None:
        raise ValueError("slide missing p:spTree")
    return spTree


# ---------------------------------------------------------------------------
# Core: recursive traversal → bbox list
# ---------------------------------------------------------------------------


def compute_bboxes_for_slide(
    pptx_path: str,
    slide_index: int,
    png_w: int,
    png_h: int,
) -> Dict[str, Any]:
    with zipfile.ZipFile(pptx_path, "r") as zf:
        slide_cx, slide_cy = _read_slide_size_emu(zf)
        slide_root = _read_slide_xml_root(zf, slide_index)
        spTree = _find_spTree(slide_root)

    sx = png_w / slide_cx
    sy = png_h / slide_cy

    out_elems: List[Dict[str, Any]] = []
    z_counter = 0

    def emit(node: ET.Element, M_parent: Affine2D, parent_id: Optional[str]) -> None:
        nonlocal z_counter

        node_type = _tag_localname(node)
        xfrm = _get_node_xfrm(node)
        el_id, el_name, hidden = _get_cNvPr(node)

        # skip hidden elements
        if hidden:
            out_elems.append(
                {
                    "id": el_id,
                    "name": el_name,
                    "type": node_type,
                    "parent_id": parent_id,
                    "z_index": z_counter,
                    "skipped_reason": "hidden",
                }
            )
            z_counter += 1
            return

        # --- group ---
        if node_type == "grpSp":
            if xfrm is None:
                out_elems.append(
                    {
                        "id": el_id,
                        "name": el_name,
                        "type": "grpSp",
                        "parent_id": parent_id,
                        "z_index": z_counter,
                        "skipped_reason": "missing_xfrm",
                    }
                )
                z_counter += 1
                return

            chExt_w = xfrm.chExt_w or xfrm.ext_w
            chExt_h = xfrm.chExt_h or xfrm.ext_h

            M_g = group_matrix(
                off_x=xfrm.off_x,
                off_y=xfrm.off_y,
                ext_w=xfrm.ext_w,
                ext_h=xfrm.ext_h,
                chOff_x=xfrm.chOff_x,
                chOff_y=xfrm.chOff_y,
                chExt_w=chExt_w,
                chExt_h=chExt_h,
                rot_60000=xfrm.rot_60000,
                flipH=xfrm.flipH,
                flipV=xfrm.flipV,
            )
            M_here = M_parent @ M_g

            grp_elem: Dict[str, Any] = {
                "id": el_id,
                "name": el_name,
                "type": "grpSp",
                "parent_id": parent_id,
                "z_index": z_counter,
                "transform": xfrm.__dict__,
            }
            grp_elem.update(_classify_element_semantics(node, "grpSp", None, (png_w, png_h)))
            out_elems.append(grp_elem)
            z_counter += 1

            for gc in list(node):
                ln = _tag_localname(gc)
                if ln in ("nvGrpSpPr", "grpSpPr"):
                    continue
                if ln not in KNOWN_ELEMENTS:
                    continue
                emit(gc, M_here, el_id)
            return

        # --- normal element ---
        if xfrm is None or xfrm.ext_w <= 0 or xfrm.ext_h <= 0:
            out_elems.append(
                {
                    "id": el_id,
                    "name": el_name,
                    "type": node_type,
                    "parent_id": parent_id,
                    "z_index": z_counter,
                    "skipped_reason": "missing_or_invalid_xfrm",
                }
            )
            z_counter += 1
            return

        M_e = element_matrix(
            xfrm.off_x, xfrm.off_y, xfrm.ext_w, xfrm.ext_h,
            xfrm.rot_60000, xfrm.flipH, xfrm.flipV,
        )
        M = M_parent @ M_e
        minx, miny, maxx, maxy = M.apply_rect_aabb(xfrm.ext_w, xfrm.ext_h)

        bbox_px = [minx * sx, miny * sy, maxx * sx, maxy * sy]

        out_elem: Dict[str, Any] = {
            "id": el_id,
            "name": el_name,
            "type": node_type,
            "parent_id": parent_id,
            "z_index": z_counter,
            "transform": xfrm.__dict__,
            "bbox_emu": [minx, miny, maxx, maxy],
            "bbox_px": bbox_px,
        }
        out_elem.update(_classify_element_semantics(
            node, node_type, tuple(bbox_px), (png_w, png_h),
        ))
        out_elems.append(out_elem)
        z_counter += 1

    M0 = Affine2D.identity()
    for child in list(spTree):
        if _tag_localname(child) not in KNOWN_ELEMENTS:
            continue
        emit(child, M0, None)

    return {
        "slide_index": slide_index,
        "png_size": [png_w, png_h],
        "slide_size_emu": [slide_cx, slide_cy],
        "sx": sx,
        "sy": sy,
        "elements": out_elems,
    }


# ---------------------------------------------------------------------------
# Debug overlay
# ---------------------------------------------------------------------------


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def draw_overlay(
    png_path: str,
    elements: List[Dict[str, Any]],
    out_path: str,
    *,
    draw_labels: bool = True,
    label_fields: Tuple[str, ...] = ("id", "kind", "subtype", "name"),
    line_width: int = 2,
    z_order: str = "asc",
    max_labels: Optional[int] = None,
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(png_path).convert("RGBA")
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    def _get_z(e: Dict[str, Any]) -> int:
        z = e.get("z_index")
        if isinstance(z, int):
            return z
        if isinstance(z, float):
            return int(z)
        if isinstance(z, str) and z.isdigit():
            return int(z)
        return 0

    elems = [e for e in elements if isinstance(e, dict) and e.get("bbox_px")]
    elems.sort(key=_get_z, reverse=(z_order == "desc"))

    label_count = 0

    for e in elems:
        bbox = e.get("bbox_px")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            continue

        x1, y1, x2, y2 = map(float, bbox)
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1

        x1 = _clamp(x1, 0, W - 1)
        y1 = _clamp(y1, 0, H - 1)
        x2 = _clamp(x2, 0, W - 1)
        y2 = _clamp(y2, 0, H - 1)

        if (x2 - x1) < 1 or (y2 - y1) < 1:
            continue

        # deterministic colour from id/type/z
        key = f"{e.get('id', '')}|{e.get('type', '')}|{_get_z(e)}"
        h = 0
        for ch in key:
            h = (h * 131 + ord(ch)) & 0xFFFFFFFF
        r = 60 + (h & 0x7F)
        g = 60 + ((h >> 8) & 0x7F)
        b = 60 + ((h >> 16) & 0x7F)
        color = (r, g, b, 255)

        x1i, y1i, x2i, y2i = (
            int(round(x1)),
            int(round(y1)),
            int(round(x2)),
            int(round(y2)),
        )
        for k in range(line_width):
            draw.rectangle([x1i - k, y1i - k, x2i + k, y2i + k], outline=color)

        if draw_labels and (max_labels is None or label_count < max_labels):
            parts = []
            for fld in label_fields:
                v = e.get(fld)
                if v is None:
                    continue
                s = str(v).strip()
                if s:
                    parts.append(s)
            if parts:
                text = " | ".join(parts)
                tx, ty = x1i + 2, max(0, y1i - 12)
                if font is not None:
                    x0, y0, x1b, y1b = draw.textbbox((tx, ty), text, font=font)
                    bw, bh = x1b - x0, y1b - y0
                else:
                    bw, bh = (len(text) * 6, 10)
                draw.rectangle(
                    [tx - 2, ty - 2, tx + bw + 2, ty + bh + 2], fill=(0, 0, 0, 140)
                )
                draw.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)
                label_count += 1

    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(out_path)


def draw_overlay_from_json(
    png_path: str,
    json_path: str,
    out_path: str,
    **kwargs: Any,
) -> None:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    elements = data.get("elements", [])
    if not isinstance(elements, list):
        raise ValueError("JSON missing 'elements' list")
    draw_overlay(png_path, elements, out_path, **kwargs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _png_dimensions(png_path: str) -> Tuple[int, int]:
    from PIL import Image

    with Image.open(png_path) as im:
        return im.size  # (width, height)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="PPTX slide element bbox extractor")
    parser.add_argument("pptx_path", help="Path to .pptx file")
    parser.add_argument("slide_index", type=int, help="Slide number (1-based)")
    parser.add_argument("png_path", help="Rendered PNG of the slide")
    parser.add_argument("--out-json", default="out.json", help="Output JSON path (default: out.json)")
    parser.add_argument("--overlay", default=None, help="Output overlay PNG path (default: no overlay)")
    parser.add_argument("--no-labels", action="store_true", help="Disable labels on overlay")
    parser.add_argument("--max-labels", type=int, default=200, help="Max labels on overlay")
    args = parser.parse_args(argv)

    png_w, png_h = _png_dimensions(args.png_path)

    result = compute_bboxes_for_slide(args.pptx_path, args.slide_index, png_w, png_h)

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"Wrote {args.out_json}  ({len(result['elements'])} elements)")

    if args.overlay:
        draw_overlay(
            args.png_path,
            result["elements"],
            args.overlay,
            draw_labels=not args.no_labels,
            max_labels=args.max_labels,
        )
        print(f"Wrote {args.overlay}")


if __name__ == "__main__":
    main()

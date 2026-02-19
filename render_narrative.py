"""
render_narrative.py
===================
Canonical spatial relations JSON → Line-based narrative DSL

设计原则：
- 每条关系一行，行首带 relation_type 标记（`overlap|`, `align|` 等）
- 元素引用格式：semantic_name(canonical_id)，无语义名时仅 (canonical_id)
- 数值格式固定：px 统一 1 位小数，ratio 统一整数百分比，列表 [a, b, c]
- 词汇表封闭：重叠/对齐/排列/分布/包含/相邻/分离/在上层/在下层
- 纯函数，无状态，无判断
"""

from typing import Optional


def _ref(canonical_id: str, id2name: Optional[dict] = None) -> str:
    """生成元素引用字符串。有语义名则 name(id)，否则 (id)。"""
    if id2name and canonical_id in id2name:
        return f"{id2name[canonical_id]}({canonical_id})"
    return f"({canonical_id})"


def _fmt_px(v) -> str:
    """格式化 px 值：统一 1 位小数。"""
    if isinstance(v, list):
        return "[" + ", ".join(f"{x:.1f}" for x in v) + "]"
    return f"{v:.1f}"


def _fmt_pct(v) -> str:
    """格式化百分比：0-1 的 float → 整数百分比字符串。"""
    return f"{v * 100:.0f}%"


def _render_overlap(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    必填: subject_id, object_id, intersection_px2
    可选: overlap_ratio_object, top_id, z_diff
    """
    m = rel["metrics"]
    subj = _ref(rel["subject_id"], id2name)
    obj = _ref(rel["object_id"], id2name)
    area = _fmt_px(m["intersection_px2"])

    parts = [f"overlap| {subj} 与 {obj} 重叠 {area}px²"]

    if "overlap_ratio_object" in m:
        parts.append(f"（占 {obj} 面积的 {_fmt_pct(m['overlap_ratio_object'])}）")

    if "top_id" in m and "z_diff" in m:
        top = _ref(m["top_id"], id2name)
        parts.append(f"，{top} 在上层（z差 {m['z_diff']}）")

    return "".join(parts) + "。"


def _render_edge_alignment(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    必填: members, metrics.edge, metrics.max_delta_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])
    edge = m["edge"]

    edge_label = {
        "left": "左", "right": "右",
        "top": "上", "bottom": "下",
        "center_x": "水平中轴", "center_y": "垂直中轴",
    }.get(edge, edge)

    delta = _fmt_px(m["max_delta_px"])
    return f"align| {members} 沿{edge_label}边对齐，最大偏差 {delta}px。"


def _render_sequence(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    必填: members, metrics.gaps_px
    可选: metrics.axis, metrics.edge, metrics.max_delta_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])
    gaps = _fmt_px(m["gaps_px"])

    axis = m.get("axis", "y")
    direction = "纵向" if axis in ("y", "vertical") else "横向"

    parts = [f"sequence| {members} {direction}排列，间距 {gaps}px"]

    if "edge" in m and "max_delta_px" in m:
        edge_label = {
            "left": "左", "right": "右",
            "top": "上", "bottom": "下",
            "center_x": "水平中轴", "center_y": "垂直中轴",
        }.get(m["edge"], m["edge"])
        delta = _fmt_px(m["max_delta_px"])
        parts.append(f"，{edge_label}对齐（偏差 {delta}px）")

    return "".join(parts) + "。"


def _render_distribution(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    必填: members, metrics.intervals_px
    可选: metrics.axis, metrics.range_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])
    intervals = _fmt_px(m["intervals_px"])

    axis = m.get("axis", "x")
    axis_label = {"x": "x", "y": "y", "x_gap": "x", "y_gap": "y"}.get(axis, axis)

    parts = [f"dist| {members} 沿{axis_label}轴等距分布，间距 {intervals}px"]

    if "range_px" in m:
        parts.append(f"（极差 {_fmt_px(m['range_px'])}px）")

    return "".join(parts) + "。"


def _render_containment(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    必填: container_id, child_ids
    可选: metrics.padding_px
    """
    m = rel.get("metrics", {})
    container = _ref(rel["container_id"], id2name)
    children = ", ".join(_ref(cid, id2name) for cid in rel["child_ids"])

    parts = [f"contain| {container} 包含 [{children}]"]

    if "padding_px" in m:
        p = m["padding_px"]
        if isinstance(p, dict):
            parts.append(
                f"，内边距 上{_fmt_px(p.get('top', 0))} "
                f"右{_fmt_px(p.get('right', 0))} "
                f"下{_fmt_px(p.get('bottom', 0))} "
                f"左{_fmt_px(p.get('left', 0))}px"
            )
        else:
            parts.append(f"，内边距 {_fmt_px(p)}px")

    return "".join(parts) + "。"


def _render_adjacency(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    必填: subject_id, object_id, metrics.direction
    可选: metrics.gap_px
    """
    m = rel["metrics"]
    subj = _ref(rel["subject_id"], id2name)
    obj = _ref(rel["object_id"], id2name)

    dir_label = {
        "left": "左侧", "right": "右侧",
        "above": "上方", "below": "下方",
    }.get(m["direction"], m["direction"])

    parts = [f"adj| {subj} 位于 {obj} {dir_label}"]

    if "gap_px" in m:
        parts.append(f"，间距 {_fmt_px(m['gap_px'])}px")

    return "".join(parts) + "。"


def _render_group(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    必填: group_id, child_ids
    可选: metrics.internal_alignment, metrics.internal_gap_px
    """
    m = rel.get("metrics", {})
    group = _ref(rel["group_id"], id2name)
    children = ", ".join(_ref(cid, id2name) for cid in rel["child_ids"])

    parts = [f"group| {group} 内部包含 [{children}]"]

    if "internal_alignment" in m:
        parts.append(f"，内部沿{m['internal_alignment']}对齐")
        if "internal_alignment_delta_px" in m:
            parts.append(f"（偏差 {_fmt_px(m['internal_alignment_delta_px'])}px）")

    if "internal_gap_px" in m:
        parts.append(f"，内部间距 {_fmt_px(m['internal_gap_px'])}px")

    return "".join(parts) + "。"


# ── 路由表 ──────────────────────────────────────────────

_RENDERERS = {
    "overlap": _render_overlap,
    "edge_alignment": _render_edge_alignment,
    "sequence": _render_sequence,
    "distribution": _render_distribution,
    "containment": _render_containment,
    "adjacency": _render_adjacency,
    "group": _render_group,
}


def render_narrative(
    relations: list[dict],
    id2name: Optional[dict] = None,
) -> str:
    """
    将 canonical relations[] 渲染为 line-based 叙述 DSL。

    Parameters
    ----------
    relations : list[dict]
        canonical 层的 spatial.relations 数组。
        每条必须有 "relation_type" 字段。
    id2name : dict, optional
        canonical_id → semantic_name 的映射表。
        例如 {"sh_5": "bg_image", "sh_2": "title_1"}

    Returns
    -------
    str
        每条关系一行的叙述文本。
    """
    lines = []
    for rel in relations:
        rtype = rel["relation_type"]
        renderer = _RENDERERS.get(rtype)
        if renderer is None:
            # 未知类型：保留原始 JSON 作为 fallback，不中断流程
            import json
            lines.append(f"unknown| {json.dumps(rel, ensure_ascii=False)}")
        else:
            lines.append(renderer(rel, id2name))
    return "\n".join(lines)


# ── 测试 ────────────────────────────────────────────────

if __name__ == "__main__":
    # 模拟 canonical relations
    test_relations = [
        {
            "relation_type": "overlap",
            "subject_id": "sh_5",
            "object_id": "sh_2",
            "metrics": {
                "intersection_px2": 240,
                "overlap_ratio_object": 0.15,
                "z_diff": 3,
                "top_id": "sh_5",
                "bottom_id": "sh_2",
            },
        },
        {
            "relation_type": "edge_alignment",
            "members": ["sh_1", "sh_2", "sh_3"],
            "metrics": {"edge": "left", "max_delta_px": 2.4},
        },
        {
            "relation_type": "sequence",
            "members": ["sh_1", "sh_2", "sh_4"],
            "metrics": {
                "axis": "y",
                "gaps_px": [24, 48],
                "edge": "left",
                "max_delta_px": 0.5,
            },
        },
        {
            "relation_type": "distribution",
            "members": ["sh_10", "sh_11", "sh_12"],
            "metrics": {
                "axis": "x_gap",
                "intervals_px": [42, 45],
                "range_px": 3,
            },
        },
        {
            "relation_type": "containment",
            "container_id": "sh_20",
            "child_ids": ["sh_21", "sh_22"],
            "metrics": {
                "padding_px": {"top": 8, "right": 12, "bottom": 8, "left": 12},
            },
        },
        {
            "relation_type": "adjacency",
            "subject_id": "sh_6",
            "object_id": "sh_7",
            "metrics": {"direction": "left", "gap_px": 16},
        },
        {
            "relation_type": "group",
            "group_id": "sh_30",
            "child_ids": ["sh_31", "sh_32"],
            "metrics": {
                "internal_alignment": "中轴线",
                "internal_alignment_delta_px": 0,
                "internal_gap_px": 12,
            },
        },
    ]

    # 语义名映射
    test_id2name = {
        "sh_1": "title_1",
        "sh_2": "body_text_1",
        "sh_3": "footer_1",
        "sh_4": "page_num",
        "sh_5": "pic_logo",
        "sh_6": "icon_1",
        "sh_7": "label_1",
        "sh_10": "pic_a",
        "sh_11": "pic_b",
        "sh_12": "pic_c",
        "sh_20": "content_box",
        "sh_21": "inner_title",
        "sh_22": "inner_body",
        "sh_30": "icon_group_1",
        "sh_31": "icon_star",
        "sh_32": "text_rating",
    }

    output = render_narrative(test_relations, test_id2name)
    print(output)

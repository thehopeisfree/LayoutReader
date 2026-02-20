"""
render_narrative.py
===================
Canonical spatial relations JSON -> Line-based narrative DSL

Design principles:
- One relation per line, prefixed by relation_type tag (`overlap|`, `align|`, etc.)
- Element reference format: semantic_name(canonical_id), or just (canonical_id)
- Number format: px values to 1 decimal, ratios as integer percentages, lists [a, b, c]
- Closed vocabulary: overlap/aligned/sequence/distributed/contains/adjacent/on top/below
- Pure function, no state, no decisions
"""

from typing import Optional


def _ref(canonical_id: str, id2name: Optional[dict] = None) -> str:
    """Build element reference string: name(sh_id) if semantic name exists, else (sh_id)."""
    display_id = f"sh_{canonical_id}" if canonical_id.isdigit() else canonical_id
    if id2name and canonical_id in id2name:
        return f"{id2name[canonical_id]}({display_id})"
    return f"({display_id})"


def _fmt_px(v) -> str:
    """Format px value: 1 decimal place."""
    if isinstance(v, list):
        return "[" + ", ".join(f"{x:.1f}" for x in v) + "]"
    return f"{v:.1f}"


def _fmt_pct(v) -> str:
    """Format percentage: 0-1 float -> 1-decimal percentage string."""
    return f"{v * 100:.1f}%"


def _render_overlap(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    Required: subject_id, object_id, intersection_px2
    Optional: overlap_ratio_object, top_id, z_diff
    """
    m = rel["metrics"]
    subj = _ref(rel["subject_id"], id2name)
    obj = _ref(rel["object_id"], id2name)
    area = _fmt_px(m["intersection_px2"])

    parts = [f"overlap| {subj} overlaps {obj} by {area}px2"]

    if "overlap_ratio_object" in m:
        parts.append(f" ({_fmt_pct(m['overlap_ratio_object'])} of {obj})")

    if "top_id" in m and "z_diff" in m:
        top = _ref(m["top_id"], id2name)
        parts.append(f", {top} on top (z-diff {m['z_diff']})")

    return "".join(parts) + "."


def _render_edge_alignment(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    Required: members, metrics.edge, metrics.max_delta_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])
    edge = m["edge"]

    edge_label = {
        "left": "left", "right": "right",
        "top": "top", "bottom": "bottom",
        "center_x": "x-center", "center_y": "y-center",
    }.get(edge, edge)

    delta = _fmt_px(m["max_delta_px"])
    suffix = "" if edge.startswith("center_") else " edge"
    return f"align| {members} aligned on {edge_label}{suffix}, max delta {delta}px."


def _render_sequence(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    Required: members, metrics.gaps_px
    Optional: metrics.axis, metrics.edge, metrics.max_delta_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])
    gaps = _fmt_px(m["gaps_px"])

    axis = m.get("axis", "y")
    direction = "vertical" if axis in ("y", "vertical") else "horizontal"

    parts = [f"sequence| {members} {direction} sequence, gaps {gaps}px"]

    if "edge" in m and "max_delta_px" in m:
        edge_label = {
            "left": "left", "right": "right",
            "top": "top", "bottom": "bottom",
            "center_x": "x-center", "center_y": "y-center",
        }.get(m["edge"], m["edge"])
        delta = _fmt_px(m["max_delta_px"])
        parts.append(f", aligned on {edge_label} (delta {delta}px)")

    return "".join(parts) + "."


def _render_distribution(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    Required: members, metrics.intervals_px
    Optional: metrics.axis, metrics.range_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])
    intervals = _fmt_px(m["intervals_px"])

    axis = m.get("axis", "x")
    axis_label = {"x": "x", "y": "y", "x_gap": "x", "y_gap": "y"}.get(axis, axis)

    parts = [f"dist| {members} evenly distributed along {axis_label}-axis, intervals {intervals}px"]

    if "range_px" in m:
        parts.append(f" (range {_fmt_px(m['range_px'])}px)")

    return "".join(parts) + "."


def _render_containment(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    Required: container_id, child_ids
    Optional: metrics.padding_px
    """
    m = rel.get("metrics", {})
    container = _ref(rel["container_id"], id2name)
    children = ", ".join(_ref(cid, id2name) for cid in rel["child_ids"])

    parts = [f"contain| {container} contains [{children}]"]

    if "padding_px" in m:
        p = m["padding_px"]
        if isinstance(p, dict):
            parts.append(
                f", padding top {_fmt_px(p.get('top', 0))} "
                f"right {_fmt_px(p.get('right', 0))} "
                f"bottom {_fmt_px(p.get('bottom', 0))} "
                f"left {_fmt_px(p.get('left', 0))}px"
            )
        else:
            parts.append(f", padding {_fmt_px(p)}px")

    return "".join(parts) + "."


def _render_adjacency(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    Required: subject_id, object_id, metrics.direction
    Optional: metrics.gap_px
    """
    m = rel["metrics"]
    subj = _ref(rel["subject_id"], id2name)
    obj = _ref(rel["object_id"], id2name)

    dir_label = {
        "left": "left_of", "right": "right_of",
        "above": "above", "below": "below",
    }.get(m["direction"], m["direction"])

    parts = [f"adj| {subj} {dir_label} {obj}"]

    if "gap_px" in m:
        parts.append(f", gap {_fmt_px(m['gap_px'])}px")

    return "".join(parts) + "."


def _render_group(rel: dict, id2name: Optional[dict] = None) -> str:
    """
    Required: group_id, child_ids
    Optional: metrics.internal_alignment, metrics.internal_gap_px
    """
    m = rel.get("metrics", {})
    group = _ref(rel["group_id"], id2name)
    children = ", ".join(_ref(cid, id2name) for cid in rel["child_ids"])

    parts = [f"group| {group} contains [{children}]"]

    if "internal_alignment" in m:
        parts.append(f", internally aligned on {m['internal_alignment']}")
        if "internal_alignment_delta_px" in m:
            parts.append(f" (delta {_fmt_px(m['internal_alignment_delta_px'])}px)")

    if "internal_gap_px" in m:
        parts.append(f", internal gaps {_fmt_px(m['internal_gap_px'])}px")

    return "".join(parts) + "."


# -- Renderer dispatch table ------------------------------------------------

_RENDERERS = {
    "overlap": _render_overlap,
    "edge_alignment": _render_edge_alignment,
    "sequence": _render_sequence,
    "distribution": _render_distribution,
    "containment": _render_containment,
    "adjacency": _render_adjacency,
    "group": _render_group,
}


def _fmt_offset_axis(val: float, pos_label: str, neg_label: str) -> str:
    """Format CoM offset axis: < 0.5% shows 'centered', else direction+value."""
    if abs(val) < 0.005:
        return "centered"
    direction = pos_label if val >= 0 else neg_label
    return f"{direction} {abs(val) * 100:.1f}%"


def render_density(slide_metrics: dict) -> str:
    """Render slide-level density + CoM metrics as one DSL line."""
    rho = slide_metrics.get("occupancy_ratio", 0)
    om = slide_metrics.get("occupancy_match", 0)
    offset = slide_metrics.get("center_of_mass_offset", [0, 0])
    ox, oy = offset[0], offset[1]

    x_str = _fmt_offset_axis(ox, "right", "left")
    y_str = _fmt_offset_axis(oy, "down", "up")

    return (
        f"density| occupancy {rho * 100:.1f}%, "
        f"target_match {om * 100:.0f}%, "
        f"CoM offset ({x_str}, {y_str})."
    )


def render_narrative(
    data,
    id2name: Optional[dict] = None,
) -> str:
    """
    Render compute_spatial_relations output as line-based narrative DSL.

    Parameters
    ----------
    data : dict or list
        Output dict from compute_spatial_relations() (with slide_metrics and relations),
        or a relations list directly (backward compatible).
    id2name : dict, optional
        canonical_id -> semantic_name mapping.

    Returns
    -------
    str
        One relation per line narrative text.
    """
    if isinstance(data, list):
        relations = data
        slide_metrics = None
    else:
        relations = data.get("relations", [])
        slide_metrics = data.get("slide_metrics")

    lines = ["dsl_version| 1"]
    if slide_metrics:
        lines.append(render_density(slide_metrics))
    for rel in relations:
        rtype = rel["relation_type"]
        renderer = _RENDERERS.get(rtype)
        if renderer is None:
            import json
            lines.append(f"unknown| {json.dumps(rel, ensure_ascii=False)}")
        else:
            lines.append(renderer(rel, id2name))
    return "\n".join(lines)


# -- Demo -------------------------------------------------------------------

if __name__ == "__main__":
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
                "internal_alignment": "center_x",
                "internal_alignment_delta_px": 0,
                "internal_gap_px": 12,
            },
        },
    ]

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

    test_data = {"relations": test_relations}
    output = render_narrative(test_data, test_id2name)
    print(output)

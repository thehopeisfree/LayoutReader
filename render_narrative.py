"""
render_narrative.py
===================
Canonical spatial relations JSON -> Line-based narrative DSL

Design principles:
- One relation per line, prefixed by relation_type tag (`overlap|`, `align|`, etc.)
- Element reference format: semantic_name(canonical_id), or just (canonical_id)
- Dual-track numbers: % primary (with %W/%H axis suffix), px backup in parentheses
- Closed vocabulary: overlap/aligned/sequence/distributed/contains/adjacent/on top/below
- Pure function, no state, no decisions
"""

from typing import Optional


# -- Element reference -------------------------------------------------------


def _ref(canonical_id: str, id2name: Optional[dict] = None) -> str:
    """Build element reference string: name(sh_id) if semantic name exists, else (sh_id)."""
    display_id = f"sh_{canonical_id}" if canonical_id.isdigit() else canonical_id
    if id2name and canonical_id in id2name:
        return f"{id2name[canonical_id]}({display_id})"
    return f"({display_id})"


# -- Formatting helpers ------------------------------------------------------


def _fmt_px(v) -> str:
    """Format px value: 1 decimal place."""
    if isinstance(v, list):
        return "[" + ", ".join(f"{x:.1f}" for x in v) + "]"
    return f"{v:.1f}"


def _fmt_pct_guard(pct: float, suffix: str = "") -> str:
    """Format percentage (0-100 scale) with <0.1% guard and optional axis suffix.

    Returns '0.0%W', '<0.1%H', '12.3%', etc.
    """
    if pct == 0.0:
        return f"0.0%{suffix}"
    if 0 < pct < 0.1:
        return f"<0.1%{suffix}"
    return f"{pct:.1f}%{suffix}"


def _to_pct(px_val: float, canvas_dim: float) -> float:
    """Convert px value to percentage (0-100 scale)."""
    if canvas_dim <= 0:
        return 0.0
    return abs(px_val) / canvas_dim * 100


def _fmt_axis_pct(px_val: float, canvas_dim: float, suffix: str) -> str:
    """Convert single px value to axis-relative percentage string: '2.3%W'."""
    pct = _to_pct(px_val, canvas_dim)
    return _fmt_pct_guard(pct, suffix)


def _fmt_axis_pct_list(px_list: list, canvas_dim: float, suffix: str) -> str:
    """Convert list of px values to axis-relative percentage list: '[2.3%W, 4.4%W]'."""
    return "[" + ", ".join(_fmt_axis_pct(v, canvas_dim, suffix) for v in px_list) + "]"


def _edge_suffix(edge: str) -> str:
    """Map edge name to axis suffix for percentage."""
    return "H" if edge in ("top", "bottom", "center_y") else "W"


def _edge_dim(edge: str, cw: float, ch: float) -> float:
    """Map edge name to the canvas dimension for percentage."""
    return ch if edge in ("top", "bottom", "center_y") else cw


def _dir_suffix(direction: str) -> str:
    """Map adjacency direction to axis suffix."""
    return "H" if direction in ("above", "below") else "W"


def _dir_dim(direction: str, cw: float, ch: float) -> float:
    """Map adjacency direction to the canvas dimension for percentage."""
    return ch if direction in ("above", "below") else cw


# -- Relation renderers ------------------------------------------------------


def _render_overlap(rel: dict, id2name: Optional[dict], cw: float, ch: float) -> str:
    """
    Required: subject_id, object_id, intersection_px2
    Optional: overlap_ratio_object, top_id, z_diff
    """
    m = rel["metrics"]
    subj = _ref(rel["subject_id"], id2name)
    obj = _ref(rel["object_id"], id2name)
    area = _fmt_px(m["intersection_px2"])

    parts = [f"overlap| {subj} overlaps {obj} by {area}px2"]

    pct_parts = []
    canvas_area = cw * ch
    if canvas_area > 0:
        canvas_pct = m["intersection_px2"] / canvas_area * 100
        pct_parts.append(f"{_fmt_pct_guard(canvas_pct)} canvas")

    if "overlap_ratio_object" in m:
        obj_pct = m["overlap_ratio_object"] * 100
        pct_parts.append(f"{_fmt_pct_guard(obj_pct)} of {obj}")

    if pct_parts:
        parts.append(f" ({', '.join(pct_parts)})")

    if "top_id" in m and "z_diff" in m:
        top = _ref(m["top_id"], id2name)
        parts.append(f", {top} on top (z-diff {m['z_diff']})")

    return "".join(parts) + "."


def _render_edge_alignment(rel: dict, id2name: Optional[dict], cw: float, ch: float) -> str:
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

    dim = _edge_dim(edge, cw, ch)
    ax = _edge_suffix(edge)
    pct = _fmt_axis_pct(m["max_delta_px"], dim, ax)

    return f"align| {members} aligned on {edge_label}{suffix}, max delta {delta}px ({pct})."


def _render_sequence(rel: dict, id2name: Optional[dict], cw: float, ch: float) -> str:
    """
    Required: members, metrics.gaps_px
    Optional: metrics.axis, metrics.edge, metrics.max_delta_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])

    axis = m.get("axis", "y")
    direction = "vertical" if axis in ("y", "vertical") else "horizontal"

    gap_dim = ch if axis in ("y", "vertical") else cw
    gap_suffix = "H" if axis in ("y", "vertical") else "W"
    gaps_pct = _fmt_axis_pct_list(m["gaps_px"], gap_dim, gap_suffix)
    gaps_px = _fmt_px(m["gaps_px"])

    parts = [f"sequence| {members} {direction} sequence, gaps {gaps_pct} ({gaps_px}px)"]

    if "edge" in m and "max_delta_px" in m:
        edge_label = {
            "left": "left", "right": "right",
            "top": "top", "bottom": "bottom",
            "center_x": "x-center", "center_y": "y-center",
        }.get(m["edge"], m["edge"])
        delta_px = _fmt_px(m["max_delta_px"])
        e_dim = _edge_dim(m["edge"], cw, ch)
        delta_pct = _fmt_axis_pct(m["max_delta_px"], e_dim, _edge_suffix(m["edge"]))
        parts.append(f", aligned on {edge_label} (delta {delta_px}px, {delta_pct})")

    return "".join(parts) + "."


def _render_distribution(rel: dict, id2name: Optional[dict], cw: float, ch: float) -> str:
    """
    Required: members, metrics.intervals_px
    Optional: metrics.axis, metrics.range_px
    """
    m = rel["metrics"]
    members = ", ".join(_ref(mid, id2name) for mid in rel["members"])

    axis = m.get("axis", "x")
    axis_label = {"x": "x", "y": "y", "x_gap": "x", "y_gap": "y"}.get(axis, axis)

    dist_dim = ch if axis_label == "y" else cw
    dist_suffix = "H" if axis_label == "y" else "W"

    intervals_pct = _fmt_axis_pct_list(m["intervals_px"], dist_dim, dist_suffix)
    intervals_px = _fmt_px(m["intervals_px"])

    parts = [f"dist| {members} evenly distributed along {axis_label}-axis, intervals {intervals_pct} ({intervals_px}px)"]

    if "range_px" in m:
        range_pct = _fmt_axis_pct(m["range_px"], dist_dim, dist_suffix)
        parts.append(f" (range {range_pct}, {_fmt_px(m['range_px'])}px)")

    return "".join(parts) + "."


def _render_containment(rel: dict, id2name: Optional[dict], cw: float, ch: float) -> str:
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
            t_pct = _fmt_axis_pct(p.get('top', 0), ch, 'H')
            r_pct = _fmt_axis_pct(p.get('right', 0), cw, 'W')
            b_pct = _fmt_axis_pct(p.get('bottom', 0), ch, 'H')
            l_pct = _fmt_axis_pct(p.get('left', 0), cw, 'W')
            t_px = _fmt_px(p.get('top', 0))
            r_px = _fmt_px(p.get('right', 0))
            b_px = _fmt_px(p.get('bottom', 0))
            l_px = _fmt_px(p.get('left', 0))
            parts.append(
                f", padding top {t_pct} right {r_pct} bottom {b_pct} left {l_pct}"
                f" (top {t_px} right {r_px} bottom {b_px} left {l_px}px)"
            )
        else:
            dim = min(cw, ch) if cw > 0 and ch > 0 else max(cw, ch)
            pct = _fmt_axis_pct(p, dim, '')
            parts.append(f", padding {pct} ({_fmt_px(p)}px)")

    return "".join(parts) + "."


def _render_adjacency(rel: dict, id2name: Optional[dict], cw: float, ch: float) -> str:
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
        dim = _dir_dim(m["direction"], cw, ch)
        suffix = _dir_suffix(m["direction"])
        gap_pct = _fmt_axis_pct(m["gap_px"], dim, suffix)
        parts.append(f", gap {gap_pct} ({_fmt_px(m['gap_px'])}px)")

    return "".join(parts) + "."


def _render_group(rel: dict, id2name: Optional[dict], cw: float, ch: float) -> str:
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
            delta_px = _fmt_px(m["internal_alignment_delta_px"])
            align = m["internal_alignment"]
            if align in ("top", "bottom", "y-center"):
                dim, suffix = ch, "H"
            else:
                dim, suffix = cw, "W"
            delta_pct = _fmt_axis_pct(m["internal_alignment_delta_px"], dim, suffix)
            parts.append(f" (delta {delta_px}px, {delta_pct})")

    if "internal_gap_px" in m:
        gap_axis = m.get("internal_gap_axis", "x")
        gap_dim = ch if gap_axis == "y" else cw
        gap_suffix = "H" if gap_axis == "y" else "W"
        gaps_pct = _fmt_axis_pct_list(m["internal_gap_px"], gap_dim, gap_suffix)
        gaps_px = _fmt_px(m["internal_gap_px"])
        parts.append(f", internal gaps {gaps_pct} ({gaps_px}px)")

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


# -- Density / CoM ----------------------------------------------------------


def _fmt_com_axis(offset_ratio: float, px_offset: float, pos_label: str, neg_label: str) -> str:
    """Format CoM offset axis: 'centered' or 'left 8.2% (157.4px)'."""
    if abs(offset_ratio) < 0.005:
        return "centered"
    direction = pos_label if offset_ratio >= 0 else neg_label
    pct = abs(offset_ratio) * 100
    px = abs(px_offset)
    return f"{direction} {pct:.1f}% ({px:.1f}px)"


def render_density(slide_metrics: dict) -> str:
    """Render slide-level density + CoM metrics as one DSL line."""
    rho = slide_metrics.get("occupancy_ratio", 0)
    om = slide_metrics.get("occupancy_match", 0)
    offset = slide_metrics.get("center_of_mass_offset", [0, 0])
    com_px = slide_metrics.get("center_of_mass_px", [0, 0])
    canvas_px = slide_metrics.get("canvas_center_px", [0, 0])

    ox, oy = offset[0], offset[1]
    px_dx = com_px[0] - canvas_px[0]
    px_dy = com_px[1] - canvas_px[1]

    x_str = _fmt_com_axis(ox, px_dx, "right", "left")
    y_str = _fmt_com_axis(oy, px_dy, "down", "up")

    return (
        f"density| occupancy {rho * 100:.1f}%, "
        f"target_match {om * 100:.0f}%, "
        f"CoM offset x {x_str}, y {y_str}."
    )


# -- Main renderer ----------------------------------------------------------


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

    # Extract canvas dimensions for % conversion
    cw, ch = 0.0, 0.0
    if slide_metrics:
        canvas_center = slide_metrics.get("canvas_center_px", [0, 0])
        cw = canvas_center[0] * 2
        ch = canvas_center[1] * 2

    # Extract adjacency coverage audit
    adj_coverage = None
    if not isinstance(data, list):
        adj_coverage = data.get("adj_coverage")

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
            lines.append(renderer(rel, id2name, cw, ch))

    # Adjacency coverage summary (after all relations)
    if adj_coverage:
        filled = adj_coverage["slots_filled"]
        total = adj_coverage["slots_total"]
        lines.append(f"adj_summary| slots filled {filled}/{total}.")

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
                "internal_alignment": "x-center",
                "internal_alignment_delta_px": 0,
                "internal_gap_px": [12],
                "internal_gap_axis": "x",
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

    test_data = {
        "slide_metrics": {
            "occupancy_ratio": 0.72,
            "occupancy_match": 0.88,
            "center_of_mass_offset": [0.032, 0.071],
            "center_of_mass_px": [532.0, 571.0],
            "canvas_center_px": [500.0, 500.0],
        },
        "relations": test_relations,
    }
    output = render_narrative(test_data, test_id2name)
    print(output)

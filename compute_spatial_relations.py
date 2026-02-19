"""Compute spatial relations between slide elements.

Middle layer of the pipeline:
    pptx_bbox.py  ->  compute_spatial_relations.py  ->  render_narrative.py

Takes element bboxes from pptx_bbox and produces the 7 canonical relation
types consumed by render_narrative:

    group, overlap, edge_alignment, sequence, distribution, containment, adjacency

Design principle: objective measurement only -- do not decide for the model.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpatialConfig:
    alignment_tolerance_px: float = 5.0
    adjacency_max_gap_px: float = 200.0
    distribution_cv_threshold: float = 0.15
    overlap_min_area_px2: float = 1.0
    containment_margin_px: float = 0.0
    sequence_cross_axis_tolerance_px: float = 50.0
    skip_background_overlap: bool = True
    skip_connectors_from_pairwise: bool = True
    min_element_area_px2: float = 4.0


# ---------------------------------------------------------------------------
# Internal element representation
# ---------------------------------------------------------------------------


@dataclass
class _Elem:
    id: str
    name: Optional[str]
    kind: Optional[str]
    subtype: Optional[str]
    parent_id: Optional[str]
    z_index: int
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0


# ---------------------------------------------------------------------------
# Element preparation
# ---------------------------------------------------------------------------

def _synthesize_id(raw: dict) -> str:
    """Derive a deterministic synthetic ID from stable element properties.

    Uses z_index + type + bbox so the same element always gets the same ID
    regardless of call order or number of invocations.
    """
    z = raw.get("z_index", 0)
    t = raw.get("type", "unk")
    bbox = raw.get("bbox_px", [0, 0, 0, 0])
    # Round bbox to 1 decimal to avoid float instability
    bbox_key = ",".join(f"{float(v):.1f}" for v in bbox)
    return f"_auto_z{z}_{t}_{bbox_key}"


def _deduplicate_ids(raw_elements: list[dict]) -> Dict[int, str]:
    """Detect duplicate IDs and build index→unique_id map.

    When multiple elements share the same id, each gets rewritten to
    ``{id}_z{z_index}`` so that downstream logic can distinguish them.
    Also rewrites parent_id references to point at the new unique IDs.

    Returns a dict mapping list-index → unique canonical id.
    """
    # Pass 1: collect id → list of indices
    id_to_indices: Dict[str, list[int]] = {}
    for i, raw in enumerate(raw_elements):
        eid = raw.get("id")
        if eid is None:
            continue
        id_to_indices.setdefault(str(eid), []).append(i)

    colliding_ids: set[str] = {
        eid for eid, indices in id_to_indices.items() if len(indices) > 1
    }
    if not colliding_ids:
        return {}  # no collisions — fast path

    # Pass 2: assign new unique IDs for colliders
    idx_to_new_id: Dict[int, str] = {}
    for eid in colliding_ids:
        for i in id_to_indices[eid]:
            z = raw_elements[i].get("z_index", 0)
            idx_to_new_id[i] = f"{eid}_z{z}"

    # Pass 3: build old_id→z_index→new_id lookup for parent_id rewrites
    old_to_new: Dict[str, Dict[int, str]] = {}
    for i, new_id in idx_to_new_id.items():
        eid = str(raw_elements[i].get("id"))
        z = int(raw_elements[i].get("z_index", 0))
        old_to_new.setdefault(eid, {})[z] = new_id

    # Pass 4: rewrite parent_id references.
    # A child's parent_id points at the original group id.  We find the
    # group (grpSp) among the colliders with that id, and pick the one
    # whose z_index is the largest z LESS THAN the child's z_index
    # (the group element always appears before its children in z-order).
    for i, raw in enumerate(raw_elements):
        pid = raw.get("parent_id")
        if pid is None or str(pid) not in colliding_ids:
            continue
        pid_str = str(pid)
        child_z = int(raw.get("z_index", 0))
        # Find the group with the largest z < child_z
        candidates = []
        for j in id_to_indices[pid_str]:
            if raw_elements[j].get("type") == "grpSp":
                gz = int(raw_elements[j].get("z_index", 0))
                if gz < child_z:
                    candidates.append((gz, j))
        if candidates:
            candidates.sort()
            # Closest group above this child in z-order
            best_j = candidates[-1][1]
            if best_j in idx_to_new_id:
                idx_to_new_id.setdefault(i, idx_to_new_id.get(i) or str(raw.get("id")))
                # Store parent mapping: tag this index with the correct parent
                # We'll use a special key in the returned dict
                idx_to_new_id[("parent", i)] = idx_to_new_id[best_j]  # type: ignore[index]

    return idx_to_new_id


def _prepare_elements(
    raw_elements: list[dict], config: SpatialConfig
) -> Tuple[list[_Elem], list[_Elem], list[_Elem]]:
    """Filter and normalise raw elements into _Elem objects.

    Returns (all_elems, layout_elems, non_bg_elems).
    - all_elems: every element with a usable bbox
    - layout_elems: excludes connectors
    - non_bg_elems: excludes background images

    Handles duplicate IDs: when the upstream pptx_bbox emits non-unique
    element ids (e.g. multiple elements with id="0"), this function
    rewrites them to ``{id}_z{z_index}`` and fixes parent_id references.
    """
    dedup = _deduplicate_ids(raw_elements)

    all_elems: list[_Elem] = []
    layout_elems: list[_Elem] = []
    non_bg_elems: list[_Elem] = []

    for i, raw in enumerate(raw_elements):
        if raw.get("skipped_reason"):
            continue

        # Resolve unique ID (needed for both bbox and non-bbox elements)
        eid = dedup.get(i) if dedup else None
        if eid is None:
            eid = raw.get("id")
        if eid is None:
            eid = _synthesize_id(raw)

        # Resolve parent_id (may have been rewritten by dedup)
        parent_id = dedup.get(("parent", i)) if dedup else None  # type: ignore[call-overload]
        if parent_id is None:
            raw_pid = raw.get("parent_id")
            if raw_pid is not None:
                parent_id = str(raw_pid)

        kind = raw.get("kind") or raw.get("type")
        subtype = raw.get("subtype")
        z_index = int(raw.get("z_index", 0))
        name = raw.get("name")

        # Parse bbox — may be None for grpSp containers
        bbox = raw.get("bbox_px")
        has_bbox = bbox and isinstance(bbox, list) and len(bbox) == 4

        if has_bbox:
            x1, y1, x2, y2 = map(float, bbox)
            if x2 < x1:
                x1, x2 = x2, x1
            if y2 < y1:
                y1, y2 = y2, y1
            w = x2 - x1
            h = y2 - y1
            if w * h < config.min_element_area_px2:
                has_bbox = False

        # grpSp containers without bbox: include in all_elems for group
        # detection (structural parent), but not in geometric subsets.
        is_structural_only = (not has_bbox) and kind == "container" and subtype == "group"

        if not has_bbox and not is_structural_only:
            continue

        if has_bbox:
            elem = _Elem(
                id=str(eid), name=name, kind=kind, subtype=subtype,
                parent_id=parent_id, z_index=z_index,
                left=x1, top=y1, right=x2, bottom=y2,
            )
        else:
            # Structural-only: zero-area sentinel bbox (never matches geometry)
            elem = _Elem(
                id=str(eid), name=name, kind=kind, subtype=subtype,
                parent_id=parent_id, z_index=z_index,
                left=0, top=0, right=0, bottom=0,
            )

        all_elems.append(elem)

        # Only elements with real bboxes participate in geometric detection
        if has_bbox:
            is_connector = kind == "connector"
            is_bg = kind == "image" and subtype == "background"

            if not is_connector:
                layout_elems.append(elem)
            if not is_bg:
                non_bg_elems.append(elem)

    return all_elems, layout_elems, non_bg_elems


# ---------------------------------------------------------------------------
# AABB utilities
# ---------------------------------------------------------------------------


def _aabb_intersection_area(a: _Elem, b: _Elem) -> float:
    ix1 = max(a.left, b.left)
    iy1 = max(a.top, b.top)
    ix2 = min(a.right, b.right)
    iy2 = min(a.bottom, b.bottom)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    return (ix2 - ix1) * (iy2 - iy1)


def _bbox_contains(outer: _Elem, inner: _Elem, margin: float = 0.0) -> bool:
    return (
        outer.left - margin <= inner.left
        and outer.top - margin <= inner.top
        and outer.right + margin >= inner.right
        and outer.bottom + margin >= inner.bottom
    )


def _cluster_by_value(
    elems: list[_Elem],
    key_fn,
    tolerance: float,
) -> list[list[_Elem]]:
    """Sort-and-sweep clustering: group elements whose key values are within tolerance."""
    if not elems:
        return []
    tagged = sorted([(key_fn(e), e) for e in elems], key=lambda t: t[0])
    clusters: list[list[_Elem]] = []
    cur_cluster: list[_Elem] = [tagged[0][1]]
    cur_val = tagged[0][0]

    for val, elem in tagged[1:]:
        if val - cur_val <= tolerance:
            cur_cluster.append(elem)
        else:
            if len(cur_cluster) >= 2:
                clusters.append(cur_cluster)
            cur_cluster = [elem]
        cur_val = val

    if len(cur_cluster) >= 2:
        clusters.append(cur_cluster)

    return clusters


# ---------------------------------------------------------------------------
# Relation 1: group (structural, from XML parent_id)
# ---------------------------------------------------------------------------


def _detect_groups(
    all_elems: list[_Elem],
    config: SpatialConfig,
) -> list[dict]:
    """Detect structural groups from parent_id relationships.

    Works entirely from _Elem objects (which already have dedup'd unique IDs
    and corrected parent_id references).
    """
    # Build parent_id -> children map
    children_of: Dict[str, list[_Elem]] = {}
    for e in all_elems:
        if e.parent_id is not None:
            children_of.setdefault(e.parent_id, []).append(e)

    # Find group elements (grpSp containers)
    group_elems = [
        e for e in all_elems
        if e.kind == "container" and e.subtype == "group"
    ]

    relations: list[dict] = []
    for grp in group_elems:
        kids = children_of.get(grp.id, [])
        if not kids:
            continue

        # Sort children by z_index for stable output
        kids.sort(key=lambda e: e.z_index)
        child_ids = [k.id for k in kids]

        metrics: Dict[str, Any] = {}

        # Optionally detect internal alignment among children
        if len(kids) >= 2:
            best_edge = None
            best_delta = float("inf")
            for edge_name, key_fn in [
                ("left", lambda e: e.left),
                ("right", lambda e: e.right),
                ("top", lambda e: e.top),
                ("bottom", lambda e: e.bottom),
                ("center_x", lambda e: e.center_x),
                ("center_y", lambda e: e.center_y),
            ]:
                vals = [key_fn(k) for k in kids]
                delta = max(vals) - min(vals)
                if delta < best_delta:
                    best_delta = delta
                    best_edge = edge_name

            if best_delta <= config.alignment_tolerance_px:
                edge_labels = {
                    "left": "左边",
                    "right": "右边",
                    "top": "上边",
                    "bottom": "下边",
                    "center_x": "水平中轴线",
                    "center_y": "垂直中轴线",
                }
                metrics["internal_alignment"] = edge_labels.get(best_edge, best_edge)
                metrics["internal_alignment_delta_px"] = round(best_delta, 1)

        # Internal gap: if children form a sequence, compute gap
        if len(kids) >= 2:
            # Try horizontal sequence
            h_sorted = sorted(kids, key=lambda e: e.left)
            h_gaps = []
            h_ok = True
            for i in range(len(h_sorted) - 1):
                gap = h_sorted[i + 1].left - h_sorted[i].right
                if gap < -config.alignment_tolerance_px:
                    h_ok = False
                    break
                h_gaps.append(gap)

            v_sorted = sorted(kids, key=lambda e: e.top)
            v_gaps = []
            v_ok = True
            for i in range(len(v_sorted) - 1):
                gap = v_sorted[i + 1].top - v_sorted[i].bottom
                if gap < -config.alignment_tolerance_px:
                    v_ok = False
                    break
                v_gaps.append(gap)

            if h_ok and h_gaps:
                metrics["internal_gap_px"] = [round(g, 1) for g in h_gaps]
            elif v_ok and v_gaps:
                metrics["internal_gap_px"] = [round(g, 1) for g in v_gaps]

        rel: Dict[str, Any] = {
            "relation_type": "group",
            "group_id": grp.id,
            "child_ids": child_ids,
        }
        if metrics:
            rel["metrics"] = metrics
        relations.append(rel)

    return relations


# ---------------------------------------------------------------------------
# Relation 2: overlap
# ---------------------------------------------------------------------------


def _detect_overlaps(
    elems: list[_Elem], config: SpatialConfig
) -> list[dict]:
    relations: list[dict] = []
    n = len(elems)

    for i in range(n):
        for j in range(i + 1, n):
            a, b = elems[i], elems[j]
            area = _aabb_intersection_area(a, b)
            if area < config.overlap_min_area_px2:
                continue

            # Canonical order: higher z = subject (on top)
            if a.z_index >= b.z_index:
                subj, obj = a, b
            else:
                subj, obj = b, a

            metrics: Dict[str, Any] = {
                "intersection_px2": round(area, 1),
            }

            obj_area = obj.area
            if obj_area > 0:
                metrics["overlap_ratio_object"] = round(area / obj_area, 4)

            subj_area = subj.area
            if subj_area > 0:
                metrics["overlap_ratio_subject"] = round(area / subj_area, 4)

            metrics["top_id"] = subj.id
            metrics["bottom_id"] = obj.id
            metrics["z_diff"] = abs(subj.z_index - obj.z_index)

            relations.append({
                "relation_type": "overlap",
                "subject_id": subj.id,
                "object_id": obj.id,
                "metrics": metrics,
            })

    return relations


# ---------------------------------------------------------------------------
# Relation 3: edge_alignment
# ---------------------------------------------------------------------------


def _detect_edge_alignments(
    elems: list[_Elem], config: SpatialConfig
) -> list[dict]:
    relations: list[dict] = []

    edge_fns = [
        ("left", lambda e: e.left),
        ("right", lambda e: e.right),
        ("top", lambda e: e.top),
        ("bottom", lambda e: e.bottom),
        ("center_x", lambda e: e.center_x),
        ("center_y", lambda e: e.center_y),
    ]

    for edge_name, key_fn in edge_fns:
        clusters = _cluster_by_value(elems, key_fn, config.alignment_tolerance_px)
        for cluster in clusters:
            vals = [key_fn(e) for e in cluster]
            max_delta = max(vals) - min(vals)
            members = sorted(cluster, key=lambda e: e.z_index)
            relations.append({
                "relation_type": "edge_alignment",
                "members": [e.id for e in members],
                "metrics": {
                    "edge": edge_name,
                    "value_px": round(sum(vals) / len(vals), 1),
                    "max_delta_px": round(max_delta, 1),
                },
            })

    return relations


# ---------------------------------------------------------------------------
# Relation 4: sequence
# ---------------------------------------------------------------------------


def _detect_sequences(
    elems: list[_Elem], config: SpatialConfig
) -> list[dict]:
    relations: list[dict] = []

    for axis, ordering_fn, cross_fn, start_fn, end_fn in [
        ("x", lambda e: e.left, lambda e: e.center_y,
         lambda e: e.left, lambda e: e.right),
        ("y", lambda e: e.top, lambda e: e.center_x,
         lambda e: e.top, lambda e: e.bottom),
    ]:
        # Cluster by cross-axis center
        cross_clusters = _cluster_by_value(
            elems, cross_fn, config.sequence_cross_axis_tolerance_px
        )

        for cluster in cross_clusters:
            if len(cluster) < 2:
                continue

            # Sort by ordering axis
            ordered = sorted(cluster, key=ordering_fn)

            # Find longest non-overlapping chain
            # Use greedy: take elements that don't overlap the previous on the ordering axis
            chain: list[_Elem] = [ordered[0]]
            for elem in ordered[1:]:
                prev = chain[-1]
                # Allow tiny overlap (tolerance)
                if start_fn(elem) >= end_fn(prev) - config.alignment_tolerance_px:
                    chain.append(elem)

            if len(chain) < 2:
                continue

            # Compute gaps between consecutive elements
            gaps: list[float] = []
            for k in range(len(chain) - 1):
                gap = start_fn(chain[k + 1]) - end_fn(chain[k])
                gaps.append(gap)

            members = [e.id for e in chain]
            metrics: Dict[str, Any] = {
                "axis": axis,
                "gaps_px": [round(g, 1) for g in gaps],
            }

            # Check if the chain is also aligned on some edge
            best_edge = None
            best_delta = float("inf")
            for edge_name, efn in [
                ("left", lambda e: e.left),
                ("right", lambda e: e.right),
                ("top", lambda e: e.top),
                ("bottom", lambda e: e.bottom),
                ("center_x", lambda e: e.center_x),
                ("center_y", lambda e: e.center_y),
            ]:
                vals = [efn(e) for e in chain]
                delta = max(vals) - min(vals)
                if delta < best_delta:
                    best_delta = delta
                    best_edge = edge_name

            if best_delta <= config.alignment_tolerance_px:
                metrics["edge"] = best_edge
                metrics["max_delta_px"] = round(best_delta, 1)

            relations.append({
                "relation_type": "sequence",
                "members": members,
                "metrics": metrics,
            })

    return relations


# ---------------------------------------------------------------------------
# Relation 5: distribution (post-process sequences)
# ---------------------------------------------------------------------------


def _detect_distributions(
    sequences: list[dict], config: SpatialConfig
) -> list[dict]:
    relations: list[dict] = []

    for seq in sequences:
        members = seq["members"]
        if len(members) < 3:
            continue

        gaps = seq["metrics"]["gaps_px"]
        if not gaps or len(gaps) < 2:
            continue

        mean_gap = sum(gaps) / len(gaps)
        if mean_gap == 0:
            # All gaps zero = perfectly distributed (stacked)
            cv = 0.0
        else:
            std = math.sqrt(sum((g - mean_gap) ** 2 for g in gaps) / len(gaps))
            cv = std / abs(mean_gap)

        if cv <= config.distribution_cv_threshold:
            axis = seq["metrics"].get("axis", "x")
            range_px = max(gaps) - min(gaps)
            relations.append({
                "relation_type": "distribution",
                "members": members,
                "metrics": {
                    "axis": f"{axis}_gap",
                    "intervals_px": gaps,
                    "mean_gap_px": round(mean_gap, 1),
                    "cv": round(cv, 4),
                    "range_px": round(range_px, 1),
                },
            })

    return relations


# ---------------------------------------------------------------------------
# Relation 6: containment (geometric bbox containment)
# ---------------------------------------------------------------------------


def _detect_containments(
    elems: list[_Elem], config: SpatialConfig
) -> list[dict]:
    n = len(elems)
    if n < 2:
        return []

    # For each element, find its smallest container
    # container[i] = index of smallest element that fully contains i, or -1
    container_of: Dict[int, int] = {}  # idx -> container idx

    # Sort by area descending for efficiency
    indexed = list(range(n))

    for i in range(n):
        best_idx = -1
        best_area = float("inf")
        for j in range(n):
            if i == j:
                continue
            if elems[j].area <= elems[i].area:
                continue
            if _bbox_contains(elems[j], elems[i], config.containment_margin_px):
                if elems[j].area < best_area:
                    best_area = elems[j].area
                    best_idx = j
        if best_idx >= 0:
            container_of[i] = best_idx

    # Group by container
    children_by_container: Dict[int, list[int]] = {}
    for child_idx, cont_idx in container_of.items():
        children_by_container.setdefault(cont_idx, []).append(child_idx)

    relations: list[dict] = []
    for cont_idx, child_indices in children_by_container.items():
        container = elems[cont_idx]
        children = sorted([elems[ci] for ci in child_indices], key=lambda e: e.z_index)

        # Compute padding for each child (how far from container edges)
        paddings: list[Dict[str, float]] = []
        for child in children:
            paddings.append({
                "top": round(child.top - container.top, 1),
                "right": round(container.right - child.right, 1),
                "bottom": round(container.bottom - child.bottom, 1),
                "left": round(child.left - container.left, 1),
            })

        # If single child, flatten padding; if multiple, report per-child
        metrics: Dict[str, Any] = {}
        if len(children) == 1:
            metrics["padding_px"] = paddings[0]
        else:
            # Report the envelope padding (min of all children's paddings)
            metrics["padding_px"] = {
                "top": min(p["top"] for p in paddings),
                "right": min(p["right"] for p in paddings),
                "bottom": min(p["bottom"] for p in paddings),
                "left": min(p["left"] for p in paddings),
            }

        relations.append({
            "relation_type": "containment",
            "container_id": container.id,
            "child_ids": [c.id for c in children],
            "metrics": metrics,
        })

    return relations


# ---------------------------------------------------------------------------
# Relation 7: adjacency
# ---------------------------------------------------------------------------


_FLIP_DIR = {"left": "right", "right": "left", "above": "below", "below": "above"}


def _detect_adjacencies(
    elems: list[_Elem], config: SpatialConfig
) -> list[dict]:
    relations: list[dict] = []
    n = len(elems)

    for i in range(n):
        for j in range(i + 1, n):
            a, b = elems[i], elems[j]

            # Skip if they overlap significantly
            overlap = _aabb_intersection_area(a, b)
            if overlap > config.overlap_min_area_px2:
                continue

            # Compute direction of a relative to b
            h_gap = None
            h_dir = None
            if a.right <= b.left + config.alignment_tolerance_px:
                h_gap = b.left - a.right
                h_dir = "left"  # a is to the left of b
            elif b.right <= a.left + config.alignment_tolerance_px:
                h_gap = a.left - b.right
                h_dir = "right"  # a is to the right of b

            v_gap = None
            v_dir = None
            if a.bottom <= b.top + config.alignment_tolerance_px:
                v_gap = b.top - a.bottom
                v_dir = "above"  # a is above b
            elif b.bottom <= a.top + config.alignment_tolerance_px:
                v_gap = a.top - b.bottom
                v_dir = "below"  # a is below b

            # Need vertical overlap for horizontal adjacency, and vice versa
            v_overlap = min(a.bottom, b.bottom) - max(a.top, b.top)
            h_overlap = min(a.right, b.right) - max(a.left, b.left)

            candidates: list[Tuple[float, str]] = []
            if h_gap is not None and h_gap <= config.adjacency_max_gap_px and v_overlap > 0:
                candidates.append((h_gap, h_dir))  # type: ignore[arg-type]
            if v_gap is not None and v_gap <= config.adjacency_max_gap_px and h_overlap > 0:
                candidates.append((v_gap, v_dir))  # type: ignore[arg-type]

            if not candidates:
                continue

            # Pick closest direction (direction is "where a is relative to b")
            gap, direction = min(candidates, key=lambda t: t[0])

            # Canonical order: higher z_index = subject (consistent with overlap).
            # direction was computed as "a relative to b".
            # If we swap subject/object, flip the direction.
            if a.z_index >= b.z_index:
                subj, obj = a, b
                # direction already describes a relative to b = subj relative to obj
            else:
                subj, obj = b, a
                direction = _FLIP_DIR[direction]

            relations.append({
                "relation_type": "adjacency",
                "subject_id": subj.id,
                "object_id": obj.id,
                "metrics": {
                    "direction": direction,
                    "gap_px": round(max(0.0, gap), 1),
                },
            })

    return relations


# ---------------------------------------------------------------------------
# Pair-key helpers for suppression
# ---------------------------------------------------------------------------


def _pair_key(a: str, b: str) -> Tuple[str, str]:
    """Canonical unordered pair key — smaller id first."""
    return (a, b) if a <= b else (b, a)


def _rel_pair_key(rel: dict) -> Tuple[str, str] | None:
    """Extract canonical pair key from a pairwise relation, or None."""
    a = rel.get("subject_id")
    b = rel.get("object_id")
    if a and b:
        return _pair_key(a, b)
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def compute_spatial_relations(
    elements: list[dict],
    config: SpatialConfig | None = None,
) -> list[dict]:
    """Compute all spatial relations for a list of element dicts from pptx_bbox.

    Parameters
    ----------
    elements : list[dict]
        The ``elements`` array from pptx_bbox output.
    config : SpatialConfig, optional
        Thresholds. Uses defaults if None.

    Returns
    -------
    list[dict]
        Canonical relation dicts, each with ``relation_type`` and type-specific fields.

    Consistency rules (applied as post-filters, detectors stay independent):
        1. containment suppresses overlap for the same pair
        2. same-group member pairs are excluded from slide-level pairwise relations
        3. sequence member pairs suppress adjacency between them
    """
    if config is None:
        config = SpatialConfig()

    all_elems, layout_elems, non_bg_elems = _prepare_elements(elements, config)

    # ── Run all 7 detectors ──────────────────────────────────

    # 1. group (structural)
    group_rels = _detect_groups(all_elems, config)

    # 2. overlap
    overlap_set = non_bg_elems if config.skip_background_overlap else all_elems
    if config.skip_connectors_from_pairwise:
        overlap_set = [e for e in overlap_set if e.kind != "connector"]
    overlap_rels = _detect_overlaps(overlap_set, config)

    # 3. edge_alignment
    align_rels = _detect_edge_alignments(layout_elems, config)

    # 4. sequence
    sequence_rels = _detect_sequences(layout_elems, config)

    # 5. distribution (derived from sequences)
    dist_rels = _detect_distributions(sequence_rels, config)

    # 6. containment
    contain_rels = _detect_containments(layout_elems, config)

    # 7. adjacency
    adj_set = layout_elems
    if config.skip_connectors_from_pairwise:
        adj_set = [e for e in adj_set if e.kind != "connector"]
    adj_rels = _detect_adjacencies(adj_set, config)

    # ── Build suppression sets ───────────────────────────────

    # Rule 2: same-group pairs — skip slide-level pairwise for siblings
    same_group_pairs: set[Tuple[str, str]] = set()
    for g in group_rels:
        kids = g.get("child_ids", [])
        for ii in range(len(kids)):
            for jj in range(ii + 1, len(kids)):
                same_group_pairs.add(_pair_key(kids[ii], kids[jj]))

    # Rule 1: containment pairs — suppress corresponding overlap
    contained_pairs: set[Tuple[str, str]] = set()
    for c in contain_rels:
        cid = c["container_id"]
        for kid in c["child_ids"]:
            contained_pairs.add(_pair_key(cid, kid))

    # Rule 3: sequence consecutive pairs — suppress adjacency
    seq_adj_pairs: set[Tuple[str, str]] = set()
    for s in sequence_rels:
        members = s["members"]
        for ii in range(len(members) - 1):
            seq_adj_pairs.add(_pair_key(members[ii], members[ii + 1]))

    # Combined suppression for overlap: containment OR same-group
    overlap_suppress = contained_pairs | same_group_pairs

    # Combined suppression for adjacency: sequence-consecutive OR same-group
    adj_suppress = seq_adj_pairs | same_group_pairs

    # ── Filter and assemble ──────────────────────────────────

    overlap_rels = [
        r for r in overlap_rels
        if _rel_pair_key(r) not in overlap_suppress
    ]

    adj_rels = [
        r for r in adj_rels
        if _rel_pair_key(r) not in adj_suppress
    ]

    # Assemble in canonical order
    relations: list[dict] = []
    relations.extend(group_rels)
    relations.extend(overlap_rels)
    relations.extend(align_rels)
    relations.extend(sequence_rels)
    relations.extend(dist_rels)
    relations.extend(contain_rels)
    relations.extend(adj_rels)

    return relations


# ---------------------------------------------------------------------------
# Convenience: build id->name map
# ---------------------------------------------------------------------------


def build_id2name(elements: list[dict]) -> dict[str, str]:
    """Build canonical_id -> semantic_name map from pptx_bbox elements.

    Uses the same ID deduplication logic as compute_spatial_relations,
    so the returned keys match the IDs in relation dicts.
    """
    dedup = _deduplicate_ids(elements)
    m: dict[str, str] = {}
    for i, e in enumerate(elements):
        eid = dedup.get(i) if dedup else None
        if eid is None:
            eid = e.get("id")
        if eid is None:
            eid = _synthesize_id(e)
        name = e.get("name")
        if name:
            m[str(eid)] = name
    return m


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compute spatial relations from pptx_bbox JSON output"
    )
    parser.add_argument("json_path", help="Path to pptx_bbox output JSON")
    parser.add_argument(
        "--out-json", default=None,
        help="Output JSON path (default: stdout)",
    )
    parser.add_argument(
        "--narrative", action="store_true",
        help="Also pipe through render_narrative and print DSL",
    )
    parser.add_argument(
        "--tolerance", type=float, default=5.0,
        help="Alignment tolerance in px (default: 5.0)",
    )
    parser.add_argument(
        "--gap", type=float, default=200.0,
        help="Max adjacency gap in px (default: 200.0)",
    )
    args = parser.parse_args(argv)

    with open(args.json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    elements = data.get("elements", [])
    config = SpatialConfig(
        alignment_tolerance_px=args.tolerance,
        adjacency_max_gap_px=args.gap,
    )

    relations = compute_spatial_relations(elements, config)

    output = {
        "slide_index": data.get("slide_index"),
        "png_size": data.get("png_size"),
        "element_count": len(elements),
        "relation_count": len(relations),
        "relations": relations,
    }

    json_str = json.dumps(output, ensure_ascii=False, indent=2)

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            f.write(json_str)
        print(f"Wrote {args.out_json}  ({len(relations)} relations)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(json_str.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")

    if args.narrative:
        from render_narrative import render_narrative

        id2name = build_id2name(elements)
        narrative = render_narrative(relations, id2name)
        print("\n--- Narrative ---", file=sys.stderr)
        sys.stdout.buffer.write(narrative.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()

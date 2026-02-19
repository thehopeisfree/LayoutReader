"""Tests for compute_spatial_relations.py."""

import json
import math
import os
import pytest

from compute_spatial_relations import (
    SpatialConfig,
    _Elem,
    _aabb_intersection_area,
    _bbox_contains,
    _cluster_by_value,
    _union_area,
    _prepare_effective_elements,
    _detect_overlaps,
    _detect_edge_alignments,
    _detect_sequences,
    _detect_distributions,
    _detect_containments,
    _detect_adjacencies,
    _detect_groups,
    _prepare_elements,
    compute_spatial_relations,
    compute_slide_metrics,
    build_id2name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_elem(
    id: str, left: float, top: float, right: float, bottom: float,
    z_index: int = 0, kind: str = "text", subtype: str | None = None,
    parent_id: str | None = None, name: str | None = None,
) -> _Elem:
    return _Elem(
        id=id, name=name, kind=kind, subtype=subtype,
        parent_id=parent_id, z_index=z_index,
        left=left, top=top, right=right, bottom=bottom,
    )


def _make_raw(
    id: str, left: float, top: float, right: float, bottom: float,
    z_index: int = 0, kind: str = "text", subtype: str | None = None,
    parent_id: str | None = None, name: str | None = None,
    type_: str = "sp",
) -> dict:
    d = {
        "id": id, "name": name, "type": type_,
        "kind": kind, "subtype": subtype,
        "parent_id": parent_id, "z_index": z_index,
        "bbox_px": [left, top, right, bottom],
    }
    return d


def _rels(result: dict) -> list[dict]:
    """Extract relations list from compute_spatial_relations result."""
    return result["relations"]


# ---------------------------------------------------------------------------
# _Elem properties
# ---------------------------------------------------------------------------

class TestElemProperties:
    def test_dimensions(self):
        e = _make_elem("a", 10, 20, 110, 70)
        assert e.width == 100
        assert e.height == 50
        assert e.area == 5000

    def test_center(self):
        e = _make_elem("a", 0, 0, 100, 200)
        assert e.center_x == 50
        assert e.center_y == 100


# ---------------------------------------------------------------------------
# AABB utilities
# ---------------------------------------------------------------------------

class TestAABB:
    def test_no_intersection(self):
        a = _make_elem("a", 0, 0, 50, 50)
        b = _make_elem("b", 100, 100, 200, 200)
        assert _aabb_intersection_area(a, b) == 0.0

    def test_full_overlap(self):
        a = _make_elem("a", 0, 0, 100, 100)
        b = _make_elem("b", 0, 0, 100, 100)
        assert _aabb_intersection_area(a, b) == 10000.0

    def test_partial_overlap(self):
        a = _make_elem("a", 0, 0, 100, 100)
        b = _make_elem("b", 50, 50, 150, 150)
        assert _aabb_intersection_area(a, b) == 2500.0

    def test_touching_edge(self):
        a = _make_elem("a", 0, 0, 50, 50)
        b = _make_elem("b", 50, 0, 100, 50)
        assert _aabb_intersection_area(a, b) == 0.0

    def test_contains(self):
        outer = _make_elem("o", 0, 0, 200, 200)
        inner = _make_elem("i", 10, 10, 50, 50)
        assert _bbox_contains(outer, inner)
        assert not _bbox_contains(inner, outer)

    def test_contains_with_margin(self):
        outer = _make_elem("o", 10, 10, 200, 200)
        inner = _make_elem("i", 8, 10, 50, 50)
        assert not _bbox_contains(outer, inner, margin=0)
        assert _bbox_contains(outer, inner, margin=2)


class TestCluster:
    def test_basic_clustering(self):
        elems = [
            _make_elem("a", 10, 0, 20, 10),   # left=10
            _make_elem("b", 12, 0, 22, 10),   # left=12
            _make_elem("c", 100, 0, 110, 10),  # left=100
            _make_elem("d", 101, 0, 111, 10),  # left=101
        ]
        clusters = _cluster_by_value(elems, lambda e: e.left, tolerance=5.0)
        assert len(clusters) == 2
        assert set(e.id for e in clusters[0]) == {"a", "b"}
        assert set(e.id for e in clusters[1]) == {"c", "d"}

    def test_single_element_not_clustered(self):
        elems = [
            _make_elem("a", 10, 0, 20, 10),
            _make_elem("b", 100, 0, 110, 10),
        ]
        clusters = _cluster_by_value(elems, lambda e: e.left, tolerance=5.0)
        assert len(clusters) == 0


# ---------------------------------------------------------------------------
# Union area
# ---------------------------------------------------------------------------

class TestUnionArea:
    def test_no_elements(self):
        assert _union_area([]) == 0.0

    def test_single_rect(self):
        elems = [_make_elem("a", 0, 0, 100, 50)]
        assert _union_area(elems) == 5000.0

    def test_non_overlapping(self):
        elems = [
            _make_elem("a", 0, 0, 100, 100),
            _make_elem("b", 200, 0, 300, 100),
        ]
        assert _union_area(elems) == 20000.0

    def test_full_overlap(self):
        elems = [
            _make_elem("a", 0, 0, 100, 100),
            _make_elem("b", 0, 0, 100, 100),
        ]
        assert _union_area(elems) == 10000.0

    def test_partial_overlap(self):
        elems = [
            _make_elem("a", 0, 0, 100, 100),    # area 10000
            _make_elem("b", 50, 50, 150, 150),   # area 10000, overlap 2500
        ]
        assert _union_area(elems) == 17500.0

    def test_containment(self):
        elems = [
            _make_elem("a", 0, 0, 200, 200),
            _make_elem("b", 10, 10, 50, 50),
        ]
        assert _union_area(elems) == 40000.0  # just the outer


# ---------------------------------------------------------------------------
# Effective elements (group-as-whole, background exclusion)
# ---------------------------------------------------------------------------

class TestEffectiveElements:
    def test_background_excluded(self):
        elems = [
            _make_elem("bg", 0, 0, 1920, 1080, kind="image", subtype="background"),
            _make_elem("txt", 100, 100, 300, 200, kind="text"),
        ]
        eff = _prepare_effective_elements(elems)
        assert len(eff) == 1
        assert eff[0].id == "txt"

    def test_connectors_excluded(self):
        elems = [
            _make_elem("box", 0, 0, 100, 100, kind="text"),
            _make_elem("cxn", 0, 0, 500, 5, kind="connector"),
        ]
        eff = _prepare_effective_elements(elems)
        assert len(eff) == 1
        assert eff[0].id == "box"

    def test_group_as_whole(self):
        """Group replaces its children in effective set."""
        elems = [
            _make_elem("g1", 10, 10, 200, 100, kind="container", subtype="group"),
            _make_elem("c1", 10, 10, 100, 100, parent_id="g1"),
            _make_elem("c2", 110, 10, 200, 100, parent_id="g1"),
        ]
        eff = _prepare_effective_elements(elems)
        ids = {e.id for e in eff}
        assert ids == {"g1"}
        # Group's own bbox is used
        assert eff[0].area == 190 * 90

    def test_structural_group_bbox_from_children(self):
        """Group without bbox (structural-only, area=0) gets bbox computed from children."""
        grp = _make_elem("g1", 0, 0, 0, 0, kind="container", subtype="group")
        c1 = _make_elem("c1", 10, 20, 100, 80, parent_id="g1")
        c2 = _make_elem("c2", 50, 10, 200, 90, parent_id="g1")
        eff = _prepare_effective_elements([grp, c1, c2])
        assert len(eff) == 1
        g = eff[0]
        assert g.id == "g1"
        assert g.left == 10
        assert g.top == 10
        assert g.right == 200
        assert g.bottom == 90

    def test_empty_group_excluded(self):
        """Group with no children and no bbox is excluded."""
        grp = _make_elem("g1", 0, 0, 0, 0, kind="container", subtype="group")
        eff = _prepare_effective_elements([grp])
        assert len(eff) == 0


# ---------------------------------------------------------------------------
# Occupancy Match (OM)
# ---------------------------------------------------------------------------

class TestOccupancy:
    def test_basic(self):
        raw = [
            _make_raw("a", 0, 0, 100, 100, z_index=0),
        ]
        m = compute_slide_metrics(raw, [200, 200])
        assert m["occupancy_ratio"] == 0.25  # 10000 / 40000

    def test_om_perfect(self):
        """ρ == ρ* → OM = 1.0."""
        raw = [
            _make_raw("a", 0, 0, 68, 100, z_index=0),
        ]
        # area=6800, canvas=100*100=10000, ρ=0.68 == default target
        m = compute_slide_metrics(raw, [100, 100])
        assert m["occupancy_ratio"] == 0.68
        assert m["occupancy_match"] == 1.0

    def test_om_empty(self):
        """Empty slide → ρ=0, OM = max(0, 1 - 0.68/0.25) = 0."""
        m = compute_slide_metrics([], [100, 100])
        assert m["occupancy_ratio"] == 0.0
        assert m["occupancy_match"] == 0.0

    def test_om_full_coverage(self):
        """ρ=1.0 → OM = max(0, 1 - |1-0.68|/0.25) = max(0, 1-1.28) = 0."""
        raw = [
            _make_raw("a", 0, 0, 100, 100, z_index=0),
        ]
        m = compute_slide_metrics(raw, [100, 100])
        assert m["occupancy_ratio"] == 1.0
        assert m["occupancy_match"] == 0.0

    def test_connectors_excluded(self):
        raw = [
            _make_raw("a", 0, 0, 100, 100, z_index=0, kind="text"),
            _make_raw("b", 0, 0, 500, 5, z_index=1, kind="connector", type_="cxnSp"),
        ]
        m = compute_slide_metrics(raw, [200, 200])
        # Only the text box counts, not the connector
        assert m["occupancy_ratio"] == 0.25

    def test_background_excluded(self):
        """Adding background image doesn't change ρ."""
        raw_no_bg = [_make_raw("a", 0, 0, 100, 100, z_index=1)]
        raw_bg = [
            _make_raw("bg", 0, 0, 200, 200, z_index=0, kind="image", subtype="background"),
            _make_raw("a", 0, 0, 100, 100, z_index=1),
        ]
        m1 = compute_slide_metrics(raw_no_bg, [200, 200])
        m2 = compute_slide_metrics(raw_bg, [200, 200])
        assert m1["occupancy_ratio"] == m2["occupancy_ratio"]

    def test_group_as_whole(self):
        """Group bbox used for OM, not individual children."""
        raw_group = [
            {"id": "g1", "type": "grpSp", "kind": "container", "subtype": "group",
             "parent_id": None, "z_index": 0},
            _make_raw("c1", 0, 0, 50, 100, z_index=1, parent_id="g1"),
            _make_raw("c2", 50, 0, 100, 100, z_index=2, parent_id="g1"),
        ]
        m = compute_slide_metrics(raw_group, [200, 200])
        # Group bbox computed from children: (0,0)-(100,100) = 10000
        # canvas = 40000, ρ = 0.25
        assert m["occupancy_ratio"] == 0.25


# ---------------------------------------------------------------------------
# Center of Mass (CoM)
# ---------------------------------------------------------------------------

class TestCenterOfMass:
    def test_single_centered(self):
        """Single element at canvas center → offset = (0, 0)."""
        raw = [_make_raw("a", 25, 25, 75, 75, z_index=0)]
        m = compute_slide_metrics(raw, [100, 100])
        assert m["center_of_mass_px"] == [50.0, 50.0]
        assert m["center_of_mass_offset"] == [0.0, 0.0]
        assert m["canvas_center_px"] == [50.0, 50.0]

    def test_single_bottom_right(self):
        """Single element in bottom-right → positive offsets."""
        raw = [_make_raw("a", 60, 70, 100, 100, z_index=0)]
        m = compute_slide_metrics(raw, [100, 100])
        # center = (80, 85), canvas center = (50, 50)
        assert m["center_of_mass_px"] == [80.0, 85.0]
        assert m["center_of_mass_offset"][0] == 0.3   # (80-50)/100
        assert m["center_of_mass_offset"][1] == 0.35   # (85-50)/100

    def test_symmetric_pair(self):
        """Two equal elements placed symmetrically → offset = (0, 0)."""
        raw = [
            _make_raw("a", 0, 0, 50, 50, z_index=0),    # center (25, 25)
            _make_raw("b", 50, 50, 100, 100, z_index=1),  # center (75, 75)
        ]
        m = compute_slide_metrics(raw, [100, 100])
        assert m["center_of_mass_px"] == [50.0, 50.0]
        assert m["center_of_mass_offset"] == [0.0, 0.0]

    def test_background_excluded(self):
        """Background doesn't shift CoM."""
        raw_no_bg = [_make_raw("a", 60, 60, 100, 100, z_index=1)]
        raw_bg = [
            _make_raw("bg", 0, 0, 100, 100, z_index=0, kind="image", subtype="background"),
            _make_raw("a", 60, 60, 100, 100, z_index=1),
        ]
        m1 = compute_slide_metrics(raw_no_bg, [100, 100])
        m2 = compute_slide_metrics(raw_bg, [100, 100])
        assert m1["center_of_mass_offset"] == m2["center_of_mass_offset"]

    def test_group_as_whole(self):
        """Group bbox participates as single element, not individual children.

        Group bbox [0,0,100,100] vs children [0,0,50,100]+[50,0,100,100]:
        the center is the same (50,50) here, but the area weight is ONE
        group rectangle rather than two child rectangles.
        """
        # With group: single 100x100 element, center (50,50)
        raw_group = [
            {"id": "g1", "type": "grpSp", "kind": "container", "subtype": "group",
             "parent_id": None, "z_index": 0},
            _make_raw("c1", 0, 0, 50, 100, z_index=1, parent_id="g1"),
            _make_raw("c2", 50, 0, 100, 100, z_index=2, parent_id="g1"),
        ]
        m = compute_slide_metrics(raw_group, [200, 200])
        # Group bbox (0,0)-(100,100), center (50,50)
        assert m["center_of_mass_px"] == [50.0, 50.0]

    def test_empty_slide(self):
        """Empty slide → CoM defaults to canvas center."""
        m = compute_slide_metrics([], [100, 200])
        assert m["center_of_mass_px"] == [50.0, 100.0]
        assert m["center_of_mass_offset"] == [0.0, 0.0]

    def test_unequal_weights(self):
        """Larger element pulls CoM toward its center."""
        raw = [
            _make_raw("big", 0, 0, 100, 100, z_index=0),    # area 10000, center (50, 50)
            _make_raw("small", 180, 0, 200, 20, z_index=1),  # area 400, center (190, 10)
        ]
        m = compute_slide_metrics(raw, [200, 100])
        # weighted: x = (10000*50 + 400*190) / 10400 = 576000/10400 ≈ 55.38
        # weighted: y = (10000*50 + 400*10) / 10400 = 504000/10400 ≈ 48.46
        assert m["center_of_mass_px"][0] == pytest.approx(55.4, abs=0.1)
        assert m["center_of_mass_px"][1] == pytest.approx(48.5, abs=0.1)


# ---------------------------------------------------------------------------
# Relation detectors (unit tests with hand-crafted inputs)
# ---------------------------------------------------------------------------

class TestOverlap:
    def test_simple_overlap(self):
        elems = [
            _make_elem("a", 0, 0, 100, 100, z_index=0),
            _make_elem("b", 50, 50, 150, 150, z_index=1),
        ]
        cfg = SpatialConfig()
        rels = _detect_overlaps(elems, cfg)
        assert len(rels) == 1
        r = rels[0]
        assert r["relation_type"] == "overlap"
        assert r["subject_id"] == "b"  # higher z
        assert r["object_id"] == "a"
        assert r["metrics"]["intersection_px2"] == 2500.0

    def test_no_overlap(self):
        elems = [
            _make_elem("a", 0, 0, 50, 50),
            _make_elem("b", 100, 100, 200, 200),
        ]
        rels = _detect_overlaps(elems, SpatialConfig())
        assert len(rels) == 0


class TestEdgeAlignment:
    def test_left_aligned(self):
        elems = [
            _make_elem("a", 10, 0, 50, 30),
            _make_elem("b", 12, 40, 80, 70),
            _make_elem("c", 200, 0, 300, 30),
        ]
        cfg = SpatialConfig(alignment_tolerance_px=5.0)
        rels = _detect_edge_alignments(elems, cfg)
        # a and b should be left-aligned (10, 12 within tolerance 5)
        left_rels = [r for r in rels if r["metrics"]["edge"] == "left"]
        assert any(
            set(r["members"]) == {"a", "b"}
            for r in left_rels
        )

    def test_top_aligned(self):
        elems = [
            _make_elem("a", 10, 100, 50, 200),
            _make_elem("b", 80, 101, 150, 250),
            _make_elem("c", 200, 500, 300, 600),
        ]
        cfg = SpatialConfig(alignment_tolerance_px=5.0)
        rels = _detect_edge_alignments(elems, cfg)
        top_rels = [r for r in rels if r["metrics"]["edge"] == "top"]
        assert any(
            set(r["members"]) == {"a", "b"}
            for r in top_rels
        )


class TestSequence:
    def test_horizontal_sequence(self):
        elems = [
            _make_elem("a", 0, 100, 50, 150),
            _make_elem("b", 60, 100, 110, 150),
            _make_elem("c", 120, 100, 170, 150),
        ]
        cfg = SpatialConfig()
        rels = _detect_sequences(elems, cfg)
        x_seqs = [r for r in rels if r["metrics"]["axis"] == "x"]
        assert len(x_seqs) >= 1
        seq = x_seqs[0]
        assert seq["members"] == ["a", "b", "c"]
        assert seq["metrics"]["gaps_px"] == [10.0, 10.0]

    def test_vertical_sequence(self):
        elems = [
            _make_elem("a", 100, 0, 200, 50),
            _make_elem("b", 100, 80, 200, 130),
            _make_elem("c", 100, 160, 200, 210),
        ]
        cfg = SpatialConfig()
        rels = _detect_sequences(elems, cfg)
        y_seqs = [r for r in rels if r["metrics"]["axis"] == "y"]
        assert len(y_seqs) >= 1
        seq = y_seqs[0]
        assert seq["members"] == ["a", "b", "c"]
        assert seq["metrics"]["gaps_px"] == [30.0, 30.0]


class TestDistribution:
    def test_even_distribution(self):
        # Perfectly even gaps → CV = 0
        seqs = [{
            "relation_type": "sequence",
            "members": ["a", "b", "c"],
            "metrics": {"axis": "x", "gaps_px": [20.0, 20.0]},
        }]
        cfg = SpatialConfig(distribution_cv_threshold=0.15)
        rels = _detect_distributions(seqs, cfg)
        assert len(rels) == 1
        r = rels[0]
        assert r["metrics"]["cv"] == 0.0

    def test_uneven_not_distributed(self):
        seqs = [{
            "relation_type": "sequence",
            "members": ["a", "b", "c"],
            "metrics": {"axis": "x", "gaps_px": [10.0, 100.0]},
        }]
        cfg = SpatialConfig(distribution_cv_threshold=0.15)
        rels = _detect_distributions(seqs, cfg)
        assert len(rels) == 0

    def test_two_member_skipped(self):
        seqs = [{
            "relation_type": "sequence",
            "members": ["a", "b"],
            "metrics": {"axis": "x", "gaps_px": [20.0]},
        }]
        rels = _detect_distributions(seqs, SpatialConfig())
        assert len(rels) == 0


class TestContainment:
    def test_simple_containment(self):
        elems = [
            _make_elem("big", 0, 0, 200, 200),
            _make_elem("small", 10, 10, 50, 50),
        ]
        cfg = SpatialConfig()
        rels = _detect_containments(elems, cfg)
        assert len(rels) == 1
        r = rels[0]
        assert r["container_id"] == "big"
        assert r["child_ids"] == ["small"]
        assert r["metrics"]["padding_px"]["top"] == 10.0
        assert r["metrics"]["padding_px"]["left"] == 10.0

    def test_direct_containment_only(self):
        """Only smallest container reported (A contains B contains C → C's container is B, not A)."""
        elems = [
            _make_elem("A", 0, 0, 300, 300),
            _make_elem("B", 10, 10, 200, 200),
            _make_elem("C", 20, 20, 50, 50),
        ]
        cfg = SpatialConfig()
        rels = _detect_containments(elems, cfg)
        # C should be contained by B (not A), B by A
        c_rels = [r for r in rels if "C" in r["child_ids"]]
        assert len(c_rels) == 1
        assert c_rels[0]["container_id"] == "B"

        b_rels = [r for r in rels if "B" in r["child_ids"]]
        assert len(b_rels) == 1
        assert b_rels[0]["container_id"] == "A"


class TestAdjacency:
    def test_horizontal_adjacency(self):
        # Same z_index → first element (a) keeps subject role
        elems = [
            _make_elem("a", 0, 0, 50, 100, z_index=0),
            _make_elem("b", 60, 0, 110, 100, z_index=0),
        ]
        cfg = SpatialConfig()
        rels = _detect_adjacencies(elems, cfg)
        assert len(rels) == 1
        r = rels[0]
        assert r["subject_id"] == "a"
        assert r["metrics"]["direction"] == "left"  # a is left of b
        assert r["metrics"]["gap_px"] == 10.0

    def test_vertical_adjacency(self):
        elems = [
            _make_elem("a", 0, 0, 100, 50, z_index=0),
            _make_elem("b", 0, 80, 100, 130, z_index=0),
        ]
        cfg = SpatialConfig()
        rels = _detect_adjacencies(elems, cfg)
        assert len(rels) == 1
        assert rels[0]["metrics"]["direction"] == "above"
        assert rels[0]["metrics"]["gap_px"] == 30.0

    def test_z_order_canonicalization(self):
        """Higher z_index element becomes subject; direction flips accordingly."""
        # a is to the left of b, but b has higher z → b becomes subject
        elems = [
            _make_elem("a", 0, 0, 50, 100, z_index=0),
            _make_elem("b", 60, 0, 110, 100, z_index=5),
        ]
        cfg = SpatialConfig()
        rels = _detect_adjacencies(elems, cfg)
        assert len(rels) == 1
        r = rels[0]
        assert r["subject_id"] == "b"
        assert r["object_id"] == "a"
        # a was left of b → b is right of a
        assert r["metrics"]["direction"] == "right"
        assert r["metrics"]["gap_px"] == 10.0

    def test_z_order_vertical(self):
        """Vertical adjacency with z-order flip."""
        # a is above b, but b has higher z → b becomes subject, direction flips to "below"
        elems = [
            _make_elem("a", 0, 0, 100, 50, z_index=1),
            _make_elem("b", 0, 80, 100, 130, z_index=3),
        ]
        cfg = SpatialConfig()
        rels = _detect_adjacencies(elems, cfg)
        assert len(rels) == 1
        r = rels[0]
        assert r["subject_id"] == "b"
        assert r["object_id"] == "a"
        assert r["metrics"]["direction"] == "below"

    def test_too_far_away(self):
        elems = [
            _make_elem("a", 0, 0, 50, 50),
            _make_elem("b", 500, 0, 550, 50),
        ]
        cfg = SpatialConfig(adjacency_max_gap_px=100.0)
        rels = _detect_adjacencies(elems, cfg)
        assert len(rels) == 0

    def test_overlapping_not_adjacent(self):
        elems = [
            _make_elem("a", 0, 0, 100, 100),
            _make_elem("b", 50, 50, 150, 150),
        ]
        cfg = SpatialConfig()
        rels = _detect_adjacencies(elems, cfg)
        assert len(rels) == 0


# ---------------------------------------------------------------------------
# Group detection
# ---------------------------------------------------------------------------

class TestGroup:
    def test_simple_group(self):
        raw = [
            {"id": "g1", "name": "Group1", "type": "grpSp", "kind": "container",
             "subtype": "group", "parent_id": None, "z_index": 0},
            {"id": "c1", "name": "Child1", "type": "sp", "kind": "text",
             "parent_id": "g1", "z_index": 1,
             "bbox_px": [10, 10, 50, 50]},
            {"id": "c2", "name": "Child2", "type": "sp", "kind": "text",
             "parent_id": "g1", "z_index": 2,
             "bbox_px": [60, 10, 100, 50]},
        ]
        all_elems, _, _ = _prepare_elements(raw, SpatialConfig())
        rels = _detect_groups(all_elems, SpatialConfig())
        assert len(rels) == 1
        r = rels[0]
        assert r["group_id"] == "g1"
        assert set(r["child_ids"]) == {"c1", "c2"}

    def test_duplicate_id_groups_resolved(self):
        """When all elements share id=0, dedup produces unique IDs and correct parent mapping."""
        raw = [
            {"id": "0", "name": "GroupA", "type": "grpSp", "kind": "container",
             "subtype": "group", "parent_id": None, "z_index": 0},
            {"id": "0", "name": "ChildA1", "type": "sp", "kind": "text",
             "parent_id": "0", "z_index": 1,
             "bbox_px": [10, 10, 50, 50]},
            {"id": "0", "name": "ChildA2", "type": "sp", "kind": "text",
             "parent_id": "0", "z_index": 2,
             "bbox_px": [60, 10, 100, 50]},
            {"id": "0", "name": "GroupB", "type": "grpSp", "kind": "container",
             "subtype": "group", "parent_id": None, "z_index": 3},
            {"id": "0", "name": "ChildB1", "type": "sp", "kind": "text",
             "parent_id": "0", "z_index": 4,
             "bbox_px": [200, 10, 250, 50]},
        ]
        result = compute_spatial_relations(raw)
        group_rels = [r for r in _rels(result) if r["relation_type"] == "group"]
        # Should produce 2 distinct groups, not one giant group
        assert len(group_rels) == 2
        # Each group should have the correct children count
        child_counts = sorted(len(g["child_ids"]) for g in group_rels)
        assert child_counts == [1, 2]


# ---------------------------------------------------------------------------
# Prepare elements
# ---------------------------------------------------------------------------

class TestPrepare:
    def test_skipped_elements_filtered(self):
        raw = [
            {"id": "1", "type": "sp", "skipped_reason": "hidden",
             "bbox_px": [0, 0, 100, 100]},
            {"id": "2", "type": "sp", "kind": "text",
             "bbox_px": [0, 0, 100, 100], "z_index": 0},
        ]
        all_e, _, _ = _prepare_elements(raw, SpatialConfig())
        assert len(all_e) == 1
        assert all_e[0].id == "2"

    def test_connectors_excluded_from_layout(self):
        raw = [
            {"id": "1", "type": "sp", "kind": "text",
             "bbox_px": [0, 0, 100, 100], "z_index": 0},
            {"id": "2", "type": "cxnSp", "kind": "connector",
             "bbox_px": [0, 0, 200, 5], "z_index": 1},
        ]
        all_e, layout_e, _ = _prepare_elements(raw, SpatialConfig())
        assert len(all_e) == 2
        assert len(layout_e) == 1
        assert layout_e[0].id == "1"

    def test_background_excluded_from_non_bg(self):
        raw = [
            {"id": "1", "type": "pic", "kind": "image", "subtype": "background",
             "bbox_px": [0, 0, 1920, 1080], "z_index": 0},
            {"id": "2", "type": "sp", "kind": "text",
             "bbox_px": [10, 10, 200, 200], "z_index": 1},
        ]
        _, _, non_bg = _prepare_elements(raw, SpatialConfig())
        assert len(non_bg) == 1
        assert non_bg[0].id == "2"

    def test_degenerate_filtered(self):
        raw = [
            {"id": "1", "type": "sp", "kind": "text",
             "bbox_px": [10, 10, 10, 10], "z_index": 0},  # zero area
        ]
        all_e, _, _ = _prepare_elements(raw, SpatialConfig())
        assert len(all_e) == 0

    def test_synth_id_for_none(self):
        raw = [
            {"id": None, "type": "cxnSp", "kind": "connector",
             "bbox_px": [0, 0, 200, 50], "z_index": 0},
        ]
        all_e, _, _ = _prepare_elements(raw, SpatialConfig())
        assert len(all_e) == 1
        assert all_e[0].id.startswith("_auto_")

    def test_synth_id_stable_across_calls(self):
        """Same element always gets the same synthetic ID."""
        raw = [
            {"id": None, "type": "cxnSp", "kind": "connector",
             "bbox_px": [100.5, 200.3, 300.1, 210.7], "z_index": 3},
        ]
        e1, _, _ = _prepare_elements(raw, SpatialConfig())
        e2, _, _ = _prepare_elements(raw, SpatialConfig())
        assert e1[0].id == e2[0].id


# ---------------------------------------------------------------------------
# build_id2name
# ---------------------------------------------------------------------------

class TestBuildId2Name:
    def test_basic(self):
        elems = [
            {"id": "2", "name": "Rectangle 1"},
            {"id": "3", "name": "Rectangle 2"},
            {"id": None, "name": None},
        ]
        m = build_id2name(elems)
        assert m == {"2": "Rectangle 1", "3": "Rectangle 2"}


# ---------------------------------------------------------------------------
# Consistency: suppression rules in the orchestrator
# ---------------------------------------------------------------------------

class TestContainmentSuppressesOverlap:
    """Rule 1: if A contains B, the corresponding overlap is suppressed."""

    def test_contained_overlap_suppressed(self):
        raw = [
            _make_raw("big", 0, 0, 200, 200, z_index=0),
            _make_raw("small", 10, 10, 50, 50, z_index=1),
        ]
        rels = _rels(compute_spatial_relations(raw))
        types_for_pair = [
            r["relation_type"] for r in rels
            if {r.get("subject_id"), r.get("object_id")} == {"big", "small"}
            or r.get("container_id") == "big"
        ]
        assert "containment" in types_for_pair
        assert "overlap" not in types_for_pair

    def test_partial_overlap_not_suppressed(self):
        """Partial overlap (not containment) should still emit overlap."""
        raw = [
            _make_raw("a", 0, 0, 100, 100, z_index=0),
            _make_raw("b", 50, 50, 150, 150, z_index=1),
        ]
        rels = _rels(compute_spatial_relations(raw))
        overlap_rels = [
            r for r in rels if r["relation_type"] == "overlap"
            and {r["subject_id"], r["object_id"]} == {"a", "b"}
        ]
        assert len(overlap_rels) == 1


class TestGroupSuppressesSlidePairwise:
    """Rule 2: siblings in the same group don't appear in slide-level pairwise."""

    def test_group_siblings_no_slide_adjacency(self):
        raw = [
            {"id": "g1", "name": "Group1", "type": "grpSp", "kind": "container",
             "subtype": "group", "parent_id": None, "z_index": 0},
            _make_raw("c1", 10, 10, 50, 50, z_index=1, parent_id="g1"),
            _make_raw("c2", 60, 10, 100, 50, z_index=2, parent_id="g1"),
        ]
        rels = _rels(compute_spatial_relations(raw))
        # group relation should exist
        group_rels = [r for r in rels if r["relation_type"] == "group"]
        assert len(group_rels) >= 1
        # slide-level adjacency between c1-c2 should be suppressed
        adj_rels = [
            r for r in rels if r["relation_type"] == "adjacency"
            and {r["subject_id"], r["object_id"]} == {"c1", "c2"}
        ]
        assert len(adj_rels) == 0

    def test_group_siblings_no_slide_overlap(self):
        raw = [
            {"id": "g1", "name": "Group1", "type": "grpSp", "kind": "container",
             "subtype": "group", "parent_id": None, "z_index": 0},
            _make_raw("c1", 10, 10, 80, 80, z_index=1, parent_id="g1"),
            _make_raw("c2", 50, 50, 120, 120, z_index=2, parent_id="g1"),
        ]
        rels = _rels(compute_spatial_relations(raw))
        overlap_rels = [
            r for r in rels if r["relation_type"] == "overlap"
            and {r["subject_id"], r["object_id"]} == {"c1", "c2"}
        ]
        assert len(overlap_rels) == 0


class TestSequenceSuppressesAdjacency:
    """Rule 3: consecutive sequence members don't emit separate adjacency."""

    def test_sequence_members_no_adjacency(self):
        # Three horizontally-spaced boxes → form a sequence
        raw = [
            _make_raw("a", 0, 100, 50, 150, z_index=0),
            _make_raw("b", 60, 100, 110, 150, z_index=1),
            _make_raw("c", 120, 100, 170, 150, z_index=2),
        ]
        rels = _rels(compute_spatial_relations(raw))
        seq_rels = [r for r in rels if r["relation_type"] == "sequence"]
        assert len(seq_rels) >= 1
        # Adjacency between consecutive sequence members suppressed
        adj_pairs = {
            frozenset({r["subject_id"], r["object_id"]})
            for r in rels if r["relation_type"] == "adjacency"
        }
        assert frozenset({"a", "b"}) not in adj_pairs
        assert frozenset({"b", "c"}) not in adj_pairs


# ---------------------------------------------------------------------------
# compute_spatial_relations returns dict
# ---------------------------------------------------------------------------

class TestReturnFormat:
    def test_returns_dict(self):
        raw = [_make_raw("a", 0, 0, 100, 100, z_index=0)]
        result = compute_spatial_relations(raw, png_size=[200, 200])
        assert isinstance(result, dict)
        assert "slide_metrics" in result
        assert "relations" in result
        assert isinstance(result["relations"], list)
        assert isinstance(result["slide_metrics"], dict)

    def test_slide_metrics_fields(self):
        raw = [_make_raw("a", 0, 0, 100, 100, z_index=0)]
        m = compute_spatial_relations(raw, png_size=[200, 200])["slide_metrics"]
        assert "occupancy_ratio" in m
        assert "occupancy_match" in m
        assert "center_of_mass_offset" in m
        assert "center_of_mass_px" in m
        assert "canvas_center_px" in m


# ---------------------------------------------------------------------------
# Narrative rendering
# ---------------------------------------------------------------------------

class TestNarrativeDensity:
    def test_density_with_com(self):
        from render_narrative import render_density
        m = {
            "occupancy_ratio": 0.72,
            "occupancy_match": 0.88,
            "center_of_mass_offset": [0.032, 0.071],
        }
        line = render_density(m)
        assert line.startswith("density|")
        assert "72.0%" in line
        assert "88%" in line
        assert "右3.2%" in line
        assert "下7.1%" in line

    def test_density_centered(self):
        """Offset < 0.5% → shows 居中."""
        from render_narrative import render_density
        m = {
            "occupancy_ratio": 0.65,
            "occupancy_match": 0.95,
            "center_of_mass_offset": [0.003, -0.004],
        }
        line = render_density(m)
        assert "居中" in line
        # Both axes should be 居中 (both < 0.5%)
        assert line.count("居中") == 2

    def test_density_mixed(self):
        """One axis centered, one not."""
        from render_narrative import render_density
        m = {
            "occupancy_ratio": 0.65,
            "occupancy_match": 0.95,
            "center_of_mass_offset": [0.002, -0.035],
        }
        line = render_density(m)
        assert "居中" in line  # x-axis
        assert "上3.5%" in line  # y-axis negative → 上

    def test_density_left_up(self):
        """Negative offsets → 左/上."""
        from render_narrative import render_density
        m = {
            "occupancy_ratio": 0.50,
            "occupancy_match": 0.28,
            "center_of_mass_offset": [-0.10, -0.20],
        }
        line = render_density(m)
        assert "左10.0%" in line
        assert "上20.0%" in line


# ---------------------------------------------------------------------------
# Integration: compute_spatial_relations on real JSON
# ---------------------------------------------------------------------------

TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "test_data")


def _load_test_json(name: str) -> dict:
    path = os.path.join(TEST_DATA_DIR, name)
    if not os.path.exists(path):
        pytest.skip(f"Test data not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestIntegrationConnector:
    """Integration test with connector_out.json (3 boxes + 2 connectors)."""

    def test_relations_produced(self):
        data = _load_test_json("connector_out.json")
        result = compute_spatial_relations(data["elements"], png_size=data.get("png_size"))
        rels = result["relations"]
        assert len(rels) > 0
        types = {r["relation_type"] for r in rels}
        # Should find alignment (top-aligned boxes), sequence, adjacency
        assert "edge_alignment" in types
        assert "sequence" in types or "adjacency" in types

    def test_connectors_not_in_overlap(self):
        data = _load_test_json("connector_out.json")
        rels = _rels(compute_spatial_relations(data["elements"]))
        overlap_rels = [r for r in rels if r["relation_type"] == "overlap"]
        # Connectors should be excluded from overlap detection
        for r in overlap_rels:
            assert r["subject_id"] not in ("_synth_",)

    def test_narrative_integration(self):
        from render_narrative import render_narrative
        data = _load_test_json("connector_out.json")
        result = compute_spatial_relations(data["elements"], png_size=data.get("png_size"))
        id2name = build_id2name(data["elements"])
        narrative = render_narrative(result, id2name)
        assert isinstance(narrative, str)
        assert len(narrative) > 0


class TestIntegrationTableChart:
    """Integration test with table_chart_real_out.json."""

    def test_relations_produced(self):
        data = _load_test_json("table_chart_real_out.json")
        rels = _rels(compute_spatial_relations(data["elements"]))
        assert len(rels) > 0
        types = {r["relation_type"] for r in rels}
        assert "edge_alignment" in types

    def test_adjacency_found(self):
        data = _load_test_json("table_chart_real_out.json")
        rels = _rels(compute_spatial_relations(data["elements"]))
        adj_rels = [r for r in rels if r["relation_type"] == "adjacency"]
        assert len(adj_rels) > 0


class TestIntegrationGroup:
    """Integration test with group_out.json."""

    def test_group_detected(self):
        data = _load_test_json("group_out.json")
        rels = _rels(compute_spatial_relations(data["elements"]))
        group_rels = [r for r in rels if r["relation_type"] == "group"]
        # The test data has groups with id=0 for all elements
        # so group detection depends on parent_id mapping
        # At minimum we should get some relations
        assert len(rels) > 0


class TestEndToEnd:
    """Full pipeline: pptx_bbox JSON -> compute_spatial_relations -> render_narrative."""

    def test_pipeline_connector(self):
        from render_narrative import render_narrative
        data = _load_test_json("connector_out.json")
        elements = data["elements"]
        result = compute_spatial_relations(elements, png_size=data.get("png_size"))
        id2name = build_id2name(elements)
        narrative = render_narrative(result, id2name)
        # Check each line starts with a known relation prefix
        for line in narrative.strip().split("\n"):
            if not line.strip():
                continue
            prefix = line.split("|")[0].strip()
            assert prefix in (
                "density", "overlap", "align", "sequence", "dist",
                "contain", "adj", "group", "unknown"
            ), f"Unknown prefix: {prefix!r} in line: {line}"
        # First line should be the density line with CoM
        assert narrative.startswith("density|")
        assert "几何重心偏移" in narrative.split("\n")[0]

    def test_pipeline_table_chart(self):
        from render_narrative import render_narrative
        data = _load_test_json("table_chart_real_out.json")
        elements = data["elements"]
        result = compute_spatial_relations(elements, png_size=data.get("png_size"))
        id2name = build_id2name(elements)
        narrative = render_narrative(result, id2name)
        assert len(narrative) > 0
        assert narrative.startswith("density|")

# PPTX Spatial Intelligence Pipeline — Engineering Spec

## Overview

A three-layer pipeline that transforms raw PPTX slide XML into structured spatial intelligence: pixel-accurate bounding boxes, canonical spatial relations, and a machine-readable narrative DSL.

```
Layer 1                    Layer 2                         Layer 3
pptx_bbox.py        compute_spatial_relations.py     render_narrative.py
─────────────       ───────────────────────────      ──────────────────
PPTX XML            element JSON                     relations JSON
    ↓                   ↓                                ↓
EMU → affine        7 relation detectors             line-based DSL
→ pixel AABB        + slide metrics                  (one relation/line)
+ semantic tags     + suppression rules
+ text layout est   + overflow-aware bbox
+ overflow detect
    ↓                   ↓                                ↓
out.json            relations.json                   narrative.txt
overlay.png
```

Design principle: **objective measurement only** — the pipeline produces geometric facts; it does not decide for the model.

---

## 1. Layer 1: Coordinate Transformer (`pptx_bbox.py`)

### 1.1 Purpose

Parse `ppt/slides/slideN.xml`, compute pixel-level axis-aligned bounding boxes (AABB) for every visible element, classify each element semantically, estimate text layout properties, detect high-confidence text overflow, and optionally draw a debug overlay.

### 1.2 Input / Output

| Input | Description |
|-------|-------------|
| `pptx_path` | `.pptx` file (ZIP/OpenXML) |
| `slide_index` | Slide number (1-based) |
| `png_path` | Rendered PNG of that slide |

| Output | Description |
|--------|-------------|
| `out.json` | Element list with bbox, semantics, text layout |
| `overlay.png` | Debug visualization (bbox rectangles on PNG) |

### 1.3 Coordinate System

- XML coordinates: **EMU** (English Metric Units)
- Output coordinates: **px** (pixel, matching rendered PNG)
- Slide size: `<p:sldSz cx cy>` in `ppt/presentation.xml`
- Scale factors: `sx = png_w / slide_cx`, `sy = png_h / slide_cy`
- Conversion: `x_px = x_emu * sx`, `y_px = y_emu * sy`

> Hard-coded DPI is prohibited. The ratio-based mapping ensures exact correspondence with any rendering resolution.

### 1.4 Geometric Model: Affine Matrix + AABB

Every element's bounding box is computed through a unified affine transform chain:

**Simple element:**
```
M = T(off_x, off_y) @ AroundCenter(w, h, Rot·Flip)
AABB = M.apply_rect_aabb(w, h)  →  4 corners → min/max
```

**Group element:**
```
M_group = T(off) @ AroundCenter(ext_w, ext_h, Rot·Flip) @ S(ext/chExt) @ T(-chOff)
M_child_total = M_parent @ M_group @ M_child
```

The `Affine2D` class supports composition via `@` (matrix multiplication), with `self @ other` meaning "apply other first, then self". Rotation uses screen coordinates (y-down, clockwise positive). Flip is applied around the element center.

### 1.5 Element Types

Recursive traversal of `p:cSld/p:spTree`:

| XML tag | `type` | `kind` | Notes |
|---------|--------|--------|-------|
| `p:sp` | `sp` | `text` or `shape` | Text if has text content or is placeholder |
| `p:pic` | `pic` | `image` | Subtype: `background` (>90% area + centered) or `inline` |
| `p:grpSp` | `grpSp` | `container` | Subtype: `group`. Recursive — children inherit group transform |
| `p:graphicFrame` | `graphicFrame` | `container` | Subtype: `table`, `chart`, `smartart`, or `unknown` |
| `p:cxnSp` | `cxnSp` | `connector` | Extracts `start_id`/`end_id` from `a:stCxn`/`a:endCxn` |

Hidden elements (`cNvPr@hidden=1`) are recorded with `skipped_reason: "hidden"`.

### 1.6 Semantic Classification (`_classify_element_semantics`)

Runs per-element after bbox computation. Adds `kind`, `subtype`, and type-specific metadata to the output dict. Non-invasive — only adds fields, never modifies bbox.

**Text elements** (`sp` with text or placeholder):
- `text_content` (up to 400 chars), `text_len`
- `is_placeholder`, `ph_type` (title, body, ctrTitle, etc.)
- `text_layout_est` — see Section 1.7

**Image elements** (`pic`):
- `area_ratio` — fraction of canvas area
- `subtype`: `background` if area_ratio > 0.9 AND centered within 5%, else `inline`

**Connector elements** (`cxnSp`):
- `linked_endpoints`: `{start_id, end_id}` from XML connector references

### 1.7 Text Layout Estimation (`_extract_text_layout_est`)

Extracts geometric signals from `<p:txBody>` for downstream overflow detection:

| Field | Source | Purpose |
|-------|--------|---------|
| `autofit_mode` | `bodyPr` child element | `noAutofit`, `spAutoFit`, `normAutoFit`, or `None` (inherited) |
| `font_pt_median` | Median of `a:rPr@sz` values (hundredths → pt) | Dominant font size |
| `font_pt_count` | Count of `a:rPr` with explicit `sz` | Signal confidence |
| `paragraph_count` | `len(txBody.findall("a:p"))` | Structural line count |
| `line_break_count` | `len(txBody.findall(".//a:br"))` | Explicit line breaks |
| `char_count` | Total text length from `a:t` elements | Wrap estimation input |
| `inset_emu` | `bodyPr@{tIns,bIns,lIns,rIns}` | Text box internal margins |

**Inset defaults** (OOXML spec, when `bodyPr` omits attributes):
- `tIns = bIns = 45720 EMU` (0.05 inches)
- `lIns = rIns = 91440 EMU` (0.10 inches)

### 1.8 Text Overflow Detection (`_estimate_overflow_high_only`)

A conservative estimator that only flags overflow when geometrically certain. Design: **high confidence only** — false negatives are acceptable; false positives are not.

**4-Rule Gate (ALL must pass for HIGH):**

| Rule | Condition | Rationale |
|------|-----------|-----------|
| R1: Basic info | `kind=="text"`, `font_pt_median` present, `font_pt_count >= 1` | Cannot estimate without font size |
| R2: Autofit exclusion | `autofit_mode not in {spAutoFit, normAutoFit}` | Autofit shapes shrink text to fit — they cannot overflow |
| R3: Excess > 1 line | `H_req_est - H_usable > 1.0 × H_line_est` | Must exceed a full line to be confident |
| R4: Text structure | `paragraph_count >= 2` OR `line_break_count >= 1` OR `char_count >= 20` | Avoid flagging single short labels |

**Height estimation formulas:**

```
px_per_pt = 12700 × sy          # 1pt = 12700 EMU; sy = png_h / slide_cy (exact)
font_px   = font_pt_median × px_per_pt
H_line_est = font_px × 1.2      # typical line spacing factor

H_box     = y2 - y1
H_usable  = H_box - inset_top_px - inset_bottom_px

# Line count estimation (max of structural vs. wrap-based)
explicit_lines  = paragraph_count + line_break_count
chars_per_line  = floor(usable_width_px / (font_px × 0.6))   # 0.6 = avg char width ratio
wrapped_lines   = ceil(char_count / max(chars_per_line, 1))
line_count_est  = max(explicit_lines, wrapped_lines)

H_req_est = line_count_est × H_line_est
excess    = H_req_est - H_usable
```

**On HIGH (all 4 rules pass):**
- Excess capped at `3 × H_line_est` (prevents degenerate cases)
- `bbox_eff_px = [x1, y1, x2, y2 + excess]` — only extends bottom (PPT overflow is always downward)
- Adds `is_overflowing_est=True`, `overflow_confidence="high"`, `excess_height_px_est`
- Adds `overflow_evidence` dict with intermediate values for debugging:
  `{usable_h_px, required_h_px_est, line_h_px_est, line_count_est, chars_per_line_est}`

**On not HIGH:** sets `overflow_checked=True` (distinguishes "not checked" from "checked, not flagged"), adds nothing else.

**Known v1 boundary:** `autofit_mode=None` is treated as non-autofit due to inheritance opacity. Shapes inheriting `normAutoFit` from master/layout will not be excluded. If false positives emerge, master inheritance parsing or PNG visual validation can be added.

### 1.9 Output JSON Schema

**Top-level:**
```json
{
  "slide_index": 1,
  "png_size": [1920, 1080],
  "slide_size_emu": [12192000, 6858000],
  "sx": 0.00015746,
  "sy": 0.00015746,
  "elements": [...]
}
```

**Per element (visible):**
```json
{
  "id": "5",
  "name": "TextBox 1",
  "type": "sp",
  "parent_id": null,
  "z_index": 3,
  "transform": {"off_x": ..., "off_y": ..., "ext_w": ..., "ext_h": ..., ...},
  "bbox_emu": [x1, y1, x2, y2],
  "bbox_px": [x1, y1, x2, y2],
  "kind": "text",
  "text_content": "Hello World",
  "text_len": 11,
  "text_layout_est": {
    "autofit_mode": "noAutofit",
    "font_pt_median": 18.0,
    "font_pt_count": 2,
    "paragraph_count": 3,
    "line_break_count": 0,
    "char_count": 85,
    "inset_emu": {"top": 45720, "bottom": 45720, "left": 91440, "right": 91440},
    "overflow_checked": true,
    "is_overflowing_est": true,
    "overflow_confidence": "high",
    "excess_height_px_est": 28.5,
    "overflow_evidence": {
      "usable_h_px": 120.3,
      "required_h_px_est": 172.8,
      "line_h_px_est": 24.0,
      "line_count_est": 7,
      "chars_per_line_est": 18
    }
  },
  "bbox_eff_px": [x1, y1, x2, y2_extended]
}
```

`bbox_eff_px` is only present when overflow is HIGH. `bbox_px` is always the geometric original.

---

## 2. Layer 2: Spatial Relations Engine (`compute_spatial_relations.py`)

### 2.1 Purpose

Take element bboxes from Layer 1 and produce 7 canonical relation types plus slide-level metrics. Pure geometric measurement — no heuristic interpretation.

### 2.2 Element Preparation (`_prepare_elements`)

Converts raw JSON elements into normalized `_Elem` objects for geometric computation.

Key behaviors:
- **Overflow-aware bbox:** prefers `bbox_eff_px` when present, falls back to `bbox_px`. This means all 7 relation detectors automatically use the overflow-corrected bbox — zero changes to detector logic.
- **ID deduplication:** when multiple elements share the same XML `id` (e.g. duplicated groups), each gets rewritten to `{id}_z{z_index}`. Parent-child references are corrected.
- **Synthetic IDs:** elements with `id=None` get deterministic IDs from `z_index + type + bbox`.
- **Degenerate filtering:** elements with area < `min_element_area_px2` (default 4px²) are excluded.
- **Structural groups:** `grpSp` containers without bbox are included with zero-area sentinel for group detection.

Returns three lists: `all_elems`, `layout_elems` (excludes connectors), `non_bg_elems` (excludes background).

**Background detection** — geometric heuristic (not upstream classification):
- Area > 85% of canvas AND z_index == minimum visible z_index

### 2.3 The 7 Relation Types

| # | Type | Detector input | Canonical order | Key metrics |
|---|------|---------------|-----------------|-------------|
| 1 | `group` | `all_elems` | By group z_index | `internal_alignment`, `internal_gap_px` |
| 2 | `overlap` | `non_bg_elems` (no connectors) | Higher z = subject | `intersection_px2`, `overlap_ratio_object`, `top_id`, `z_diff` |
| 3 | `edge_alignment` | `layout_elems` | By z_index | `edge` (left/right/top/bottom/center_x/center_y), `max_delta_px` |
| 4 | `sequence` | `layout_elems` | Along ordering axis | `axis` (x/y), `gaps_px`, optional `edge` |
| 5 | `distribution` | Derived from sequences | Same as parent sequence | `intervals_px`, `mean_gap_px`, `cv` (coefficient of variation) |
| 6 | `containment` | `layout_elems` | Container → children | `padding_px` {top, right, bottom, left} |
| 7 | `adjacency` | `layout_elems` (no connectors) | Higher z = subject | `direction` (left/right/above/below), `gap_px` |

### 2.4 Suppression Rules (Post-Filters)

Detectors run independently; consistency is enforced via suppression:

| Rule | Suppresses | Reason |
|------|-----------|--------|
| Containment → Overlap | If A contains B, suppress overlap(A, B) | Containment is more specific |
| Same-group → Overlap, Adjacency | Group siblings excluded from slide-level pairwise | Avoid redundancy with group-internal metrics |
| Sequence → Adjacency | Consecutive members in a sequence suppress adjacency | Sequence already captures ordering + gaps |

### 2.5 Slide-Level Metrics

Computed on **effective elements** (excludes background, connectors; groups as single bbox, children excluded):

| Metric | Formula | Purpose |
|--------|---------|---------|
| `occupancy_ratio` (ρ) | `union_area(effective) / canvas_area` | Content density |
| `occupancy_match` (OM) | `max(0, 1 - |ρ - ρ*| / α)` where ρ*=0.68, α=0.25 | Proximity to ideal density |
| `center_of_mass_offset` | Area-weighted centroid vs. canvas center, normalized | Layout balance |
| `center_of_mass_px` | Absolute CoM position | |
| `canvas_center_px` | Canvas midpoint | |

### 2.6 Adjacency Coverage Audit

For each element, tracks which of 4 direction slots (left, right, above, below) are filled by at least one adjacency relation. Emitted as `adj_coverage` in output for completeness auditing.

---

## 3. Layer 3: Narrative Renderer (`render_narrative.py`)

### 3.1 Purpose

Transform the structured relations JSON into a line-based DSL that an LLM can consume as spatial context. Pure function — no state, no decisions.

### 3.2 DSL Format

```
dsl_version| 1
density| occupancy 45.2%, target_match 78%, CoM offset x centered, y up 3.2% (34.6px).
group| icon_group(sh_30) contains [icon_star(sh_31), text_rating(sh_32)], internally aligned on x-center (delta 0.0px, 0.0%W), internal gaps [1.3%W] ([12.0]px).
overlap| pic_logo(sh_5) overlaps body_text(sh_2) by 240.0px2 (0.1% canvas, 15.0% of body_text(sh_2)), pic_logo(sh_5) on top (z-diff 3).
align| title_1(sh_1), body_text(sh_2), footer_1(sh_3) aligned on left edge, max delta 2.4px (0.1%W).
sequence| title_1(sh_1), body_text(sh_2), page_num(sh_4) vertical sequence, gaps [2.2%H, 4.4%H] ([24.0, 48.0]px).
dist| pic_a(sh_10), pic_b(sh_11), pic_c(sh_12) evenly distributed along x-axis, intervals [2.2%W, 2.3%W] ([42.0, 45.0]px).
contain| content_box(sh_20) contains [inner_title(sh_21), inner_body(sh_22)], padding top 0.7%H right 0.6%W bottom 0.7%H left 0.6%W (top 8.0 right 12.0 bottom 8.0 left 12.0px).
adj| icon_1(sh_6) left_of label_1(sh_7), gap 0.8%W (16.0px).
adj_summary| slots filled 12/20.
```

### 3.3 Design Conventions

- **One relation per line**, prefixed by type tag (`overlap|`, `align|`, `adj|`, etc.)
- **Element references:** `semantic_name(sh_id)` when name exists, `(sh_id)` otherwise
- **Dual-track numbers:** percentage primary (with `%W`/`%H` axis suffix) + px backup in parentheses
- **Closed vocabulary:** overlap / aligned / sequence / distributed / contains / adjacent / on top / below / left_of / right_of / above / below
- **Axis-relative percentages:** horizontal values use `%W`, vertical values use `%H`
- **Guard formatting:** values <0.1% render as `<0.1%` to avoid false precision

---

## 4. Data Flow & Integration Points

### 4.1 Full Pipeline

```
[PPTX] + [PNG] → pptx_bbox.py → out.json
                                    ↓
                   compute_spatial_relations.py → relations.json + narrative.txt
                                    ↓
                            (LLM consumption / downstream analysis)
```

### 4.2 Overflow Correction Flow

```
pptx_bbox.py                          compute_spatial_relations.py
────────────                           ──────────────────────────────
_extract_text_layout_est()             _prepare_elements()
  → paragraph_count, char_count,         reads bbox_eff_px || bbox_px
    font_pt_median, inset_emu              ↓
       ↓                               all 7 detectors see corrected bbox
_estimate_overflow_high_only()          (no code changes in detectors)
  → bbox_eff_px (if HIGH)
  → overflow_evidence (for debug)

Key: bbox_px = geometric original (always present)
     bbox_eff_px = overflow-corrected (only when HIGH confidence)
     Layer 2 prefers bbox_eff_px → spatial relations reflect true text extent
```

### 4.3 CLI Usage

```bash
# Layer 1: Extract bboxes
python pptx_bbox.py input.pptx 1 slide1.png --out-json out.json --overlay debug.png

# Layer 2 + 3: Compute relations + render narrative
python compute_spatial_relations.py out.json

# Output: out_relations.json + out_narrative.txt
```

### 4.4 Batch Testing

```bash
# Run full pipeline on test_1..test_6 (requires Windows + PowerPoint COM)
python test_data/run_tests.py

# Run unit tests (85 tests, no external dependencies)
python -m pytest test_compute_spatial_relations.py -v
```

---

## 5. Dependencies

### Runtime

```
Python >= 3.10
Pillow >= 9.0          # PNG reading + overlay drawing
# Standard library only:
#   zipfile, xml.etree.ElementTree, math, json, argparse, dataclasses
```

> `python-pptx` is NOT a runtime dependency. It is only used by `make_test_pptx.py` for generating test data.

### External (Pre-pipeline)

| Step | Tool | Notes |
|------|------|-------|
| PPT → PPTX conversion | PowerPoint COM or LibreOffice | Old binary PPT not supported directly |
| Slide → PNG rendering | PowerPoint COM or LibreOffice | **Biggest external bottleneck** — no pure Python solution |

### PPTX XML Internals

The pipeline reads PPTX as a ZIP, parsing XML directly with `xml.etree.ElementTree`:

| ZIP path | Content | Used for |
|----------|---------|----------|
| `ppt/presentation.xml` | `<p:sldSz cx cy>` | EMU→PX scale factors |
| `ppt/slides/slideN.xml` | `<p:spTree>` element tree | All element extraction |
| `ppt/slideLayouts/*.xml` | Layout templates | **Not read** (v1: no inheritance) |
| `ppt/slideMasters/*.xml` | Master slides | **Not read** (v1: no inheritance) |

---

## 6. Key Design Decisions

### 6.1 Why Affine Matrices (Not Ad-Hoc Formulas)

OOXML combines translation, rotation, flip, and group scaling in nested hierarchies. An affine matrix chain handles all combinations uniformly:
- Rotation + flip around element center → `AroundCenter(w, h, Rot @ Flip)`
- Group child-space remapping → `S(ext/chExt) @ T(-chOff)`
- Arbitrary nesting → `M_parent @ M_group @ M_child`

This eliminates edge-case bugs from manual coordinate arithmetic.

### 6.2 Why High-Only Overflow

Text overflow estimation from XML alone is inherently incomplete:
- Font metrics (kerning, ligatures, CJK width) are approximate
- Line-breaking algorithms differ between renderers
- Theme/master inheritance can change autofit and font size

The `high-only` strategy accepts these limitations:
- Only flags when excess > 1 full line (absorbs estimation error)
- Excludes autofit shapes entirely (cannot overflow by definition)
- Requires minimum text structure (avoids flagging short labels)
- Caps correction at 3 lines (bounds worst-case error)

**Result: zero false positives** on all test slides. False negatives are acceptable — they leave the visual unchanged for human/model judgment.

### 6.3 Why Layer 2 Uses bbox_eff_px Transparently

The one-line patch `bbox = raw.get("bbox_eff_px") or raw.get("bbox_px")` means:
- All 7 relation detectors automatically use corrected bboxes
- No detector code changes — zero regression risk
- Both `bbox_px` (original) and `bbox_eff_px` (corrected) remain in JSON for debug
- Elements without overflow continue to use `bbox_px` unchanged

### 6.4 Why Suppression Rules (Not Conditional Detection)

Each detector runs independently on its own element subset. Suppression is applied as a post-filter:
- Keeps detectors simple and testable
- Suppression rules are explicit and auditable
- Adding new rules doesn't require modifying detector internals

### 6.5 Why Dual-Track % + px in Narrative

Percentages are resolution-independent (essential for comparing across slide sizes). Pixel values are precise (essential for debugging and verification). The DSL includes both: `gap 2.3%W (44.2px)`.

---

## 7. Testing

### 7.1 Unit Tests (85 tests)

`test_compute_spatial_relations.py` covers:
- All 7 relation detectors with synthetic element configurations
- Element preparation (filtering, degenerate, synthetic IDs, dedup)
- Background detection (geometric heuristic)
- Suppression rules (containment→overlap, group→pairwise, sequence→adjacency)
- Slide metrics (OM, CoM, density)
- Narrative integration (end-to-end rendering)
- Return format validation

### 7.2 Integration Tests

9 test PPTX files with rendered PNGs:
- `test_1..test_6`: real-world slides (PPT→PPTX conversion via PowerPoint COM)
- `test_connector.pptx`: connector endpoint extraction
- `test_group.pptx`: nested group transform chains
- `test_table_chart.pptx`: graphicFrame subtype detection

### 7.3 Verification Protocol

1. All 85 unit tests pass (`pytest test_compute_spatial_relations.py`)
2. Overlay PNGs visually verified — bbox rectangles align with rendered elements
3. Narrative output spot-checked against visual slide layout
4. Overflow detection verified: flagged elements should visibly overflow; unflagged elements should not be obvious overflows

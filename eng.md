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

## 3. Layer 3: Narrative DSL (`render_narrative.py`)

### 3.1 Purpose & Design Goal

Transform structured spatial relations JSON into a **line-based DSL** that an LLM can consume as spatial context in its prompt. The DSL is the pipeline's final output — the contract between geometric computation and language model understanding.

Core constraint: the DSL must be **parseable by both machines and LLMs**. This rules out free-form prose (ambiguous to parse) and raw JSON (wasteful in tokens, hard for LLMs to reason about spatially).

### 3.2 Why Line-Based DSL (Not JSON, Not Prose)

| Format | Tokens | Parseable | LLM-friendly | Chosen |
|--------|--------|-----------|--------------|--------|
| Raw JSON | High (braces, quotes, keys) | Exact | Poor — nested structure, hard to scan | No |
| Free prose | Medium | Ambiguous | Medium — natural but imprecise | No |
| Line-based DSL | Low | Regex-friendly | Good — one fact per line, scannable | **Yes** |

The line-based format achieves:
- **Token efficiency:** ~40-60% fewer tokens than equivalent JSON
- **Grepping:** each line is self-contained — LLM can ctrl+F for `adj|` to find all adjacency relations
- **Incremental reading:** LLM can stop reading when it has enough context
- **Deterministic parsing:** `tag| content` format is trivially regex-parseable for downstream tooling

### 3.3 DSL Grammar

#### 3.3.1 Line Structure

Every line follows the pattern:

```
TAG| BODY.
```

- `TAG` — lowercase relation type identifier, immediately followed by `|`
- `BODY` — relation content in semi-structured English, ending with `.`
- One relation per line, no multi-line spans
- Lines appear in canonical order: `dsl_version` → `density` → relations (by type) → `adj_summary`

#### 3.3.2 Tag Vocabulary (Closed Set)

| Tag | Relation | Arity | Pattern |
|-----|----------|-------|---------|
| `dsl_version` | Meta | — | `dsl_version\| 1` |
| `density` | Slide-level | — | `density\| occupancy ...%, target_match ...%, CoM offset x ..., y ....` |
| `group` | Structural | 1 → N | `group\| PARENT contains [CHILDREN], ...` |
| `overlap` | Pairwise | 2 | `overlap\| SUBJ overlaps OBJ by ...` |
| `align` | Set | N | `align\| MEMBERS aligned on EDGE edge, ...` |
| `sequence` | Ordered set | N | `sequence\| MEMBERS {horizontal\|vertical} sequence, gaps ...` |
| `dist` | Ordered set | N | `dist\| MEMBERS evenly distributed along AXIS-axis, ...` |
| `contain` | 1 → N | N | `contain\| CONTAINER contains [CHILDREN], padding ...` |
| `adj` | Pairwise | 2 | `adj\| SUBJ {left_of\|right_of\|above\|below} OBJ, gap ...` |
| `adj_summary` | Audit | — | `adj_summary\| slots filled N/M.` |

This vocabulary is **closed** — no new tags without a version bump. An LLM seeing `dsl_version| 1` knows exactly which tags to expect.

#### 3.3.3 Element Reference Format

```
semantic_name(sh_ID)     # when name exists in PowerPoint
(sh_ID)                  # when name is absent
```

Examples:
- `Title 1(sh_2)` — named shape with XML id "2"
- `(sh_15)` — unnamed shape
- `InnerRect1(0_z9)` — shape with deduplicated ID (original id "0", z_index 9)

Design rationale:
- **Name first:** LLMs understand `Title 1` better than `sh_2`. The name provides semantic context.
- **ID in parentheses:** always present for unambiguous cross-referencing back to JSON.
- **`sh_` prefix:** distinguishes shape IDs from numbers in metrics. Prevents LLM confusion between "element 2" and "2 pixels".

#### 3.3.4 Number Format: Dual-Track `%` + `px`

Every measurement appears twice — percentage primary, pixel backup:

```
gap 2.3%W (44.2px)
padding top 0.7%H right 0.6%W bottom 0.7%H left 0.6%W (top 8.0 right 12.0 bottom 8.0 left 12.0px)
intervals [2.2%W, 2.3%W] ([42.0, 45.0]px)
```

**Why percentage as primary:**
- Resolution-independent — `2.3%W` means the same thing on a 1920px slide and a 3840px slide
- Intuitive for layout reasoning — "5% of slide width" is immediately meaningful
- Comparable across slides of different sizes

**Why pixel as backup:**
- Exact for debugging and verification
- Needed when comparing against absolute thresholds
- Ground truth — percentage is derived from px

**Axis suffixes (`%W` / `%H`):**
- Horizontal measurements (left, right, center_x, gap in x-direction): `%W` (percentage of canvas width)
- Vertical measurements (top, bottom, center_y, gap in y-direction): `%H` (percentage of canvas height)
- This prevents ambiguity: `5%W` on a 16:9 slide is not the same physical distance as `5%H`

**Guard formatting:**
- `0.0%` — exactly zero
- `<0.1%` — positive but below display threshold (avoids misleading `0.0%` for non-zero values)
- `12.3%` — normal display (1 decimal place)

### 3.4 Per-Tag Line Templates

#### `density|` — Slide-Level Metrics

```
density| occupancy {ρ}%, target_match {OM}%, CoM offset x {x_desc}, y {y_desc}.
```

Where `{x_desc}` / `{y_desc}` is one of:
- `centered` — offset < 0.5%
- `right 8.2% (157.4px)` / `left ...` — horizontal shift
- `down 3.1% (33.5px)` / `up ...` — vertical shift

This line tells the LLM: "the slide is X% full, Y% close to ideal density, and the visual weight is shifted in Z direction."

#### `group|` — Structural Group

```
group| PARENT contains [CHILD1, CHILD2, ...].
group| PARENT contains [CHILD1, CHILD2], internally aligned on EDGE (delta Dpx, D%), internal gaps [G1%, G2%] ([G1, G2]px).
```

Optional metrics appear only when detected:
- `internally aligned on {left|right|top|bottom|x-center|y-center}` — children share an edge
- `internal gaps [...]` — spacing between children along sequence axis

#### `overlap|` — Area Overlap

```
overlap| SUBJ overlaps OBJ by Apx2 (C% canvas, P% of OBJ), SUBJ on top (z-diff Z).
```

- `Apx2` — intersection area in square pixels
- `C% canvas` — intersection as percentage of total slide area
- `P% of OBJ` — how much of the object is covered (occlusion severity)
- `z-diff Z` — layer distance (higher z = visually on top)

#### `align|` — Edge Alignment

```
align| M1, M2, M3 aligned on {left|right|top|bottom|x-center|y-center} edge, max delta Dpx (D%).
```

- `max delta` — worst-case deviation within the alignment cluster
- Edge types: `left edge`, `right edge`, `top edge`, `bottom edge`, `x-center` (no "edge" suffix), `y-center`

#### `sequence|` — Ordered Spatial Sequence

```
sequence| M1, M2, M3 {horizontal|vertical} sequence, gaps [G1%, G2%] ([G1, G2]px).
sequence| M1, M2, M3 vertical sequence, gaps [G1%H] ([G1]px), aligned on left (delta Dpx, D%W).
```

- Members listed in spatial order (left-to-right or top-to-bottom)
- Optional `, aligned on EDGE (delta ...)` — when the sequence also forms an alignment

#### `dist|` — Even Distribution

```
dist| M1, M2, M3 evenly distributed along {x|y}-axis, intervals [I1%, I2%] ([I1, I2]px) (range R%, Rpx).
```

- Only emitted when gap coefficient of variation (CV) ≤ 0.15 — genuinely even spacing
- `range` — max gap minus min gap (how "even" the distribution really is)

#### `contain|` — Geometric Containment

```
contain| CONTAINER contains [CHILD1, CHILD2], padding top T%H right R%W bottom B%H left L%W (top T right R bottom B left Lpx).
```

- Padding in CSS order: top, right, bottom, left
- Each padding value uses its axis-appropriate suffix

#### `adj|` — Adjacency

```
adj| SUBJ {left_of|right_of|above|below} OBJ, gap G% (Gpx).
```

- Direction describes subject relative to object: `A left_of B` means A is to the left of B
- Canonical order: higher z_index = subject (consistent with overlap convention)

#### `adj_summary|` — Coverage Audit

```
adj_summary| slots filled F/T.
```

- `T` = total direction slots (elements × 4 directions)
- `F` = slots with at least one adjacency relation
- Low coverage signals sparse layout or elements too far apart for adjacency detection

### 3.5 Output Order

Lines appear in this fixed order:

```
1. dsl_version| 1
2. density| ...                    (slide-level metrics)
3. group| ...                      (structural — from XML parent_id)
4. overlap| ...                    (pairwise — higher z = subject)
5. align| ...                      (set — per edge type)
6. sequence| ...                   (ordered set — per axis)
7. dist| ...                       (derived from sequences)
8. contain| ...                    (1→N — smallest container first)
9. adj| ...                        (pairwise — higher z = subject)
10. adj_summary| ...               (audit)
```

This order is intentional:
- **Structural relations first** (group) — establishes hierarchy before spatial details
- **Overlap before adjacency** — overlapping elements are more visually salient
- **Sequence before distribution** — distribution is a refinement of sequence
- **Adjacency last** — most numerous, lowest information density per line

### 3.6 Design Principles

1. **Pure function, no state** — `render_narrative(data, id2name) → str`. No side effects, no configuration, no memory.

2. **No decisions** — the renderer never filters, ranks, or omits relations. Every relation from Layer 2 produces exactly one line. Importance/relevance judgment is left to the consuming LLM.

3. **Closed vocabulary** — the set of verbs is fixed: `overlaps`, `aligned on`, `sequence`, `evenly distributed`, `contains`, `left_of`/`right_of`/`above`/`below`, `on top`. An LLM can learn this vocabulary once and apply it to any slide.

4. **Self-describing** — each line contains all information needed to understand the relation. No cross-line references required (element names are repeated, not "see line 5").

5. **Deterministic** — same input always produces same output. No randomness, no sampling, no heuristic ordering within a type. This enables diff-based regression testing.

### 3.7 Real Output Example

From `test_connector.pptx` — three horizontally-spaced rectangles:

```
dsl_version| 1
density| occupancy 10.8%, target_match 0%, CoM offset x left 0.8% (15.1px), y up 2.6% (28.2px).
align| Rectangle 1(sh_2), Rectangle 2(sh_3), Rectangle 3(sh_4) aligned on top edge, max delta 0.0px (0.0%H).
align| Rectangle 1(sh_2), Rectangle 2(sh_3), Rectangle 3(sh_4) aligned on bottom edge, max delta 0.0px (0.0%H).
align| Rectangle 1(sh_2), Rectangle 2(sh_3), Rectangle 3(sh_4) aligned on y-center, max delta 0.0px (0.0%H).
sequence| Rectangle 1(sh_2), Rectangle 2(sh_3), Rectangle 3(sh_4) horizontal sequence, gaps [16.4%W, 16.4%W] ([315.0, 315.0]px), aligned on top (delta 0.0px, 0.0%H).
dist| Rectangle 1(sh_2), Rectangle 2(sh_3), Rectangle 3(sh_4) evenly distributed along x-axis, intervals [16.4%W, 16.4%W] ([315.0, 315.0]px) (range 0.0%W, 0.0px).
adj_summary| slots filled 0/12.
```

What an LLM can read from this:
- Three rectangles, perfectly aligned top/bottom/center — they form a row
- Evenly spaced horizontally at 16.4%W each — uniform grid
- Low occupancy (10.8%) — mostly empty slide
- CoM slightly left and up — the row is above center
- No adjacency filled (0/12) — rectangles are spaced far apart (gaps > adjacency threshold)

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

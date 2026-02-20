"""analyze_layout.py — Spatial layout skill for agentic LLM environments.

Analyze a PowerPoint slide's spatial structure. Outputs a line-based
narrative DSL to stdout describing element positions, alignments,
overlaps, sequences, containment, and adjacency relationships.

Usage:
    python analyze_layout.py <pptx> <slide_index> <png> [--overlay out.png] [--save-json out.json]

Example:
    python analyze_layout.py deck.pptx 1 slide1.png
"""
import argparse
import json
import sys

from PIL import Image

from pptx_bbox import compute_bboxes_for_slide, draw_overlay
from compute_spatial_relations import compute_spatial_relations, build_id2name
from render_narrative import render_narrative


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyze spatial layout of a PPTX slide. Prints narrative DSL to stdout."
    )
    parser.add_argument("pptx", help="Path to .pptx file")
    parser.add_argument("slide", type=int, help="Slide number (1-based)")
    parser.add_argument("png", help="Rendered PNG of the slide")
    parser.add_argument("--overlay", default=None, help="Save debug overlay PNG")
    parser.add_argument("--save-json", default=None, help="Save full pipeline JSON")
    args = parser.parse_args(argv)

    try:
        # Layer 1: extract bboxes
        png_w, png_h = Image.open(args.png).size
        bbox_data = compute_bboxes_for_slide(args.pptx, args.slide, png_w, png_h)
        elements = bbox_data["elements"]
        png_size = bbox_data["png_size"]

        # Layer 2: compute relations
        relations = compute_spatial_relations(elements, png_size=png_size)

        # Layer 3: render narrative
        id2name = build_id2name(elements)
        narrative = render_narrative(relations, id2name)

        # stdout: narrative DSL (what the agent reads)
        print(narrative)

        # stderr: one-line summary for logging
        n_elem = len([e for e in elements if "bbox_px" in e])
        n_rel = len(relations.get("relations", []))
        print(f"slide {args.slide}: {n_elem} elements, {n_rel} relations", file=sys.stderr)

        # optional outputs
        if args.overlay:
            draw_overlay(args.png, elements, args.overlay)

        if args.save_json:
            out = {
                "slide_index": bbox_data["slide_index"],
                "png_size": png_size,
                "elements": elements,
                "slide_metrics": relations.get("slide_metrics"),
                "relations": relations.get("relations"),
                "adj_coverage": relations.get("adj_coverage"),
                "narrative": narrative,
            }
            with open(args.save_json, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)

    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

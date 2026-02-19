"""Batch test: convert PPT -> PPTX, export PNGs, run pptx_bbox on test_1..test_6.

Steps for each test file:
  1. Open old PPT in PowerPoint, copy slides to a new pres, save as PPTX
  2. Export each slide as PNG via PowerPoint
  3. Run compute_bboxes_for_slide() to extract element bounding boxes
  4. Generate JSON + debug overlay
  5. Print summary
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from pptx_bbox import compute_bboxes_for_slide, draw_overlay

TEST_DIR = Path(__file__).resolve().parent


def convert_ppt_to_pptx_and_pngs(
    ppt_path: Path, out_dir: Path, stem: str
) -> tuple[Path, list[Path]]:
    """Open old-format PPT, copy slides to new PPTX, export PNGs.

    Returns (pptx_path, [png_paths]).
    """
    import win32com.client
    import pythoncom

    out_dir.mkdir(exist_ok=True)
    abs_path = str(ppt_path.resolve())

    pythoncom.CoInitialize()
    try:
        ppt = win32com.client.Dispatch("PowerPoint.Application")
        ppt.DisplayAlerts = 0

        # Open the old PPT
        pres = ppt.Presentations.Open(abs_path, WithWindow=False)

        # Create new blank presentation and copy slides
        new_pres = ppt.Presentations.Add(WithWindow=False)
        for i in range(1, pres.Slides.Count + 1):
            pres.Slides(i).Copy()
            new_pres.Slides.Paste()

        # Match slide size
        new_pres.PageSetup.SlideWidth = pres.PageSetup.SlideWidth
        new_pres.PageSetup.SlideHeight = pres.PageSetup.SlideHeight

        slide_count = new_pres.Slides.Count

        # Save as PPTX
        pptx_path = out_dir / f"{stem}.pptx"
        new_pres.SaveAs(str(pptx_path.resolve()), 24)

        # Export each slide as PNG
        png_dir = out_dir / "pngs"
        png_dir.mkdir(exist_ok=True)
        png_paths = []
        for i in range(1, slide_count + 1):
            png_file = png_dir / f"slide_{i}.png"
            new_pres.Slides(i).Export(str(png_file.resolve()), "PNG", 1920, 1080)
            png_paths.append(png_file)

        new_pres.Close()
        pres.Close()
        ppt.Quit()

        return pptx_path, png_paths
    finally:
        pythoncom.CoUninitialize()


def run_test(ppt_path: Path) -> bool:
    """Run full test on one PPT file. Returns True on success."""
    stem = ppt_path.stem.replace(" ", "_").replace("(", "").replace(")", "")
    out_dir = TEST_DIR / f"{stem}_output"

    print(f"\n{'='*60}")
    print(f"  Testing: {ppt_path.name}")
    print(f"{'='*60}")

    # Step 1: Convert and export
    print("  Converting PPT -> PPTX and exporting PNGs...")
    pptx_path, png_paths = convert_ppt_to_pptx_and_pngs(ppt_path, out_dir, stem)
    print(f"  Converted: {pptx_path.name} ({pptx_path.stat().st_size} bytes)")
    print(f"  Exported {len(png_paths)} slide PNG(s)")

    if not png_paths:
        print("  [WARN] No slides!")
        return False

    # Step 2: Run bbox extraction
    all_ok = True
    for slide_idx, png_p in enumerate(png_paths, start=1):
        try:
            with Image.open(str(png_p)) as im:
                png_w, png_h = im.size

            print(f"\n  --- Slide {slide_idx} (PNG: {png_w}x{png_h}) ---")

            result = compute_bboxes_for_slide(
                str(pptx_path), slide_idx, png_w, png_h
            )

            # Save JSON
            json_path = out_dir / f"{stem}_slide{slide_idx}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            # Draw overlay
            overlay_path = out_dir / f"{stem}_slide{slide_idx}_overlay.png"
            draw_overlay(str(png_p), result["elements"], str(overlay_path))

            # Summary
            elements = result["elements"]
            visible = [e for e in elements if "bbox_px" in e]
            skipped = [e for e in elements if "skipped_reason" in e]

            print(f"  Total elements: {len(elements)}")
            print(f"  Visible (with bbox): {len(visible)}")
            if skipped:
                print(f"  Skipped: {len(skipped)}")
                for s in skipped:
                    print(
                        f"    - {s.get('name', '?')} ({s.get('type', '?')}): "
                        f"{s.get('skipped_reason')}"
                    )

            for e in visible:
                bbox = e["bbox_px"]
                name = e.get("name", "")
                if len(name) > 30:
                    name = name[:27] + "..."
                print(
                    f"  [{e.get('type', '?'):15s}] "
                    f"id={e.get('id', '?'):>3s}  "
                    f"name={name:30s}  "
                    f"bbox_px=({bbox[0]:7.1f}, {bbox[1]:7.1f}, "
                    f"{bbox[2]:7.1f}, {bbox[3]:7.1f})"
                )

            print(f"  [OK] {json_path.name}, {overlay_path.name}")

        except Exception as exc:
            print(f"  [FAIL] Slide {slide_idx}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            all_ok = False

    return all_ok


def main() -> None:
    test_files = []
    for i in range(1, 7):
        candidates = [
            TEST_DIR / f"test_{i}.pptx",
            TEST_DIR / f"test_{i} (1).pptx",
        ]
        for c in candidates:
            if c.exists():
                test_files.append(c)
                break
        else:
            print(f"[WARN] test_{i} not found")

    print(f"Found {len(test_files)} test files:")
    for tf in test_files:
        print(f"  - {tf.name}")

    passed = 0
    failed = 0
    for tf in test_files:
        try:
            ok = run_test(tf)
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as exc:
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed} passed, {failed} failed, {len(test_files)} total")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

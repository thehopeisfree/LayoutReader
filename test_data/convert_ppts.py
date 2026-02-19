"""Convert PPT files to PPTX and export slide PNGs using PowerPoint COM."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import win32com.client
import pythoncom

TEST_DIR = Path(__file__).resolve().parent


def convert_one(ppt_path: Path, out_dir: Path) -> tuple[Path | None, list[Path]]:
    """Convert a single PPT to PPTX and export slide PNGs.

    Returns (pptx_path, [png_paths]).
    """
    stem = ppt_path.stem.replace(" ", "_").replace("(", "").replace(")", "")
    abs_path = str(ppt_path.resolve())
    out_dir.mkdir(exist_ok=True)

    pythoncom.CoInitialize()
    try:
        ppt = win32com.client.Dispatch("PowerPoint.Application")
        ppt.DisplayAlerts = 0

        pres = ppt.Presentations.Open(abs_path, WithWindow=False)

        # Save as PPTX (ppSaveAsOpenXMLPresentation = 24)
        pptx_path = out_dir / f"{stem}.pptx"
        pres.SaveAs(str(pptx_path.resolve()), FileFormat=24)
        pres.Close()

        # Reopen the saved PPTX to export PNGs
        pres2 = ppt.Presentations.Open(str(pptx_path.resolve()), WithWindow=False)

        png_dir = out_dir / f"{stem}_pngs"
        png_dir.mkdir(exist_ok=True)

        # Export each slide individually
        png_paths = []
        for i, slide in enumerate(pres2.Slides, 1):
            png_file = png_dir / f"slide_{i}.png"
            slide.Export(str(png_file.resolve()), "PNG", 1920, 1080)
            png_paths.append(png_file)

        pres2.Close()
        ppt.Quit()

        return pptx_path, png_paths
    finally:
        pythoncom.CoUninitialize()


def main() -> None:
    for i in range(1, 7):
        candidates = [
            TEST_DIR / f"test_{i}.pptx",
            TEST_DIR / f"test_{i} (1).pptx",
        ]
        ppt_path = None
        for c in candidates:
            if c.exists():
                ppt_path = c
                break

        if ppt_path is None:
            print(f"[SKIP] test_{i} not found")
            continue

        print(f"Converting: {ppt_path.name}")
        out_dir = TEST_DIR / f"test_{i}_output"

        try:
            pptx_path, png_paths = convert_one(ppt_path, out_dir)
            if pptx_path:
                # Verify it's a valid ZIP
                import zipfile
                with zipfile.ZipFile(str(pptx_path), "r") as zf:
                    names = zf.namelist()
                print(f"  -> {pptx_path.name} (valid PPTX, {len(names)} entries)")
            for p in png_paths:
                print(f"  -> {p.name} ({p.stat().st_size} bytes)")
        except Exception as exc:
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()

    print("\nDone.")


if __name__ == "__main__":
    main()

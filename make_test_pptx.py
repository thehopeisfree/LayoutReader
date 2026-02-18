"""Generate a test PPTX with various element types and a matching reference PNG.

Elements included:
  1. Title textbox (no rotation)
  2. A colored rectangle (45-degree rotation)
  3. A second rectangle (no rotation, flipH)
  4. A group containing two shapes
  5. A picture placeholder (simulated with a shape)

Also renders a "ground-truth" PNG using Pillow so we can run pptx_bbox.py
without LibreOffice.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches, Emu, Pt
from pptx.enum.shapes import MSO_SHAPE
from pptx.dml.color import RGBColor

OUT_DIR = Path(__file__).parent / "test_data"
OUT_DIR.mkdir(exist_ok=True)

SLIDE_W_EMU = 12192000  # 10 inches (default widescreen)
SLIDE_H_EMU = 6858000   # 7.5 inches

PNG_W = 1920
PNG_H = 1080

SX = PNG_W / SLIDE_W_EMU
SY = PNG_H / SLIDE_H_EMU


def emu2px(x_emu: int, y_emu: int) -> tuple[float, float]:
    return x_emu * SX, y_emu * SY


def make_pptx() -> Path:
    prs = Presentation()
    prs.slide_width = Emu(SLIDE_W_EMU)
    prs.slide_height = Emu(SLIDE_H_EMU)

    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank layout

    # --- 1. Title textbox (no rotation) ---
    txBox = slide.shapes.add_textbox(Emu(500000), Emu(200000), Emu(5000000), Emu(600000))
    txBox.text_frame.text = "Test Slide Title"
    for para in txBox.text_frame.paragraphs:
        for run in para.runs:
            run.font.size = Pt(28)
            run.font.color.rgb = RGBColor(0, 0, 0)

    # --- 2. Rotated rectangle (45 deg) ---
    rect1 = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(2000000), Emu(2000000),
        Emu(3000000), Emu(1500000),
    )
    rect1.rotation = 45.0
    rect1.fill.solid()
    rect1.fill.fore_color.rgb = RGBColor(0x44, 0x88, 0xCC)

    # --- 3. FlipH rectangle ---
    rect2 = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(7000000), Emu(1000000),
        Emu(2500000), Emu(1200000),
    )
    rect2.fill.solid()
    rect2.fill.fore_color.rgb = RGBColor(0xCC, 0x44, 0x44)

    # --- 4. Another shape lower ---
    rect3 = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Emu(1000000), Emu(4500000),
        Emu(4000000), Emu(1800000),
    )
    rect3.fill.solid()
    rect3.fill.fore_color.rgb = RGBColor(0x44, 0xCC, 0x44)
    rect3.text_frame.text = "Green Box"

    # --- 5. Oval ---
    oval = slide.shapes.add_shape(
        MSO_SHAPE.OVAL,
        Emu(7500000), Emu(3500000),
        Emu(3000000), Emu(2500000),
    )
    oval.fill.solid()
    oval.fill.fore_color.rgb = RGBColor(0xFF, 0xAA, 0x00)

    # --- 6. Small rotated shape (90 deg) ---
    rect4 = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(6000000), Emu(5000000),
        Emu(1500000), Emu(800000),
    )
    rect4.rotation = 90.0
    rect4.fill.solid()
    rect4.fill.fore_color.rgb = RGBColor(0x88, 0x44, 0xCC)

    pptx_path = OUT_DIR / "test_slide.pptx"
    prs.save(str(pptx_path))
    print(f"Saved {pptx_path}")
    return pptx_path


def _rotate_box_corners(
    cx: float, cy: float, hw: float, hh: float, angle_deg: float,
) -> list[tuple[float, float]]:
    """Return 4 rotated corners of a box centered at (cx,cy)."""
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return [(cx + cos_a * dx - sin_a * dy, cy + sin_a * dx + cos_a * dy) for dx, dy in corners]


def make_reference_png() -> Path:
    """Draw a simple reference PNG that approximates the slide layout."""
    img = Image.new("RGB", (PNG_W, PNG_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default()

    # 1. Title textbox
    x1, y1 = emu2px(500000, 200000)
    x2, y2 = emu2px(500000 + 5000000, 200000 + 600000)
    draw.rectangle([x1, y1, x2, y2], outline=(180, 180, 180))
    draw.text((x1 + 5, y1 + 5), "Test Slide Title", fill=(0, 0, 0), font=font)

    # 2. Rotated rectangle (45 deg) — draw as polygon
    cx_emu, cy_emu = 2000000 + 3000000 / 2, 2000000 + 1500000 / 2
    cx_px, cy_px = emu2px(cx_emu, cy_emu)
    hw_px = 3000000 * SX / 2
    hh_px = 1500000 * SY / 2
    corners = _rotate_box_corners(cx_px, cy_px, hw_px, hh_px, 45)
    draw.polygon(corners, fill=(0x44, 0x88, 0xCC))

    # 3. Red rectangle
    x1, y1 = emu2px(7000000, 1000000)
    x2, y2 = emu2px(7000000 + 2500000, 1000000 + 1200000)
    draw.rectangle([x1, y1, x2, y2], fill=(0xCC, 0x44, 0x44))

    # 4. Green rounded rect (draw as normal rect)
    x1, y1 = emu2px(1000000, 4500000)
    x2, y2 = emu2px(1000000 + 4000000, 4500000 + 1800000)
    draw.rectangle([x1, y1, x2, y2], fill=(0x44, 0xCC, 0x44))
    draw.text((x1 + 10, y1 + 10), "Green Box", fill=(0, 0, 0), font=font)

    # 5. Oval
    x1, y1 = emu2px(7500000, 3500000)
    x2, y2 = emu2px(7500000 + 3000000, 3500000 + 2500000)
    draw.ellipse([x1, y1, x2, y2], fill=(0xFF, 0xAA, 0x00))

    # 6. Purple rect rotated 90 deg
    cx_emu2, cy_emu2 = 6000000 + 1500000 / 2, 5000000 + 800000 / 2
    cx_px2, cy_px2 = emu2px(cx_emu2, cy_emu2)
    hw2 = 1500000 * SX / 2
    hh2 = 800000 * SY / 2
    corners2 = _rotate_box_corners(cx_px2, cy_px2, hw2, hh2, 90)
    draw.polygon(corners2, fill=(0x88, 0x44, 0xCC))

    png_path = OUT_DIR / "test_slide.png"
    img.save(str(png_path))
    print(f"Saved {png_path}")
    return png_path


if __name__ == "__main__":
    make_pptx()
    make_reference_png()
    print("Done. Now run:")
    print("  python pptx_bbox.py test_data/test_slide.pptx 1 test_data/test_slide.png --out-json test_data/out.json --overlay test_data/debug_overlay.png")

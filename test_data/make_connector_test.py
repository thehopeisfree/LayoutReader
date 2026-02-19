"""Generate a test PPTX with connectors linking shapes (flowchart style).

Elements:
  1. Three rectangles (A, B, C)
  2. Two connectors: A->B, B->C
"""
from __future__ import annotations
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu, Pt
from pptx.enum.shapes import MSO_SHAPE
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn

OUT_DIR = Path(__file__).parent


def make_pptx() -> Path:
    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # --- Three boxes ---
    box_a = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(1000000), Emu(2500000), Emu(2000000), Emu(1500000),
    )
    box_a.fill.solid()
    box_a.fill.fore_color.rgb = RGBColor(0x44, 0x88, 0xCC)
    box_a.text_frame.text = "Box A"

    box_b = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(5000000), Emu(2500000), Emu(2000000), Emu(1500000),
    )
    box_b.fill.solid()
    box_b.fill.fore_color.rgb = RGBColor(0x44, 0xCC, 0x44)
    box_b.text_frame.text = "Box B"

    box_c = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(9000000), Emu(2500000), Emu(2000000), Emu(1500000),
    )
    box_c.fill.solid()
    box_c.fill.fore_color.rgb = RGBColor(0xCC, 0x44, 0x44)
    box_c.text_frame.text = "Box C"

    # Get shape IDs for connector references
    id_a = box_a.shape_id
    id_b = box_b.shape_id
    id_c = box_c.shape_id

    # --- Connector A -> B ---
    # Manually add cxnSp XML since python-pptx doesn't have a high-level API
    from pptx.oxml import parse_xml

    cxn_ab_xml = f'''<p:cxnSp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                               xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                               xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <p:nvCxnSpPr>
        <p:cNvPr id="0" name="Connector A-B"/>
        <p:cNvCxnSpPr>
          <a:stCxn id="{id_a}" idx="3"/>
          <a:endCxn id="{id_b}" idx="1"/>
        </p:cNvCxnSpPr>
        <p:nvPr/>
      </p:nvCxnSpPr>
      <p:spPr>
        <a:xfrm>
          <a:off x="3000000" y="3200000"/>
          <a:ext cx="2000000" cy="100000"/>
        </a:xfrm>
        <a:prstGeom prst="straightConnector1"><a:avLst/></a:prstGeom>
        <a:ln w="28575">
          <a:solidFill><a:srgbClr val="333333"/></a:solidFill>
          <a:tailEnd type="triangle"/>
        </a:ln>
      </p:spPr>
    </p:cxnSp>'''

    cxn_ab = parse_xml(cxn_ab_xml)
    slide._element.find(qn('p:cSld')).find(qn('p:spTree')).append(cxn_ab)

    # --- Connector B -> C ---
    cxn_bc_xml = f'''<p:cxnSp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                               xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                               xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <p:nvCxnSpPr>
        <p:cNvPr id="0" name="Connector B-C"/>
        <p:cNvCxnSpPr>
          <a:stCxn id="{id_b}" idx="3"/>
          <a:endCxn id="{id_c}" idx="1"/>
        </p:cNvCxnSpPr>
        <p:nvPr/>
      </p:nvCxnSpPr>
      <p:spPr>
        <a:xfrm>
          <a:off x="7000000" y="3200000"/>
          <a:ext cx="2000000" cy="100000"/>
        </a:xfrm>
        <a:prstGeom prst="straightConnector1"><a:avLst/></a:prstGeom>
        <a:ln w="28575">
          <a:solidFill><a:srgbClr val="333333"/></a:solidFill>
          <a:tailEnd type="triangle"/>
        </a:ln>
      </p:spPr>
    </p:cxnSp>'''

    cxn_bc = parse_xml(cxn_bc_xml)
    slide._element.find(qn('p:cSld')).find(qn('p:spTree')).append(cxn_bc)

    pptx_path = OUT_DIR / "test_connector.pptx"
    prs.save(str(pptx_path))
    print(f"Saved {pptx_path}")
    print(f"  Box A id={id_a}, Box B id={id_b}, Box C id={id_c}")
    return pptx_path


if __name__ == "__main__":
    make_pptx()

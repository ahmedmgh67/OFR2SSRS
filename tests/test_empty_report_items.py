"""An empty <ReportItems> is upload-fatal; its absence is legal RDL.

A source with no layout at all (a foreign XML that is not an Oracle Reports
export) produced ``<Body><ReportItems /></Body>``: the XSD leg and the MS
engine both rejected the file while the preflight BLOCKER already said the
report had no content. The generator now drops every empty <ReportItems>
before serialization, so even an honestly-empty report is a valid file.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from converter import convert  # noqa: E402
from converter.generators import rdl as rdl_mod  # noqa: E402

XSD = ROOT / "tests" / "fixtures" / "schema" / "ReportDefinition_2008.xsd"

# A data model with no layout: the shape the foreign fixture produced.
_NO_LAYOUT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<report name="NO_LAYOUT" DTDVersion="9.0.2.0.10">
  <data>
    <dataSource name="Q_MAIN">
      <select><![CDATA[SELECT e.Emp_Id, e.Emp_Name FROM Emp e]]></select>
      <group name="G_MAIN">
        <dataItem name="Emp_Id" datatype="number"/>
        <dataItem name="Emp_Name" datatype="vchar2"/>
      </group>
    </dataSource>
  </data>
  <layout/>
</report>
"""


def _xsd_errors(xml: str):
    from lxml import etree
    schema = etree.XMLSchema(etree.parse(str(XSD)))
    tree = etree.fromstring(xml.encode("utf-8"))
    return [] if schema.validate(tree) else [e.message for e in schema.error_log]


def test_a_report_with_no_layout_is_still_valid_rdl():
    res = convert(_NO_LAYOUT_XML, target_db="oracle")
    xml = res["rdl_xml"]
    assert "<ReportItems />" not in xml and "<ReportItems/>" not in xml
    assert not _xsd_errors(xml), _xsd_errors(xml)[:3]
    assert res.get("conversion_error") is None


def test_the_pass_removes_only_empty_report_items_and_the_xsd_agrees():
    ns = "http://schemas.microsoft.com/sqlserver/reporting/2008/01/reportdefinition"
    root = ET.Element("Report", {"xmlns": ns})
    body = ET.SubElement(root, "Body")
    ET.SubElement(body, "ReportItems")                       # empty -> must go
    ET.SubElement(body, "Height").text = "1in"
    ET.SubElement(root, "Width").text = "6in"
    page = ET.SubElement(root, "Page")
    hdr = ET.SubElement(page, "PageHeader")
    ET.SubElement(hdr, "Height").text = "0.5in"
    ET.SubElement(hdr, "PrintOnFirstPage").text = "true"
    ET.SubElement(hdr, "PrintOnLastPage").text = "true"
    items = ET.SubElement(hdr, "ReportItems")                # NOT empty -> stays
    tb = ET.SubElement(items, "Textbox", {"Name": "T"})
    run = ET.SubElement(ET.SubElement(ET.SubElement(ET.SubElement(tb, "Paragraphs"), "Paragraph"),
                                      "TextRuns"), "TextRun")
    ET.SubElement(run, "Value").text = "x"
    before = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")
    assert _xsd_errors(before), "the empty element must be invalid, or this test proves nothing"
    assert rdl_mod._strip_empty_report_items(root) == 1
    after = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")
    assert not _xsd_errors(after), _xsd_errors(after)[:3]
    assert after.count("<ReportItems>") == 1 and 'Name="T"' in after

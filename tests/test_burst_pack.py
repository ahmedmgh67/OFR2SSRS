"""The Burst Pack: Oracle distribution reproduced on any SSRS edition.

Oracle ran a bursting report ONCE and wrote one file per record. The pack
reproduces that with three pieces, each proven here:

  * the main RDL gains ONE hidden parameter + ONE dataset filter and stays
    XSD-valid, prompt-free and publish-clean (the SQL is never touched);
  * a companion ``<Report>_BurstList.rdl`` groups the same dataset by the
    key, so the report server itself produces the key list;
  * ``Run-Burst.ps1`` -- Windows PowerShell 5.1 built-ins only -- renders
    the report once per key through URL access. It is parsed by the REAL
    PowerShell parser and EXECUTED against a fake report server here: dry
    run, real run, rerun history, -Force, -TestLimit, -Key, URL encoding of
    keys with spaces and slashes, unsafe file-name characters, an empty key
    list, a failing server, and every exit code.

The earlier shipped script had eleven parse errors and called a cmdlet that
does not exist; nothing had ever executed it. Every check below is one that
would have gone red on it.
"""
from __future__ import annotations

import http.server
import io
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from converter import convert  # noqa: E402
from converter import burst_pack as bp  # noqa: E402
from converter.bursting import build_burst_pack_zip, build_powershell_dds_script  # noqa: E402
from converter.validators.no_prompt_gate import audit_no_prompt  # noqa: E402
from converter.validators.preflight import preflight_audit  # noqa: E402
from converter.validators.publish_semantics import publish_violations  # noqa: E402

XSD = ROOT / "tests" / "fixtures" / "schema" / "ReportDefinition_2008.xsd"
DRIVER_SRC = ROOT / "backend" / "converter" / "burst_driver.ps1.txt"
PS = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" or not PS, reason="Windows PowerShell required")

# A synthetic bursting source: the P_AS_PATH/P_DISTRIBUTE distribution
# parameters trip the detector; nothing here names a customer.
BURST_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<report name="SAMPLE_BURST" DTDVersion="9.0.2.0.10">
  <data>
    <userParameter name="P_AS_PATH" datatype="character"/>
    <userParameter name="P_DISTRIBUTE" datatype="character"/>
    <userParameter name="P_RUN_YEAR" datatype="number"/>
    <dataSource name="Q_MAIN">
      <select>
      <![CDATA[SELECT R.Recipient_Name, R.Email_Addr, R.Doc_No
FROM Recipients R WHERE R.Active = 'Y']]>
      </select>
      <group name="G_MAIN">
        <dataItem name="Recipient_Name" datatype="vchar2"/>
        <dataItem name="Email_Addr" datatype="vchar2"/>
        <dataItem name="Doc_No" datatype="number"/>
      </group>
    </dataSource>
  </data>
  <layout>
  <section name="main">
    <body width="8.0" height="10.0">
      <text name="B_TITLE" x="0.5" y="0.5" width="7.0" height="0.4">
        <textsettings justify="center"/>
        <font face="Arial" size="14" bold="yes"/>
        <contents>Sample Letter</contents>
      </text>
      <field name="F_NAME" x="0.5" y="1.2" width="7.0" height="0.3" source="Recipient_Name"/>
      <field name="F_DOC" x="0.5" y="1.6" width="7.0" height="0.3" source="Doc_No"/>
    </body>
  </section>
  </layout>
</report>
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SCHEMA = None


def _xsd_errors(rdl_xml: str):
    global _SCHEMA
    from lxml import etree
    if _SCHEMA is None:
        _SCHEMA = etree.XMLSchema(etree.parse(str(XSD)))
    tree = etree.fromstring(rdl_xml.encode("utf-8"))
    if _SCHEMA.validate(tree):
        return []
    return [e.message for e in _SCHEMA.error_log]


def _gates(rdl_xml: str, what: str):
    assert not _xsd_errors(rdl_xml), what + " is not XSD-valid: " + "\n".join(_xsd_errors(rdl_xml))[:800]
    assert audit_no_prompt(rdl_xml) == [], what + " would prompt"
    assert publish_violations(rdl_xml) == [], what + " has publish violations"
    pre = preflight_audit(rdl_xml, target_db="oracle")
    blockers = [i.get("rule") for i in pre.get("issues", []) if i.get("severity") == "BLOCKER"]
    assert not blockers, what + " preflight BLOCKERs: " + str(blockers)


@pytest.fixture(scope="module")
def converted():
    res = convert(BURST_XML, target_db="oracle")
    assert res["rdl_xml"].strip()
    return res


def _uninjected(rdl_xml: str) -> str:
    """The converted RDL with the two Burst Pack additions taken back out
    (convert() injects them itself, so this is the only way to a plain one)."""
    out = re.sub(r"\s*<Filters>.*?</Filters>", "", rdl_xml, flags=re.S)
    out = re.sub(r'\s*<ReportParameter Name="' + bp.BURST_PARAM + r'">.*?</ReportParameter>', "", out, flags=re.S)
    assert bp.BURST_PARAM not in out
    return out


def _ps_parse_errors(text: str, tmp_path: Path):
    """Parse ``text`` with the real PowerShell parser; the error messages."""
    f = tmp_path / "candidate.ps1"
    f.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    checker = tmp_path / "check.ps1"
    checker.write_text(
        "param([string]$Path)\n"
        "$t = $null; $e = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$t, [ref]$e)\n"
        "foreach ($x in $e) { 'ERR ' + $x.Extent.StartLineNumber + ': ' + $x.Message }\n"
        "'COUNT ' + $e.Count\n", encoding="utf-8")
    proc = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(checker), "-Path", str(f)],
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout
    m = re.search(r"COUNT (\d+)", out)
    assert m, "parser check did not report: " + out + proc.stderr
    return int(m.group(1)), out


# ---------------------------------------------------------------------------
# 1. the driver text
# ---------------------------------------------------------------------------

def test_driver_template_is_pure_ascii_with_no_modules_and_no_sql():
    src = DRIVER_SRC.read_bytes()
    assert all(b < 128 for b in src), "the driver must be pure ASCII (PS 5.1 reads BOM-less files as ANSI)"
    text = src.decode("ascii")
    assert "#requires -Version 5.1" in text
    assert "#requires -Modules" not in text and "Import-Module" not in text
    assert "Export-RsReport" not in text, "the cmdlet that never existed"
    assert "Invoke-Sqlcmd" not in text and "SELECT " not in text
    assert "rs:Format=" in text and "rs:Command=Render" in text
    assert "__REPORT_NAME__" in text and "__BIND_PARAM__" in text
    # hashtable literals are newline- or semicolon-separated (commas are a parse error)
    for m in re.finditer(r"@\{([^}]*)\}", text):
        assert "," not in m.group(1).replace("-join ','", ""), "comma inside a hashtable literal: " + m.group(0)[:80]


def test_build_driver_fills_both_placeholders():
    ps = bp.build_driver("MY REPORT")
    assert "__REPORT_NAME__" not in ps and "__BIND_PARAM__" not in ps
    assert "MY REPORT" in ps and bp.BURST_PARAM in ps
    assert all(ord(c) < 128 for c in ps)


@needs_powershell
def test_driver_parses_with_zero_errors_and_the_check_can_fail(tmp_path):
    n, out = _ps_parse_errors(bp.build_driver("SAMPLE_BURST"), tmp_path)
    assert n == 0, out
    # The gate must be able to go red: the two defects the OLD script shipped
    # with (commas in a hashtable, an unterminated construct).
    broken = bp.build_driver("SAMPLE_BURST") + "\n$x = @{ A = 1, B = 2 }\nif ($x { }\n"
    n2, _ = _ps_parse_errors(broken, tmp_path)
    assert n2 > 0, "the parser check cannot fail -- it proves nothing"


def test_hostile_report_name_is_whitelisted_everywhere():
    name = 'R"X\'Y$(calc)`n#>; Remove-Item ..\\..\\x'
    info = {"is_bursting": True, "burst_key_field": "Doc_No", "filename_pattern": "<Doc_No>.pdf"}
    pack = bp.prepare(name, "<Report/>", info)
    for k in ("driver", "config_json", "readme"):
        assert '$(calc)' not in pack[k] and '"X' not in pack[k] and "#>;" not in pack[k], k
    assert pack["config"]["ReportName"] == bp._safe_name(name)
    assert re.fullmatch(r"[A-Za-z0-9_ .\-]+", pack["config"]["ReportName"])
    z = zipfile.ZipFile(io.BytesIO(bp.build_zip(name, "<Report/>", info)))
    for n in z.namelist():
        assert ".." not in n and "/" not in n and "\\" not in n, n


# ---------------------------------------------------------------------------
# 2. the main RDL: one hidden parameter + one filter, nothing else
# ---------------------------------------------------------------------------

def test_convert_injects_the_per_key_filter_and_every_gate_stays_green(converted):
    b = converted["bursting"]
    assert b["is_bursting"] is True
    assert b["filter_injected"] is True, b.get("filter_reason")
    assert b["bind_parameter"] == bp.BURST_PARAM
    rdl = converted["rdl_xml"]
    assert rdl.count('<ReportParameter Name="' + bp.BURST_PARAM + '">') == 1
    block = rdl.split('<ReportParameter Name="' + bp.BURST_PARAM + '">', 1)[1].split("</ReportParameter>", 1)[0]
    assert "<Hidden>true</Hidden>" in block and "<Value>=Nothing</Value>" in block
    assert "<Nullable>true</Nullable>" in block and "<DataType>String</DataType>" in block
    assert rdl.count("<Filters>") == 1
    filt = rdl.split("<Filters>", 1)[1].split("</Filters>", 1)[0]
    assert "IsNothing(Parameters!" + bp.BURST_PARAM + ".Value)" in filt
    assert "Fields!" + b["burst_key_field"] + ".Value" in filt
    assert "<Operator>Equal</Operator>" in filt and "<FilterValue>=True</FilterValue>" in filt
    _gates(rdl, "the injected main RDL")
    # the preflight verdict the UI shows is the one on the injected RDL
    assert (converted.get("preflight") or {}).get("verdict") in ("READY", "GREEN", "OK", "ready")


def test_injection_never_touches_the_sql(converted):
    """The filter is a dataset filter; CommandText is byte-identical, and
    re-injecting into the plain RDL reproduces convert()'s output exactly."""
    plain = _uninjected(converted["rdl_xml"])
    a = re.findall(r"<CommandText>.*?</CommandText>", converted["rdl_xml"], re.S)
    b = re.findall(r"<CommandText>.*?</CommandText>", plain, re.S)
    assert a == b and a
    again, meta = bp.inject_burst_key_filter(plain, converted["bursting"])
    assert meta["injected"] is True and meta["reason"].startswith("filter on ")
    assert again == converted["rdl_xml"]


def test_injection_is_idempotent(converted):
    rdl = converted["rdl_xml"]
    again, meta = bp.inject_burst_key_filter(rdl, converted["bursting"])
    assert again == rdl and meta["injected"] is True


def test_non_bursting_report_is_byte_identical(synthetic_xml_bytes):
    res = convert(synthetic_xml_bytes, target_db="oracle")
    assert not (res.get("bursting") or {}).get("is_bursting")
    assert bp.BURST_PARAM not in res["rdl_xml"] and "<Filters>" not in res["rdl_xml"]
    same, meta = bp.inject_burst_key_filter(res["rdl_xml"], {"is_bursting": False})
    assert same == res["rdl_xml"] and meta["injected"] is False


def test_key_that_is_not_a_field_declines_with_a_reason(converted):
    plain = _uninjected(converted["rdl_xml"])
    out, meta = bp.inject_burst_key_filter(plain, {"is_bursting": True, "burst_key_field": "NO_SUCH_COLUMN"})
    assert meta["injected"] is False and meta["param_injected"] is True
    assert "NO_SUCH_COLUMN" in meta["reason"]
    # the hidden parameter is still added (harmless; the driver's URL
    # parameter is then accepted the moment a filter is added by hand) --
    # but no filter, and every gate still green
    assert "<Filters>" not in out and '<ReportParameter Name="' + bp.BURST_PARAM + '">' in out
    _gates(out, "the param-only RDL")
    pack = bp.prepare("SAMPLE_BURST", plain, {"is_bursting": True, "burst_key_field": "NO_SUCH_COLUMN"})
    assert pack["burst_list_rdl"] is None
    assert pack["config"]["BurstListPath"] == "", "no key list -> the driver must refuse to run, not 404"
    assert "NO_SUCH_COLUMN" in pack["readme"] and "refuses to run" in pack["readme"]
    assert any(s["step"] == 0 and "exit 2" in s["body"] for s in pack["checklist"])


def test_injection_creates_the_parameters_block_when_the_report_has_none(converted):
    plain = _uninjected(converted["rdl_xml"])
    stripped = re.sub(r"\s*<ReportParameters>.*?</ReportParameters>", "", plain, flags=re.S)
    assert "<ReportParameters>" not in stripped
    out, meta = bp.inject_burst_key_filter(stripped, converted["bursting"])
    assert meta["injected"] is True
    assert out.count("<ReportParameters>") == 1
    assert out.index("<ReportParameters>") < out.index("<Body>")
    assert not _xsd_errors(out), _xsd_errors(out)[:3]


_TWO_DATASET_RDL = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2008/01/reportdefinition"'
    ' xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner">\n'
    '  <DataSources><DataSource Name="DS"><DataSourceReference>/DS/Oracle</DataSourceReference>'
    '<rd:SecurityType>None</rd:SecurityType></DataSource></DataSources>\n'
    '  <DataSets>\n'
    '    <DataSet Name="Q_MAIN"><Query><DataSourceName>DS</DataSourceName>'
    '<CommandText>SELECT a.Perm_Num, a.Site_Name FROM Permit a</CommandText></Query>'
    '<Fields><Field Name="Perm_Num"><DataField>Perm_Num</DataField></Field>'
    '<Field Name="Site_Name"><DataField>Site_Name</DataField></Field></Fields></DataSet>\n'
    '    <DataSet Name="Q_LOGO"><Query><DataSourceName>DS</DataSourceName>'
    '<CommandText>SELECT l.Img FROM Logos l</CommandText></Query>'
    '<Fields><Field Name="Img"><DataField>Img</DataField></Field></Fields></DataSet>\n'
    '  </DataSets>\n'
    '  <Body><ReportItems><Textbox Name="T1"><Paragraphs><Paragraph><TextRuns><TextRun>'
    '<Value>x</Value></TextRun></TextRuns></Paragraph></Paragraphs></Textbox></ReportItems>'
    '<Height>1in</Height></Body>\n  <Width>7.5in</Width>\n</Report>\n'
)


def test_dataset_filter_is_in_scope_for_the_publish_auditor_in_a_multi_dataset_report():
    """A dataset filter's Fields! reference is scoped to THAT dataset. The
    publish auditor used to judge it 'outside any data region' in every
    multi-dataset report (it never saw a dataset filter before); the same
    auditor must still flag a filter naming a field the dataset lacks."""
    info = {"is_bursting": True, "burst_key_field": "Perm_Num", "filename_pattern": "<Perm_Num>.pdf"}
    out, meta = bp.inject_burst_key_filter(_TWO_DATASET_RDL, info)
    assert meta["injected"] is True and meta["dataset"] == "Q_MAIN"
    assert not _xsd_errors(out), _xsd_errors(out)[:3]
    assert publish_violations(out) == []
    # the mutation: the filter names a field Q_MAIN does not declare
    bad = out.replace("Fields!Perm_Num.Value", "Fields!NOT_HERE.Value")
    assert any("publish.field_not_in_dataset" in v for v in publish_violations(bad)), \
        "the auditor no longer sees a bad field inside a dataset filter"
    # and the key-list report built from it is valid too
    lst = bp.build_burst_list_rdl(out, info, dict(meta, pattern="<Perm_Num>.pdf"))
    assert lst and not _xsd_errors(lst), (_xsd_errors(lst) or [""])[:3]
    assert publish_violations(lst) == []


def test_key_dataset_is_found_case_insensitively(converted):
    rdl = _uninjected(converted["rdl_xml"])
    hit = bp.find_key_dataset(rdl, "recipient_NAME")
    assert hit and hit[0] == "Q_MAIN" and hit[1] == "Recipient_Name"
    assert bp.find_key_dataset(rdl, "nope") is None


# ---------------------------------------------------------------------------
# 3. the key-list report
# ---------------------------------------------------------------------------

def test_burst_list_rdl_groups_the_key_and_passes_every_gate(converted):
    b = converted["bursting"]
    lst = b["burst_list_rdl"]
    assert lst
    _gates(lst, "the key-list RDL")
    key = b["burst_key_field"]
    assert '<Group Name="G_BurstKey">' in lst
    assert "<GroupExpression>=Fields!" + key + ".Value</GroupExpression>" in lst
    assert '<Textbox Name="' + bp.KEY_COLUMN + '">' in lst
    assert "=CStr(Fields!" + key + ".Value)" in lst
    assert "<Filters>" not in lst, "the list is never filtered by the key"
    assert "<DataSourceReference>" in lst
    assert '<DataSet Name="Q_MAIN">' in lst
    # the report's own parameters ride along (URL access can set them)
    assert '<ReportParameter Name="P_RUN_YEAR">' in lst
    assert '<ReportParameter Name="' + bp.BURST_PARAM + '">' in lst
    assert "<Value>=Nothing</Value>" in lst


def test_pattern_columns_become_list_columns_and_unknown_tokens_are_named(converted):
    plain = _uninjected(converted["rdl_xml"])
    info = {"is_bursting": True, "burst_key_field": "Doc_No",
            "filename_pattern": "<Recipient_Name>_<Doc_No>_<P_RUN_YEAR>_<Mystery>_YYYYMMDD.pdf"}
    pack = bp.prepare("SAMPLE_BURST", plain, info)
    # the key column's token is rewritten to the column the key list carries
    assert pack["meta"]["pattern"] == "<Recipient_Name>_<BURST_KEY>_<P_RUN_YEAR>_<Mystery>_{date:yyyyMMdd}.pdf"
    assert pack["columns"] == {"columns": ["Recipient_Name"], "params": ["P_RUN_YEAR"], "unknown": ["Mystery"]}
    lst = pack["burst_list_rdl"]
    assert '<Textbox Name="Recipient_Name">' in lst
    assert "=CStr(First(Fields!Recipient_Name.Value))" in lst
    assert '<Textbox Name="Doc_No">' not in lst, "the key itself is the BURST_KEY column"
    assert "P_RUN_YEAR" in pack["config"]["ReportParameters"]
    assert "Mystery" in pack["readme"]
    assert not _xsd_errors(lst), _xsd_errors(lst)[:3]


@pytest.mark.parametrize("src,expected", [
    ("<Permit>_YYYYMMDD.pdf", "<Permit>_{date:yyyyMMdd}.pdf"),
    ("<A>_<B>.pdf", "<A>_<B>.pdf"),
    ("SUMMARY_<Id>.pdf", "SUMMARY_<Id>.pdf"),          # letters that are not a mask stay
    ("<Id>_MM-DD-YYYY_HH24MISS.pdf", "<Id>_{date:MM-dd-yyyy_HHmmss}.pdf"),
    ("<Id>.pdf", "<Id>.pdf"),
    ("", ""),
])
def test_normalize_pattern(src, expected):
    assert bp.normalize_pattern(src) == expected


def test_burst_list_sql_is_derived_from_the_reports_own_command_text(converted):
    b = converted["bursting"]
    sql = b["burst_query"]
    assert sql.startswith("-- one row per burst key")
    assert "SELECT DISTINCT " + b["burst_key_field"] + " AS BURST_KEY" in sql
    assert "FROM Recipients R" in sql and "ORDER BY 1" in sql


@pytest.mark.skipif(sys.platform != "win32", reason="MS engine is Windows-only")
def test_burst_list_renders_through_the_real_engine(converted, tmp_path):
    sys.path.insert(0, str(ROOT / "tools" / "renderlab"))
    try:
        from render import lib_ready, render_rdl
    except Exception:  # noqa: BLE001
        pytest.skip("renderlab not available")
    if not lib_ready():
        pytest.skip("ReportViewer DLLs not fetched")
    rdl = tmp_path / "list.rdl"
    rdl.write_text(converted["bursting"]["burst_list_rdl"], encoding="utf-8")
    res = render_rdl(rdl, tmp_path / "list.pdf", rows=3, timeout=240)
    assert res.get("ok"), res.get("log", "")[-800:]
    assert (tmp_path / "list.pdf").stat().st_size > 0


# ---------------------------------------------------------------------------
# 4. the pack
# ---------------------------------------------------------------------------

def test_pack_zip_contents_and_config(converted):
    b = converted["bursting"]
    blob = build_burst_pack_zip(type("R", (), {"name": "SAMPLE_BURST"})(), converted["rdl_xml"], b,
                                {"report_server_url": "http://srv/ReportServer?/Env/Letters/"})
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert sorted(z.namelist()) == sorted([
        "SAMPLE_BURST.rdl", "SAMPLE_BURST_BurstList.rdl", bp.DRIVER_NAME,
        "burst.config.json", "README.md", "service-account-setup.md"])
    assert z.read("SAMPLE_BURST.rdl").decode("utf-8") == converted["rdl_xml"]
    ps = z.read(bp.DRIVER_NAME)
    assert ps.startswith(b"\xef\xbb\xbf") and ps[3:].decode("ascii") == bp.build_driver("SAMPLE_BURST")
    cfg = json.loads(z.read("burst.config.json"))
    assert cfg["ReportServer"] == "http://srv/ReportServer"
    assert cfg["ReportPath"] == "/Env/Letters/SAMPLE_BURST"
    assert cfg["BurstListPath"] == "/Env/Letters/SAMPLE_BURST_BurstList"
    assert cfg["BindParameter"] == bp.BURST_PARAM
    assert cfg["FileNamePattern"] == b["filename_pattern_normalized"]
    assert cfg["Deliver"] == "file" and cfg["RenderFormat"] == "PDF"
    assert cfg["FileNamePattern"] == "<BURST_KEY>.pdf" and cfg["KeyField"] == b["burst_key_field"]
    assert cfg["History"] == "day" and cfg["AbortAfterFailures"] == 5
    # every report parameter is visible to the operator (empty = the report's default)
    assert set(cfg["ReportParameters"]) == {"P_AS_PATH", "P_DISTRIBUTE", "P_RUN_YEAR"}
    assert bp.prepare("SAMPLE_BURST", converted["rdl_xml"], b, {"RenderFormat": "EXCELOPENXML"})["config"]["FileNamePattern"] == "<BURST_KEY>.xlsx"
    readme = z.read("README.md").decode("utf-8")
    assert "-DryRun" in readme and "-TestLimit 2" in readme and "SAMPLE_BURST_BurstList.rdl" in readme
    md = z.read("service-account-setup.md").decode("utf-8")
    assert "Browser" in md and "schtasks" in md


def test_old_builder_names_still_work_and_agree(converted):
    r = type("R", (), {"name": "SAMPLE_BURST", "_o2s_rdl_xml": converted["rdl_xml"]})()
    assert build_powershell_dds_script(r, converted["bursting"], "x.rdl") == bp.build_driver("SAMPLE_BURST")
    assert converted["bursting"]["powershell_script"] == bp.build_driver("SAMPLE_BURST")
    assert converted["bursting"]["email_powershell_script"] == bp.build_driver("SAMPLE_BURST")


# ---------------------------------------------------------------------------
# 5. the driver, executed against a fake report server
# ---------------------------------------------------------------------------

class _FakeSsrs(http.server.BaseHTTPRequestHandler):
    """URL access the way Reporting Services answers it:
    ``http://host/ReportServer?/Folder/Item&rs:Command=Render&rs:Format=X&Param=v``."""

    def log_message(self, *_a):  # quiet
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append(self.path)
        _path, _, query = self.path.partition("?")
        parts = query.split("&")
        item = urllib.parse.unquote(parts[0])
        kv = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                kv[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        fmt = kv.get("rs:Format", "")
        if item == srv.list_path:
            if fmt != "CSV":
                return self._send(400, b"list must be CSV", "text/plain")
            return self._send(200, b"\xef\xbb\xbf" + srv.csv.encode("utf-8"), "text/csv")
        if item == srv.report_path:
            if srv.fail_main:
                code = 500 if srv.fail_main is True else int(srv.fail_main)
                # the way Reporting Services really answers: an HTML page with the rs code
                return self._send(code, b"<html><body><p>The item '/x' cannot be found. "
                                        b"(rsItemNotFound)</p></body></html>", "text/html")
            key = kv.get(bp.BURST_PARAM, "")
            srv.rendered.append((key, fmt, dict(kv)))
            return self._send(200, ("%PDF-1.4 fake " + key).encode("utf-8"), "application/pdf")
        return self._send(404, b"rsItemNotFound", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_ssrs():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeSsrs)
    srv.requests, srv.rendered = [], []
    srv.report_path = "/Folder A/SAMPLE_BURST"
    srv.list_path = "/Folder A/SAMPLE_BURST_BurstList"
    srv.csv = "BURST_KEY,Renewal_Year,EmailTo\r\nA-1,2026,a@example.test\r\nB 2,2026,b@example.test\r\nC/3,2027,\r\n"
    srv.fail_main = False
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _unpack(converted, tmp_path: Path, srv, **cfg_over):
    blob = build_burst_pack_zip(type("R", (), {"name": "SAMPLE_BURST"})(), converted["rdl_xml"],
                                converted["bursting"], {})
    pack = tmp_path / "pack"
    pack.mkdir()
    zipfile.ZipFile(io.BytesIO(blob)).extractall(pack)
    cfg = json.loads((pack / "burst.config.json").read_text(encoding="utf-8"))
    cfg.update({
        "ReportServer": "http://127.0.0.1:%d/ReportServer" % srv.server_address[1],
        "ReportPath": srv.report_path,
        "BurstListPath": srv.list_path,
        "OutputRoot": str(tmp_path / "out"),
        "LogRoot": str(tmp_path / "log"),
        "Retries": 1,
        "TimeoutSec": 60,
    })
    cfg.update(cfg_over)
    (pack / "burst.config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return pack


def _run(pack: Path, *args):
    return subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(pack / bp.DRIVER_NAME), "-ConfigPath", str(pack / "burst.config.json"), *args],
        capture_output=True, text=True, timeout=300, cwd=str(pack))


def _events(tmp_path: Path):
    out = []
    for f in sorted((tmp_path / "log").glob("*.jsonl")):
        for ln in f.read_text(encoding="utf-8-sig").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


@needs_powershell
def test_driver_dry_run_lists_every_key_and_renders_nothing(converted, tmp_path, fake_ssrs):
    pack = _unpack(converted, tmp_path, fake_ssrs, FileNamePattern="<Renewal_Year>_<BURST_KEY>_{date:yyyy}.pdf")
    proc = _run(pack, "-DryRun")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert fake_ssrs.rendered == [], "a dry run must not render the report"
    assert len(fake_ssrs.requests) == 1 and "rs:Format=CSV" in fake_ssrs.requests[0]
    assert "rc:Encoding=UTF-8" in fake_ssrs.requests[0]
    assert "/Folder%20A/SAMPLE_BURST_BurstList" in fake_ssrs.requests[0], "item paths are segment-escaped"
    ev = [e for e in _events(tmp_path) if e["event"] == "dry_run"]
    assert [e["key"] for e in ev] == ["A-1", "B 2", "C/3"]
    import datetime
    y = str(datetime.date.today().year)
    assert ev[0]["file"].endswith("2026_A-1_" + y + ".pdf")
    assert ev[2]["file"].endswith("2027_C_3_" + y + ".pdf"), "a slash in a key is not a path separator"
    assert not (tmp_path / "out").exists()


@needs_powershell
def test_driver_renders_one_file_per_key_and_reruns_skip_done_keys(converted, tmp_path, fake_ssrs):
    pack = _unpack(converted, tmp_path, fake_ssrs, FileNamePattern="<Renewal_Year>_<BURST_KEY>_{date:yyyy}.pdf")
    proc = _run(pack)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [k for k, _f, _q in fake_ssrs.rendered] == ["A-1", "B 2", "C/3"]
    assert all(f == "PDF" for _k, f, _q in fake_ssrs.rendered)
    # keys travel URL-encoded
    assert any(bp.BURST_PARAM + "=B%202" in r for r in fake_ssrs.requests)
    assert any(bp.BURST_PARAM + "=C%2F3" in r for r in fake_ssrs.requests)
    import datetime
    y = str(datetime.date.today().year)
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == sorted([
        "2026_A-1_" + y + ".pdf", "2026_B 2_" + y + ".pdf", "2027_C_3_" + y + ".pdf"])
    assert (out / ("2026_B 2_" + y + ".pdf")).read_bytes() == b"%PDF-1.4 fake B 2"
    done_files = list((tmp_path / "log").glob("done_keys_*.txt"))
    assert len(done_files) == 1 and re.fullmatch(r"done_keys_\d{8}\.txt", done_files[0].name), done_files
    done = done_files[0].read_text(encoding="utf-8-sig").splitlines()
    assert done == ["A-1", "B 2", "C/3"]
    summary = [e for e in _events(tmp_path) if e["event"] == "run_complete"][-1]
    # every summary field lands (a hashtable entry named "keys" once shadowed
    # the .Keys collection and emptied the record -- measured)
    assert (summary["report"], summary["key_count"], summary["ok"], summary["failed"], summary["skipped"]) \
        == ("SAMPLE_BURST", "3", "3", "0", "0"), summary
    assert sum(1 for e in _events(tmp_path) if e["event"] == "file_written") == 3

    # rerun: history skips everything, nothing is rendered again
    fake_ssrs.rendered.clear()
    proc = _run(pack)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert fake_ssrs.rendered == []
    assert sum(1 for e in _events(tmp_path) if e["event"] == "skip_done") == 3

    # -Force redoes them
    proc = _run(pack, "-Force")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [k for k, _f, _q in fake_ssrs.rendered] == ["A-1", "B 2", "C/3"]


@needs_powershell
def test_driver_test_limit_and_single_key(converted, tmp_path, fake_ssrs):
    pack = _unpack(converted, tmp_path, fake_ssrs)
    proc = _run(pack, "-TestLimit", "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [k for k, _f, _q in fake_ssrs.rendered] == ["A-1"]
    fake_ssrs.rendered.clear()
    proc = _run(pack, "-Key", "B 2", "-Force")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [k for k, _f, _q in fake_ssrs.rendered] == ["B 2"]


@needs_powershell
def test_driver_passes_report_parameters_and_render_format(converted, tmp_path, fake_ssrs):
    pack = _unpack(converted, tmp_path, fake_ssrs, RenderFormat="EXCELOPENXML",
                   ReportParameters={"P_RUN_YEAR": "2026", "P_EMPTY": ""},
                   FileNamePattern="<BURST_KEY>_<P_RUN_YEAR>.xlsx")
    proc = _run(pack, "-TestLimit", "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    key, fmt, q = fake_ssrs.rendered[0]
    assert fmt == "EXCELOPENXML" and q["P_RUN_YEAR"] == "2026" and "P_EMPTY" not in q
    assert any("P_RUN_YEAR=2026" in r and "BurstList" in r for r in fake_ssrs.requests), \
        "the key list is rendered with the same report parameters"
    assert (tmp_path / "out" / "A-1_2026.xlsx").exists(), "a parameter fills a file-name token"


@needs_powershell
def test_driver_reports_a_failing_server_honestly(converted, tmp_path, fake_ssrs):
    fake_ssrs.fail_main = True
    pack = _unpack(converted, tmp_path, fake_ssrs)
    proc = _run(pack)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Every key failed" in proc.stderr
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())
    assert sum(1 for e in _events(tmp_path) if e["event"] == "render_failed") == 3
    assert not [f for f in (tmp_path / "log").glob("done_keys*.txt")
                if f.read_text(encoding="utf-8-sig").strip()], "a failed key is never recorded as done"


@needs_powershell
def test_driver_handles_an_empty_key_list_and_a_wrong_shape(converted, tmp_path, fake_ssrs):
    fake_ssrs.csv = "BURST_KEY,Renewal_Year\r\n"
    pack = _unpack(converted, tmp_path, fake_ssrs)
    proc = _run(pack)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(e["event"] == "no_keys" for e in _events(tmp_path))
    fake_ssrs.csv = "Textbox1,Textbox2\r\nx,y\r\n"
    proc = _run(pack)
    assert proc.returncode == 1
    assert "BURST_KEY" in proc.stderr


@needs_powershell
def test_driver_exit_codes_for_config_errors(converted, tmp_path, fake_ssrs):
    pack = _unpack(converted, tmp_path, fake_ssrs, ReportServer="")
    proc = _run(pack)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "ReportServer" in proc.stderr
    proc = subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(pack / bp.DRIVER_NAME), "-ConfigPath", str(tmp_path / "nope.json")],
        capture_output=True, text=True, timeout=120, cwd=str(pack))
    assert proc.returncode == 2 and "Config not found" in proc.stderr


# ---------------------------------------------------------------------------
# 6. What the adversarial review found. Each was a real defect the earlier
#    tests had masked (they overrode the shipped file pattern); each is now
#    EXECUTED against the fake server, not reasoned about.
# ---------------------------------------------------------------------------

@needs_powershell
def test_shipped_pattern_produces_one_distinct_file_per_key(converted, tmp_path, fake_ssrs):
    """The Oracle pattern names the KEY column ('<Recipient_Name>.pdf'); the
    key list carries the key as BURST_KEY. Before the rewrite the token never
    resolved: every key wrote '_Recipient_Name_.pdf', each render overwrote
    the last, and a 500-key run left ONE file with exit 0."""
    pack = _unpack(converted, tmp_path, fake_ssrs)
    cfg = json.loads((pack / "burst.config.json").read_text(encoding="utf-8"))
    assert cfg["FileNamePattern"] == "<BURST_KEY>.pdf" and cfg["KeyField"] == "Recipient_Name"
    proc = _run(pack)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["A-1.pdf", "B 2.pdf", "C_3.pdf"]
    assert (out / "C_3.pdf").read_bytes() == b"%PDF-1.4 fake C/3"


@needs_powershell
def test_key_column_token_is_an_alias_for_the_key(converted, tmp_path, fake_ssrs):
    """A hand-edited pattern may still use the Oracle column name."""
    pack = _unpack(converted, tmp_path, fake_ssrs, FileNamePattern="<recipient_name>_<Renewal_Year>.pdf")
    proc = _run(pack, "-DryRun")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    files = [Path(e["file"]).name for e in _events(tmp_path) if e["event"] == "dry_run"]
    assert files == ["A-1_2026.pdf", "B 2_2026.pdf", "C_3_2027.pdf"]


@needs_powershell
def test_history_is_per_day_and_test_runs_are_not_recorded(converted, tmp_path, fake_ssrs):
    """A weekly schedule must produce every key every week. The history is
    scoped to the DAY: a rerun the same day resumes, another day starts
    over; -TestLimit / -Key runs record nothing; a run that produced nothing
    because everything was already done logs all_skipped."""
    pack = _unpack(converted, tmp_path, fake_ssrs)
    log = tmp_path / "log"
    proc = _run(pack, "-TestLimit", "2")
    assert proc.returncode == 0 and len(fake_ssrs.rendered) == 2, proc.stdout + proc.stderr
    assert not list(log.glob("done_keys*.txt")), "a test run must never be recorded"
    fake_ssrs.rendered.clear()
    proc = _run(pack, "-Key", "B 2")
    assert proc.returncode == 0 and [k for k, _f, _q in fake_ssrs.rendered] == ["B 2"]
    assert not list(log.glob("done_keys*.txt")), "-Key runs are not recorded either"
    fake_ssrs.rendered.clear()
    proc = _run(pack)
    assert proc.returncode == 0 and [k for k, _f, _q in fake_ssrs.rendered] == ["A-1", "B 2", "C/3"]
    done = list(log.glob("done_keys_*.txt"))
    assert len(done) == 1 and re.fullmatch(r"done_keys_\d{8}\.txt", done[0].name)
    # a same-day rerun: nothing to do, and that is VISIBLE
    fake_ssrs.rendered.clear()
    proc = _run(pack)
    assert proc.returncode == 0 and fake_ssrs.rendered == []
    assert any(e["event"] == "all_skipped" for e in _events(tmp_path))
    # another day (simulated by retiring today's file; yesterday's is another file)
    (log / "done_keys_20000101.txt").write_text("A-1\nB 2\nC/3\n", encoding="utf-8")
    done[0].unlink()
    fake_ssrs.rendered.clear()
    proc = _run(pack)
    assert proc.returncode == 0 and len(fake_ssrs.rendered) == 3, "another day renders everything again"


@needs_powershell
def test_history_none_never_skips(converted, tmp_path, fake_ssrs):
    pack = _unpack(converted, tmp_path, fake_ssrs, History="none")
    for _ in range(2):
        fake_ssrs.rendered.clear()
        proc = _run(pack)
        assert proc.returncode == 0 and len(fake_ssrs.rendered) == 3, proc.stdout + proc.stderr
    assert not list((tmp_path / "log").glob("done_keys*.txt"))


class _FakeSmtp(threading.Thread):
    """Just enough SMTP for System.Net.Mail: EHLO, MAIL, RCPT, DATA, QUIT."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        f = conn.makefile("rb")

        def send(line):
            conn.sendall((line + "\r\n").encode("ascii"))

        send("220 fake ESMTP")
        while True:
            line = f.readline()
            if not line:
                break
            up = line.decode("latin-1").strip().upper()
            if up.startswith("EHLO") or up.startswith("HELO"):
                send("250-fake")
                send("250 OK")
            elif up == "DATA":
                send("354 end with <CRLF>.<CRLF>")
                buf = []
                while True:
                    ln = f.readline()
                    if not ln or ln == b".\r\n":
                        break
                    buf.append(ln)
                self.messages.append(b"".join(buf))
                send("250 queued")
            elif up == "QUIT":
                send("221 bye")
                break
            else:
                send("250 OK")
        conn.close()

    def stop(self):
        self.sock.close()


@needs_powershell
def test_email_delivery_attaches_the_named_file_and_keys_are_literal(converted, tmp_path, fake_ssrs):
    """Deliver=email used to attach the temp file 'o2s_burst_<guid>.bin';
    and a key containing '$' used to act as a regex substitution in the
    subject. Both are executed here through a real SMTP conversation."""
    smtp = _FakeSmtp()
    smtp.start()
    try:
        fake_ssrs.csv = "BURST_KEY,EmailTo\r\nA-1,a@example.test\r\nACC$_1,b@example.test\r\nC/3,\r\n"
        pack = _unpack(converted, tmp_path, fake_ssrs, Deliver="email", SmtpServer="127.0.0.1",
                       SmtpPort=smtp.port, SmtpFrom="reports@example.test")
        proc = _run(pack)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not (tmp_path / "out").exists(), "email-only delivery writes no files"
        deadline = time.time() + 10
        while len(smtp.messages) < 2 and time.time() < deadline:
            time.sleep(0.1)
        assert len(smtp.messages) == 2, proc.stdout
        joined = b"\n".join(smtp.messages)
        assert b"A-1.pdf" in joined and b"ACC$_1.pdf" in joined, joined[:600]
        assert b"o2s_burst_" not in joined and b".bin" not in joined
        assert b"Subject: SAMPLE_BURST - ACC$_1" in joined
        ev = _events(tmp_path)
        assert any(e["event"] == "skip_no_email" and e["key"] == "C/3" for e in ev)
        assert sum(1 for e in ev if e["event"] == "email_sent") == 2
    finally:
        smtp.stop()


@needs_powershell
def test_permanent_errors_are_not_retried_and_the_run_aborts_early(converted, tmp_path, fake_ssrs):
    """A wrong path / unmodified RDL answers 4xx for EVERY key. That used to
    be retried with back-off per key (hours for a long list); now a 4xx
    fails at once, carries the server's rs message, and the run aborts
    after AbortAfterFailures identical failures."""
    fake_ssrs.csv = "BURST_KEY\r\n" + "".join("K%d\r\n" % i for i in range(10))
    fake_ssrs.fail_main = 404
    pack = _unpack(converted, tmp_path, fake_ssrs, Retries=3)
    t0 = time.time()
    proc = _run(pack)
    elapsed = time.time() - t0
    assert proc.returncode == 1 and "aborted" in proc.stderr.lower(), proc.stdout + proc.stderr
    renders = [r for r in fake_ssrs.requests if "BurstList" not in r]
    assert len(renders) == 5, "one request per key (no retry on 4xx), abort after 5: " + str(len(renders))
    assert elapsed < 60, "no back-off sleeps for permanent errors"
    ev = _events(tmp_path)
    assert any(e["event"] == "run_aborted" for e in ev)
    failed = [e for e in ev if e["event"] == "render_failed"]
    assert failed and all("HTTP 404" in e["msg"] for e in failed), failed[:1]
    assert any("rsItemNotFound" in e["msg"] for e in failed), "the server's own reason must reach the log: " + failed[0]["msg"]


@needs_powershell
def test_config_mistakes_are_config_errors_not_key_failures(converted, tmp_path, fake_ssrs):
    pack = _unpack(converted, tmp_path, fake_ssrs)
    good = (pack / "burst.config.json").read_text(encoding="utf-8")
    (pack / "burst.config.json").write_text('{"ReportServer": "x",}', encoding="utf-8")
    proc = _run(pack)
    assert proc.returncode == 2 and "not valid JSON" in proc.stderr, proc.stderr
    (pack / "burst.config.json").write_text(good, encoding="utf-8")
    pack = _unpack(converted, tmp_path / "p2", fake_ssrs, FileNamePattern="<BURST_KEY>_{date:yyyy'MM}.pdf") \
        if (tmp_path / "p2").mkdir() is None else None
    proc = _run(pack)
    assert proc.returncode == 2 and "FileNamePattern is invalid" in proc.stderr, proc.stdout + proc.stderr
    assert fake_ssrs.rendered == []
    assert any(e["event"] == "config_invalid" for e in _events(tmp_path / "p2"))
    # a pack shipped WITHOUT a key list (key not a field) refuses to run
    pack3 = _unpack(converted, tmp_path / "p3", fake_ssrs, BurstListPath="") \
        if (tmp_path / "p3").mkdir() is None else None
    proc = _run(pack3)
    assert proc.returncode == 2 and "BurstListPath" in proc.stderr and "key-list" in proc.stderr


@needs_powershell
def test_overwrite_false_as_text_is_honoured_and_device_names_become_files(converted, tmp_path, fake_ssrs):
    fake_ssrs.csv = "BURST_KEY\r\nA-1\r\nCON\r\nnul.txt\r\n"
    pack = _unpack(converted, tmp_path, fake_ssrs, Overwrite="false")
    out = tmp_path / "out"
    out.mkdir()
    (out / "A-1.pdf").write_bytes(b"OLD")
    proc = _run(pack)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (out / "A-1.pdf").read_bytes() == b"OLD", '"Overwrite": "false" (a string) must not overwrite'
    assert any(e["event"] == "exists_kept" for e in _events(tmp_path))
    assert (out / "_CON.pdf").exists() and (out / "_nul.txt.pdf").exists(), sorted(p.name for p in out.iterdir())


@needs_powershell
def test_render_format_drives_the_file_extension(converted, tmp_path, fake_ssrs):
    """A hand-edited config that still says '.pdf' with an Excel format
    used to write OOXML bytes into *.pdf files."""
    pack = _unpack(converted, tmp_path, fake_ssrs, RenderFormat="EXCELOPENXML")
    cfg = json.loads((pack / "burst.config.json").read_text(encoding="utf-8"))
    assert cfg["FileNamePattern"] == "<BURST_KEY>.pdf"
    proc = _run(pack, "-TestLimit", "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "out" / "A-1.xlsx").exists() and not (tmp_path / "out" / "A-1.pdf").exists()
    assert fake_ssrs.rendered[0][1] == "EXCELOPENXML"


def _tablix_bound_to(ds_name: str) -> str:
    return (
        '<Tablix Name="T_' + ds_name + '"><TablixBody><TablixColumns><TablixColumn><Width>2in</Width>'
        '</TablixColumn></TablixColumns><TablixRows><TablixRow><Height>0.25in</Height><TablixCells>'
        '<TablixCell><CellContents><Textbox Name="TB_' + ds_name + '"><Paragraphs><Paragraph><TextRuns>'
        '<TextRun><Value>=Fields!Perm_Num.Value</Value></TextRun></TextRuns></Paragraph></Paragraphs>'
        '</Textbox></CellContents></TablixCell></TablixCells></TablixRow></TablixRows></TablixBody>'
        '<TablixColumnHierarchy><TablixMembers><TablixMember /></TablixMembers></TablixColumnHierarchy>'
        '<TablixRowHierarchy><TablixMembers><TablixMember><Group Name="G_' + ds_name + '" /></TablixMember>'
        '</TablixMembers></TablixRowHierarchy><DataSetName>' + ds_name + '</DataSetName>'
        '<Top>0in</Top><Left>0in</Left><Height>0.25in</Height><Width>2in</Width></Tablix>'
    )


_MASTER_DETAIL_RDL = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2008/01/reportdefinition"'
    ' xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner">\n'
    '  <DataSources><DataSource Name="DS"><DataSourceReference>/DS/Oracle</DataSourceReference>'
    '<rd:SecurityType>None</rd:SecurityType></DataSource></DataSources>\n'
    '  <DataSets>\n'
    '    <DataSet Name="Q_MASTER"><Query><DataSourceName>DS</DataSourceName>'
    '<CommandText>SELECT m.Perm_Num, m.Holder FROM Permits m</CommandText></Query>'
    '<Fields><Field Name="Perm_Num"><DataField>Perm_Num</DataField></Field>'
    '<Field Name="Holder"><DataField>Holder</DataField></Field></Fields></DataSet>\n'
    '    <DataSet Name="Q_DETAIL"><Query><DataSourceName>DS</DataSourceName>'
    '<CommandText>SELECT d.Perm_Num, d.Line FROM Lines d</CommandText></Query>'
    '<Fields><Field Name="Perm_Num"><DataField>Perm_Num</DataField></Field>'
    '<Field Name="Line"><DataField>Line</DataField></Field></Fields></DataSet>\n'
    '  </DataSets>\n'
    '  <EmbeddedImages><EmbeddedImage Name="Seal"><MIMEType>image/png</MIMEType>'
    '<ImageData>iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==</ImageData>'
    '</EmbeddedImage></EmbeddedImages>\n'
    '  <Body><ReportItems>' + _tablix_bound_to("Q_DETAIL") + '</ReportItems><Height>1in</Height></Body>\n'
    '  <Width>7.5in</Width>\n'
    '  <Page><PageHeader><Height>0.5in</Height><PrintOnFirstPage>true</PrintOnFirstPage>'
    '<PrintOnLastPage>true</PrintOnLastPage><ReportItems><Image Name="Seal"><Source>Embedded</Source>'
    '<Value>Seal</Value><Sizing>FitProportional</Sizing><Top>0in</Top><Left>0in</Left><Height>0.5in</Height>'
    '<Width>0.5in</Width></Image></ReportItems></PageHeader>'
    '<PageHeight>11in</PageHeight><PageWidth>8.5in</PageWidth><LeftMargin>0.5in</LeftMargin>'
    '<RightMargin>0.5in</RightMargin><TopMargin>0.5in</TopMargin><BottomMargin>0.5in</BottomMargin></Page>\n'
    '</Report>\n'
)


def test_every_dataset_declaring_the_key_is_filtered_and_the_list_follows_the_body():
    """Master-detail: the key is in BOTH datasets and the body prints the
    detail (declared second). The filter used to land only on the first
    dataset, so every per-key file still carried every record's detail
    rows; the key list was built from the wrong dataset too."""
    info = {"is_bursting": True, "burst_key_field": "Perm_Num", "filename_pattern": "<Perm_Num>.pdf"}
    out, meta = bp.inject_burst_key_filter(_MASTER_DETAIL_RDL, info)
    assert meta["injected"] is True
    assert meta["datasets"] == ["Q_MASTER", "Q_DETAIL"]
    assert meta["dataset"] == "Q_DETAIL", "the dataset the body prints wins over document order"
    assert out.count("<Filters>") == 2
    for ds in ("Q_MASTER", "Q_DETAIL"):
        block = re.search(r'<DataSet Name="' + ds + '">.*?</DataSet>', out, re.S).group(0)
        assert "<Filters>" in block and "Fields!Perm_Num.Value" in block, ds
    assert not _xsd_errors(out), _xsd_errors(out)[:3]
    assert publish_violations(out) == []
    pack = bp.prepare("MD", out, info)
    lst = pack["burst_list_rdl"]
    assert '<DataSet Name="Q_DETAIL">' in lst and '<DataSet Name="Q_MASTER">' not in lst
    assert not _xsd_errors(lst), _xsd_errors(lst)[:3]
    assert pack["meta"]["list_violations"] == []
    assert pack["config"]["FileNamePattern"] == "<BURST_KEY>.pdf"


def test_key_list_report_carries_page_geometry_only():
    """The main report's page bands reference images and datasets the list
    report does not have (an embedded seal here); the companion used to copy
    the whole <Page> and would not publish."""
    info = {"is_bursting": True, "burst_key_field": "Perm_Num"}
    out, meta = bp.inject_burst_key_filter(_MASTER_DETAIL_RDL, info)
    lst = bp.build_burst_list_rdl(out, info, dict(meta, pattern="<BURST_KEY>.pdf"))
    assert "<PageHeader>" not in lst and "Seal" not in lst
    assert "<PageHeight>11in</PageHeight>" in lst and "<LeftMargin>0.5in</LeftMargin>" in lst
    assert not _xsd_errors(lst), _xsd_errors(lst)[:3]
    assert publish_violations(lst) == []


def test_key_list_sql_uses_the_real_column_name():
    rdl = _MASTER_DETAIL_RDL.replace('<Field Name="Perm_Num"><DataField>Perm_Num</DataField>',
                                     '<Field Name="Perm_"><DataField>Perm#</DataField>')
    info = {"is_bursting": True, "burst_key_field": "Perm#", "filename_pattern": "<Perm#>.pdf"}
    out, meta = bp.inject_burst_key_filter(rdl, info)
    assert meta["injected"] and meta["field"] == "Perm_" and meta["datafield"] == "Perm#"
    assert "Fields!Perm_.Value" in out                      # the RDL field name inside the report
    sql = bp.burst_list_sql(out, meta)
    assert 'SELECT DISTINCT "Perm#" AS BURST_KEY' in sql   # the SQL column outside it
    pack = bp.prepare("MD", out, info)
    assert pack["config"]["FileNamePattern"] == "<BURST_KEY>.pdf"
    assert pack["config"]["KeyField"] == "Perm_"


def test_report_derived_text_in_the_checklist_is_html_safe():
    """The checklist bodies are innerHTML'd by the UI; the burst key comes
    from the uploaded report."""
    info = {"is_bursting": True, "burst_key_field": '<img src=x onerror="alert(1)">'}
    steps = bp.build_checklist("R", info, {"injected": True, "field": "x", "dataset": "Q"}, {"columns": [], "params": [], "unknown": []})
    text = "\n".join(s["body"] for s in steps)
    assert "<img" not in text and "&lt;img" in text
    assert "<code>" in text, "the checklist's own markup stays"


def test_form_pattern_override_drives_the_key_list_and_readme(converted):
    b = converted["bursting"]
    pack = bp.prepare("SAMPLE_BURST", converted["rdl_xml"], b,
                      {"FileNamePattern": "<Recipient_Name>_<Doc_No>_<P_RUN_YEAR>.pdf", "RenderFormat": "WORDOPENXML"})
    assert pack["meta"]["pattern"] == "<BURST_KEY>_<Doc_No>_<P_RUN_YEAR>.docx"
    assert pack["columns"] == {"columns": ["Doc_No"], "params": ["P_RUN_YEAR"], "unknown": []}
    assert '<Textbox Name="Doc_No">' in pack["burst_list_rdl"]
    assert "<Doc_No>" in pack["readme"] and "P_RUN_YEAR" in pack["config"]["ReportParameters"]
    assert pack["config"]["FileNamePattern"] == "<BURST_KEY>_<Doc_No>_<P_RUN_YEAR>.docx"

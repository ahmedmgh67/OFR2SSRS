"""Tests for the plug-and-play bursting tab (auto-fill + Burst Pack zip)."""
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from converter.models import (  # noqa: E402
    ParsedReport, DataQuery, DataItem, ReportParameter, FormulaColumn,
)
from converter import bursting as b  # noqa: E402


def _make_bursting_report(main_table="Permit", email_col="Recipient_Email",
                          declare_mail=True):
    """A bursting source.

    ``declare_mail`` controls whether the source DECLARES an Oracle
    distribution <mail> destination bound to ``email_col``. Recipient detection
    reads that declaration, so a fixture that wants a recipient column detected
    has to declare one -- exactly like a real Oracle report.
    """
    r = ParsedReport(name="TEST_REPORT")
    r.parameters = [ReportParameter(name="P_AS_PATH"), ReportParameter(name="P_PERM_NUM")]
    q = DataQuery(name="Q_MAIN")
    q.tsql = "SELECT a.Perm_Num, a." + email_col + ", a.Site_Name FROM dbo." + main_table + " AS a"
    q.items = [DataItem(name="Perm_Num"), DataItem(name=email_col), DataItem(name="Site_Name")]
    r.queries = [q]
    r.formulas = [FormulaColumn(name="CF_File_F", plsql_body="RETURN(:P_AS_PATH || :Perm_Num || '.pdf');")]
    r.triggers = []
    r.raw_xml = _destinations_xml(email_col) if declare_mail else "<report/>"
    return r


def _destinations_xml(email_col):
    """The distribution instructions an Oracle source declares: one per-row
    destination, with the recipient slot bound to a column by reference."""
    return (
        '<report name="TEST_REPORT"><destinations>'
        '<foreach>'
        '<mail id="d1" to="&amp;&lt;' + email_col + '&gt;" subject="x">'
        '<include src="mainSection"/>'
        '</mail>'
        '<file id="d2" name="&amp;&lt;CF_File_F&gt;.pdf" format="pdf" '
        'instance="this"/>'
        '</foreach>'
        '</destinations></report>'
    )


def _strip_header(sql):
    """Return only the live SQL body (after the leading '--' comments)."""
    out = []
    started = False
    for ln in sql.splitlines():
        s = ln.strip()
        if not started:
            if not s or s.startswith("--"):
                continue
            started = True
        out.append(ln)
    return "\n".join(out)


def test_email_burst_query_autofills_main_table():
    r = _make_bursting_report(main_table="Permit")
    info = b.detect_bursting(r)
    sql = b.build_email_burst_query(r, info)
    body = _strip_header(sql)
    assert "FROM dbo.Permit AS p" in body
    assert "<MainTable>" not in body
    assert "<MainTable>      -> Permit" in sql


def test_email_burst_query_autofills_email_column():
    """The recipient column comes from the DECLARED <mail> recipient slot --
    and the name it happens to carry is irrelevant, so the same declaration
    binds a column with no address-like word in it at all."""
    for column in ("Recipient_Email", "Zzqx_Destination_Slot"):
        r = _make_bursting_report(email_col=column)
        info = b.detect_bursting(r)
        assert b._detect_email_column(r) == column
        sql = b.build_email_burst_query(r, info)
        body = _strip_header(sql)
        assert column in body
        assert "<RecipientEmail>" not in body


def test_recipient_detection_needs_a_declaration_not_a_name():
    """FAIL CLOSED: an address-shaped column NAME with no declared mail
    destination is never promoted into the recipient slot. This is the guard
    that keeps a postal-address column out of an SMTP To: header."""
    for column in ("Recipient_Email", "Site_Address", "Mailing_Addr",
                   "Contact_Email"):
        r = _make_bursting_report(email_col=column, declare_mail=False)
        assert b._detect_email_column(r) is None
        name_col, email_col = b._pick_recipient_columns(r)
        assert email_col == "Email_Or_Path"
        assert column not in (name_col, email_col)
        sql = b.build_email_burst_query(r, b.detect_bursting(r))
        assert "<RecipientEmail>" in _strip_header(sql)
        assert "NOT DETECTED" in sql


def test_declared_distribution_is_read_from_plsql_built_markup():
    """Oracle reports BUILD the distribution document with PL/SQL string
    concatenation; the reader must see the element that spans those literals."""
    r = _make_bursting_report(declare_mail=False)
    r.triggers = [type("T", (), {"name": "AfterPForm", "body": (
        "BEGIN\n"
        "  Text_IO.Put_Line(File => f, Item => '<file '\n"
        "      || 'id=\"d\" '\n"
        "      || 'name=\"&amp;&lt;CF_File_F&gt;.pdf\" '\n"
        "      || 'instance=\"this\">');\n"
        "END;")})()]
    decl = b.declared_distribution(r)
    assert decl["declared"] is True
    assert decl["per_row"] is True
    assert [x.upper() for x in decl["file_refs"]] == ["CF_FILE_F"]


def test_email_burst_query_leaves_placeholder_when_no_email_col():
    r = ParsedReport(name="X")
    r.parameters = [ReportParameter(name="P_AS_PATH")]
    q = DataQuery(name="Q")
    q.tsql = "SELECT id FROM dbo.Things"
    q.items = [DataItem(name="Id")]
    r.queries = [q]
    r.formulas = [FormulaColumn(name="CF_File_F", plsql_body="RETURN(:P_AS_PATH || :Id);")]
    r.triggers = []
    info = b.detect_bursting(r)
    sql = b.build_email_burst_query(r, info)
    body = _strip_header(sql)
    assert "<RecipientEmail>" in body
    assert "NOT DETECTED" in sql


def test_build_burst_pack_zip_contents_and_overrides():
    r = _make_bursting_report()
    info = b.detect_bursting(r)
    overrides = {
        "Deliver":    "both",
        "SmtpServer": "smtp.example.test",
        "SmtpPort":   587,
        "SmtpFrom":   "reports@example.test",
        "Subject":    "{ReportName} - {BurstKey}",
        "Body":       "Body for {BurstKey}",
        "report_server_url": "http://srv/ReportServer?/Env/Letters",
    }
    blob = b.build_burst_pack_zip(r, "<Report/>", info, overrides)
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = sorted(z.namelist())
    # "<Report/>" declares no dataset: no per-key filter and therefore no
    # key-list report -- the pack still ships and the README says why.
    assert names == sorted([
        "TEST_REPORT.rdl", "burst.config.json", "Run-Burst.ps1",
        "README.md", "service-account-setup.md",
    ])
    cfg = json.loads(z.read("burst.config.json"))
    for k in ("Deliver", "SmtpServer", "SmtpPort", "SmtpFrom", "Subject", "Body"):
        assert cfg[k] == overrides[k]
    assert cfg["ReportServer"] == "http://srv/ReportServer"
    assert cfg["ReportPath"] == "/Env/Letters/TEST_REPORT"
    # no key-list report could be built -> the driver must refuse to run
    # (exit 2) rather than ask the server for an item that does not exist
    assert cfg["BurstListPath"] == ""
    assert cfg["BindParameter"] == "P_O2S_BURST_KEY"
    ps = z.read("Run-Burst.ps1")
    assert ps.startswith(b"\xef\xbb\xbf"), "the driver ships with a UTF-8 BOM"
    text = ps[3:].decode("ascii")
    assert "TEST_REPORT" in text and "P_O2S_BURST_KEY" in text
    readme = z.read("README.md").decode("utf-8")
    assert "Run-Burst.ps1" in readme and "TEST_REPORT_BurstList.rdl" in readme
    assert "not a field of any dataset" in readme


def _rdl_with_dataset(sql="SELECT a.Perm_Num, a.Site_Name FROM Permit a"):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2008/01/reportdefinition">\n'
        '  <DataSources><DataSource Name="DS"><DataSourceReference>/DS/Oracle</DataSourceReference>'
        '</DataSource></DataSources>\n'
        '  <DataSets>\n    <DataSet Name="Q_MAIN">\n      <Query><DataSourceName>DS</DataSourceName>'
        '<CommandText>' + sql + '</CommandText></Query>\n'
        '      <Fields><Field Name="Perm_Num"><DataField>Perm_Num</DataField></Field>'
        '<Field Name="Site_Name"><DataField>Site_Name</DataField></Field></Fields>\n'
        '    </DataSet>\n  </DataSets>\n'
        '  <Body><ReportItems/><Height>1in</Height></Body>\n  <Width>7.5in</Width>\n</Report>\n'
    )


def test_burst_pack_driver_carries_no_sql_and_the_key_list_sql_is_the_reports_own():
    """The driver never touches a database: the key list is the report server
    rendering the companion report. The SQL shown in the tab is informational
    (for an Enterprise data-driven subscription) and is derived from the
    report's OWN CommandText, never from a table name we guess."""
    r = _make_bursting_report()
    info = b.detect_bursting(r)
    blob = b.build_burst_pack_zip(r, _rdl_with_dataset(), info)
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert "TEST_REPORT_BurstList.rdl" in z.namelist()
    ps = z.read("Run-Burst.ps1")[3:].decode("ascii")
    assert "SELECT " not in ps and "Invoke-Sqlcmd" not in ps and "Import-Module" not in ps
    readme = z.read("README.md").decode("utf-8")
    assert "SELECT DISTINCT Perm_Num AS BURST_KEY" in readme
    assert "FROM Permit a" in readme
    cfg = json.loads(z.read("burst.config.json"))
    assert cfg["BurstListPath"].endswith("/TEST_REPORT_BurstList")
    assert cfg["KeyField"] == "Perm_Num"
    # the Oracle key token resolves: the key list carries the key as BURST_KEY
    assert cfg["FileNamePattern"] == "<BURST_KEY>.pdf"



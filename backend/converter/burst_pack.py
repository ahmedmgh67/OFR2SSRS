"""Burst Pack: the SSRS side of Oracle Reports "distribution" (bursting).

Oracle ran a bursting report ONCE and wrote one output file per record
(one PDF per permit / invoice / grantee), naming each file from a
formula and dropping it in a folder (``P_AS_PATH``) or mailing it. SSRS
cannot split one render into many files; the equivalent is one render per
key. Below Enterprise edition SSRS has no built-in loop for that, so the
pack ships one -- and it is built so that NOTHING has to be installed on
any server and no SQL is ever rewritten:

  * the main RDL gains ONE hidden parameter (``P_O2S_BURST_KEY``) and ONE
    dataset filter on EVERY dataset that declares the burst key: set the
    parameter and the report shows that key's rows; leave it empty (the
    default) and the report behaves exactly as before. Row order, SQL,
    binds: untouched.
  * a companion ``<Report>_BurstList.rdl`` groups the key dataset by the
    key -- rendered as CSV by the report server it yields the exact set of
    keys the report would print, through the server's own data source.
  * ``Run-Burst.ps1`` asks the report server for that CSV, then renders the
    main report once per key through SSRS URL access
    (``?/path&rs:Format=PDF&P_O2S_BURST_KEY=...``) and saves each file under
    the Oracle file name. Windows PowerShell 5.1 built-ins only.

Every builder here reads the report's OWN declarations (its datasets,
fields, parameters, the detected key and file pattern). Nothing names a
customer column. Verified by the fatal gates on the injected RDL, by the
real PowerShell parser on the driver, and by an end-to-end run of the
driver against a fake report server (tests/test_burst_pack.py).
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BURST_PARAM = "P_O2S_BURST_KEY"
LIST_SUFFIX = "_BurstList"
DRIVER_NAME = "Run-Burst.ps1"
KEY_COLUMN = "BURST_KEY"

_DRIVER_TEMPLATE_PATH = Path(__file__).resolve().parent / "burst_driver.ps1.txt"

_DS_RE = re.compile(r'(<DataSet Name="([^"]+)">)(.*?)(</DataSet>)', re.S)
_FIELD_RE = re.compile(r'<Field Name="([^"]+)">\s*<DataField>([^<]*)</DataField>', re.S)
_FILTERS_RE = re.compile(r"\s*<Filters>.*?</Filters>", re.S)
_TOKEN_RE = re.compile(r"<([^<>]+)>")
_DEVICE_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.I)

# Oracle TO_CHAR date-mask tokens -> .NET custom format, longest first. Fixed
# by Oracle's mask grammar: a file-name pattern piece that is entirely made
# of these (a TO_CHAR(SYSDATE, 'YYYYMMDD') literal) becomes {date:...}.
_DATE_MASK = (("YYYY", "yyyy"), ("RRRR", "yyyy"), ("HH24", "HH"), ("MM", "MM"),
              ("DD", "dd"), ("HH", "hh"), ("MI", "mm"), ("SS", "ss"))

# SSRS rendering-extension names -> the file extension their bytes need.
# The driver applies the same map; the pack's pattern is pre-adjusted so
# what the UI shows is what gets written.
_FORMAT_EXT = {"PDF": ".pdf", "EXCELOPENXML": ".xlsx", "WORDOPENXML": ".docx",
               "EXCEL": ".xls", "WORD": ".doc", "CSV": ".csv", "XML": ".xml",
               "MHTML": ".mhtml", "IMAGE": ".tif"}
# one or more trailing known extensions (a source that names ".pdf" in both
# the formula and the destination arrives as "<key>.pdf.pdf")
_KNOWN_EXT_RE = re.compile(r"(\.(pdf|xlsx|xls|docx|doc|csv|xml|mhtml|tif|tiff))+$", re.I)


def _safe_name(text: str) -> str:
    """A report name that is safe in EVERY context it is spliced into
    (PowerShell comment/string, JSON, Markdown, a file name)."""
    s = re.sub(r"[^A-Za-z0-9_ .\-]", "_", str(text or ""))
    s = re.sub(r"\.{2,}", ".", s).strip(" .")   # no '..' anywhere, ever
    if _DEVICE_RE.match(s.split(".", 1)[0] or ""):
        s = "_" + s                              # CON.rdl is a device, not a file
    return s or "report"


def _xml_text(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _is_ident(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s or ""))


# ---------------------------------------------------------------------------
# File-name pattern
# ---------------------------------------------------------------------------

def normalize_pattern(pattern: str) -> str:
    """Turn Oracle date-mask literals into ``{date:...}`` tokens.

    The detector keeps the literal pieces of the Oracle file formula, so a
    ``TO_CHAR(SYSDATE, 'YYYYMMDD')`` arrives as the bare text ``YYYYMMDD``.
    A piece that is ENTIRELY mask tokens (plus - _ . separators) is a date
    the driver must fill at run time; anything else is left as typed."""
    if not pattern:
        return pattern

    def _mask_piece(piece: str) -> Optional[str]:
        out, i, seen = [], 0, False
        while i < len(piece):
            for tok, net in _DATE_MASK:
                if piece.upper().startswith(tok, i):
                    out.append(net)
                    i += len(tok)
                    seen = True
                    break
            else:
                if piece[i] in "-_.":
                    out.append(piece[i])
                    i += 1
                else:
                    return None
        return "".join(out) if seen else None

    parts = re.split(r"(<[^<>]+>|\.[A-Za-z0-9]+$)", pattern)
    fixed = []
    for part in parts:
        if not part or part.startswith("<") or part.startswith("."):
            fixed.append(part)
            continue
        m = re.match(r"^(_?)([A-Za-z0-9\-_.]+?)(_?)$", part)
        net = _mask_piece(m.group(2)) if m else None
        if net:
            fixed.append(m.group(1) + "{date:" + net + "}" + m.group(3))
        else:
            fixed.append(part)
    return "".join(fixed)


def pattern_tokens(pattern: str) -> List[str]:
    return [t.strip() for t in _TOKEN_RE.findall(pattern or "") if t.strip()]


def key_token_to_column(pattern: str, *aliases: str) -> str:
    """``<KeyColumn>`` in the Oracle pattern names the key; the key list
    carries it as ``BURST_KEY``. Rewrite every alias (the RDL field name,
    the SQL column, the detector's key) to the token that resolves."""
    out = pattern or ""
    for a in aliases:
        if a:
            out = re.sub(r"<\s*" + re.escape(a) + r"\s*>", "<" + KEY_COLUMN + ">", out, flags=re.I)
    return out


def with_format_extension(pattern: str, render_format: str) -> str:
    ext = _FORMAT_EXT.get((render_format or "").upper())
    if not pattern or not ext:
        return pattern
    if _KNOWN_EXT_RE.search(pattern):
        return _KNOWN_EXT_RE.sub(ext, pattern)
    return pattern + ext


# ---------------------------------------------------------------------------
# The main RDL: one hidden parameter + one dataset filter per key dataset
# ---------------------------------------------------------------------------

def find_key_datasets(rdl_xml: str, key: str) -> List[Tuple[str, str, str, "re.Match"]]:
    """Every dataset whose Fields declare the burst key, in document order:
    ``(dataset_name, field_name, data_field, match)``. The key is matched
    against DataField or Name, case-insensitively."""
    out = []
    if not rdl_xml or not key:
        return out
    ku = key.strip().upper()
    for m in _DS_RE.finditer(rdl_xml):
        for fname, dfield in _FIELD_RE.findall(m.group(3)):
            if dfield.strip().upper() == ku or fname.strip().upper() == ku:
                out.append((m.group(2), fname, dfield.strip(), m))
                break
    return out


def _body_bound_datasets(rdl_xml: str) -> List[str]:
    """DataSetName values bound by the body's data regions, in order."""
    b = re.search(r"<Body>.*?</Body>", rdl_xml or "", re.S)
    if not b:
        return []
    return [n.strip() for n in re.findall(r"<DataSetName>([^<]*)</DataSetName>", b.group(0))]


def find_key_dataset(rdl_xml: str, key: str):
    """The dataset the key list is built from: the first key-declaring
    dataset a BODY data region binds to (that is the one the report prints),
    else the first key-declaring dataset in document order. Returns
    ``(dataset_name, field_name, match)`` or ``None``."""
    hits = find_key_datasets(rdl_xml, key)
    if not hits:
        return None
    by_name = {h[0]: h for h in hits}
    for bound in _body_bound_datasets(rdl_xml):
        if bound in by_name:
            h = by_name[bound]
            return h[0], h[1], h[3]
    h = hits[0]
    return h[0], h[1], h[3]


def dataset_fields(rdl_xml: str, ds_name: str) -> List[str]:
    for m in _DS_RE.finditer(rdl_xml):
        if m.group(2) == ds_name:
            return [f for f, _d in _FIELD_RE.findall(m.group(3))]
    return []


def report_parameter_names(rdl_xml: str) -> List[str]:
    return re.findall(r'<ReportParameter Name="([^"]+)">', rdl_xml or "")


def _filter_block(field_name: str) -> str:
    return (
        "\n      <Filters>\n"
        "        <Filter>\n"
        "          <FilterExpression>=IsNothing(Parameters!" + BURST_PARAM + ".Value)"
        " OrElse (CStr(Fields!" + field_name + ".Value) = Parameters!"
        + BURST_PARAM + ".Value)</FilterExpression>\n"
        "          <Operator>Equal</Operator>\n"
        "          <FilterValues>\n"
        "            <FilterValue>=True</FilterValue>\n"
        "          </FilterValues>\n"
        "        </Filter>\n"
        "      </Filters>\n    "
    )


def _param_block() -> str:
    return (
        "    <ReportParameter Name=\"" + BURST_PARAM + "\">\n"
        "      <DataType>String</DataType>\n"
        "      <Nullable>true</Nullable>\n"
        "      <DefaultValue>\n"
        "        <Values>\n"
        "          <Value>=Nothing</Value>\n"
        "        </Values>\n"
        "      </DefaultValue>\n"
        "      <AllowBlank>true</AllowBlank>\n"
        "      <Prompt>" + BURST_PARAM + "</Prompt>\n"
        "      <Hidden>true</Hidden>\n"
        "    </ReportParameter>\n"
    )


def _add_param(rdl_xml: str) -> Optional[str]:
    """The hidden parameter, appended to <ReportParameters> or inserted
    before <Body>. None when there is nowhere to put it."""
    if "</ReportParameters>" in rdl_xml:
        return rdl_xml.replace("</ReportParameters>", _param_block() + "  </ReportParameters>", 1)
    body_at = rdl_xml.find("\n  <Body>")
    if body_at < 0:
        body_at = rdl_xml.find("<Body>")
    if body_at < 0:
        return None
    block = "\n  <ReportParameters>\n" + _param_block() + "  </ReportParameters>"
    return rdl_xml[:body_at] + block + rdl_xml[body_at:]


def inject_burst_key_filter(rdl_xml: str, info: dict) -> Tuple[str, dict]:
    """Give the main RDL a per-key filter without touching its SQL.

    Returns ``(rdl_xml, meta)``. The filter goes on EVERY dataset that
    declares the key (a master-detail report must filter both sides);
    ``meta["dataset"]`` names the one the key list is built from.
    ``meta["injected"]`` is False, with a named reason, when the key is not a
    field of any dataset -- the hidden parameter is still added (harmless,
    and the driver's URL parameter is then accepted the moment a filter is
    added by hand) but the pack ships without a key-list report.
    Idempotent: an RDL that already carries the parameter is returned as is."""
    meta = {"injected": False, "param_injected": False, "bind_parameter": BURST_PARAM,
            "dataset": None, "field": None, "datafield": None, "datasets": [], "reason": ""}
    key = (info or {}).get("burst_key_field")
    if not rdl_xml or not (info or {}).get("is_bursting") or not key:
        meta["reason"] = "not a bursting report"
        return rdl_xml, meta
    hits = find_key_datasets(rdl_xml, key)
    hits = [h for h in hits if _is_ident(h[1])]
    if 'Name="' + BURST_PARAM + '"' in rdl_xml:
        pref = find_key_dataset(rdl_xml, key)
        meta.update(param_injected=True, injected=bool(hits and pref),
                    dataset=pref[0] if pref else None, field=pref[1] if pref else None,
                    datafield=next((h[2] for h in hits if pref and h[0] == pref[0]), None),
                    datasets=[h[0] for h in hits], reason="already present")
        return rdl_xml, meta
    if not hits:
        out = _add_param(rdl_xml)
        meta["reason"] = ("the burst key " + key + " is not a field of any "
                          "dataset, so the report cannot be filtered to one key")
        if out is None:
            return rdl_xml, meta
        meta["param_injected"] = True
        return out, meta
    # 1. the dataset filter, one per key dataset (spliced last-to-first so
    #    earlier offsets stay valid)
    out = rdl_xml
    for _ds, fname, _df, m in reversed(hits):
        out = out[:m.start(4)] + _filter_block(fname) + out[m.start(4):]
    # 2. the hidden parameter
    with_param = _add_param(out)
    if with_param is None:
        meta["reason"] = "no <Body> element to anchor the parameter block"
        return rdl_xml, meta
    out = with_param
    pref = find_key_dataset(out, key)
    ds_name, field_name = (pref[0], pref[1]) if pref else (hits[0][0], hits[0][1])
    datafield = next((h[2] for h in hits if h[0] == ds_name), hits[0][2])
    meta.update(injected=True, param_injected=True, dataset=ds_name, field=field_name,
                datafield=datafield, datasets=[h[0] for h in hits],
                reason="filter on " + ", ".join(h[0] + "." + h[1] for h in hits))
    return out, meta


# ---------------------------------------------------------------------------
# The companion key-list RDL
# ---------------------------------------------------------------------------

def _textbox(name: str, value_expr: str) -> str:
    return (
        "                <TablixCell>\n"
        "                  <CellContents>\n"
        "                    <Textbox Name=\"" + name + "\">\n"
        "                      <CanGrow>true</CanGrow>\n"
        "                      <Paragraphs>\n"
        "                        <Paragraph>\n"
        "                          <TextRuns>\n"
        "                            <TextRun>\n"
        "                              <Value>" + _xml_text(value_expr) + "</Value>\n"
        "                              <Style>\n"
        "                                <FontSize>9pt</FontSize>\n"
        "                              </Style>\n"
        "                            </TextRun>\n"
        "                          </TextRuns>\n"
        "                        </Paragraph>\n"
        "                      </Paragraphs>\n"
        "                      <Style>\n"
        "                        <Border>\n"
        "                          <Style>None</Style>\n"
        "                        </Border>\n"
        "                      </Style>\n"
        "                    </Textbox>\n"
        "                  </CellContents>\n"
        "                </TablixCell>\n"
    )


def list_columns(rdl_xml: str, info: dict, meta: dict) -> dict:
    """Which ``<Token>``s of the file pattern are dataset FIELDS (they ride
    along in the key list), which are report PARAMETERS (the driver fills
    them from its config), and which are unknown (left as typed). The key
    itself is the ``BURST_KEY`` column."""
    ds = meta.get("dataset")
    fields = dataset_fields(rdl_xml, ds) if ds else []
    fmap = {f.upper(): f for f in fields}
    pmap = {p.upper(): p for p in report_parameter_names(rdl_xml)}
    key_names = {(meta.get("field") or "").upper(), (meta.get("datafield") or "").upper(),
                 ((info or {}).get("burst_key_field") or "").upper(), KEY_COLUMN}
    cols, params, unknown = [], [], []
    for tok in pattern_tokens(meta.get("pattern") or ""):
        tu = tok.upper()
        if tu in key_names:
            continue
        if tu in fmap:
            if fmap[tu] not in cols:
                cols.append(fmap[tu])
        elif tu in pmap:
            if pmap[tu] not in params:
                params.append(pmap[tu])
        else:
            unknown.append(tok)
    return {"columns": cols, "params": params, "unknown": unknown}


_PAGE_GEOMETRY = ("PageHeight", "PageWidth", "InteractiveHeight", "InteractiveWidth",
                  "LeftMargin", "RightMargin", "TopMargin", "BottomMargin")


def _bare_page(rdl_xml: str) -> str:
    """The main report's page GEOMETRY only. Its header/footer bands carry
    images and expressions scoped to datasets the list report does not have,
    and a CSV render prints no chrome anyway."""
    page = re.search(r"<Page>.*?</Page>", rdl_xml, re.S)
    src = page.group(0) if page else ""
    inner = ""
    for tag in _PAGE_GEOMETRY:
        m = re.search(r"<" + tag + r">([^<]*)</" + tag + ">", src)
        if m:
            inner += "    <" + tag + ">" + m.group(1) + "</" + tag + ">\n"
    if not inner:
        inner = "    <PageHeight>11in</PageHeight>\n    <PageWidth>8.5in</PageWidth>\n"
    return "  <Page>\n" + inner + "  </Page>"


def build_burst_list_rdl(rdl_xml: str, info: dict, meta: dict) -> Optional[str]:
    """The companion report: the key dataset (unfiltered) under a tablix
    grouped and sorted by the key. One row per distinct key; the first
    column is ``BURST_KEY`` (as text), then any file-pattern columns.
    Rendered as CSV by the report server it IS the key list."""
    if not meta.get("injected") or not meta.get("dataset"):
        return None
    ds_name, field = meta["dataset"], meta["field"]
    ds_block = None
    for m in _DS_RE.finditer(rdl_xml):
        if m.group(2) == ds_name:
            ds_block = m.group(0)
            break
    if ds_block is None:
        return None
    ds_block = _FILTERS_RE.sub("", ds_block)          # the list is never filtered

    root = re.search(r"<Report\b[^>]*>", rdl_xml)
    dsrc = re.search(r"<DataSources>.*?</DataSources>", rdl_xml, re.S)
    params = re.search(r"<ReportParameters>.*?</ReportParameters>", rdl_xml, re.S)
    code = re.search(r"\n  <Code>.*?</Code>", rdl_xml, re.S)
    width = re.search(r"\n  <Width>[^<]*</Width>", rdl_xml)
    lang = re.search(r"\n  <Language>[^<]*</Language>", rdl_xml)
    if not (root and dsrc):
        return None

    cols = list_columns(rdl_xml, info, meta)["columns"]
    names = [KEY_COLUMN] + [c for c in cols if c.upper() != KEY_COLUMN]
    col_w = 2.5
    tcols = "".join("            <TablixColumn>\n              <Width>%.2fin</Width>\n"
                    "            </TablixColumn>\n" % col_w for _ in names)
    cells = _textbox(KEY_COLUMN, "=CStr(Fields!" + field + ".Value)")
    for c in names[1:]:
        cells += _textbox(c, "=CStr(First(Fields!" + c + ".Value))")
    members = "".join("          <TablixMember />\n" for _ in names)
    tablix = (
        "  <Body>\n"
        "    <ReportItems>\n"
        "      <Tablix Name=\"Tablix_BurstList\">\n"
        "        <TablixBody>\n"
        "          <TablixColumns>\n" + tcols +
        "          </TablixColumns>\n"
        "          <TablixRows>\n"
        "            <TablixRow>\n"
        "              <Height>0.25in</Height>\n"
        "              <TablixCells>\n" + cells +
        "              </TablixCells>\n"
        "            </TablixRow>\n"
        "          </TablixRows>\n"
        "        </TablixBody>\n"
        "        <TablixColumnHierarchy>\n"
        "          <TablixMembers>\n" + members +
        "          </TablixMembers>\n"
        "        </TablixColumnHierarchy>\n"
        "        <TablixRowHierarchy>\n"
        "          <TablixMembers>\n"
        "            <TablixMember>\n"
        "              <Group Name=\"G_BurstKey\">\n"
        "                <GroupExpressions>\n"
        "                  <GroupExpression>=Fields!" + field + ".Value</GroupExpression>\n"
        "                </GroupExpressions>\n"
        "              </Group>\n"
        "              <SortExpressions>\n"
        "                <SortExpression>\n"
        "                  <Value>=Fields!" + field + ".Value</Value>\n"
        "                </SortExpression>\n"
        "              </SortExpressions>\n"
        "            </TablixMember>\n"
        "          </TablixMembers>\n"
        "        </TablixRowHierarchy>\n"
        "        <DataSetName>" + ds_name + "</DataSetName>\n"
        "        <Top>0in</Top>\n"
        "        <Left>0in</Left>\n"
        "        <Height>0.25in</Height>\n"
        "        <Width>%.2fin</Width>\n" % (col_w * len(names)) +
        "      </Tablix>\n"
        "    </ReportItems>\n"
        "    <Height>0.5in</Height>\n"
        "  </Body>"
    )
    out = ['<?xml version="1.0" encoding="utf-8"?>', root.group(0),
           "  " + dsrc.group(0), "  <DataSets>", "    " + ds_block.strip(), "  </DataSets>"]
    if params:
        out.append("  " + params.group(0))
    out.append(tablix)
    out.append(width.group(0).strip("\n") if width else "  <Width>7.5in</Width>")
    out.append(_bare_page(rdl_xml))
    if code:
        out.append(code.group(0).strip("\n"))
    if lang:
        out.append(lang.group(0).strip("\n"))
    out.append("</Report>\n")
    return "\n".join(out)


def _sql_column(datafield: str, field: str) -> str:
    col = datafield or field or ""
    if _is_ident(col):
        return col
    return '"' + col.replace('"', '""') + '"'


def burst_list_sql(rdl_xml: str, meta: dict) -> str:
    """Informational: the SQL the key list amounts to (for a native
    Data-Driven Subscription on Enterprise edition, or a DBA). Projects the
    dataset's REAL column (DataField), not the RDL's sanitized field name.
    Never executed by anything the pack ships."""
    ds = meta.get("dataset")
    if not ds or not (meta.get("datafield") or meta.get("field")):
        return ""
    for m in _DS_RE.finditer(rdl_xml or ""):
        if m.group(2) == ds:
            ct = re.search(r"<CommandText>(.*?)</CommandText>", m.group(3), re.S)
            if not ct:
                return ""
            inner = (ct.group(1).replace("&lt;", "<").replace("&gt;", ">")
                     .replace("&amp;", "&").strip())
            return ("-- one row per burst key; the inner query is the report's own\n"
                    "-- (bind variables keep their values; drop a trailing ORDER BY if Oracle objects)\n"
                    "SELECT DISTINCT " + _sql_column(meta.get("datafield"), meta.get("field"))
                    + " AS " + KEY_COLUMN + "\nFROM (\n" + inner + "\n) O2S_BURST\nORDER BY 1")
    return ""


# ---------------------------------------------------------------------------
# Driver + config + docs
# ---------------------------------------------------------------------------

def build_driver(report_name: str) -> str:
    """The PowerShell driver with only the report name filled in. The text
    is pure ASCII and constant apart from that name, so one parser check
    covers every report."""
    src = _DRIVER_TEMPLATE_PATH.read_text(encoding="ascii")
    return (src.replace("__REPORT_NAME__", _safe_name(report_name))
               .replace("__BIND_PARAM__", BURST_PARAM))


_SCALAR = (str, int, float, bool)


def build_config(report_name: str, info: dict, meta: dict, columns: dict,
                 overrides: Optional[dict] = None, rdl_xml: str = "") -> dict:
    rname = _safe_name(report_name)
    folder = "/Reports"
    server = "http://YOUR-REPORT-SERVER/ReportServer"
    o = dict(overrides or {})
    # the sidebar's report-server URL is the service endpoint plus the folder
    rsu = str(o.pop("report_server_url", "") or "").strip()
    if rsu:
        base, _, rest = rsu.partition("?")
        server = base.rstrip("/")
        if rest.strip("/"):
            folder = "/" + rest.strip("/")
    o.pop("FileNamePattern", None)      # already folded into meta["pattern"]
    fmt = str(o.pop("RenderFormat", "") or "PDF").upper()
    # EVERY report parameter is listed (empty = the report's own default), so
    # nothing the report needs is invisible to the operator.
    all_params = [p for p in report_parameter_names(rdl_xml) if p != BURST_PARAM]
    rparams = {p: "" for p in all_params}
    for p in columns.get("params", []):
        rparams.setdefault(p, "")
    cfg = {
        "_help": ("Every value the driver uses. ReportServer is the SERVICE "
                  "endpoint (usually http://host/ReportServer), not the "
                  "/Reports web portal. Paths are the folder paths shown in "
                  "the portal. Run the driver with -DryRun first. History: day "
                  "(a rerun the same day resumes; another day renders everything "
                  "again) | forever | none."),
        "ReportName": rname,
        "ReportServer": server,
        "ReportPath": folder + "/" + rname,
        "BurstListPath": (folder + "/" + rname + LIST_SUFFIX) if meta.get("injected") else "",
        "BindParameter": meta.get("bind_parameter") or BURST_PARAM,
        "KeyField": meta.get("field") or "",
        "Deliver": "file",
        "OutputRoot": "C:\\Oracle2SSRS\\" + rname + "\\out",
        "FileNamePattern": meta.get("pattern") or ("<" + KEY_COLUMN + ">.pdf"),
        "RenderFormat": fmt,
        "Overwrite": True,
        "TimeoutSec": 600,
        "Retries": 3,
        "AbortAfterFailures": 5,
        "History": "day",
        "TestLimit": 0,
        "ReportParameters": rparams,
        "EmailToColumn": "EmailTo",
        "SmtpServer": "",
        "SmtpPort": 25,
        "SmtpFrom": "",
        "SmtpUseSsl": False,
        "SmtpCredentialFile": "",
        "Subject": "{ReportName} - {BurstKey}",
        "Body": "Your {ReportName} for {BurstKey} is attached.",
        "TestRedirect": "",
    }
    for k, v in o.items():
        if k.startswith("_"):
            continue
        if v is None or isinstance(v, _SCALAR):
            cfg[k] = v
    return cfg


def build_checklist(report_name: str, info: dict, meta: dict, columns: dict) -> List[dict]:
    rname = _safe_name(report_name)
    key = _xml_text((info or {}).get("burst_key_field") or "key")   # HTML-safe: the UI innerHTMLs these
    steps = [
        {"step": 1, "title": "Know which SSRS edition you have",
         "body": "On the report server: <code>SELECT SERVERPROPERTY('Edition')</code>. "
                 "<b>Enterprise</b> (or Developer) has built-in <i>data-driven subscriptions</i> "
                 "that do this loop for you -- if that is what you have, use them (see the README) "
                 "and you never need the script. <b>Standard</b> has no such feature; "
                 "the driver in this pack is the replacement."},
        {"step": 2, "title": "Upload BOTH reports to the same folder",
         "body": "<code>" + rname + ".rdl</code> (the report, with a hidden per-key parameter) and "
                 "<code>" + rname + LIST_SUFFIX + ".rdl</code> (the key list). Same folder, same "
                 "shared data source. Open each once in the portal to confirm it runs."},
        {"step": 3, "title": "Fill in burst.config.json",
         "body": "<code>ReportServer</code> = the service endpoint, usually "
                 "<code>http://host/ReportServer</code> (NOT the /Reports portal). "
                 "<code>ReportPath</code> / <code>BurstListPath</code> = the folder paths as shown "
                 "in the portal. <code>OutputRoot</code> = where the files go. Every report "
                 "parameter is listed under <code>ReportParameters</code>; leave a value empty to "
                 "use the report's own default."},
        {"step": 4, "title": "Test from your own PC first -- no server access needed",
         "body": "<code>powershell -ExecutionPolicy Bypass -File .\\" + DRIVER_NAME + " -DryRun</code> "
                 "lists every key and file name without rendering. Then "
                 "<code>-TestLimit 2</code> renders two files into OutputRoot (test runs are not "
                 "recorded, so the real run still produces them). Open one next to "
                 "the Oracle output for the same " + key + "."},
        {"step": 5, "title": "Production: a service account",
         "body": "Ask AD for a service account (a gMSA is best: its password rotates itself). "
                 "It needs the <b>Browser</b> role on the report folder (portal → folder → "
                 "Manage → Security), write access to OutputRoot, and "
                 "<i>Log on as a batch job</i> on the machine that runs the task. Nothing is "
                 "installed for it: the driver uses only Windows PowerShell built-ins."},
        {"step": 6, "title": "Schedule it",
         "body": "<code>schtasks /Create /TN \"Burst " + rname + "\" /TR \"powershell -ExecutionPolicy Bypass "
                 "-File C:\\Oracle2SSRS\\" + rname + "\\" + DRIVER_NAME + "\" /SC WEEKLY /D MON /ST 06:00 "
                 "/RU DOMAIN\\svc_burst$ /RL HIGHEST</code>. Each run produces every key; a rerun "
                 "the SAME day resumes where it stopped (done_keys_&lt;date&gt;.txt); "
                 "<code>-Force</code> redoes today's keys."},
        {"step": 7, "title": "Email (optional)",
         "body": "Set <code>Deliver</code> to <code>email</code> or <code>both</code>, add an "
                 "<code>EmailTo</code> column to the key list (edit the BurstList report's dataset "
                 "in Report Builder), and fill the Smtp* settings. For an authenticated relay, "
                 "store the credential ONCE as the service account: "
                 "<code>Get-Credential | Export-Clixml smtp.cred.xml</code> and point "
                 "<code>SmtpCredentialFile</code> at it. <code>TestRedirect</code> sends every "
                 "message to one address while you test (and records nothing as done)."},
        {"step": 8, "title": "Monitoring",
         "body": "Log: <code>%ProgramData%\\Oracle2SSRS\\" + rname + "\\YYYYMMDD.jsonl</code>, one JSON "
                 "line per event. Exit code 0 = ran, 1 = every key failed or the run aborted, "
                 "2 = config error. Alert on <code>event = \"run_complete\" AND failed &gt; 0</code>, "
                 "on <code>run_aborted</code>, and on <code>all_skipped</code> (nothing was produced)."},
    ]
    if not meta.get("injected"):
        steps.insert(1, {"step": 0, "title": "This report could not be given a per-key filter",
                         "body": _xml_text(meta.get("reason") or "") +
                                 ". The pack ships WITHOUT a key-list report and the driver refuses "
                                 "to run (exit 2: BurstListPath is empty) until you (a) add a dataset "
                                 "filter on the key column against the parameter " + BURST_PARAM +
                                 " in Report Builder and (b) build a key-list report: one row per key "
                                 "with a textbox named " + KEY_COLUMN + ", uploaded as " + rname
                                 + LIST_SUFFIX + " and named in BurstListPath."})
    return steps


def build_readme(report_name: str, info: dict, meta: dict, columns: dict, cfg: dict,
                 list_sql: str) -> str:
    rname = _safe_name(report_name)
    key = (info or {}).get("burst_key_field") or "key"
    pattern = cfg.get("FileNamePattern", "")
    lines = [
        "# " + rname + " -- Burst Pack",
        "",
        "Oracle ran this report once and wrote **one file per " + key + "** "
        "(the Oracle file name pattern was `" + pattern + "`). SSRS cannot split one render "
        "into many files, so this pack renders the report **once per key** and saves each "
        "file under that name.",
        "",
        "## What is in the zip",
        "",
        "| File | What it is |",
        "|---|---|",
        "| `" + rname + ".rdl` | The report. It has ONE extra hidden parameter, `" + BURST_PARAM + "`: "
        "set it and the report shows only that key's rows; leave it empty and the report is "
        "exactly the one you would deploy anyway (same SQL, same order, same output). |",
        "| `" + rname + LIST_SUFFIX + ".rdl` | The key list: the same dataset, one row per distinct "
        + key + ". The driver asks the report server to render it as CSV. Because it runs on the "
        "report server, it uses the server's own data source -- nothing to install anywhere. |",
        "| `" + DRIVER_NAME + "` | The loop. Windows PowerShell 5.1 built-ins only, no modules. |",
        "| `burst.config.json` | Every setting. The driver reads it from the same folder. |",
        "| `service-account-setup.md` | The production checklist. |",
        "",
        "## Try it from your own PC (5 minutes)",
        "",
        "1. Upload **both** `.rdl` files to the same folder on the report server (same shared data source).",
        "2. Edit `burst.config.json`: `ReportServer` (the service endpoint, e.g. `http://host/ReportServer` -- "
        "not the `/Reports` portal), `ReportPath`, `BurstListPath`, `OutputRoot`.",
        "3. `powershell -ExecutionPolicy Bypass -File .\\" + DRIVER_NAME + " -DryRun` -- lists the keys and file names, renders nothing.",
        "4. `powershell -ExecutionPolicy Bypass -File .\\" + DRIVER_NAME + " -TestLimit 2` -- renders two files (not recorded as done). Compare one with the Oracle output for the same " + key + ".",
        "5. Run without switches for the full set. A rerun the **same day** resumes where it stopped "
        "(`done_keys_<date>.txt`); a run on another day produces everything again; `-Force` redoes "
        "today's keys; `-Key X` does one key regardless. `History` in the config: `day` (default), `forever`, `none`.",
        "",
        "You authenticate as yourself (Windows). You need the Browser role on the folder -- "
        "if you can open the report in the portal, the driver can render it.",
        "",
        "## File names",
        "",
        "`FileNamePattern` is `" + pattern + "`. `<" + KEY_COLUMN + ">` is the key; other `<Column>` tokens are filled from the key list row",
    ]
    if columns.get("columns"):
        lines.append("(the key list also carries: " + ", ".join(columns["columns"]) + ");")
    if columns.get("params"):
        lines.append("`<" + ">`, `<".join(columns["params"]) + ">` are report parameters -- give them "
                     "values under `ReportParameters` in the config;")
    if columns.get("unknown"):
        lines.append("`<" + ">`, `<".join(columns["unknown"]) + ">` could not be matched to a column or "
                     "parameter and are left as typed -- edit the pattern;")
    lines += [
        "`{date:yyyyMMdd}` inserts today's date. Characters Windows forbids in file names become `_`, "
        "and the extension always matches `RenderFormat` (PDF → .pdf, EXCELOPENXML → .xlsx, WORDOPENXML → .docx).",
        "",
        "## Report parameters",
        "",
        "Every parameter the report declares is listed under `ReportParameters` in the config. "
        "An empty value is not sent, so the report uses its own default; fill in the ones a run "
        "needs (a year, a region ...). They are passed to BOTH the key list and each render.",
        "",
        "## Production",
        "",
        "See `service-account-setup.md`: a service account with the Browser role on the folder and "
        "write access to `OutputRoot`, a Task Scheduler entry, and the log at "
        "`%ProgramData%\\Oracle2SSRS\\" + rname + "\\`. Exit codes: 0 ran, 1 every key failed or "
        "the run aborted (the same failure repeated `AbortAfterFailures` times), 2 config error. "
        "A run that produced nothing because every key was already done today logs `all_skipped`.",
        "",
        "## If you have Enterprise edition",
        "",
        "SSRS Enterprise has *data-driven subscriptions* that do this loop natively: portal → the report → "
        "Subscribe → New data-driven subscription → a query that returns one row per key → map its "
        "column to the parameter `" + BURST_PARAM + "` and choose file-share or e-mail delivery. "
        "The query is the key list's own SQL:",
        "",
        "```sql",
        list_sql or "-- (no key dataset)",
        "```",
        "",
        "## Email",
        "",
        "Optional. Set `Deliver` to `email` or `both`, add an `EmailTo` column to the key list report, "
        "fill the `Smtp*` settings (`SmtpCredentialFile` = a credential saved with "
        "`Get-Credential | Export-Clixml`, created while logged in as the service account). "
        "Each message carries the file under its real name (`" + pattern + "`). "
        "`TestRedirect` sends everything to one address while you test.",
        "",
    ]
    if not meta.get("injected"):
        lines.insert(4, "> **Note:** " + (meta.get("reason") or "") + ". The pack ships WITHOUT a "
                     "key-list report and `BurstListPath` is empty, so the driver refuses to run "
                     "(exit 2) until you add a dataset filter on the key column against `" + BURST_PARAM + "` "
                     "in Report Builder and build a key-list report (one row per key, textbox named `"
                     + KEY_COLUMN + "`) uploaded as `" + rname + LIST_SUFFIX + "`.")
    return "\n".join(lines)


def checklist_markdown(steps: List[dict]) -> str:
    out = ["# Service-account setup", ""]
    for s in steps:
        body = re.sub(r"<[^>]+>", "", str(s.get("body", "")))
        body = (body.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&").replace("&quot;", '"').replace("&nbsp;", " "))
        out += ["## " + str(s.get("step", "?")) + ". " + str(s.get("title", "")), "", body, ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The pack
# ---------------------------------------------------------------------------

def prepare(report_name: str, rdl_xml: str, info: dict, overrides: Optional[dict] = None) -> dict:
    """Everything the pack and the UI need, from the (deploy-bound) main RDL.

    The file-name pattern comes from the form override first, then from the
    detector; the key-column token is rewritten to ``<BURST_KEY>`` (the
    column the key list actually carries) and the extension follows the
    chosen ``RenderFormat``, so config, README and key list all agree."""
    info = dict(info or {})
    o = dict(overrides or {})
    rdl_xml, meta = inject_burst_key_filter(rdl_xml or "", info)
    pat = str(o.get("FileNamePattern") or info.get("filename_pattern") or "")
    pat = normalize_pattern(pat) or ("<" + KEY_COLUMN + ">.pdf")
    pat = key_token_to_column(pat, meta.get("field"), meta.get("datafield"), info.get("burst_key_field"))
    pat = with_format_extension(pat, str(o.get("RenderFormat") or "PDF"))
    meta["pattern"] = pat
    cols = list_columns(rdl_xml, info, meta)
    list_rdl = build_burst_list_rdl(rdl_xml, info, meta)
    meta["list_violations"] = []
    if list_rdl:
        try:
            from .validators.publish_semantics import publish_violations
            meta["list_violations"] = list(publish_violations(list_rdl))
        except Exception as exc:  # noqa: BLE001 -- the check is advisory
            meta["list_violations"] = ["check failed: " + type(exc).__name__]
    cfg = build_config(report_name, info, meta, cols, o, rdl_xml=rdl_xml)
    sql = burst_list_sql(rdl_xml, meta)
    steps = build_checklist(report_name, info, meta, cols)
    return {
        "rdl_xml": rdl_xml,
        "meta": meta,
        "columns": cols,
        "burst_list_rdl": list_rdl,
        "burst_list_sql": sql,
        "driver": build_driver(report_name),
        "config": cfg,
        "config_json": json.dumps(cfg, indent=2),
        "checklist": steps,
        "readme": build_readme(report_name, info, meta, cols, cfg, sql),
    }


def build_zip(report_name: str, rdl_xml: str, info: dict, overrides: Optional[dict] = None) -> bytes:
    p = prepare(report_name, rdl_xml, info, overrides)
    rname = _safe_name(report_name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if p["rdl_xml"]:
            z.writestr(rname + ".rdl", p["rdl_xml"])
        if p["burst_list_rdl"]:
            z.writestr(rname + LIST_SUFFIX + ".rdl", p["burst_list_rdl"])
        # UTF-8 BOM: Windows PowerShell 5.1 reads a BOM-less file as ANSI.
        # The driver is ASCII, but the BOM makes that irrelevant forever.
        z.writestr(DRIVER_NAME, b"\xef\xbb\xbf" + p["driver"].encode("ascii"))
        z.writestr("burst.config.json", p["config_json"])
        z.writestr("README.md", p["readme"])
        z.writestr("service-account-setup.md", checklist_markdown(p["checklist"]))
    return buf.getvalue()


__all__ = ["BURST_PARAM", "LIST_SUFFIX", "DRIVER_NAME", "KEY_COLUMN",
           "inject_burst_key_filter", "find_key_dataset", "find_key_datasets",
           "build_burst_list_rdl", "burst_list_sql", "build_driver", "build_config",
           "build_checklist", "build_readme", "normalize_pattern", "pattern_tokens",
           "key_token_to_column", "with_format_extension", "list_columns", "prepare", "build_zip"]

"""
Bursting / Data-Driven Subscription support for Oracle -> SSRS conversion.

Oracle Reports has a "distribution" mechanism: a single report run can emit
N output files (typically one PDF per group key) by reading a destination
parameter such as P_AS_PATH and a per-row filename built by a CF_File-style
formula. SSRS Standard edition has no native data-driven subscription, so we
generate a PowerShell script that loops a "burst query" and renders the RDL
once per row using the ReportingServicesTools module.

Public API (consumed by converter/__init__.py via the integration agent):

    detect_bursting(report) -> dict
        {
          "is_bursting": bool,
          "evidence": [str, ...],
          "burst_key_field": str | None,
          "filename_pattern": str | None,
        }

    build_burst_query(report, info) -> str
        T-SQL stub returning one row per delivery target.

    build_powershell_dds_script(report, info, rdl_path) -> str
        PowerShell driver script that emulates DDS on SSRS Standard.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Sanitizers -- the report is UNTRUSTED. Report/column/parameter names and
# filename patterns flow into the GENERATED PowerShell + T-SQL burst artifacts.
# A hostile name must not be able to close a PS hashtable or break out of a SQL
# string/comment and inject code that later runs on the customer's SSRS host.
# ---------------------------------------------------------------------------

def _ps_ident(name: str) -> str:
    """Safe PowerShell variable identifier (letters / digits / underscore)."""
    s = re.sub(r"[^A-Za-z0-9_]", "", str(name or ""))
    if not s:
        return "Param"
    return ("P_" + s) if s[0].isdigit() else s


def _ps_text(text: str) -> str:
    """Text safe inside a PowerShell double-quoted string OR a # comment:
    newlines removed, and backtick / double-quote / $ escaped."""
    s = re.sub(r"[\r\n]+", " ", str(text or ""))
    return s.replace("`", "``").replace('"', '`"').replace("$", "`$")


def _sql_ident(name: str) -> str:
    """Safe T-SQL identifier fragment (schema.table / column): letters,
    digits, underscore and a single dot. Strips everything else."""
    s = re.sub(r"[^A-Za-z0-9_.]", "", str(name or ""))
    return s or "col"


def _sql_str(text: str) -> str:
    """Text safe inside a single-quoted T-SQL literal: quotes doubled,
    newlines removed."""
    s = re.sub(r"[\r\n]+", " ", str(text or ""))
    return s.replace("'", "''")


def _sql_comment(text: str) -> str:
    """Text safe on a -- comment line: no newlines."""
    return re.sub(r"[\r\n]+", " ", str(text or "")).strip()


def _safe_name(text: str) -> str:
    """Report/display name safe to splice into ANY generated artifact context
    (PowerShell string/comment, JSON config, SQL comment): newlines removed and
    every quote / backtick / $ / backslash / semicolon / brace stripped."""
    s = re.sub(r"[\r\n\t]+", " ", str(text or ""))
    s = re.sub(r"""[`"'$\\;{}]""", "", s)
    return s.strip() or "report"


# ---------------------------------------------------------------------------
# Heuristic markers
# ---------------------------------------------------------------------------

_BURST_PARAM_NAMES = {
    "P_AS_PATH",
    "P_DISTRIBUTE",
    "P_DISTR_ABBR",
    "P_DESNAME",
    "P_DESTYPE",
    "P_DESFORMAT",
}

_BURST_BODY_HINTS = ("P_AS_PATH", "P_DISTRIBUTE", "DESNAME")


# ---------------------------------------------------------------------------
# DECLARED distribution instructions
#
# Oracle Reports never guesses who receives which file: the report ships (or
# writes at run time) a <destinations> document that names one <file>, <mail>
# or <printer> destination per delivery, and each destination's attributes
# point back at the report's own columns/formulas with Oracle's &<NAME>
# reference syntax:
#
#     <foreach>
#       <mail id="..." to="&<...>" subject="..."/>
#       <file id="..." name="&<...>.pdf" instance="this"/>
#     </foreach>
#
# Those references ARE the binding between a delivery slot and a report
# column -- the only non-guessing answer to "which column is the recipient".
# Every element and attribute name used below is Oracle Reports' OWN
# distribution dialect (fixed by the product); none of it is a guess about
# what a site might call a column.
# ---------------------------------------------------------------------------

# Oracle Reports distribution destination elements.
_DEST_ELEMENTS = ("mail", "file", "printer")
# Oracle Reports <mail> recipient attributes.
_MAIL_RECIPIENT_ATTRS = ("to", "cc", "bcc")

# An element's attribute region may itself contain "&<NAME>" (angle brackets
# and all), so the scan allows that form explicitly instead of stopping at the
# first '>'.
_DEST_ELEMENT_RE = re.compile(
    r"(?is)<\s*(" + "|".join(_DEST_ELEMENTS) + r")\b"
    r"((?:&\s*<[A-Za-z_][A-Za-z0-9_]*>|[^<>])*)>")
_ATTR_RE = re.compile(r"""(?is)\b([a-z_][a-z0-9_]*)\s*=\s*(['"])(.*?)\2""")
# Oracle's reference syntax inside a distribution attribute: &<NAME> or &NAME.
_ORACLE_REF_RE = re.compile(r"&\s*<?\s*([A-Za-z_][A-Za-z0-9_]*)\s*>?")
# Oracle's own mail-destination built-in.
_SET_MAIL_RE = re.compile(r"(?is)\bSRW\s*\.\s*SET_MAILDESTINATION\s*\((.*?)\)")


def _merge_plsql_literals(text):
    """Splice PL/SQL string concatenation back together.

    A report BUILDS its distribution document with Text_IO writes, so a single
    <file ...> element arrives as a dozen quoted literals joined by ``||``.
    Dropping only the ``' || '`` glue reconstructs the markup exactly as the
    report writes it. A literal interrupted by a VARIABLE stays interrupted,
    which is correct: there is no static reference to read at that spot.
    """
    if not text:
        return ""
    return re.sub(r"'\s*\|\|\s*'", "", str(text))


def _distribution_texts(report):
    """Every text a report's distribution instructions can live in: the source
    document itself (a shipped <destinations> block) and each program-unit
    body, with PL/SQL concatenation spliced back together."""
    out = []
    raw = getattr(report, "raw_xml", "") or ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if raw:
        raw = _html.unescape(raw)
        out.append(raw)
        merged = _merge_plsql_literals(raw)
        if merged != raw:
            out.append(merged)
    for t in (getattr(report, "triggers", None) or []):
        out.append(_merge_plsql_literals(
            _html.unescape(getattr(t, "body", "") or "")))
    for f in (getattr(report, "formulas", None) or []):
        out.append(_merge_plsql_literals(
            _html.unescape(getattr(f, "plsql_body", "") or "")))
    return [t for t in out if t]


def declared_distribution(report):
    """Read the distribution instructions the SOURCE declares.

    Returns::

        {"declared": bool,          # the source declares any destination
         "per_row": bool,           # a destination is per-ROW (instance="this")
         "has_mail": bool,          # a <mail> destination is declared
         "recipient_refs": [...],   # names referenced by to / cc / bcc slots
         "file_refs": [...]}        # names referenced by a file NAME slot
    """
    cached = getattr(report, "_o2s_declared_distribution", None)
    if cached is not None:
        return cached
    recipient_refs, file_refs = [], []
    declared = per_row = has_mail = False
    for text in _distribution_texts(report):
        for m in _DEST_ELEMENT_RE.finditer(text):
            element = m.group(1).lower()
            attrs = {a.lower(): v for a, _quote, v in _ATTR_RE.findall(m.group(2))}
            declared = True
            if element == "mail":
                has_mail = True
            if (attrs.get("instance", "") or "").strip().lower() == "this":
                per_row = True
            for attr, value in attrs.items():
                refs = [r.group(1) for r in _ORACLE_REF_RE.finditer(value)]
                if not refs:
                    continue
                if attr in _MAIL_RECIPIENT_ATTRS:
                    recipient_refs.extend(refs)
                elif attr == "name":
                    file_refs.extend(refs)
        for m in _SET_MAIL_RE.finditer(text):
            declared = True
            has_mail = True
            recipient_refs.extend(
                r.group(1) for r in re.finditer(
                    r"[:&]\s*<?\s*([A-Za-z_][A-Za-z0-9_]*)\s*>?", m.group(1)))

    def _dedupe(seq):
        seen, out = set(), []
        for s in seq:
            u = s.upper()
            if u not in seen:
                seen.add(u)
                out.append(s)
        return out

    info = {"declared": declared, "per_row": per_row, "has_mail": has_mail,
            "recipient_refs": _dedupe(recipient_refs),
            "file_refs": _dedupe(file_refs)}
    try:
        setattr(report, "_o2s_declared_distribution", info)
    except Exception:
        pass
    return info


def _resolve_ref_to_columns(report, ref):
    """Resolve one declared reference to the dataset column(s) carrying it.

    A reference names either a query column (resolves to itself), a formula
    column (resolves to the columns its PL/SQL body reads) or a parameter
    (carries no per-row value -- resolves to nothing).
    """
    if not ref:
        return []
    target = ref.upper()
    columns = {}
    for _q, c in _all_query_columns(report):
        if c:
            columns.setdefault(c.upper(), c)
    if target in columns:
        return [columns[target]]
    for f in (getattr(report, "formulas", None) or []):
        if _norm(getattr(f, "name", "")) != target:
            continue
        body = _html.unescape(getattr(f, "plsql_body", "") or "")
        out = []
        for m in re.finditer(r":([A-Za-z_][A-Za-z0-9_]*)", body):
            col = columns.get(m.group(1).upper())
            if col and col not in out:
                out.append(col)
        return out
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(s):
    return (s or "").upper()


def _all_param_names(report):
    return [_norm(getattr(p, "name", "")) for p in getattr(report, "parameters", [])]


def _all_query_columns(report):
    out = []
    for q in getattr(report, "queries", []):
        for it in getattr(q, "items", []):
            out.append((getattr(q, "name", ""), getattr(it, "name", "")))
    return out


def _outermost_break_column(query, allowed):
    """The break column of the query's OUTERMOST declared <group>.

    Oracle splits a bursting run at a group break, so the outermost group's
    break column is the per-file key. ``allowed`` is the roster of columns that
    may serve as a key (aggregates already excluded); a break column outside it
    is ignored rather than trusted.
    """
    allowed_by_upper = {(c or "").upper(): c for c in (allowed or []) if c}
    groups = list(getattr(query, "groups", None) or [])
    while groups:
        nxt = []
        for g in groups:
            col = (getattr(g, "break_col", "") or "").strip()
            if col and col.upper() in allowed_by_upper:
                return allowed_by_upper[col.upper()]
            nxt.extend(getattr(g, "children", None) or [])
        groups = nxt
    return None


def _bind_refs(plsql):
    if not plsql:
        return []
    return re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", plsql)


# ---------------------------------------------------------------------------
# detect_bursting
# ---------------------------------------------------------------------------

def detect_bursting(report):
    """
    Decide whether ``report`` was using Oracle Reports distribution.

    Returns a dict with keys is_bursting, evidence, burst_key_field,
    filename_pattern.
    """
    evidence = []
    is_bursting = False

    # ---- 1. Parameter sniff -------------------------------------------------
    param_names = _all_param_names(report)
    for pname in param_names:
        if pname in _BURST_PARAM_NAMES:
            evidence.append("parameter " + pname + " present")
            if pname in ("P_AS_PATH", "P_DISTRIBUTE"):
                is_bursting = True

    # ---- 2. Formula sniff ---------------------------------------------------
    # The file-template formula is identified two ways, both declaration-driven:
    # its body reads one of Oracle's OWN destination parameters, or the report's
    # DECLARED distribution instructions name it in a destination slot. Neither
    # asks what the formula is CALLED.
    decl = declared_distribution(report)
    declared_template_refs = {r.upper() for r in
                              (decl["file_refs"] + decl["recipient_refs"])}
    if decl["declared"]:
        evidence.append("source declares distribution destinations"
                        + (" (per row)" if decl["per_row"] else ""))
    if decl["per_row"]:
        is_bursting = True

    burst_formula = None
    for f in getattr(report, "formulas", []):
        fname = _norm(getattr(f, "name", ""))
        body = getattr(f, "plsql_body", "") or ""
        body_u = body.upper()

        declared_hit = fname in declared_template_refs
        body_hit = any(h in body_u for h in _BURST_BODY_HINTS)

        if body_hit:
            hit = [h for h in _BURST_BODY_HINTS if h in body_u][0]
            evidence.append("formula " + str(f.name) + " references " + hit)
            is_bursting = True
            if burst_formula is None:
                burst_formula = f
        elif declared_hit:
            evidence.append("formula " + str(f.name)
                            + " is bound to a declared destination slot")
            is_bursting = True
            if burst_formula is None:
                burst_formula = f

    # ---- 3. Triggers / hyperlink-style references ---------------------------
    for t in getattr(report, "triggers", []):
        body_u = (getattr(t, "body", "") or "").upper()
        if "P_AS_PATH" in body_u:
            evidence.append("trigger " + str(t.name) + " references distribution path")
            is_bursting = True

    # ---- 4. Resolve burst key + filename pattern ----------------------------
    burst_key_field = None
    filename_pattern = None

    if burst_formula is not None:
        body = burst_formula.plsql_body or ""
        binds = _bind_refs(body)
        param_set = set(p.upper() for p in param_names)
        path_like = {"P_AS_PATH", "P_DESNAME", "P_DESTYPE", "P_DESFORMAT",
                     "P_DISTR_ABBR", "P_DISTRIBUTE"}
        for b in binds:
            bu = b.upper()
            # Skip distribution-path params AND Oracle summary/formula/
            # placeholder binds (CS_/CF_/CP_) -- an aggregate is never a
            # per-recipient burst key.
            if bu in param_set or re.match(r"(?i)^(cs|cf|cp)_", b):
                continue
            burst_key_field = b
            break

        m = re.search(r"RETURN\s*\((.+?)\)\s*;", body, re.DOTALL | re.IGNORECASE)
        ret_expr = m.group(1) if m else body
        pieces = []
        for tok in re.split(r"\|\|", ret_expr):
            tok = tok.strip()
            if not tok:
                continue
            mb = re.search(r":([A-Za-z_][A-Za-z0-9_]*)", tok)
            if mb:
                bname = mb.group(1)
                if bname.upper() in path_like:
                    continue
                pieces.append("<" + bname + ">")
                continue
            ml = re.search(r"'([^']*)'", tok)
            if ml:
                pieces.append(ml.group(1))
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", tok):
                continue
            pieces.append(tok[:24])
        joined = "".join(pieces)
        joined = re.sub(r"_{2,}", "_", joined).strip("_")
        if joined:
            filename_pattern = joined + ".pdf"

    # Derive a burst key without naming any specific report-domain columns.
    # Order of preference:
    #   1. Any bind reference from the bursting formula that is NOT one of
    #      the distribution-path params (already attempted above via the
    #      _bind_refs / param_set loop).
    #   2. The FIRST column of the report's main query — i.e. the first
    #      defined data item on whatever query has the most items. This is
    #      a structural fallback that works for any Oracle Report.
    #   3. None — the upstream caller can treat a missing burst key as
    #      "no per-row split detected" and proceed accordingly.
    if is_bursting and not burst_key_field:
        queries = getattr(report, "queries", []) or []
        if queries:
            main_q = max(queries, key=lambda q: len(getattr(q, "items", []) or []))
            cols = [getattr(it, "name", "") for it in (getattr(main_q, "items", []) or [])
                    if getattr(it, "name", "")]
            # A burst key is a per-recipient IDENTIFIER -- never an Oracle
            # summary/formula/placeholder column (CS_/CF_/CP_), which is an
            # aggregate, not a row key.
            real = [c for c in cols if not re.match(r"(?i)^(cs|cf|cp)_", c)]
            # Oracle bursts at a GROUP BREAK -- one output per value of the
            # outermost <group>'s break column -- so that column IS the per-file
            # key. Read it off the declared group tree; a query that declares no
            # group yields one row per delivery anyway, so its first data item
            # is the key. Nothing here reads a column NAME.
            burst_key_field = _outermost_break_column(main_q, real)
            if not burst_key_field:
                burst_key_field = real[0] if real else (cols[0] if cols else None)

    if is_bursting and not filename_pattern and burst_key_field:
        filename_pattern = "<" + burst_key_field + ">.pdf"

    return {
        "is_bursting": bool(is_bursting),
        "evidence": evidence,
        "burst_key_field": burst_key_field,
        "filename_pattern": filename_pattern,
    }


# ---------------------------------------------------------------------------
# build_burst_query
# ---------------------------------------------------------------------------

def _pick_recipient_columns(report, exclude=None):
    """``[recipient-label column, recipient-destination column]``.

    Both come from the DECLARED distribution slots: the destination column is
    whatever the source's <mail> recipient attribute (or SRW.SET_MAILDESTINATION)
    references, and the label column is whatever its per-row file-name template
    references. A source that declares neither keeps the neutral placeholders,
    so the generated query still shows the user exactly where to edit.

    It does NOT look at column names. Matching a column because it is spelled
    like an address is how a postal-address column reaches an SMTP To: header.
    """
    email_hit = _detect_email_column(report)
    # The label column must add something the row does not already carry, so
    # the burst key itself (and the destination column) are excluded.
    taken = {(c or "").upper() for c in
             ([email_hit] + list(exclude or [])) if c}
    name_hit = None
    for ref in declared_distribution(report)["file_refs"]:
        for c in _resolve_ref_to_columns(report, ref):
            if (c or "").upper() not in taken:
                name_hit = c
                break
        if name_hit:
            break
    return [name_hit or "Recipient_Name", email_hit or "Email_Or_Path"]


def _filename_replace_chain(pattern, key):
    """T-SQL that rebuilds the per-row output filename from the SOURCE's own
    file template.

    ``pattern`` carries one ``<REF>`` placeholder per reference the source's
    file-name template used, so the REPLACE chain is generated FROM those
    references: the burst key resolves to the row's key column, every other
    reference to the same-named report parameter. Nothing is hardcoded, so a
    template built from three references produces three REPLACEs and a
    template built from none produces a plain literal.
    """
    refs = []
    for m in re.finditer(r"<([A-Za-z_][A-Za-z0-9_]*)>", str(pattern or "")):
        name = m.group(1)
        if name not in refs:
            refs.append(name)
    literal = "        '" + _sql_str(pattern) + "'"
    if not refs:
        return "    " + literal.strip() + "\n"
    lines = ["    " + ("REPLACE(" * len(refs)) + "\n", literal + ",\n"]
    for name in refs:
        safe = _sql_ident(name)
        if safe.upper() == (key or "").upper():
            value = "CAST(p." + safe + " AS NVARCHAR(64))"
        else:
            value = "ISNULL(CAST(@" + safe + " AS NVARCHAR(64)), '')"
        lines.append("        '<" + safe + ">', " + value + "),\n")
    # The final REPLACE closes the expression; drop its trailing comma.
    lines[-1] = lines[-1].rstrip(",\n") + "\n"
    return "".join(lines)


def build_burst_query(report, info):
    """
    Returns a T-SQL stub that yields ONE row per delivery target.
    """
    # All of these flow into generated T-SQL -- sanitize (the report is untrusted).
    # Every fallback below is a NEUTRAL placeholder the user is told to edit --
    # never a column/table name borrowed from some other report's schema.
    key = _sql_ident(info.get("burst_key_field") or "Burst_Key")
    name_col, email_col = _pick_recipient_columns(
        report, exclude=[info.get("burst_key_field"), key])
    name_col = _sql_ident(name_col)
    email_col = _sql_ident(email_col)
    pattern = info.get("filename_pattern") or ("<" + key + ">.pdf")
    rname = _sql_comment(getattr(report, "name", "REPORT") or "REPORT")

    first_q = ""
    qs = getattr(report, "queries", [])
    if qs:
        first_q = getattr(qs[0], "name", "") or ""

    evidence_str = _sql_comment(", ".join(info.get("evidence", []) or []) or "(none)")
    table = _sql_ident(first_q or "MainTable")
    recipient_table = _sql_ident("RecipientTable")

    sql = (
        "-- Data-Driven Subscription / bursting source for " + rname + "\n"
        "-- One row per output file. Edit the FROM/JOIN to suit your environment.\n"
        "--\n"
        "-- Detected:\n"
        "--   burst_key_field  = " + key + "\n"
        "--   filename_pattern = " + _sql_comment(pattern) + "\n"
        "--   evidence         = " + evidence_str + "\n"
        "--\n"
        "SELECT\n"
        "    p." + key + "                                   AS Burst_Key,\n"
        "    p." + name_col + "                              AS Recipient_Name,\n"
        "    COALESCE(r." + email_col + ", '\\\\fileshare\\reports\\out')\n"
        "                                              AS Email_Or_Path,\n"
        "    'PDF'                                     AS Render_Format,\n"
        "    -- Per-row filename rebuilt from the source's own file template.\n"
        + _filename_replace_chain(pattern, key) +
        "                                              AS Output_File\n"
        "FROM dbo." + table + " AS p\n"
        "LEFT JOIN dbo." + recipient_table + " AS r\n"
        "       ON r." + key + " = p." + key + "\n"
        "WHERE p." + key + " IS NOT NULL\n"
        "ORDER BY p." + key + ";\n"
    )
    return sql


# ---------------------------------------------------------------------------
# build_powershell_dds_script
# ---------------------------------------------------------------------------

def build_powershell_dds_script(report, info, rdl_path):
    """The Burst Pack driver (see burst_pack). Kept under its historical
    name for every caller; the report name is the only thing spliced in,
    through a whitelist sanitizer, so no report-derived text can reach
    PowerShell syntax."""
    from . import burst_pack
    return burst_pack.build_driver(getattr(report, "name", "") or "report")


__all__ = [
    "detect_bursting",
    "build_burst_query",
    "build_powershell_dds_script",
]


# ---------------------------------------------------------------------------
# Email-via-service-account distribution
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Production-grade email bursting helpers — ecosystem-specific (SSRS)
# These replace the earlier stub versions (later defs win in Python).
# ---------------------------------------------------------------------------

def _detect_main_table(report):
    """Inspect the parsed report's queries and try to pick the primary table
    the report binds against. Strategy: look at every dataset's tsql/sql,
    parse `FROM <ident>` and `JOIN <ident>`, and return the most-frequently
    referenced one. Falls back to None.
    """
    counts = {}
    pat = re.compile(
        r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_\.]*)",
        re.IGNORECASE,
    )
    for q in getattr(report, "queries", []):
        body = (getattr(q, "tsql", "") or getattr(q, "sql", "") or "")
        for m in pat.finditer(body):
            tok = m.group(1)
            # Strip schema-prefix for the comparison (dbo.X -> X)
            short = tok.split(".")[-1]
            if not short or short.upper() in (
                "DUAL", "SYS", "INFORMATION_SCHEMA", "SELECT",
            ):
                continue
            counts[short] = counts.get(short, 0) + 1
    if not counts:
        return None
    # Most-referenced wins; tie-broken by first-seen order (stable sort).
    return sorted(counts.items(), key=lambda kv: -kv[1])[0][0]


def _detect_email_column(report):
    """The dataset column bound to the source's DECLARED mail-recipient slot.

    Structural: a <mail> destination's to/cc/bcc attribute (or Oracle's
    SRW.SET_MAILDESTINATION) names a column or a formula, and that reference IS
    the binding. Returns the column in its original casing.

    FAIL CLOSED: a source that declares no mail destination has no recipient
    column to find, and this returns None so the caller keeps its labelled
    <RecipientEmail> placeholder. Picking a column because its NAME resembles
    an address is how a postal-address column (or a column merely containing
    'MAIL') ends up addressing real outbound e-mail.
    """
    for ref in declared_distribution(report)["recipient_refs"]:
        cols = _resolve_ref_to_columns(report, ref)
        if cols:
            return cols[0]
    return None


def build_email_burst_query(report, info):
    """The burst query the PowerShell driver loops over.

    One row per email recipient. The driver reads each row, binds the report
    parameter, renders to PDF, sends via SMTP, and logs the outcome.

    The query is intentionally written to FAIL CLOSED (no spam): rows with
    no email are skipped, not sent to a fallback.

    Improvement: we now AUTO-DETECT the main table and (if possible) the
    recipient-email column from the parsed report's own datasets, and
    substitute them directly into the rendered SQL. Anything we can't infer
    is left as a clearly-labeled placeholder so the user knows where to
    edit. A header comment shows exactly what was substituted.
    """
    # Untrusted report-derived names flow into this generated T-SQL -- sanitize.
    burst_key = _sql_ident((info or {}).get("burst_key_field") or "Burst_Key")
    rname = _sql_comment((report.name if hasattr(report, "name") else "") or "report")

    detected_main = _detect_main_table(report)
    detected_email_col = _detect_email_column(report)

    main_table = _sql_ident(detected_main) if detected_main else "<MainTable>"
    email_table = "<EmailTable>"
    if detected_email_col:
        # We have an email column somewhere in the schema; for the rendered
        # SQL we assume it lives on the main table unless the user overrides.
        # That keeps the JOIN sane while still being copy-pastable.
        email_expr = "p." + _sql_ident(detected_email_col)
        email_join = ""  # no separate email-lookup table needed
    else:
        email_expr = "o.<RecipientEmail>"
        email_join = (
            "LEFT JOIN dbo." + email_table + " AS o\n"
            "    ON o." + burst_key + " = p." + burst_key + "\n"
        )

    subst_summary = (
        "--   <MainTable>      -> " + (main_table if detected_main else "<MainTable>  (NOT DETECTED — edit me)")
        + "\n--   <RecipientEmail> -> " + (detected_email_col if detected_email_col else "<RecipientEmail>  (NOT DETECTED — edit me)")
        + "\n--   burst_key        -> " + burst_key
    )

    return (
        "-- =============================================================\n"
        "-- " + rname + " — Email Burst Query\n"
        "-- One row per recipient. Driver loops this and emails each row.\n"
        "-- =============================================================\n"
        "-- Required columns (the driver references them by name):\n"
        "--   Burst_Key       value bound to the report's per-recipient parameter\n"
        "--   EmailTo         primary recipient address (REQUIRED — rows without this are skipped)\n"
        "--   EmailCc         optional cc list (semicolon-separated)\n"
        "--   Subject         email subject line\n"
        "--   Recipient_Name  for logging only (helps trace failures)\n"
        "--   Render_Format   PDF | EXCELOPENXML | WORDOPENXML  (default PDF)\n"
        "--\n"
        "-- Auto-substitutions (override any '<...>' that remains):\n"
        + subst_summary + "\n"
        "\n"
        "SELECT\n"
        "    CAST(p." + burst_key + " AS NVARCHAR(64))                         AS Burst_Key,\n"
        "    " + email_expr + "                                                AS EmailTo,\n"
        "    NULL                                                              AS EmailCc,\n"
        "    CONCAT('[" + rname + "] — ', CAST(p." + burst_key + " AS NVARCHAR(64)))  AS Subject,\n"
        "    CAST(p." + burst_key + " AS NVARCHAR(64))                         AS Recipient_Name,\n"
        "    'PDF'                                                             AS Render_Format\n"
        "FROM dbo." + main_table + " AS p\n"
        + email_join +
        "WHERE " + email_expr + " IS NOT NULL          -- fail-closed: never email '[unknown]'\n"
        "ORDER BY p." + burst_key + ";\n"
    )


# Production PowerShell template. Reads its config from a sibling JSON file so
# the same script can drive every report. Has structured logging, retries on
# transient SMTP errors, send-history tracking to prevent duplicate emails on
# rerun, and a hard test-mode redirect that is impossible to forget about.
def build_email_powershell_script(report, info, rdl_path):
    """Same driver as build_powershell_dds_script (one driver does file and
    email delivery; the mode lives in burst.config.json)."""
    return build_powershell_dds_script(report, info, rdl_path)


def _bind_param_for_key(report, burst_key_field):
    """Best report PARAMETER to filter the report to one burst-key value -- the
    SSRS parameter the driver sets per row. Matches the burst-key column to a
    ReportParameter: an exact de-prefixed name match (burst key 'Perm_Name' ->
    'P_PERM_NAME') wins; else a clear report-specific placeholder 'P_<KEY>' the
    user edits (better than the old opaque 'P___KEY__'). Generic, name-based."""
    if not burst_key_field:
        return "P___KEY__"

    def _n(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    key_n = _n(burst_key_field)
    params = [getattr(p, "name", "") for p in getattr(report, "parameters", [])]
    # 1. exact de-prefixed match (P_PERM_NAME == key Perm_Name)
    for p in params:
        if _n(re.sub(r"(?i)^p_?", "", p)) == key_n and key_n:
            return p
    # 2. clear, report-specific fill-in placeholder (NOT the opaque P___KEY__).
    return "P_" + re.sub(r"[^A-Za-z0-9]", "_", burst_key_field).upper()


def _pick_bind_candidates(report, burst_key_field):
    """Report parameters that plausibly filter to one burst key -- a parameter
    whose name shares the burst key's leading token (e.g. key 'Permit' ->
    P_PERM_NUM / P_PERM_NAME / P_PERMITTEE). For the README's guidance list."""
    ktoks = [t for t in re.split(r"[^a-z0-9]+", str(burst_key_field or "").lower()) if t]
    if not ktoks:
        return []
    pref = ktoks[0][:4]
    out = []
    for p in (getattr(report, "parameters", []) or []):
        pn = getattr(p, "name", "")
        ptoks = [t for t in re.split(r"[^a-z0-9]+", pn.lower()) if t and t != "p"]
        if any(t.startswith(pref) or pref.startswith(t) for t in ptoks):
            out.append(pn)
    return out[:6]


def build_email_config_template(report, info):
    """burst.config.json for this report (see burst_pack.build_config)."""
    from . import burst_pack
    rname = getattr(report, "name", "") or "report"
    rdl = getattr(report, "_o2s_rdl_xml", "") or ""
    meta = burst_pack.inject_burst_key_filter(rdl, info or {})[1] if rdl else {}
    meta["pattern"] = burst_pack.normalize_pattern((info or {}).get("filename_pattern") or "")
    cols = burst_pack.list_columns(rdl, info or {}, meta) if rdl else {"columns": [], "params": [], "unknown": []}
    import json as _json
    return _json.dumps(burst_pack.build_config(rname, info or {}, meta, cols), indent=2)


def build_service_account_checklist(report, info):
    """Concrete, verifiable steps (see burst_pack.build_checklist)."""
    from . import burst_pack
    rname = getattr(report, "name", "") or "report"
    rdl = getattr(report, "_o2s_rdl_xml", "") or ""
    meta = burst_pack.inject_burst_key_filter(rdl, info or {})[1] if rdl else {"injected": False, "reason": "no RDL"}
    meta.setdefault("pattern", burst_pack.normalize_pattern((info or {}).get("filename_pattern") or ""))
    cols = burst_pack.list_columns(rdl, info or {}, meta) if rdl else {"columns": [], "params": [], "unknown": []}
    return burst_pack.build_checklist(rname, info or {}, meta, cols)


# ---------------------------------------------------------------------------
# Burst Pack zip — plug-and-play download
# ---------------------------------------------------------------------------

def build_burst_readme(report, info, config):
    """README that ships inside the Burst Pack zip (see burst_pack)."""
    from . import burst_pack
    rname = getattr(report, "name", "") or "report"
    rdl = getattr(report, "_o2s_rdl_xml", "") or ""
    return burst_pack.prepare(rname, rdl, info or {}, dict(config or {}))["readme"]


def _service_account_md(report, info):
    """Flat-markdown rendering of the service-account checklist."""
    from . import burst_pack
    return burst_pack.checklist_markdown(build_service_account_checklist(report, info))


def _apply_config_overrides(template_json, overrides):
    """Merge UI form overrides on top of the JSON template. Returns the
    final JSON string. Unknown keys from the UI are preserved (e.g. AuthMode)
    so the PowerShell driver can read them if it grows new knobs."""
    import json as _json
    try:
        cfg = _json.loads(template_json)
    except Exception:
        cfg = {}
    if not isinstance(overrides, dict):
        overrides = {}
    # Only let through string/number/bool/null overrides. No dict/list nesting
    # from the UI form (avoids injection of weird structures).
    for k, v in overrides.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            cfg[k] = v
    return _json.dumps(cfg, indent=2)


def build_burst_pack_zip(report, rdl_xml, bursting_info, config_overrides=None):
    """The plug-and-play Burst Pack zip (see burst_pack.build_zip):
    <report>.rdl (with the hidden per-key parameter), <report>_BurstList.rdl,
    Run-Burst.ps1, burst.config.json, README.md, service-account-setup.md."""
    from . import burst_pack
    rname = getattr(report, "name", "") or "report"
    return burst_pack.build_zip(rname, rdl_xml or "", bursting_info or {}, config_overrides)


__all__ = [
    "detect_bursting",
    "build_burst_query",
    "build_powershell_dds_script",
    "build_email_burst_query",
    "build_email_powershell_script",
    "build_email_config_template",
    "build_service_account_checklist",
    "build_burst_pack_zip",
    "build_burst_readme",
]

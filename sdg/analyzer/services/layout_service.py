"""Layout Builder.

Point at a sample file, parse its structure, assign a generation rule to every
field, and save the layout for review. Decoupled from generation entirely — this
produces a layout definition and per-field rule assignments; nothing here runs
the generator or depends on the CSV/EDI paths.

Two file kinds, both parseable from a sample with no external spec:

  csv          delimiter sniffed, header row is field names, types inferred
  fixed        column boundaries inferred from positions that are always blank

Every field gets a rule. A field whose name matches an active row in
sdm.generation_rules is auto-assigned that rule. A field with no match falls to
gen_method='default' — blank output, flagged in the UI so the user sees it was
never really assigned. 'constant' is a real rule a user defines with a value; it
is never the fallback.

Layouts persist to sdm.layouts (auto-created) with status draft/pending_review/
approved. Only approved layouts are selectable downstream.
"""

import csv as csvmod
import io
import json
import logging
import re
from datetime import datetime
from typing import Optional

import pyodbc

from sdg.analyzer.config import SQL_SCHEMA, get_connection_string

LAYOUTS_TABLE = f"{SQL_SCHEMA}.layouts"
RULES_TABLE = f"{SQL_SCHEMA}.generation_rules"
ALIASES_TABLE = f"{SQL_SCHEMA}.generation_rule_aliases"

DEFAULT_METHOD = "default"   # the fallback; blank output, not a real assignment

log = logging.getLogger(__name__)


# What each method means, for the screen that offers them: the methods of the
# one generation framework (sdg.engine.rules).
from sdg.engine.rules import METHODS as METHOD_HELP  # noqa: E402

DDL = f"""
IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'{LAYOUTS_TABLE}') AND type = N'U')
CREATE TABLE {LAYOUTS_TABLE} (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    name         NVARCHAR(200) NOT NULL,
    kind         NVARCHAR(10)  NOT NULL,          -- 'csv' or 'fixed'
    delimiter    NVARCHAR(5)   NULL,              -- csv only
    status       NVARCHAR(20)  NOT NULL DEFAULT 'pending_review',
    field_spec   NVARCHAR(MAX) NOT NULL,          -- parsed fields + assignments
    sample_name  NVARCHAR(400) NULL,
    created_by   NVARCHAR(100) NULL,
    created_at   DATETIME2     NOT NULL DEFAULT GETDATE(),
    approved_by  NVARCHAR(100) NULL,
    approved_at  DATETIME2     NULL,
    -- Who confirmed the approval at this computer (the operating-system
    -- user): approved_by is the second approver whose name was typed.
    confirmed_by NVARCHAR(100) NULL,
    -- Who last edited it; created_by stays its author.
    updated_by   NVARCHAR(100) NULL,
    -- What the layout is for. A generation layout assigns generation rules to
    -- its fields; a masking layout assigns masking rules. The two catalogs are
    -- separate, so a layout has to declare which one it draws from.
    purpose      NVARCHAR(20)  NOT NULL DEFAULT 'generation',
    CONSTRAINT UQ_layouts_name UNIQUE (name)
);
"""


def _conn():
    return pyodbc.connect(get_connection_string())


ALIAS_DDL = f"""
IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'{ALIASES_TABLE}') AND type = N'U')
CREATE TABLE {ALIASES_TABLE} (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    rule_id     INT           NOT NULL,
    -- One column name can only ever mean one element. The constraint is the
    -- point: without it two rules could both claim MEME_FIRST_NAME and
    -- resolution would pick whichever row came back first, which is a bug that
    -- only shows up as quietly wrong data.
    column_name NVARCHAR(200) NOT NULL UNIQUE,
    created_by  NVARCHAR(100) NULL,
    created_at  DATETIME2     NOT NULL DEFAULT GETDATE(),
    CONSTRAINT FK_gen_rule_alias_rule FOREIGN KEY (rule_id)
        REFERENCES {RULES_TABLE}(id) ON DELETE CASCADE
);
"""


def ensure_alias_table():
    conn = _conn()
    try:
        conn.cursor().execute(ALIAS_DDL)
        conn.commit()
    finally:
        conn.close()


def get_aliases() -> dict:
    """Every alias, as {COLUMN_NAME_UPPER: rule_id}."""
    ensure_alias_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT column_name, rule_id FROM {ALIASES_TABLE}")
        return {str(r[0]).strip().upper(): int(r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def aliases_by_rule() -> dict:
    """Every alias grouped by the element it belongs to, as {rule_id: [names]}."""
    out: dict = {}
    for name, rule_id in get_aliases().items():
        out.setdefault(rule_id, []).append(name)
    for names in out.values():
        names.sort()
    return out


def add_alias(rule_id: int, column_name: str, created_by: str = "user") -> dict:
    """Point a column name at an element."""
    name = (column_name or "").strip().upper()
    if not name:
        raise ValueError("An alias needs a column name.")

    ensure_alias_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT rule_id FROM {ALIASES_TABLE} WHERE UPPER(column_name) = ?",
                    name)
        existing = cur.fetchone()
        if existing:
            if int(existing[0]) == int(rule_id):
                return {"rule_id": rule_id, "column_name": name, "created": False}
            raise ValueError(
                f"“{name}” already maps to another element (rule {int(existing[0])}). "
                f"A column name can only mean one thing.")

        cur.execute(f"SELECT id, field_name FROM {RULES_TABLE} WHERE id = ?", rule_id)
        rule = cur.fetchone()
        if not rule:
            raise ValueError(f"No rule with id {rule_id}.")
        if name == str(rule[1]).strip().upper():
            raise ValueError(
                f"“{name}” is the element's own field name — it already resolves.")
        # Another element's own name always resolves to that element first, so
        # as an alias it would never be read, only listed as if it meant this.
        cur.execute(f"SELECT id FROM {RULES_TABLE} WHERE UPPER(field_name) = ?", name)
        other = cur.fetchone()
        if other:
            raise ValueError(
                f"“{name}” is another element's own name (rule {int(other[0])}). "
                f"A column name can only mean one thing.")

        cur.execute(
            f"INSERT INTO {ALIASES_TABLE} (rule_id, column_name, created_by) "
            f"VALUES (?, ?, ?)", rule_id, name, created_by)
        conn.commit()
        return {"rule_id": rule_id, "column_name": name, "created": True}
    finally:
        conn.close()


def remove_alias(column_name: str) -> dict:
    name = (column_name or "").strip().upper()
    ensure_alias_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {ALIASES_TABLE} WHERE UPPER(column_name) = ?", name)
        removed = cur.rowcount
        conn.commit()
        return {"column_name": name, "removed": int(removed)}
    finally:
        conn.close()


def resolve_column(name: str, rules: list[dict], aliases: dict) -> tuple:
    """Which element a column name means, and how it was decided.

    Returns (rule_or_None, how) where how is "exact", "alias" or "unmatched".
    The provenance matters: an aliased match is a mapping somebody configured,
    and a reviewer should be able to see that it was inferred rather than named
    outright — a column aliased to the wrong element produces plausible data
    rather than blanks, so it cannot be caught by looking at the output.
    """
    key = (name or "").strip().upper()
    by_name = {str(r["field_name"]).strip().upper(): r for r in rules}

    hit = by_name.get(key)
    if hit:
        return hit, "exact"

    rule_id = aliases.get(key)
    if rule_id is not None:
        by_id = {int(r["id"]): r for r in rules}
        hit = by_id.get(int(rule_id))
        if hit:
            return hit, "alias"

    return None, "unmatched"


# A layout is for generation. Masking is not SDM's (it is edi-mask's); layouts
# an earlier install saved as "masking" are still listed, and none is made.
PURPOSES = ("generation",)

# Added after the table shipped, so existing installs need it back-filled.
# Everything that already existed was built for generation.
PURPOSE_DDL = f"""
IF COL_LENGTH('{LAYOUTS_TABLE}', 'purpose') IS NULL
    ALTER TABLE {LAYOUTS_TABLE}
        ADD purpose NVARCHAR(20) NOT NULL DEFAULT 'generation';
"""

# Added after the table shipped: the typed approval confirmation
# (2026-11-06) and who last edited a layout (2026-11-07). A login that may not
# alter the table can't add them; 01_sdm_upgrade.sql does.
ADDED_COLUMNS = {
    "confirmed_by": f"""
IF COL_LENGTH('{LAYOUTS_TABLE}', 'confirmed_by') IS NULL
    ALTER TABLE {LAYOUTS_TABLE} ADD confirmed_by NVARCHAR(100) NULL;
""",
    "updated_by": f"""
IF COL_LENGTH('{LAYOUTS_TABLE}', 'updated_by') IS NULL
    ALTER TABLE {LAYOUTS_TABLE} ADD updated_by NVARCHAR(100) NULL;
""",
}


class LayoutsTableOutdated(RuntimeError):
    """sdm.layouts lacks a column SDM needs and it could not be added here."""


class LayoutArchived(ValueError):
    """An archived layout is edited only once restored."""


def _missing_columns(cur) -> list[str]:
    return [name for name in ADDED_COLUMNS
            if cur.execute("SELECT COL_LENGTH(?, ?)", LAYOUTS_TABLE, name).fetchone()[0] is None]


def ensure_table():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        cur.execute(PURPOSE_DDL)
        conn.commit()
        for ddl in ADDED_COLUMNS.values():
            try:
                cur.execute(ddl)
                conn.commit()
            except pyodbc.Error:
                # No right to alter the table: said below, by what is missing.
                conn.rollback()
        missing = _missing_columns(cur)
        if missing:
            raise LayoutsTableOutdated(
                "Run 01_sdm_upgrade.sql: sdm.layouts needs the "
                + " and ".join(missing)
                + (" column" if len(missing) == 1 else " columns"))
    finally:
        conn.close()


# ── Parsing ──────────────────────────────────────────────────────────────────

def _infer_type(values: list[str]) -> str:
    """Best-fit type from sample values: int, decimal, date, or text."""
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return "text"

    def all_match(pred):
        return all(pred(v) for v in vals)

    # Eight digits that are all real dates (YYYYMMDD) are dates, not numbers:
    # typed int, they would skip date formatting and be right-aligned.
    if all_match(_is_yyyymmdd):
        return "date"
    if all_match(lambda v: re.fullmatch(r"-?\d+", v)):
        return "int"
    if all_match(lambda v: re.fullmatch(r"-?\d*\.\d+", v)):
        return "decimal"
    date_pats = (r"\d{4}-\d{2}-\d{2}", r"\d{2}/\d{2}/\d{4}", r"\d{8}")
    if all_match(lambda v: any(re.fullmatch(p, v) for p in date_pats)):
        return "date"
    return "text"


def _is_yyyymmdd(value: str) -> bool:
    """An eight-digit value that is a calendar date between 1900 and 2099."""
    if not re.fullmatch(r"(19|20)\d{6}", value):
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def _detect_date_format(values: list[str]) -> Optional[str]:
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return None
    sample = vals[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", sample):
        return "YYYY-MM-DD"
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", sample):
        return "MM/DD/YYYY"
    if re.fullmatch(r"\d{8}", sample):
        return "YYYYMMDD"
    return None


def parse_csv(text: str) -> dict:
    """Header row → field names, sniff delimiter, infer type + width per column."""
    sample = text[:8192]
    try:
        dialect = csvmod.Sniffer().sniff(sample, delimiters=",\t|;")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ","

    reader = csvmod.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if r]
    if not rows:
        raise ValueError("The file has no rows.")

    header = rows[0]
    body = rows[1:]
    fields = []
    for i, name in enumerate(header):
        col_vals = [r[i] for r in body if i < len(r)]
        fields.append({
            "name": (name or f"FIELD_{i+1}").strip(),
            "index": i,
            "type": _infer_type(col_vals),
            "date_format": _detect_date_format(col_vals),
            "max_width": max([len(str(v)) for v in col_vals] + [len(name)]),
            "samples": [v for v in col_vals[:3]],
        })
    return {
        "kind": "csv", "delimiter": delimiter,
        "fields": fields, "row_count": len(body),
    }


def parse_fixed(text: str) -> dict:
    """Infer column boundaries from positions that are blank in every data row.

    A run of columns that is whitespace across all rows is a field gap. The
    spans between gaps are the fields. Works cleanly on padded data; the review
    step exists to fix the cases where a text field's own spaces blur a gap.
    """
    lines = [ln.rstrip("\n\r") for ln in text.split("\n") if ln.strip()]
    if not lines:
        raise ValueError("The file has no rows.")

    width = max(len(ln) for ln in lines)
    lines = [ln.ljust(width) for ln in lines]

    # A column position is a separator if it is a space in every row.
    is_sep = [all(ln[pos] == " " for ln in lines) for pos in range(width)]

    # Field spans are maximal runs of non-separator positions.
    spans = []
    start = None
    for pos in range(width):
        if not is_sep[pos] and start is None:
            start = pos
        elif is_sep[pos] and start is not None:
            spans.append((start, pos))
            start = None
    if start is not None:
        spans.append((start, width))

    # A header row, if the first line reads like one. Without it every field is
    # FIELD_1, FIELD_2, nothing matches a generated column, and the extract
    # comes out blank -- a 200 and an empty file, which is the worst way for
    # this to fail. The names can still be corrected afterwards; this just
    # means usually not having to.
    header = _fixed_header(lines, spans)
    body = lines[1:] if header else lines

    fields = []
    for i, (s, e) in enumerate(spans):
        col_vals = [ln[s:e].strip() for ln in body]
        fields.append({
            "name": header[i] if header else f"FIELD_{i+1}",
            "start": s, "length": e - s,
            "type": _infer_type(col_vals),
            "date_format": _detect_date_format(col_vals),
            "max_width": e - s,
            "samples": [v for v in col_vals[:3] if v],
        })
    return {"kind": "fixed", "delimiter": None, "fields": fields,
            "row_count": len(body), "header_row": bool(header)}


def _fixed_header(lines: list[str], spans: list[tuple[int, int]]) -> list[str] | None:
    """The first line's cells, if they are names rather than data.

    Telling one from the other has no certain answer -- a file of surnames has
    a first row that looks exactly like a row of identifiers. So the test is
    deliberately narrow: every cell has to be identifier-shaped, and at least
    one has to carry the mark of a column name rather than a value, which in
    practice means an underscore or being written in capitals.

    Narrow in the safe direction. Missing a header leaves the old behavior and
    a rename to do; inventing one would silently label data as field names.
    """
    if len(lines) < 2:
        return None

    cells = [lines[0][s:e].strip() for s, e in spans]
    if not all(cells):
        return None
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]*", cell) for cell in cells):
        return None

    # Capitals alone are not enough: an identifier column of codes like
    # ABC00000001 is upper-case too, and treating it as a header eats a row of
    # data and names every field after a value. A separator, or capitals with
    # no digits in them, is the mark of a name rather than a value.
    looks_named = any(
        "_" in cell
        or "-" in cell
        or (cell.isupper() and len(cell) > 1 and not any(c.isdigit() for c in cell))
        for cell in cells
    )
    if not looks_named:
        return None

    # And the row below has to differ, or this is two rows of the same thing.
    below = [lines[1][s:e].strip() for s, e in spans]
    if below == cells:
        return None
    return cells


def parse_sample(filename: str, content: bytes) -> dict:
    """Dispatch on extension, fall back to content sniffing."""
    text = content.decode("utf-8", errors="replace")
    name = (filename or "").lower()
    if name.endswith((".csv", ".tsv", ".txt")) and ("," in text[:2000] or "\t" in text[:2000]):
        return parse_csv(text)
    if name.endswith((".csv", ".tsv")):
        return parse_csv(text)
    # Default to fixed-width for .dat/.txt without obvious delimiters.
    if "," in text[:2000] or "|" in text[:2000] or "\t" in text[:2000]:
        return parse_csv(text)
    return parse_fixed(text)


# ── Generation rules ─────────────────────────────────────────────────────────

# What a measurement needs and the first columns could not hold: the range a
# number was seen in, how many places it carries, and how often each value of a
# list occurs. Without them a rule knew units were two digits wide and wrote 75,
# and knew the six claim statuses but not that one of them is seventy per cent.
RULE_MEASURES_DDL = f"""
IF COL_LENGTH('{RULES_TABLE}', 'min_value') IS NULL
    ALTER TABLE {RULES_TABLE} ADD min_value FLOAT NULL;
IF COL_LENGTH('{RULES_TABLE}', 'max_value') IS NULL
    ALTER TABLE {RULES_TABLE} ADD max_value FLOAT NULL;
IF COL_LENGTH('{RULES_TABLE}', 'decimals') IS NULL
    ALTER TABLE {RULES_TABLE} ADD decimals INT NULL;
IF COL_LENGTH('{RULES_TABLE}', 'weights') IS NULL
    ALTER TABLE {RULES_TABLE} ADD weights NVARCHAR(MAX) NULL;
IF COL_LENGTH('{RULES_TABLE}', 'distribution') IS NULL
    ALTER TABLE {RULES_TABLE} ADD distribution NVARCHAR(20) NULL;
IF COL_LENGTH('{RULES_TABLE}', 'mean_value') IS NULL
    ALTER TABLE {RULES_TABLE} ADD mean_value FLOAT NULL;
IF COL_LENGTH('{RULES_TABLE}', 'stddev_value') IS NULL
    ALTER TABLE {RULES_TABLE} ADD stddev_value FLOAT NULL;
"""

_measures_ready = False


def _ensure_rule_measures() -> None:
    global _measures_ready
    if _measures_ready:
        return
    conn = _conn()
    try:
        conn.cursor().execute(RULE_MEASURES_DDL)
        conn.commit()
        _measures_ready = True
    finally:
        conn.close()


def get_rules() -> list[dict]:
    """Active generation rules, for assignment dropdowns."""
    _ensure_rule_measures()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, field_name, field_label, field_category, element,
                   gen_method, data_type, length, prefix, postfix, start_value,
                   domain, format_pattern, lookup_source,
                   min_value, max_value, decimals, weights,
                   distribution, mean_value, stddev_value
            FROM {RULES_TABLE}
            WHERE active = 1
            ORDER BY field_name
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def rules_by_element() -> dict[str, dict]:
    """Every rule that names an element, keyed by it."""
    out: dict[str, dict] = {}
    for rule in get_rules():
        element = str(rule.get("element") or "").strip().upper()
        if element and element not in out:
            out[element] = rule
    return out


def create_rule(rule: dict, created_by: str = "user") -> int:
    """Insert a new generation rule and return its id."""
    _ensure_rule_measures()
    conn = _conn()
    try:
        cur = conn.cursor()
        from sdg.analyzer.db import insert_row

        rule_id = insert_row(cur, RULES_TABLE, {
            "field_name": rule["field_name"],
            "field_label": rule.get("field_label") or rule["field_name"],
            "field_category": rule.get("field_category", "custom"),
            # A rule is named by the element it produces; one made by hand and
            # given only a name is that element.
            "element": str(rule.get("element") or rule["field_name"]).strip().upper(),
            "gen_method": rule["gen_method"],
            "data_type": rule.get("data_type", "text"), "length": rule.get("length"),
            "prefix": rule.get("prefix"), "postfix": rule.get("postfix"),
            "start_value": rule.get("start_value"), "domain": rule.get("domain"),
            "format_pattern": rule.get("format_pattern"),
            "lookup_source": rule.get("lookup_source"), "min_value": rule.get("min_value"),
            "max_value": rule.get("max_value"), "decimals": rule.get("decimals"),
            "weights": rule.get("weights"), "distribution": rule.get("distribution"),
            "mean_value": rule.get("mean_value"), "stddev_value": rule.get("stddev_value"),
            "active": 1, "updated_by": created_by, "updated_at": datetime.now(),
        })
        conn.commit()
        return rule_id
    finally:
        conn.close()


RULE_COLUMNS = (
    "id", "field_name", "field_label", "field_category", "element", "gen_method",
    "data_type", "length", "prefix", "postfix", "start_value", "domain",
    "format_pattern", "lookup_source", "min_value", "max_value", "decimals",
    "weights", "distribution", "mean_value", "stddev_value",
    "active", "updated_by", "updated_at",
)

# Fields a rule edit is allowed to change. field_name is excluded on purpose:
# it is the key layouts and generated columns are matched on, so renaming one
# would silently detach every layout field pointing at it.
EDITABLE = (
    "field_label", "field_category", "element", "gen_method", "data_type",
    "length", "prefix", "postfix", "start_value", "domain", "format_pattern",
    "lookup_source", "min_value", "max_value", "decimals", "weights",
    "distribution", "mean_value", "stddev_value",
)


def get_all_rules() -> list[dict]:
    """Every rule, active or not — the central repository view."""
    _ensure_rule_measures()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT {", ".join(RULE_COLUMNS)}
            FROM {RULES_TABLE}
            ORDER BY active DESC, field_name
        """)
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            rule = dict(zip(cols, row))
            rule["id"] = int(rule["id"])
            rule["active"] = bool(rule["active"])
            if rule.get("updated_at") is not None:
                rule["updated_at"] = rule["updated_at"].isoformat()
            out.append(rule)
        return out
    finally:
        conn.close()


def update_rule(rule_id: int, changes: dict, updated_by: str = "user") -> dict:
    """Update an existing rule. Every layout referencing it picks the change up
    on the next generate, since layouts store rule ids and resolve them fresh."""
    fields = [k for k in EDITABLE if k in changes]
    if not fields:
        raise ValueError("No editable fields were supplied.")

    sets = ", ".join(f"{k} = ?" for k in fields)
    values = [changes[k] for k in fields]

    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {RULES_TABLE} SET {sets}, updated_by = ?, updated_at = ? "
            f"WHERE id = ?",
            *values, updated_by, datetime.now(), rule_id,
        )
        if cur.rowcount == 0:
            raise ValueError(f"No rule with id {rule_id}.")
        conn.commit()
    finally:
        conn.close()
    return {"id": rule_id, "updated": fields}


def set_rule_active(rule_id: int, active: bool, updated_by: str = "user") -> dict:
    """Retire or restore a rule.

    Deactivating is deliberately not a delete: a layout may still reference the
    rule, and an id that vanished would leave that field unresolvable with no
    record of what it used to be.
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {RULES_TABLE} SET active = ?, updated_by = ?, updated_at = ? "
            f"WHERE id = ?",
            1 if active else 0, updated_by, datetime.now(), rule_id,
        )
        if cur.rowcount == 0:
            raise ValueError(f"No rule with id {rule_id}.")
        conn.commit()
    finally:
        conn.close()
    return {"id": rule_id, "active": active}


def rule_usage() -> dict[int, list[str]]:
    """Which layouts reference each rule, so the UI can warn before retiring
    one that is in use."""
    usage: dict[int, list[str]] = {}
    for layout in list_layouts():
        detail = get_layout(layout["id"])
        if not detail:
            continue
        for f in detail["fields"]:
            rid = f.get("rule_id")
            if rid is None:
                continue
            usage.setdefault(int(rid), [])
            if detail["name"] not in usage[int(rid)]:
                usage[int(rid)].append(detail["name"])
    return usage


def auto_assign(fields: list[dict], rules: list[dict]) -> list[dict]:
    """Attach an element to every field.

    Resolution runs exact field_name first, then the alias table, then falls
    back. Aliases are what let one element answer to the several column names
    that mean it — MEME_FIRST_NAME and SBSB_FIRST_NAME are both a first name,
    and a customer layout will call it something else again.

    Each field records `matched_by`, because the three outcomes are not equally
    trustworthy. An exact match is unambiguous. An alias match is a mapping
    somebody configured, and if it is wrong the field produces plausible data
    instead of blanks — invisible in the output, so it has to be visible here.
    Unmatched still means blank, and is still flagged.
    """
    try:
        aliases = get_aliases()
    except pyodbc.Error as exc:
        # An alias table that cannot be read or created must not stop a layout
        # being parsed; nothing resolves by alias, and the log says why.
        log.warning("Layout fields were assigned without aliases: the alias "
                    "table could not be read (%s). Fields known only by an "
                    "alias are left unassigned.", exc)
        aliases = {}

    out = []
    for f in fields:
        match, how = resolve_column(f["name"], rules, aliases)
        if match:
            f = {**f, "rule_id": match["id"],
                 "rule_label": match["field_label"],
                 "gen_method": match["gen_method"],
                 "matched_by": how,
                 # A pattern-method element is assigned, not defaulted: its
                 # value comes from the pattern rather than from a generator.
                 "is_default": False}
        else:
            f = {**f, "rule_id": None,
                 "rule_label": "DEFAULT — no rule assigned (blank output)",
                 "gen_method": DEFAULT_METHOD,
                 "matched_by": how,
                 "is_default": True}
        out.append(f)
    return out


def alias_chosen(fields: list[dict]) -> dict:
    """Remember the rules somebody picked for a layout's fields.

    A field read from a sample whose name matched nothing, and that a person
    then pointed at an element, is a name that means that element. Kept as an
    alias, the next sample calling it the same thing binds without being asked.
    A name that already means another element is left as it is -- the layout
    still binds by the rule chosen, and the clash is reported, not resolved.
    """
    added: list[str] = []
    refused: list[dict] = []
    for f in fields:
        if f.get("matched_by") != "chosen" or f.get("rule_id") is None:
            continue
        try:
            result = add_alias(int(f["rule_id"]), str(f.get("name") or ""))
        except ValueError as e:
            if "own field name" not in str(e):
                refused.append({"name": f.get("name"), "reason": str(e)})
            continue
        if result.get("created"):
            added.append(result["column_name"])
    return {"aliased": added, "not_aliased": refused}


# ── Layout persistence ───────────────────────────────────────────────────────

def save_layout(name: str, kind: str, delimiter: Optional[str],
                fields: list[dict], sample_name: str,
                created_by: str = "user",
                purpose: str = "generation") -> dict:
    """Persist a layout as pending_review. Every field carries a rule already.

    Saving over an existing layout of the same name replaces it and sends it
    back to review — correct, because the fields changed and the old approval
    attested to the old ones. Its author stays; the person saving over it is
    recorded as who last changed it (updated_by).

    An archived layout of the same name is not overwritten: archiving keeps an
    approval nothing else records. It is renamed "<name> (archived <id>)",
    which frees the name a person dropped it to reuse.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of: {', '.join(PURPOSES)}.")

    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, status FROM {LAYOUTS_TABLE} WHERE name = ? AND status = ?",
            name, "archived")
        clash = cur.fetchone()
        if clash:
            # A dropped layout's name is free again. The archived row keeps its
            # approval under a name of its own, so nothing it records is lost
            # and the person saving is not refused for a layout they dropped.
            kept = f"{name} (archived {int(clash[0])})"[:200]
            cur.execute(f"UPDATE {LAYOUTS_TABLE} SET name = ? WHERE id = ?",
                        kept, int(clash[0]))

        cur.execute(f"""
            MERGE {LAYOUTS_TABLE} WITH (HOLDLOCK) AS t
            USING (SELECT ? AS name) AS s ON t.name = s.name
            WHEN MATCHED THEN UPDATE SET
                kind = ?, delimiter = ?, status = 'pending_review',
                field_spec = ?, sample_name = ?, updated_by = ?,
                purpose = ?, approved_by = NULL, approved_at = NULL,
                confirmed_by = NULL
            WHEN NOT MATCHED THEN INSERT
                (name, kind, delimiter, status, field_spec, sample_name,
                 created_by, created_at, purpose)
                VALUES (?, ?, ?, 'pending_review', ?, ?, ?, ?, ?);
        """,
            name,
            kind, delimiter, json.dumps(fields), sample_name, created_by,
            purpose,
            name, kind, delimiter, json.dumps(fields), sample_name, created_by,
            datetime.now(), purpose,
        )
        conn.commit()
    finally:
        conn.close()

    default_count = sum(1 for f in fields if f.get("is_default"))
    return {"name": name, "status": "pending_review", "purpose": purpose,
            "n_fields": len(fields), "n_default": default_count}


def check_field_names(fields: list[dict]) -> None:
    """Every field named, and no two the same.

    A delimited file's header is its field names, and a record is keyed by
    them on the way out, so two fields of one name write one value twice.
    """
    seen = set()
    for f in fields:
        field_name = str(f.get("name") or "").strip()
        if not field_name:
            raise ValueError("Every field needs a name.")
        key = _norm(field_name)
        if key in seen:
            raise ValueError(
                f"Two fields are both called '{field_name}'. Field names have to "
                f"be distinct — they are what the generated columns match on."
            )
        seen.add(key)


def update_layout(layout_id: int, fields: Optional[list[dict]] = None,
                  name: Optional[str] = None, delimiter: Optional[str] = None,
                  keep_approval: bool = False,
                  updated_by: str = "user") -> dict:
    """Edit an existing layout, approved or not.

    Editing an approved layout resets it to pending_review by default: the
    approval attested to a specific set of fields and rules, so silently
    changing them behind that approval would make the status meaningless. Pass
    keep_approval=True for a correction that does not change what gets
    generated — renaming a fixed-width FIELD_3 to CITY, say.

    Renaming a field is the reason this exists. parse_fixed names every field
    FIELD_1, FIELD_2, so nothing matches a generated column and every field
    renders blank; there was no way to correct that short of re-uploading.
    """
    current = get_layout(layout_id)
    if not current:
        raise ValueError(f"No layout with id {layout_id}.")
    if current["status"] == "archived":
        # Out of service, its approval kept: brought back before it changes.
        raise LayoutArchived(
            f"“{current['name']}” is archived. Restore it to edit it.")

    new_fields = fields if fields is not None else current["fields"]
    new_name = name if name is not None else current["name"]
    new_delim = delimiter if delimiter is not None else current.get("delimiter")

    if not new_fields:
        raise ValueError("A layout needs at least one field.")

    check_field_names(new_fields)

    was_approved = current["status"] == "approved"
    status = "approved" if (was_approved and keep_approval) else "pending_review"

    conn = _conn()
    try:
        cur = conn.cursor()
        # created_by stays the layout's author; updated_by is who edited it.
        if status == "approved":
            cur.execute(
                f"UPDATE {LAYOUTS_TABLE} SET name = ?, delimiter = ?, "
                f"field_spec = ?, updated_by = ? WHERE id = ? AND status <> 'archived'",
                new_name, new_delim, json.dumps(new_fields), updated_by, layout_id,
            )
        else:
            cur.execute(
                f"UPDATE {LAYOUTS_TABLE} SET name = ?, delimiter = ?, "
                f"field_spec = ?, status = 'pending_review', "
                f"approved_by = NULL, approved_at = NULL, confirmed_by = NULL, "
                f"updated_by = ? "
                f"WHERE id = ? AND status <> 'archived'",
                new_name, new_delim, json.dumps(new_fields), updated_by, layout_id,
            )
        if cur.rowcount == 0:
            raise ValueError(f"No layout with id {layout_id}.")
        conn.commit()
    finally:
        conn.close()

    return {
        "id": layout_id,
        "name": new_name,
        "status": status,
        "n_fields": len(new_fields),
        "n_default": sum(1 for f in new_fields if f.get("is_default")),
        "reapproval_required": was_approved and status != "approved",
    }


def list_layouts(include_archived: bool = False) -> list[dict]:
    """Saved layouts, newest first. Archived ones are hidden unless asked for.

    An archived layout is out of service but kept — the list would otherwise
    fill with things nobody can use, which is the reason retired rules are
    hidden the same way.
    """
    # A table that can't be read is said, never shown as no layouts.
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        where = "" if include_archived else "WHERE status <> 'archived'"
        cur.execute(f"""
            SELECT id, name, kind, delimiter, status, field_spec, sample_name,
                   created_by, created_at, approved_by, approved_at, purpose,
                   confirmed_by, updated_by
            FROM {LAYOUTS_TABLE} {where} ORDER BY created_at DESC
        """)
        out = []
        for r in cur.fetchall():
            fields = json.loads(r[5]) if r[5] else []
            out.append({
                "id": int(r[0]), "name": r[1], "kind": r[2], "delimiter": r[3],
                "status": r[4], "n_fields": len(fields),
                "n_default": sum(1 for f in fields if f.get("is_default")),
                "sample_name": r[6], "created_by": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
                "approved_by": r[9],
                "approved_at": r[10].isoformat() if r[10] else None,
                "purpose": r[11] or "generation",
                "confirmed_by": r[12],
                "updated_by": r[13],
            })
        return out
    finally:
        conn.close()


def get_layout(layout_id: int) -> Optional[dict]:
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, name, kind, delimiter, status, field_spec, sample_name,
                   created_by, created_at, approved_by, approved_at, purpose,
                   confirmed_by, updated_by
            FROM {LAYOUTS_TABLE} WHERE id = ?
        """, layout_id)
        r = cur.fetchone()
        if not r:
            return None
        return {
            "id": int(r[0]), "name": r[1], "kind": r[2], "delimiter": r[3],
            "status": r[4], "fields": json.loads(r[5]) if r[5] else [],
            "sample_name": r[6], "created_by": r[7],
            "created_at": r[8].isoformat() if r[8] else None,
            "approved_by": r[9],
            "approved_at": r[10].isoformat() if r[10] else None,
            "purpose": r[11] or "generation",
            "confirmed_by": r[12],
            "updated_by": r[13],
        }
    finally:
        conn.close()


def approve_layout(layout_id: int, approved_by: str = "user",
                   confirmed_by: Optional[str] = None) -> dict:
    """Flip pending_review to approved, and nothing else: an archived layout
    is not brought back by an approval (restore does that), and an approved
    one keeps the approval it has. approved_by is the approver named;
    confirmed_by, who confirmed it at this computer."""
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE {LAYOUTS_TABLE}
            SET status = 'approved', approved_by = ?, approved_at = ?,
                confirmed_by = ?
            WHERE id = ? AND status = 'pending_review'
        """, approved_by, datetime.now(), confirmed_by, layout_id)
        if cur.rowcount == 0:
            conn.rollback()
            cur.execute(f"SELECT name, status FROM {LAYOUTS_TABLE} WHERE id = ?",
                        layout_id)
            row = cur.fetchone()
            if not row:
                raise ValueError(f"No layout with id {layout_id}.")
            raise ValueError(
                f"“{row[0]}” is {str(row[1]).replace('_', ' ')}: only a layout "
                f"waiting for review can be approved.")
        conn.commit()
    finally:
        conn.close()
    return {"id": layout_id, "status": "approved", "approved_by": approved_by,
            "confirmed_by": confirmed_by}


def withdraw_approval(layout_id: int) -> dict:
    """Send an approved layout back to review.

    The counterpart to approve. An approval attests to a specific set of fields
    and rules; when that attestation no longer holds — a rule was repointed, or
    the reviewer changed their mind — the layout should stop being selectable
    downstream without anyone having to delete and rebuild it.

    Editing already does this implicitly (update_layout resets the status unless
    keep_approval is set). This is the explicit version, for when nothing about
    the fields is changing.
    """
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT name, status FROM {LAYOUTS_TABLE} WHERE id = ?",
                    layout_id)
        row = cur.fetchone()
        if not row:
            raise ValueError(f"No layout with id {layout_id}.")
        if str(row[1]) != "approved":
            raise ValueError(
                f"“{row[0]}” is not approved — it is already {row[1]}.")

        cur.execute(f"""
            UPDATE {LAYOUTS_TABLE}
            SET status = 'pending_review', approved_by = NULL, approved_at = NULL,
                confirmed_by = NULL
            WHERE id = ?
        """, layout_id)
        conn.commit()
        return {"id": layout_id, "name": str(row[0]), "status": "pending_review"}
    finally:
        conn.close()


def archive_layout(layout_id: int) -> dict:
    """Take an approved layout out of service without destroying it.

    Approval is a record that somebody reviewed a specific set of fields and
    rules. Deleting an approved layout throws that away, and nothing else in the
    system remembers it happened — so approved layouts are archived instead.
    approved_by and approved_at are deliberately kept: the archive is the review,
    not just the field list.

    Archived layouts drop out of the default list and out of /layouts/approved,
    so they stop being selectable for generation.
    """
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT name, status FROM {LAYOUTS_TABLE} WHERE id = ?",
                    layout_id)
        row = cur.fetchone()
        if not row:
            raise ValueError(f"No layout with id {layout_id}.")
        if str(row[1]) == "archived":
            raise ValueError(f"“{row[0]}” is already archived.")

        cur.execute(f"UPDATE {LAYOUTS_TABLE} SET status = 'archived' WHERE id = ?",
                    layout_id)
        conn.commit()
        return {"id": layout_id, "name": str(row[0]),
                "was_status": str(row[1]), "status": "archived",
                "action": "archived"}
    finally:
        conn.close()


def restore_layout(layout_id: int) -> dict:
    """Bring an archived layout back.

    It returns to approved, because only an approved layout is ever archived and
    its approved_by/approved_at were kept intact. Archiving is reversible and
    loses nothing; that is the whole point of preferring it to a delete.
    """
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT name, status, approved_by FROM {LAYOUTS_TABLE} WHERE id = ?",
            layout_id)
        row = cur.fetchone()
        if not row:
            raise ValueError(f"No layout with id {layout_id}.")
        if str(row[1]) != "archived":
            raise ValueError(f"“{row[0]}” is not archived — it is {row[1]}.")

        # An archived layout was approved when it went in; without an approver
        # recorded there is nothing to restore it to, so it goes back to review.
        back = "approved" if row[2] else "pending_review"
        cur.execute(f"UPDATE {LAYOUTS_TABLE} SET status = ? WHERE id = ?",
                    back, layout_id)
        conn.commit()
        return {"id": layout_id, "name": str(row[0]), "status": back,
                "action": "restored"}
    finally:
        conn.close()


def remove_layout(layout_id: int) -> dict:
    """Take a layout out of service, by whichever route suits its state.

    Approved layouts are archived; anything else is deleted outright. The split
    is about what would be lost: a pending layout carries no review, so deleting
    it discards a definition somebody can re-upload from the same sample. An
    approved one carries an attestation that cannot be reconstructed.
    """
    ensure_table()
    conn = _conn()
    try:
        # The status is read and the row deleted in one transaction, so a
        # layout approved in between is archived, never deleted.
        removed = _delete(conn, layout_id, keep_approved=True)
    finally:
        conn.close()

    if removed is None:
        return archive_layout(layout_id)
    return removed


def delete_layout(layout_id: int) -> dict:
    """Delete a layout outright. Used for layouts with no approval to preserve.

    A hard delete is safe here: a layout stores rule ids, but nothing anywhere
    stores a layout id. Generation takes one per request and keeps none, and no
    table has a foreign key to this one.

    Returns what was removed, including its approval, so the caller can say what
    it actually deleted rather than just confirming an id.

    It deletes whatever the status, approved included: the screens and the API
    go through remove_layout, which archives an approved layout instead. Call
    this directly only to clear a layout on purpose (test teardown).
    """
    ensure_table()
    conn = _conn()
    try:
        return _delete(conn, layout_id, keep_approved=False)
    finally:
        conn.close()


def _delete(conn, layout_id: int, *, keep_approved: bool) -> Optional[dict]:
    """Delete one layout row; with keep_approved, an approved one is left and
    None returned. The read takes an update lock held to the delete, so the
    status cannot change between the check and the delete."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT name, status, approved_by, approved_at "
        f"FROM {LAYOUTS_TABLE} WITH (UPDLOCK, HOLDLOCK) WHERE id = ?", layout_id)
    row = cur.fetchone()
    if not row:
        conn.rollback()
        raise ValueError(f"No layout with id {layout_id}.")
    if keep_approved and str(row[1]) == "approved":
        conn.rollback()
        return None

    cur.execute(f"DELETE FROM {LAYOUTS_TABLE} WHERE id = ?", layout_id)
    conn.commit()
    return {
        "id": layout_id, "name": str(row[0]), "was_status": str(row[1]),
        "was_approved_by": row[2],
        "removed": True, "action": "deleted",
    }


# ── Generation through a layout (option B: pattern data wins) ─────────────────

def _norm(name: str) -> str:
    """Collapse a field name to a comparable key: case, spaces, and separators
    are all noise when matching a sample header to a generated column."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _rules_by_id() -> dict:
    """Current central rules, keyed by id — resolved fresh at generate time so a
    changed shared rule is reflected, since layouts only reference rule ids."""
    return {r["id"]: r for r in get_rules()}


def _rule_value(rule: Optional[dict], row_idx: int, field_name: str = "") -> str:
    """Fill a layout field the generated dataset has no column for.

    Deliberately limited to deterministic, structural fills — a constant, a
    sequence, a format pattern. Lookup-method fields (names, addresses) are
    *not* invented here: drawing a fresh name would put a person in the file
    who does not exist in the generated dataset, so those come back blank and
    are reported as unmapped instead.
    """
    if not rule:
        return ""
    method = (rule.get("gen_method") or "").lower()
    if method == "constant":
        return str(rule.get("prefix") or "")
    if method in ("sequence", "sequential"):
        start = int(rule.get("start_value") or 1)
        prefix = str(rule.get("prefix") or "")
        postfix = str(rule.get("postfix") or "")
        length = int(rule.get("length") or 0)
        num = str(start + row_idx)
        if length > 0:
            num = num.zfill(max(1, length - len(prefix) - len(postfix)))
        out = f"{prefix}{num}{postfix}"
        return out[:length] if length > 0 and len(out) > length else out
    if method == "pattern":
        return str(rule.get("format_pattern") or "")
    return ""


def _format_field(value, field: dict, kind: str) -> str:
    """Render one field's value per the layout kind and the field's geometry."""
    s = "" if value is None else str(value)

    # Apply the sample's date format when the field is a date and the value is ISO.
    if field.get("type") == "date" and field.get("date_format") and s:
        try:
            from datetime import datetime as _dt
            d = _dt.fromisoformat(s[:10])
            fmt = field["date_format"]
            s = (d.strftime("%Y%m%d") if fmt == "YYYYMMDD"
                 else d.strftime("%m/%d/%Y") if fmt == "MM/DD/YYYY"
                 else d.strftime("%Y-%m-%d"))
        except ValueError:
            # Not an ISO date (already in another form): written as it is,
            # and the width check below still applies.
            pass

    if kind == "fixed":
        width = int(field.get("length") or field.get("max_width") or len(s))
        if len(s) > width:
            # Cutting the value would write a wrong record with no warning.
            # The value itself is not named: it may be a person's data.
            raise ValueError(
                f"A value for {field.get('name')!r} is {len(s)} characters, wider "
                f"than its {width}-character field. Widen the field in the "
                f"layout, or shorten the values it takes.")
        # Numbers right-aligned, text left-aligned — the usual flat-file convention.
        return s.rjust(width) if field.get("type") in ("int", "decimal") else s.ljust(width)
    return s


def unmapped_fields(df, layout: dict) -> list[str]:
    """Layout fields the generated dataset has no column for and no rule can
    honestly fill. Reported so a blank column is visible rather than silent."""
    rules = _rules_by_id()
    cols = {_norm(c) for c in df.columns}
    out = []
    for f in layout["fields"]:
        rule = rules.get(f.get("rule_id"))
        rule_col = _norm(rule.get("field_name") or "") if rule else ""
        if _norm(f["name"]) in cols or (rule_col and rule_col in cols):
            continue
        method = (rule.get("gen_method") or "").lower() if rule else ""
        if method in ("constant", "sequence", "sequential", "pattern"):
            continue
        out.append(f["name"])
    return out


def render_through_layout(df, layout: dict) -> str:
    """Format already-generated rows through an approved layout.

    No data is created here. Each layout field takes its value from the
    generated dataset — matched on the field's own name, or on the name of the
    rule assigned to it. A field the dataset doesn't cover falls to a
    deterministic rule fill, then to blank.
    """
    fields = layout["fields"]
    kind = layout["kind"]
    delimiter = layout.get("delimiter") or ","
    rules = _rules_by_id()

    # Match on a normalized key. A sample header reads "Member_First_Name"; the
    # generated frame reads "MEME_FIRST_NAME". An exact, case-sensitive match
    # missed those and silently dropped the field through to a blank.
    cols = {_norm(c): c for c in df.columns}

    # A layout field also reaches the dataset through its assigned rule: the
    # rule's field_name is the generated column name.
    resolved = []
    for f in fields:
        rule = rules.get(f.get("rule_id"))
        col = cols.get(_norm(f["name"]))
        if col is None and rule:
            col = cols.get(_norm(rule.get("field_name") or ""))
        resolved.append((f, col, rule))

    lines = []
    if kind == "csv":
        lines.append(_csv_line([f["name"] for f in fields], delimiter))

    for i, row in enumerate(df.to_dict("records")):
        out = []
        for f, col, rule in resolved:
            if col is not None:                          # 1. generated data
                value = row.get(col)
            elif not f.get("is_default"):                # 2. deterministic fill
                value = _rule_value(rule, i, f["name"])
            else:                                        # 3. default → blank
                value = ""
            out.append(_format_field(value, f, kind))
        lines.append(_csv_line(out, delimiter) if kind == "csv" else "".join(out))

    return "\n".join(lines)


def _csv_line(values: list[str], delimiter: str) -> str:
    """One CSV line, quoted where a value holds the delimiter, a quote or a
    line break ("12 Main St, Apt 4"), so no value shifts the columns after it.
    Values without one are written exactly as before. A delimiter of more
    than one character ("||"), which the csv module cannot write, is joined,
    each value quoted the same way where it needs it."""
    if len(delimiter) != 1:
        return delimiter.join(_quoted(str(v), delimiter) for v in values)
    buffer = io.StringIO()
    csvmod.writer(buffer, delimiter=delimiter, quoting=csvmod.QUOTE_MINIMAL,
                  lineterminator="").writerow(values)
    return buffer.getvalue()


def _quoted(value: str, delimiter: str) -> str:
    """A value in double quotes, its quotes doubled, where it holds the
    delimiter, a quote or a line break; otherwise as it is."""
    if delimiter in value or any(c in value for c in '"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value

"""What a pattern holds, field by field, as counts and percentages.

One shape for every view of a pattern -- the card, its scenarios, the export:
a row per field and value with how many members (or claims, or claim lines)
have it and what share of the pattern that is; a row per scenario with how
many it has and its share of the pattern. Numbers
are banded (age in years, claim amount in dollars) so they count like any
other value. Nothing is hidden: a scenario of two members is listed with its
two, since a rare scenario can be the one that matters.

A member pattern also says what its members' claims look like -- the amount,
the diagnoses and procedures -- counted over all of the pattern's claims,
never per member. A claims pattern says who its patients are.

Each row says whether generation refers to the field (`GENERATION`), so a
reader can see what a pattern will and will not carry into generated data.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

import pandas as pd

from .scenarios import AGE_BANDS, DEFAULT_SPLIT, NONE, _split, _labels
from sdg import packs
from sdg.packs import held, is_child, is_primary

# Claim totals in dollars: (low, high, label), high exclusive.
AMOUNT_BANDS = [tuple(b) for b in held("features:patterns.amount_bands")]

# Each field: the column it is read from, its description column, how its
# values are made (bands), and what is counted: members, claims or claim lines.
FIELDS: dict[str, list[dict[str, Any]]] = held("features:patterns.report_fields")

# Whether generation uses each field: one of two answers, the same for every
# pattern. A claims pattern's group, plan and product are referred to through
# its members: the claims go to the pattern's own members.
YES = "Yes, referred into data generation"
NO = "No, not referred into and not generated"
GENERATION: dict[str, dict[str, str]] = held("features:patterns.report_generation")
# Without the members' home addresses there is nothing for generation to refer to.
_ADDRESSED = ("state", "zip")

# What the standard codes mean, where the rows carry no description of their own.
CODE_NAMES: dict[str, dict[str, str]] = held("features:patterns.code_names")



def basis_word(basis: str) -> str:
    """What a count is of, in the pack's words: the primary records, the child
    records, or the child records' lines (health: members, claims, claim lines)."""
    pack = packs.active()
    if basis == "lines":
        return pack.term(pack.child, "lines")
    return pack.term(pack.child if basis in _CHILD_BASIS else pack.primary, "many")


# A count of the child records: health's fields say "claims", a pack's own by
# role ("child"); the primary records' say "members" or "primary".
_CHILD_BASIS = {"claims", "child"}


def _age_band(value: Any) -> Optional[str]:
    try:
        age = float(value)
    except (TypeError, ValueError):
        return None
    if age != age:
        return None
    # Ages are counted in whole years: 17.4 is 17, not a gap between bands. A
    # birth date after the as-of date is a baby born since, so it is 0.
    age = max(int(age // 1), 0)
    for low, high, label in AGE_BANDS:
        if low <= age <= high:
            return label
    return None


def _amount_band(value: Any) -> Optional[str]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount != amount:
        return None
    for low, high, label in AMOUNT_BANDS:
        if low <= amount < high:
            return label
    return AMOUNT_BANDS[0][2] if amount < 0 else None


def _band_start(value: str) -> float:
    """Where a band such as '1,000-4,999' or '$10,000+' starts, for ordering."""
    import re

    found = re.search(r"-?[\d,]+(\.\d+)?", value)
    try:
        return float(found.group(0).replace(",", "")) if found else float("inf")
    except ValueError:
        return float("inf")


_BAND_ORDER = {**{label: i for i, (_l, _h, label) in enumerate(AGE_BANDS)},
               **{label: i for i, (_l, _h, label) in enumerate(AMOUNT_BANDS)}}


def _values(frame: pd.DataFrame, field: dict[str, Any]) -> pd.Series:
    """The field's value on each row, banded or trimmed; NONE where there is none."""
    raw = frame[field["column"]]
    if field.get("bands") == "age":
        made = raw.map(_age_band)
    elif field.get("bands") == "amount":
        made = raw.map(_amount_band)
    elif field.get("custom"):
        made = _custom(raw)
    else:
        made = raw.map(lambda v: None if v is None or pd.isna(v) or not str(v).strip()
                       else str(v).strip())
    return made.fillna(NONE)


# An added column with more values than this is counted in bands.
MAX_PLAIN = 30


def _custom(raw: pd.Series) -> pd.Series:
    """An added denorm column's values: a date by its year; a number with many
    values in ten bands of about equal size; anything else as it is."""
    if pd.api.types.is_datetime64_any_dtype(raw) or raw.map(
            lambda v: hasattr(v, "year") and hasattr(v, "month")).any():
        return pd.to_datetime(raw, errors="coerce").dt.year.map(
            lambda y: None if pd.isna(y) else str(int(y)))
    numbers = pd.to_numeric(raw, errors="coerce")
    if numbers.notna().sum() and numbers.notna().sum() == raw.notna().sum() \
            and numbers.nunique() > MAX_PLAIN:
        edges = sorted(set(numbers.quantile([i / 10 for i in range(11)]).round(2)))
        if len(edges) > 2:
            bands = pd.cut(numbers, edges, include_lowest=True, duplicates="drop")
            return bands.map(lambda b: None if pd.isna(b) else
                             f"{_number(b.left)}-{_number(b.right)}").astype(object)
    return raw.map(lambda v: None if v is None or pd.isna(v) or not str(v).strip()
                   else str(v).strip())


def _number(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _described(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, str]:
    describe = field.get("describe")
    if not describe or describe not in frame.columns:
        return {}
    pairs = frame[[field["column"], describe]].dropna().astype(str)
    return {k.strip(): v.strip() for k, v in zip(pairs[field["column"]], pairs[describe])
            if v.strip()}


def _count(frame: pd.DataFrame, field: dict[str, Any], entity: str,
           level: str) -> list[dict[str, Any]]:
    """One row per value of a field: its count and share of what is counted."""
    if field.get("column") not in frame.columns or frame.empty:
        return []
    basis = field.get("basis", "members")
    rows = frame
    from .claims_service import RECORD_KEY

    if basis in _CHILD_BASIS and RECORD_KEY in frame.columns:
        rows = frame.drop_duplicates(RECORD_KEY)
    values = _values(rows, field)
    counts = values.value_counts()
    total = int(counts.sum())
    names = _described(rows, field)
    banded = field.get("bands") or (field.get("custom") and counts.index.map(
        lambda v: v == NONE or bool(__import__("re").fullmatch(r"-?[\d,.]+-[\d,.]+|\d{4}",
                                                                   str(v)))).all())
    order = sorted(counts.items(), key=lambda kv: (
        (_BAND_ORDER.get(kv[0], _band_start(str(kv[0])) if kv[0] != NONE else 1e18), 0)
        if banded else (-kv[1], 0), str(kv[0])))
    return [{
        "field": field["key"], "field_label": field["label"],
        "value": value,
        "value_label": names.get(value) or CODE_NAMES.get(field["key"], {}).get(value, ""),
        "count": int(n), "basis": basis_word(basis), "of": total,
        "pct": round(int(n) / total, 6) if total else 0.0,
        "generation": NO if field.get("custom")
        or (field["key"] in _ADDRESSED and set(counts.index) <= {NONE})
        else GENERATION.get(entity, {}).get(field["key"], ""),
        "level": level,
    } for value, n in order]


def claims_of_members(member_keys: Iterable[Any]) -> pd.DataFrame:
    """The claim lines of these members, the columns a member pattern's claims
    are counted on. Read in slices so a large pattern stays a few queries."""
    from sdg.analyzer.entity_loader import connect as _conn, table
    from sdg.analyzer.sql import read_sql

    keys = sorted({int(k) for k in member_keys if k is not None and str(k).strip() != ""})
    wanted = ["MEME_CK", "CLCL_ID", "CLCL_TOT_CHG", "CLCL_CL_TYPE", "IDCD_ID_PRIMARY",
              "IDCD_DESC_PRIMARY", "IPCD_ID", "IPCD_DESC"]
    if not keys:
        return pd.DataFrame(columns=wanted)
    frames = []
    conn = _conn()
    try:
        for start in range(0, len(keys), 2000):
            part = ",".join(str(k) for k in keys[start:start + 2000])
            frames.append(read_sql(
                f"SELECT {', '.join(wanted)} FROM {table(packs.active().child)} WHERE MEME_CK IN ({part})", conn))
    finally:
        conn.close()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=wanted)


def _fields(entity: str) -> list[dict[str, Any]]:
    """The fields chosen on the Config screen (every built-in one by default)."""
    from . import report_fields

    # Not guarded: a store that cannot be read is an error, not a report on
    # the built-in fields that silently drops the columns a person chose.
    return report_fields.specs(entity)


_CLAIM_SIDE = {f["key"] for f in FIELDS.get("member_claims", [])}


def mix(entity: str, frame: pd.DataFrame, claims: Optional[pd.DataFrame] = None,
        level: str = "pattern",
        fields: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """Every chosen field of a pattern, every value, with counts and shares."""
    fields = fields if fields is not None else _fields(entity)
    out: list[dict[str, Any]] = []
    for field in fields:
        if is_primary(entity) and field["key"] in _CLAIM_SIDE:
            continue
        out.extend(_count(frame, field, entity, level))
    chosen = {f["key"] for f in fields}
    if is_primary(entity) and claims is not None and chosen & _CLAIM_SIDE:
        mine = claims[claims["MEME_CK"].isin(frame["MEME_CK"])] if "MEME_CK" in frame else claims
        members = len(frame)
        with_claims = int(mine["MEME_CK"].nunique()) if not mine.empty else 0
        for value, n in (("Yes", with_claims), ("No", members - with_claims)):
            if "has_claims" not in chosen:
                break
            out.append({
                "field": "has_claims", "field_label": "Members with claims",
                "value": value, "value_label": "", "count": n, "basis": basis_word("members"),
                "of": members, "pct": round(n / members, 6) if members else 0.0,
                "generation": GENERATION.get(packs.active().primary, {}).get("has_claims", ""),
                "level": level,
            })
        for field in FIELDS.get("member_claims", []):
            if field.get("column") and field["key"] in chosen:
                out.extend(_count(mine, field, "members", level))
    return out


def _with_columns(entity: str, frame: pd.DataFrame,
                  fields: list[dict[str, Any]]) -> pd.DataFrame:
    """The frame with the added denorm columns it does not carry, read for its
    rows (a member's most recent enrollment row; a claim line by its key)."""
    from . import report_fields

    wanted = [f["column"] for f in fields if f.get("custom") and f["column"] not in frame]
    keys = report_fields.KEYS[entity]
    if not wanted or frame.empty or not set(keys) <= set(frame.columns):
        return frame
    from sdg.analyzer import entity_loader
    from sdg.analyzer.entity_loader import connect as _conn
    from sdg.analyzer.sql import read_sql

    table = entity_loader.table(entity)
    # A primary record's most recent row first (its loader's latest: health,
    # a member's latest enrollment).
    latest = entity_loader.config(entity).get("latest") if is_primary(entity) else None
    record = keys[0]
    cols = ", ".join(f"[{c}]" for c in [*keys, *wanted])
    ids = sorted({int(k) for k in frame[keys[0]].dropna()}) if is_primary(entity) else \
        sorted({str(k).strip() for k in frame[keys[0]].dropna()})
    parts = []
    conn = _conn()
    try:
        for start in range(0, len(ids), 2000):
            chunk = ids[start:start + 2000]
            listed = ",".join(str(i) for i in chunk) if is_primary(entity) else \
                ",".join("'" + i.replace("'", "''") + "'" for i in chunk)
            order = (f"ORDER BY [{packs.plain_name(latest['key'], 'loaders latest')}], "
                     f"[{packs.plain_name(latest['by'], 'loaders latest')}] DESC"
                     if latest else "")
            parts.append(read_sql(
                f"SELECT {cols} FROM {table} WHERE [{keys[0]}] IN ({listed}) {order}", conn))
    finally:
        conn.close()
    extra = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=[*keys, *wanted])
    if is_child(entity):
        extra[record] = extra[record].astype(str).str.strip()
        left = frame.assign(**{record: frame[record].astype(str).str.strip()})
        extra = extra.drop_duplicates(keys)
        merged = left.merge(extra, on=keys, how="left")
        merged.index = frame.index
        return merged
    extra = extra.drop_duplicates(record)
    merged = frame.merge(extra, on=record, how="left")
    merged.index = frame.index
    return merged


# The Scenarios panel's own default, so a count downloaded without a choice
# matches the list on screen.
DEFAULT_SCENARIOS = DEFAULT_SPLIT


def _said(dimension: dict[str, Any], value: str, labels: dict[str, dict[str, str]]) -> str:
    """A scenario's value as a person reads it: the code, and what it is called."""
    name = labels.get(dimension["key"], {}).get(value, "")
    return f"{dimension['label']} {value}" + (f" ({name})" if name and name != value else "")


def report(entity: str, frame: pd.DataFrame, scenario_keys: Optional[list[str]] = None,
           with_claims: bool = True) -> dict[str, Any]:
    """A pattern's counts and shares, whole and per scenario.

    The pattern: every field, every value, counted. A scenario -- one
    combination of `scenario_keys` its rows hold, as the Scenarios panel lists
    them -- is one row: how many it has and its share of the pattern. Every
    scenario is listed, largest first, however small.
    """
    fields = _fields(entity)
    frame = _with_columns(entity, frame, fields)
    claims = None
    if is_primary(entity) and with_claims and "MEME_CK" in frame.columns \
            and {f["key"] for f in fields} & _CLAIM_SIDE:
        claims = claims_of_members(frame["MEME_CK"])
    size = int(frame["CLCL_ID"].nunique()) if is_child(entity) and "CLCL_ID" in frame \
        else len(frame)
    out: dict[str, Any] = {
        "entity": entity,
        "size": size,
        "size_basis": basis_word("claims" if is_child(entity) else "members"),
        "rows": mix(entity, frame, claims, "pattern", fields),
        "scenario_keys": [],
        "scenarios": [],
    }
    keys = [k for k in (scenario_keys if scenario_keys is not None
                        else DEFAULT_SCENARIOS.get(entity, []))]
    from .scenarios import DIMENSIONS

    usable = [d["key"] for d in DIMENSIONS.get(entity, [])
              if d["key"] in keys and d["column"] in frame.columns]
    if not usable or frame.empty:
        return out
    values, chosen = _split(frame, entity, usable)
    labels = _labels(frame, chosen)
    out["scenario_keys"] = [{"key": d["key"], "label": d["label"]} for d in chosen]
    groups = values.groupby([d["key"] for d in chosen], sort=False).groups
    found = []
    for combo, index in groups.items():
        combo = combo if isinstance(combo, tuple) else (combo,)
        part = frame.loc[index]
        n = int(part["CLCL_ID"].nunique()) if is_child(entity) and "CLCL_ID" in part \
            else len(part)
        picked = {d["key"]: str(v) for d, v in zip(chosen, combo)}
        found.append({
            "values": picked,
            "label": " · ".join(_said(d, picked[d["key"]], labels) for d in chosen),
            "count": n,
            "pct": round(n / size, 6) if size else 0.0,
        })
    found.sort(key=lambda s: (-s["count"], s["label"]))
    out["scenarios"] = found
    return out


# ---------------------------------------------------------------------------
# One sheet: every pattern, its values and its scenarios
# ---------------------------------------------------------------------------

SHEET_COLUMNS = ["Domain", "Pattern", "Pattern size", "Counted in", "Share of population",
                 "Level", "Field", "Value", "Description", "Count", "Out of", "%",
                 "Used in generation"]

SCENARIO_GENERATION = YES


def sheet_rows(patterns: list[dict[str, Any]]) -> list[list[Any]]:
    """The rows of the single sheet. Each pattern: {name, share, report}.

    A pattern's values come first (every field, every value), then its
    scenarios, one row each, with the pattern's figures repeated on every row
    so any filter of the sheet still says which pattern a row is.
    """
    out: list[list[Any]] = []
    for p in patterns:
        rep = p["report"]
        domain = packs.active().term(rep["entity"], "Many")
        head = [domain, p["name"], rep["size"], rep["size_basis"], p.get("share")]
        for r in rep["rows"]:
            out.append([*head, "Pattern", r["field_label"], r["value"], r["value_label"],
                        r["count"], r["of"], r["pct"], r["generation"]])
        field = " · ".join(k["label"] for k in rep["scenario_keys"])
        for s in rep["scenarios"]:
            out.append([*head, "Scenario", field, s["label"], "", s["count"], rep["size"],
                        s["pct"], SCENARIO_GENERATION])
    return out


def csv_text(patterns: list[dict[str, Any]]) -> str:
    """The sheet as CSV: the same columns, a share as a fraction of one."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SHEET_COLUMNS)
    writer.writerows(sheet_rows(patterns))
    return buffer.getvalue()


def workbook(patterns: list[dict[str, Any]]) -> bytes:
    """The sheet as an .xlsx: one tab, a header to filter on, shares as %."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = "Patterns"
    sheet.append(SHEET_COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in sheet_rows(patterns):
        sheet.append(row)
    percent = {SHEET_COLUMNS.index("%") + 1, SHEET_COLUMNS.index("Share of population") + 1}
    counts = {SHEET_COLUMNS.index(c) + 1 for c in ("Pattern size", "Count", "Out of")}
    for column in percent | counts:
        for (cell,) in sheet.iter_rows(min_row=2, min_col=column, max_col=column):
            cell.number_format = "0.0%" if column in percent else "#,##0"
    widths = {"Pattern": 34, "Field": 22, "Value": 36, "Description": 34,
              "Used in generation": 60}
    for i, name in enumerate(SHEET_COLUMNS, 1):
        sheet.column_dimensions[get_column_letter(i)].width = widths.get(name, 14)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def for_saved(patterns: list[dict[str, Any]],
              scenario_keys: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Saved patterns (rows of the patterns table), each with its report. One
    that can no longer be rebuilt (its data changed) is said so, not dropped."""
    from . import pattern_data
    from . import pattern_service as ps

    out = []
    for p in patterns:
        entity = ps.entity_of(p)
        name = str(p.get("contextual_name") or f"Pattern {p['id']}")
        try:
            rows = pattern_data.every_row(entity, {}, None, 0, int(p["id"]))
            rep = report(entity, rows, scenario_keys)
        except ValueError as e:
            rep = {"entity": entity, "size": int(p.get("member_count") or 0),
                   "size_basis": basis_word("claims" if is_child(entity) else "members"),
                   "rows": [{"field_label": "(not counted)", "value": str(e)[:200],
                             "value_label": "", "count": None, "of": None, "pct": None,
                             "generation": ""}],
                   "scenario_keys": [], "scenarios": []}
        out.append({"name": name, "share": p.get("pct_of_pop"), "report": rep})
    return out


def for_analysis(entity: str, filters: dict, k: Optional[int],
                 scenario_keys: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every pattern of an analysis, each with its report."""
    from . import pattern_data

    if is_child(entity):
        from . import claims_service as cs

        result = cs.analyze(filters, k)
    else:
        from . import service

        result = service.analyze(filters, k)
    out = []
    for p in result.get("patterns") or []:
        rows = pattern_data.every_row(entity, filters, k, int(p["cluster_id"]))
        out.append({"name": p["name"], "share": p.get("pct_of_total"),
                    "report": report(entity, rows, scenario_keys)})
    return out

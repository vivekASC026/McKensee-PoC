"""Extraction criteria: which rows of a source the extract keeps.

Three kinds, each proposed from figures already read, never from a value the
profile did not keep:

- *in force*: a table's start and end dates, keeping the rows in force on
  the day of the extract (an open end is in force; a row with no start is
  not known to be, and is left out). Proposed for the record and each bridge table with
  a date pair, the pair whose end holds the far-future placeholder (an open
  span) first; selected on the record table, where open spans were seen.
- *values*: a status-like code column on the record table, keeping its most
  common value. The value comes only from the scrubbed profile's kept top
  values, or from what the person types; one whose values were not kept is
  proposed empty, for the person to fill. Never selected at first: which
  statuses matter is the person's call.
- *window*: the child's widest date column -- filled on most rows, the most
  distinct values, not an end, not a start of life (a tenure's input) and
  nothing personal -- keeping the last WINDOW_MONTHS months.

`measure` asks the source what share of rows each keeps, one COUNT_BIG each
through the run's SourceChecks and source_read, over the table's fixed
sample (a share read from a sample is rounded and noted as an estimate).
`where_sql` renders the selected ones for the extract: identifiers
come only from the model, bracket-quoted, and values are literals made by
`literal` alone.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from sdg.accelerator import source_read
from sdg.accelerator.graph import KeyGraph, SourceChecks, name_tokens
from sdg.accelerator.hierarchy import STATUS_ELEMENT, STATUS_TOKENS, TENURE_TOKENS
from sdg.accelerator.landscape import END_TOKENS
from sdg.accelerator.landscape import date_pairs as landscape_date_pairs
from sdg.accelerator.tables import ROLES
from sdg.discovery.model import Column, Table

if TYPE_CHECKING:
    from sdg.accelerator.plan import SourcePlan

KINDS = ("in_force", "window", "values")
OPS = ("in", "not_in")
# The child's window, in months, unless the person sets another.
WINDOW_MONTHS = 24
MONTHS_MAX = 600
# A value in a criterion: at most this many characters, no control characters.
VALUE_MAX = 60
VALUES_MAX = 50
# A date column empty on more of its rows than this is no window: the rows
# without it would all be dropped.
WINDOW_NULL_MAX = 0.05
# Words that mark where a start date's column says "the first", which the
# in-force pair avoids when a later start is there.
_ORIGINAL = {"ORIG", "ORIGINAL", "FIRST", "SINCE"}

# Character types stored in a code page, one byte a character.
_SINGLE_BYTE = frozenset({"char", "varchar", "text"})

_ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,30}")
# The start of the day after the extract's: a start before it began today
# or earlier, whatever its time of day.
TOMORROW = "DATEADD(day, 1, CAST(GETDATE() AS date))"
_INTEGER = re.compile(r"[+-]?\d{1,30}")
_DECIMAL = re.compile(r"[+-]?(\d{1,30}(\.\d{0,30})?|\.\d{1,30})")


@dataclass
class Criterion:
    id: str
    role: str
    table: str
    kind: str  # in_force | window | values
    columns: list[str]
    values: list[str] = field(default_factory=list)
    months: int | None = None
    op: str = "in"  # in | not_in
    selected: bool = False
    share_kept: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "Criterion":
        held = dict(held)
        held["columns"] = list(held.get("columns") or [])
        held["values"] = [str(v) for v in held.get("values") or []]
        try:
            found = cls(**held)
        except TypeError as exc:
            raise ValueError(f"a saved criterion is not one this version reads: {exc}") from exc
        if not isinstance(found.selected, bool):
            raise ValueError(f"{found.id}: selected is true or false")
        return found


def criterion_id(role: str, kind: str, table: str, column: str) -> str:
    return f"{role}:{kind}:{table}.{column}"


# --- checking against the model ---------------------------------------------

def _table(graph: KeyGraph, name: str) -> Table:
    found = graph.table(name)
    if found is None:
        raise ValueError(f"the source has no table {name!r}")
    return found


def _column(graph: KeyGraph, table: str, column: str) -> Column:
    found = _table(graph, table).column(column)
    if found is None:
        raise ValueError(f"{table} has no column {column!r}")
    return found


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def clean_value(value: Any) -> str:
    """A value as a criterion holds it: text of at most VALUE_MAX characters
    with no control character; refused otherwise."""
    text = str(value) if value is not None else ""
    if not text:
        raise ValueError("a criterion value is empty")
    if len(text) > VALUE_MAX:
        raise ValueError(f"a criterion value is at most {VALUE_MAX} characters")
    if any(ord(ch) < 32 or 127 <= ord(ch) < 160 or ch in "  " for ch in text):
        raise ValueError("a criterion value may not hold a control character")
    return text


def literal(value: str, column: Column) -> str:
    """The value as SQL, every quote doubled: a number when the column is
    numeric (refused when it is not one); '...' for a char or varchar column,
    so the column is compared as it is stored and its index can serve (a
    value outside ASCII is refused there: it would not survive the code
    page); N'...' otherwise."""
    text = clean_value(value)
    if column.is_numeric:
        pattern = _INTEGER if column.is_integer else _DECIMAL
        if not pattern.fullmatch(text):
            raise ValueError(f"{column.name} holds numbers: {text!r} is not one")
        return text
    quoted = "'" + text.replace("'", "''") + "'"
    if column.type_name in _SINGLE_BYTE:
        if not text.isascii():
            raise ValueError(f"{column.name} is {column.type_name}: give the value in plain "
                             f"letters, digits and signs")
        return quoted
    return "N" + quoted


# --- proposing ----------------------------------------------------------------

def _pairs(graph: KeyGraph, table: str) -> list[dict[str, Any]]:
    """The table's date pairs with their placeholder counts, as the graph
    keeps them (the same memo graph.date_pairs reads)."""
    found = graph.memo("date_pairs", lambda: landscape_date_pairs(graph.model, graph.profile))
    return list(found.get(table, ()))


def _open_share(graph: KeyGraph, table: str, end: str) -> float:
    """The share of rows whose end is open: empty, or a placeholder."""
    held = graph.profile_of(table, end)
    if held is None or not held.row_count:
        return 0.0
    placeholders = sum(int(p.get("count") or 0) for p in held.placeholders or [])
    return min(1.0, held.null_rate + placeholders / held.row_count)


def best_pair(graph: KeyGraph, table: str) -> dict[str, Any] | None:
    """The table's in-force pair: one whose end holds placeholders first,
    then a start that is not "the first" (the latest start), then by name."""
    pairs = _pairs(graph, table)
    if not pairs:
        return None
    return min(pairs, key=lambda p: (-(p.get("placeholders") or 0),
                                     bool(name_tokens(p["start"]) & _ORIGINAL),
                                     p["start"], p["end"]))


def _in_force(graph: KeyGraph, role: str, table: str, as_: str) -> Criterion | None:
    pair = best_pair(graph, table)
    if pair is None:
        return None
    start, end = pair["start"], pair["end"]
    placeholders = int(pair.get("placeholders") or 0)
    open_share = _open_share(graph, table, end)
    evidence = (f"{placeholders:,} rows end on a far-future placeholder" if placeholders
                else f"{open_share:.0%} of rows have no end" if open_share
                else "no open span was seen in the profile")
    selected = as_ == "record" and (placeholders > 0 or open_share > 0)
    where = "the record table" if as_ == "record" else f"a {as_} table"
    # What else it leaves out: a linked table's criterion acts as an inner
    # join, and the child follows the primary's criteria.
    scope = (f"; it keeps only the records with a row of {table} meeting it, as an inner "
             f"join would: the others are left out, not kept with that level empty"
             if as_ != "record" else "")
    if role == "primary":
        scope += ("; the child's records follow the primary's criteria: those of a record "
                  "left out (one whose span has ended) are left out too")
    return Criterion(
        id=criterion_id(role, "in_force", table, start), role=role, table=table,
        kind="in_force", columns=[start, end], selected=selected,
        reason=f"{start} to {end} on {where}: keeps the rows in force on the extract "
               f"date (a row with no start is left out); {evidence}{scope}")


def _status_columns(graph: KeyGraph, table: str) -> list[Column]:
    found = graph.table(table)
    if found is None:
        return []
    out = []
    for column in found.columns:
        # Named as a status (STATUS_TOKENS, a part of landscape.RANGE_TOKENS),
        # or mapped to the catalog's status code.
        status = (bool(name_tokens(column.name) & STATUS_TOKENS)
                  or graph.element_of(table, column.name) == STATUS_ELEMENT)
        if status and not column.is_date and not graph.is_personal(table, column.name):
            held = graph.profile_of(table, column.name)
            if held is not None and held.cardinality == "low" and held.distinct_count >= 2:
                out.append(column)
    return out


def _kept_top(graph: KeyGraph, table: str, column: Column) -> tuple[str, float] | None:
    """The column's most common value the scrubbed profile kept, and its
    share of rows; None when it kept none (or the one it kept is no value a
    criterion may hold)."""
    held = graph.profile_of(table, column.name)
    tops = sorted(held.top_values or [], key=lambda t: -(t.count or 0)) if held else []
    for top in tops:
        try:
            value = clean_value(top.value)
            literal(value, column)
        except ValueError:
            continue
        share = top.share if top.share is not None else (
            (top.count or 0) / held.row_count if held.row_count else 0.0)
        return value, float(share)
    return None


def _values(graph: KeyGraph, role: str, table: str) -> Criterion | None:
    """The record table's status-like column with the most dominant kept
    value; one whose values were not kept is proposed empty, for the person."""
    ranked = []
    for column in _status_columns(graph, table):
        top = _kept_top(graph, table, column)
        ranked.append((top is None, -(top[1] if top else 0.0), column.name, column, top))
    if not ranked:
        return None
    _, _, name, column, top = min(ranked, key=lambda r: r[:3])
    if top is None:
        return Criterion(
            id=criterion_id(role, "values", table, name), role=role, table=table,
            kind="values", columns=[name], values=[], selected=False,
            reason=f"{name} reads as a status; the profile kept none of its values, so "
                   f"type the values to keep before selecting it")
    value, share = top
    return Criterion(
        id=criterion_id(role, "values", table, name), role=role, table=table, kind="values",
        columns=[name], values=[value], selected=False,
        reason=f"{name} reads as a status; its most common value, held by {share:.0%} of "
               f"rows in the profile; select to keep only these")


def _window(graph: KeyGraph, plan: "SourcePlan", table: str) -> Criterion | None:
    found = graph.table(table)
    if found is None:
        return None
    inputs = {(f.derive or {}).get("from") for f in plan.features("child")}
    best = None
    for column in found.columns:
        tokens = name_tokens(column.name)
        if (not column.is_date or tokens & END_TOKENS or tokens & TENURE_TOKENS
                or column.name in inputs or graph.is_personal(table, column.name)):
            continue
        held = graph.profile_of(table, column.name)
        if held is None or held.null_rate > WINDOW_NULL_MAX:
            continue
        rank = (-held.distinct_count, held.null_rate, column.name)
        if best is None or rank < best[0]:
            best = (rank, column.name, held.distinct_count)
    if best is None:
        return None
    _, name, distinct = best
    return Criterion(
        id=criterion_id("child", "window", table, name), role="child", table=table,
        kind="window", columns=[name], months=WINDOW_MONTHS, selected=True,
        reason=f"{name}, the child's widest date ({distinct:,} values, filled on most rows): "
               f"keeps the last {WINDOW_MONTHS} months; a row with no {name} is left out too")


def propose(plan: "SourcePlan", graph: KeyGraph) -> list[Criterion]:
    """The criteria the plan's record kinds and tables suggest, in order:
    per role, in force on the record then on each bridge, values on the
    record, then the child's window."""
    out: list[Criterion] = []
    for role in ROLES:
        kind = plan.record_kinds.get(role)
        if not kind:
            continue
        record = kind["table"]
        tables = [(record, "record")] + [
            (t.table, "bridge") for t in plan.tables
            if t.table != record and any(s["role"] == role and s["as"] == "bridge"
                                         for s in t.serves)]
        for table, as_ in tables:
            held = _in_force(graph, role, table, as_)
            if held is not None:
                out.append(held)
        held = _values(graph, role, record)
        if held is not None:
            out.append(held)
        if role == "child":
            held = _window(graph, plan, record)
            if held is not None:
                out.append(held)
    return out


def carry(fresh: list[Criterion], old: list[Criterion]) -> list[Criterion]:
    """The fresh proposal with what the person decided kept, by id: the
    selection, values, months, op, and the share measured for them."""
    held = {c.id: c for c in old}
    for criterion in fresh:
        before = held.get(criterion.id)
        if before is None:
            continue
        criterion.selected, criterion.values = before.selected, list(before.values)
        criterion.months, criterion.op = before.months, before.op
        criterion.share_kept = before.share_kept
    return fresh


# --- rendering ------------------------------------------------------------------

def check(criterion: Criterion, graph: KeyGraph) -> None:
    """Refuse a criterion naming anything the model does not hold, or one
    whose kind, values or months are not what it may be."""
    if criterion.role not in ROLES:
        raise ValueError(f"{criterion.id}: {criterion.role!r} is not a role")
    if criterion.kind not in KINDS:
        raise ValueError(f"{criterion.id}: {criterion.kind!r} is not a kind of criterion")
    if criterion.op not in OPS:
        raise ValueError(f"{criterion.id}: {criterion.op!r} is not in or not_in")
    wanted = {"in_force": 2, "window": 1, "values": 1}[criterion.kind]
    if len(criterion.columns) != wanted:
        raise ValueError(f"{criterion.id}: a {criterion.kind} criterion names {wanted} "
                         f"column{'s' if wanted > 1 else ''}")
    columns = [_column(graph, criterion.table, c) for c in criterion.columns]
    if criterion.kind in ("in_force", "window") and not all(c.is_date for c in columns):
        raise ValueError(f"{criterion.id}: a {criterion.kind} criterion reads date columns")
    if criterion.kind == "window":
        if type(criterion.months) is not int or not 1 <= criterion.months <= MONTHS_MAX:
            raise ValueError(f"{criterion.id}: months is a whole number from 1 to {MONTHS_MAX}")
    if criterion.kind == "values":
        if len(criterion.values) > VALUES_MAX:
            raise ValueError(f"{criterion.id}: at most {VALUES_MAX} values")
        for value in criterion.values:
            literal(value, columns[0])


def condition(criterion: Criterion, alias: str, graph: KeyGraph) -> str:
    """One criterion as SQL over the table under `alias`."""
    check(criterion, graph)
    if not _ALIAS.fullmatch(alias):
        raise ValueError(f"{alias!r} is not a plain alias")
    first = f"{alias}.{_quote(criterion.columns[0])}"
    today = "CAST(GETDATE() AS date)"
    if criterion.kind == "in_force":
        # An open end (none) is in force. Written as IS NULL OR rather than
        # ISNULL(end, '99991231'): that literal overflows a smalldatetime
        # end, and the end's index can serve. A row with no start is not
        # known to be in force and is left out (the reason says so). The
        # start is compared with tomorrow, so a datetime start later today
        # is in force too.
        end = f"{alias}.{_quote(criterion.columns[1])}"
        return (f"{first} < {TOMORROW} AND ({end} IS NULL OR {end} >= {today})")
    if criterion.kind == "window":
        return f"{first} >= DATEADD(month, -{int(criterion.months or 0)}, {today})"
    if not criterion.values:
        raise ValueError(f"{criterion.id}: no values to keep; set them first")
    column = _column(graph, criterion.table, criterion.columns[0])
    listed = ", ".join(literal(v, column) for v in criterion.values)
    if criterion.op == "in":
        return f"{first} IN ({listed})"
    # Rows with no value are not among those named, so not_in keeps them.
    return f"({first} IS NULL OR {first} NOT IN ({listed}))"


def where_sql(criteria: list[Criterion], aliases: dict[str, str], graph: KeyGraph) -> str:
    """The selected criteria as one condition, each in parentheses, joined by
    AND; empty when none is selected. `aliases` maps each criterion's table
    to its alias in the statement; a table without one is refused."""
    parts = []
    for criterion in criteria:
        if not criterion.selected:
            continue
        alias = aliases.get(criterion.table)
        if alias is None:
            raise ValueError(f"{criterion.id}: {criterion.table} is not in the statement")
        parts.append(f"({condition(criterion, alias, graph)})")
    return " AND ".join(parts)


# --- measuring ------------------------------------------------------------------

MEASURE_ALIAS = "s"
# How a note on a criterion not measured begins: an earlier one is replaced.
NOT_MEASURED = "Not measured"
# How a note on a share read from a table's sample begins.
SAMPLED = "Measured on a sample"


def measure_sql(criterion: Criterion, graph: KeyGraph) -> str:
    """The one statement measuring a criterion: all rows and those it keeps,
    over the table's fixed sample when it is large."""
    table = _table(graph, criterion.table)
    kept = condition(criterion, MEASURE_ALIAS, graph)
    return (f"SELECT COUNT_BIG(*) AS rows_all, "
            f"COUNT_BIG(CASE WHEN {kept} THEN 1 END) AS rows_kept "
            f"FROM {source_read.count_source(table, MEASURE_ALIAS)}")


def measure(criteria: list[Criterion], graph: KeyGraph, checks: SourceChecks) -> list[str]:
    """Measure the share of rows each criterion keeps, one statement each
    through `checks`. A criterion not measured -- one that cannot be written
    yet (a values criterion with no value), past the run's cap or time, or
    refused by the source -- keeps share_kept None, and a note says so; the
    notes are returned. A lost connection raises."""
    notes: list[str] = []
    for criterion in criteria:
        criterion.share_kept = None
        try:
            sql = measure_sql(criterion, graph)
        except ValueError as exc:
            notes.append(f"{NOT_MEASURED}, {criterion.id}: {exc}")
            continue
        checked, rows = checks.run(
            f"the share {criterion.id} keeps",
            lambda sql=sql: source_read.query(checks.registry, sql,
                                              timeout=source_read.CHECK_TIMEOUT,
                                              lock_timeout_ms=source_read.CHECK_LOCK_TIMEOUT_MS))
        if not checked:
            notes.append(f"{NOT_MEASURED}, {criterion.id}: the check was not run (see the "
                         f"notes on checks)")
            continue
        row = rows[0] if rows else {}
        total = int(row.get("rows_all") or 0)
        if not total:
            notes.append(f"{NOT_MEASURED}, {criterion.id}: the sample held no rows")
            continue
        share = int(row.get("rows_kept") or 0) / total
        if source_read.is_sampled(graph.table(criterion.table)):
            # Whole pages are sampled, and the newest rows of a table kept in
            # date order may fall outside them: a rough figure, said so.
            criterion.share_kept = round(share, 2)
            notes.append(f"{SAMPLED}, {criterion.id}: {total:,} rows of "
                         f"{criterion.table}; the share is an estimate only")
        else:
            criterion.share_kept = round(share, 4)
    return notes

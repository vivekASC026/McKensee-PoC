"""Propose record kinds, levels and features from the key graph.

Pure: the graph in, candidates out, nothing sent to a source (a caller that can
read the source may pass `pair_counts` to settle a nesting the profile left
open). Every figure is a count already read; no value of any column appears in
a candidate, only names and counts.

The rules, each kept general rather than fitted to one source:

- A *primary* record is a population: many rows, events keyed to it, its own
  business key, columns that hold people. Code lists, lines and extension
  tables are never records.
- A *child* record keys to the primary with more than one row per parent on
  average, carries dates or amounts, and often has lines of its own.
- A *level* is a table above the record by keys -- each record falls under one
  of its values -- with between 2 and a few thousand values. One with at most
  one value per WALK_ROWS_PER_VALUE records is a walk level; more, a narrowing
  level. A plain code column (on the record or on a chosen level's table) is a
  narrowing level too: it splits groups further but is not walked.
- Levels are ordered broadest first within each chain that nests, and chains
  coarse to fine; a child's levels shared with its primary come first, in the
  primary's order, so both are read in one frame.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from sdg.accelerator import source_read
from sdg.accelerator.graph import Edge, KeyGraph, name_tokens
from sdg.accelerator.landscape import START_TOKENS, END_TOKENS
from sdg.catalog.model import Catalog
from sdg.profiling.model import CODE_MAX_DISTINCT, UNIQUE_RATIO

# Joins followed up from a record to a level. Four reaches a level two tables
# above a parent record (child -> primary -> its parent -> a level -> the level
# above it) without wandering across the whole schema.
MAX_DEPTH = 4
# A walk level's groups hold this many records each on average, at least; a
# finer level splits the records too thinly to read a pattern in each group.
WALK_ROWS_PER_VALUE = 30
# Past this many values a table is a population, not a grouping.
MAX_LEVEL_DISTINCT = 5000
# A candidate this confident is selected in the first proposal.
SELECT_AT = 0.6
# Walk levels selected at first: every level multiplies the groups walked.
MAX_WALK_SELECTED = 5
# Record candidates kept.
TOP_RECORDS = 5

# Name tokens of a column that names a level's values.
LABEL_TOKENS = {"NAME", "NM", "DESC", "DESCRIPTION", "TITLE", "LABEL"}
# Name tokens of a column that sorts records into kinds: the narrowing levels
# a person most often wants.
KIND_TOKENS = {"TYPE", "TYP", "KIND", "CLASS", "CLS", "CATEGORY", "CAT"}
# Name tokens of a yes/no column: it halves the records, rarely a level.
INDICATOR_TOKENS = {"IND", "FLAG", "FLG", "YN"}
# A status says where a record is in its life: an extraction criterion, not a
# grouping, so it is never offered as a level.
STATUS_TOKENS = {"STATUS", "STS", "STAT"}
STATUS_ELEMENT = "STATUS_CODE"
# Date names that mark when a record began, for its tenure.
TENURE_TOKENS = START_TOKENS | {"SINCE", "ORIG", "ORIGINAL", "OPEN", "OPENED", "FIRST"}
BIRTH_ELEMENT = "DOB"
AMOUNT_ELEMENT = "AMOUNT"

# What a level's reason says about the level before it, when that is not a
# plain nesting: a pair it was shown not to nest under, one nothing could say
# either way (crossed is not assumed), and, from order_levels, levels whose
# place against others was set by their counts because nesting was not known.
NEST_FLAG = "does not nest under "
UNKNOWN_FLAG = "not known to nest under "
ORDER_FLAG = "placed by its count, nesting not known against "
ORDER_SHOWN = 3

# (table, a, b) -> (rows holding both, distinct b, distinct (a, b) pairs).
PairCounts = Callable[[str, str, str], "tuple[int, int, int] | None"]


# --- shapes ---------------------------------------------------------------

@dataclass
class Join:
    from_table: str
    from_columns: list[str]
    to_table: str
    to_columns: list[str]
    kind: str
    coverage: float | None = None

    @classmethod
    def up(cls, edge: Edge) -> "Join":
        """Child to parent, along a key."""
        return cls(edge.child_table, list(edge.child_columns), edge.parent_table,
                   list(edge.parent_columns), edge.kind, edge.coverage)

    @classmethod
    def down(cls, edge: Edge) -> "Join":
        """Parent to child, along a key read the other way (into a bridge)."""
        return cls(edge.parent_table, list(edge.parent_columns), edge.child_table,
                   list(edge.child_columns), edge.kind, edge.coverage)

    def to_edge(self) -> Edge:
        return Edge(self.from_table, list(self.from_columns), self.to_table,
                    list(self.to_columns), self.kind, self.coverage)

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "Join":
        return cls(**held)


@dataclass
class RecordKindCandidate:
    table: str
    score: float
    reasons: list[str] = field(default_factory=list)
    lines_table: str | None = None
    lines_key: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "RecordKindCandidate":
        return cls(**held)


@dataclass
class LevelCandidate:
    id: str
    table: str
    column: str
    label_table: str | None
    label_column: str | None
    path: list[Join]
    distinct: int
    per_value: float
    kind: str  # walk | narrow
    confidence: float
    reason: str
    selected: bool = False
    order: int = 0
    # How the label's table joins the level's, when the label is kept apart.
    label_join: Join | None = None
    # Whether this level nests under another (by the other's id), where only
    # the source could say: a pair count from propose. Kept so an edit made
    # later, from the saved figures alone, reads the same answer.
    nesting: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "LevelCandidate":
        held = dict(held)
        held["path"] = [Join.from_dict(j) for j in held.get("path") or []]
        if held.get("label_join"):
            held["label_join"] = Join.from_dict(held["label_join"])
        return cls(**held)


@dataclass
class FeatureCandidate:
    id: str
    column: str
    kind: str  # in | range
    derive: dict[str, Any] | None
    distinct: int
    null_rate: float
    confidence: float
    reason: str
    selected: bool = False
    # The table the column is on, when not the record's own: the lines table,
    # for a feature of each line.
    table: str = ""

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "FeatureCandidate":
        return cls(**held)


def to_dict(item: Any) -> dict[str, Any]:
    return asdict(item)


def level_id(table: str, column: str) -> str:
    return f"{table}.{column}"


def _clamp(value: float) -> float:
    return round(max(0.05, min(0.95, value)), 2)


def key_columns(graph: KeyGraph, table: str) -> list[str]:
    """The record's key: its primary key, else its first unique constraint."""
    found = graph.table(table)
    if found is None:
        return []
    if found.primary_key:
        return list(found.primary_key)
    return list(found.unique_constraints[0].columns) if found.unique_constraints else []


# --- record kinds ---------------------------------------------------------

def _size(graph: KeyGraph, table: str) -> float:
    """The table's rows on a log scale, 1 for the largest in the source."""
    top = graph.memo("most_rows",
                     lambda: max((t.row_count for t in graph.model.tables), default=0))
    if top <= 0:
        return 0.0
    return math.log10(graph.rows(table) + 1) / math.log10(top + 1)


def _has_business_key(graph: KeyGraph, table: str) -> bool:
    found = graph.table(table)
    if found is None:
        return False
    unique = set(found.unique_single_columns()) | (
        set(found.primary_key) if len(found.primary_key) == 1 else set())
    return any(c.name in unique and not c.is_identity and c.bucket != "system"
               for c in found.columns)


def _record_candidate(graph: KeyGraph, table: str, score: float,
                      reasons: list[str]) -> RecordKindCandidate:
    lines = graph.lines_of(table)
    lines_key = list(graph.table(lines).primary_key) if lines and graph.table(lines) else []
    if lines:
        reasons.append(f"lines in {lines}")
    return RecordKindCandidate(table=table, score=round(score, 3), reasons=reasons,
                               lines_table=lines, lines_key=lines_key)


def propose_primary(graph: KeyGraph) -> list[RecordKindCandidate]:
    """The tables most likely to be the primary record, best first, five at
    most. Low confidence by nature: the person picks."""
    found: list[RecordKindCandidate] = []
    for table in graph.model.tables:
        name = table.name
        if table.row_count <= 0:
            continue
        kind = graph.table_kind(name)
        # A code list is a level, lines belong to a record, and an extension
        # table only describes another: none of them is a record.
        if kind == "code" or graph.is_lines(name) or graph.owner_of(name):
            continue
        size = _size(graph, name)
        score, reasons = size, [f"{table.row_count:,} rows"]
        lines = graph.lines_of(name)
        events = sorted({e.child_table for e in graph.edges_to(name)} - {lines})
        events = [c for c in events if graph.table_kind(c) == "event"]
        if events:
            # Things happening to it: what makes a population worth reading.
            score += 0.5 * min(len(events), 2)
            reasons.append(f"events key to it: {', '.join(events[:3])}")
        if _has_business_key(graph, name):
            score += 0.25
            reasons.append("has its own business key")
        weight = graph.person_weight(name)
        if weight:
            # Columns holding people say the rows are a population; an address
            # part alone counts little (graph.PERSON_WEIGHT_BY_DOMAIN).
            score += min(weight, 3.0) / 3.0 * 1.5
            reasons.append(f"personal columns weigh {weight:g}")
        if kind == "link":
            score -= 0.5
            reasons.append("an association between other tables")
        found.append(_record_candidate(graph, name, score, reasons))
    found.sort(key=lambda c: (-c.score, c.table))
    return found[:TOP_RECORDS]


def propose_child(graph: KeyGraph, primary: str) -> list[RecordKindCandidate]:
    """Tables keyed to the primary with more than one row per primary record
    on average, best first."""
    best: dict[str, RecordKindCandidate] = {}
    primary_lines = graph.lines_of(primary)
    for edge in graph.edges_to(primary):
        name = edge.child_table
        if name == primary_lines or graph.rows(name) <= 0:
            continue
        per, measured = graph.per_parent(edge)
        if per <= 1:
            continue
        score = 0.5 + 0.5 * _size(graph, name)
        reasons = [f"{per:.2f} per {primary} record" + ("" if measured else ", from row counts"),
                   f"{graph.rows(name):,} rows"]
        found = graph.table(name)
        if found is not None and any(c.is_date for c in found.columns):
            score += 0.25
            reasons.append("carries dates")
        if graph.amounts(name):
            score += 0.25
            reasons.append("carries amounts")
        if graph.lines_of(name):
            # A record with lines of its own is a document: the shape a
            # child record most often has.
            score += 0.5
        candidate = _record_candidate(graph, name, score, reasons)
        if name not in best or candidate.score > best[name].score:
            best[name] = candidate
    return sorted(best.values(), key=lambda c: (-c.score, c.table))[:TOP_RECORDS]


# --- levels ---------------------------------------------------------------

_KIND_ORDER = {"stated": 0, "declared": 1, "inferred": 2}


def _edge_order(edge: Edge) -> tuple:
    return (_KIND_ORDER.get(edge.kind, 3), edge.parent_table, edge.child_columns)


def reachable(graph: KeyGraph, record: str, *, max_depth: int = MAX_DEPTH) -> dict[str, list[Join]]:
    """Every table above the record by keys, with the shortest path to it
    (a person's stated join first, then declared, then inferred). Each step
    runs from a child to its parent, so each record falls under one row of
    every table reached."""
    paths: dict[str, list[Join]] = {record: []}
    frontier = [record]
    for _ in range(max_depth):
        reached: list[str] = []
        for table in frontier:
            for edge in sorted(graph.edges_from(table), key=_edge_order):
                if edge.parent_table in paths or graph.rows(edge.parent_table) <= 0:
                    continue
                paths[edge.parent_table] = paths[table] + [Join.up(edge)]
                reached.append(edge.parent_table)
        frontier = reached
    return paths


def bridged(graph: KeyGraph, record: str, reached: Iterable[str]) -> dict[str, list[Join]]:
    """Tables reached only through a bridge: a link table keyed to the record
    and to another table, carrying a start and an end date, so one of its rows
    is in force at a time. Two joins: record to bridge, bridge to the table."""
    held = set(reached)
    paths: dict[str, list[Join]] = {}
    for into in sorted(graph.edges_to(record), key=_edge_order):
        bridge = into.child_table
        if graph.table_kind(bridge) != "link" or not graph.date_pairs(bridge):
            continue
        for edge in sorted(graph.edges_from(bridge), key=_edge_order):
            target = edge.parent_table
            if target == record or target in held or target in paths:
                continue
            if graph.rows(target) <= 0:
                continue
            paths[target] = [Join.down(into), Join.up(edge)]
    return paths


def _owners(graph: KeyGraph) -> dict[str, list[str]]:
    """Each table, with the tables that only extend it."""
    def build() -> dict[str, list[str]]:
        held: dict[str, list[str]] = {}
        for table in graph.model.tables:
            owner = graph.owner_of(table.name)
            if owner:
                held.setdefault(owner, []).append(table.name)
        return held
    return graph.memo("owners", build)


def _key_columns_of(graph: KeyGraph, table: str) -> set[str]:
    found = graph.table(table)
    keys = set(found.primary_key) if found is not None else set()
    return keys | {c for e in graph.edges_from(table) for c in e.child_columns}


def label_for(graph: KeyGraph, table: str, column: str,
              distinct: int) -> tuple[str | None, str | None, float, Join | None]:
    """The column naming a level's values: text, named as a name or a
    description, on the level's table or on a table that only extends it;
    the one with the most distinct values. Never a personal column."""
    best: tuple | None = None
    for holder in [table] + sorted(_owners(graph).get(table, [])):
        found = graph.table(holder)
        if found is None:
            continue
        keys = _key_columns_of(graph, holder)
        for candidate in found.columns:
            if (candidate.name == column or candidate.name in keys
                    or not candidate.is_character
                    or not name_tokens(candidate.name) & LABEL_TOKENS
                    or graph.is_personal(holder, candidate.name)):
                continue
            held = graph.profile_of(holder, candidate.name)
            ratio = min(1.0, held.distinct_count / distinct) if held and distinct else 0.0
            rank = (ratio, holder == table, candidate.name)
            if best is None or rank > best[0]:
                join = None
                if holder != table:
                    edge = next(e for e in graph.edges_from(holder) if e.parent_table == table
                                and e.child_columns == found.primary_key)
                    join = Join.up(edge)
                best = (rank, holder, candidate.name, ratio, join)
    if best is None:
        return None, None, 0.0, None
    return best[1], best[2], best[3], best[4]


def _is_status(graph: KeyGraph, table: str, column: str) -> bool:
    return (bool(name_tokens(column) & STATUS_TOKENS)
            or graph.element_of(table, column) == STATUS_ELEMENT)


def _code_columns(graph: KeyGraph, table: str, *, below: int | None = None) -> list[tuple[str, int, float]]:
    """A table's plain code columns: 2 to CODE_MAX_DISTINCT values, low
    cardinality, no key, no date, no amount, no status, nothing personal,
    filled on most rows; fewer values than `below` when given. With each its
    distinct count and null rate."""
    found = graph.table(table)
    if found is None:
        return []
    keys = _key_columns_of(graph, table)
    out = []
    for column in found.columns:
        if (column.name in keys or column.is_date or column.is_identity or column.is_computed
                or (column.is_numeric and not column.is_integer)
                or name_tokens(column.name) & LABEL_TOKENS
                or _is_status(graph, table, column.name)
                or graph.is_personal(table, column.name)):
            continue
        held = graph.profile_of(table, column.name)
        if held is None or held.cardinality != "low" or held.null_rate > 0.5:
            continue
        if not 2 <= held.distinct_count <= CODE_MAX_DISTINCT:
            continue
        if below is not None and held.distinct_count >= below:
            continue
        out.append((column.name, held.distinct_count, held.null_rate))
    return out


def _narrow_confidence(column: str, distinct: int, base: float) -> tuple[float, list[str]]:
    tokens = name_tokens(column)
    confidence, notes = base, []
    if tokens & KIND_TOKENS:
        confidence += 0.2
        notes.append("named as a kind")
    if tokens & INDICATOR_TOKENS or distinct == 2:
        confidence -= 0.1
        notes.append("a yes/no split")
    return _clamp(confidence), notes


def _joins_text(path: list[Join]) -> str:
    if not path:
        return "on the record"
    kinds = sorted({j.kind for j in path})
    return f"{len(path)} join{'s' if len(path) > 1 else ''} ({', '.join(kinds)})"


def level_for(graph: KeyGraph, record: str, table: str, column: str, path: list[Join], *,
              bridge: bool = False) -> LevelCandidate | None:
    """One level: the column's values on `table`, reached from the record by
    `path`. None when the column has fewer than two values."""
    rows = graph.rows(record)
    walk_bound = max(2, rows // WALK_ROWS_PER_VALUE)
    distinct = graph.distinct(table, column)
    if distinct < 2:
        return None
    # An estimated count (counted approximately, or the table's rows standing
    # in for a key nobody profiled) is said so, and never alone turns a walk
    # level into a narrowing one when it is within twice the bound.
    estimated = graph.distinct_estimated(table, column)
    per_value = round(rows / distinct, 1) if distinct else 0.0
    notes = [f"{distinct:,} values{' (estimated)' if estimated else ''}, "
             f"{per_value:,} records each", _joins_text(path)]
    if table == record or (path and column not in path[-1].to_columns):
        # A plain code column, on the record or on a level's table.
        kind = "narrow"
        confidence, more = _narrow_confidence(column, distinct, 0.4 if table == record else 0.3)
        notes += more
        label_table = label_column = label_join = None
    else:
        within = distinct <= walk_bound or (estimated and distinct <= 2 * walk_bound)
        kind = "walk" if within else "narrow"
        confidence = 0.5 if kind == "walk" else 0.4
        if kind == "narrow":
            notes.append(f"more than {walk_bound:,} values: narrows rather than walks")
        elif distinct > walk_bound:
            notes.append(f"kept as a walk level: past {walk_bound:,} values only by an "
                         f"estimate within twice that")
        label_table, label_column, ratio, label_join = label_for(graph, table, column, distinct)
        if label_column:
            confidence += 0.2 if ratio >= 0.9 else 0.1
            notes.append(f"labeled by {label_column}" +
                         ("" if ratio >= 0.9 else f" ({ratio:.0%} distinct)"))
        else:
            notes.append("no label column")
        if bridge:
            confidence -= 0.1
            notes.append(f"through {path[0].to_table}, one row in force at a time")
        first = graph.profile_of(record, path[0].from_columns[0]) if path else None
        if first is not None and first.null_rate > 0.5:
            confidence -= 0.2
            notes.append(f"{first.null_rate:.0%} of records have none")
        confidence = _clamp(confidence)
    return LevelCandidate(
        id=level_id(table, column), table=table, column=column, label_table=label_table,
        label_column=label_column, path=list(path), distinct=distinct, per_value=per_value,
        kind=kind, confidence=confidence, reason="; ".join(notes), label_join=label_join)


def path_to(graph: KeyGraph, record: str, table: str) -> tuple[list[Join], bool] | None:
    """The path from the record to a table, and whether it runs through a
    bridge; None when the table cannot be reached by keys."""
    if table == record:
        return [], False
    paths = reachable(graph, record)
    if table in paths:
        return paths[table], False
    bridges = bridged(graph, record, paths)
    if table in bridges:
        return bridges[table], True
    return None


def level_refusal(graph: KeyGraph, record: str, table: str, column: str, path: list[Join], *,
                  lines: str | None = None, primary: str | None = None) -> str | None:
    """Why `table.column`, reached from the record by `path`, cannot be one
    of its levels; None when it can. One rule for what propose_levels offers
    and what a person may swap in.

    On the record itself only a plain code column is a level. Elsewhere:
    `lines` and `primary` are never levels (the lines sit below the record,
    and a child's primary record is its parent, not a grouping of it); nor is
    an empty table, a table that only extends another (it lends that one a
    label), a link or event table, or a table holding people (a population).
    The column is the key the path reaches the table by, or a code column."""
    found = graph.table(table)
    if found is None:
        return f"the source has no table {table!r}"
    if found.column(column) is None:
        return f"{table} has no column {column!r}"
    if graph.is_personal(table, column):
        return f"{table}.{column} holds personal values and cannot be a level"
    def is_code() -> bool:
        return any(name == column for name, _, _ in _code_columns(graph, table))

    if table == record:
        if is_code():
            return None
        return (f"{column} is not a code column of {record}: on the record itself only "
                f"a code column is a level")
    if table == lines:
        return f"{table} holds the record's lines, below it, not a level above it"
    if table == primary:
        return f"{table} is the primary record, this record's parent, not a grouping of it"
    if graph.rows(table) <= 0:
        return f"{table} holds no rows"
    owner = graph.owner_of(table)
    if owner:
        return f"{table} only extends {owner}: its columns label {owner}'s levels"
    kind = graph.table_kind(table)
    if kind in {"link", "event"}:
        return f"{table} is a {kind} table, not a grouping of records"
    if graph.person_weight(table) >= 1.0:
        return f"{table} holds people: a population, never a level"
    key = list(path[-1].to_columns) if path else []
    if key != [column] and not is_code():
        return (f"{column} is neither the key {table} is reached by nor a code column "
                f"of {table}")
    return None


def propose_levels(graph: KeyGraph, record: str, *, lines: str | None = None,
                   primary: str | None = None,
                   notes: list[str] | None = None) -> list[LevelCandidate]:
    """Levels for a record kind, unordered (order_levels orders them); each
    table above the record that level_refusal lets through. A level left out
    because its column was never profiled is said in `notes`, when given."""
    found: dict[str, LevelCandidate] = {}
    paths = reachable(graph, record)
    bridges = bridged(graph, record, paths)
    for table, path, through in ([(t, p, False) for t, p in paths.items()] +
                                 [(t, p, True) for t, p in bridges.items()]):
        if not path or len(path[-1].to_columns) != 1:
            continue
        column = path[-1].to_columns[0]
        if level_refusal(graph, record, table, column, path, lines=lines, primary=primary):
            continue
        level = level_for(graph, record, table, column, path, bridge=through)
        if level is None and graph.profile_of(table, column) is None and notes is not None:
            note = (f"{level_id(table, column)} was not profiled, so it is not offered as a "
                    f"level of {record}: read the landscape with a larger shortlist to see it")
            if note not in notes:
                notes.append(note)
        if level is not None and level.distinct <= MAX_LEVEL_DISTINCT:
            found.setdefault(level.id, level)
    # Plain code columns on the record.
    for column, _, _ in _code_columns(graph, record):
        level = level_for(graph, record, record, column, [])
        if level is not None:
            found.setdefault(level.id, level)
    # Plain code columns on the table of each likely walk level: they split
    # its groups (a kind of each level value, say).
    for level in list(found.values()):
        if level.kind != "walk" or level.confidence < SELECT_AT or level.table == record:
            continue
        for column, _, _ in _code_columns(graph, level.table, below=level.distinct):
            extra = level_for(graph, record, level.table, column, level.path)
            if extra is not None:
                found.setdefault(extra.id, extra)
    return list(found.values())


# --- order and nesting ----------------------------------------------------

def _carriers(graph: KeyGraph, level: LevelCandidate) -> dict[str, str]:
    """Tables holding the level's values: its own table, and every table
    keyed to it on that column."""
    held = {level.table: level.column}
    for edge in graph.edges_to(level.table):
        if edge.parent_columns == [level.column] and len(edge.child_columns) == 1:
            held.setdefault(edge.child_table, edge.child_columns[0])
    return held


def _above(graph: KeyGraph, table: str) -> set[str]:
    return graph.memo(("above", table), lambda: set(reachable(graph, table)) - {table})


def _unique_on(graph: KeyGraph, table: str, column: str) -> bool:
    """Unique by the schema, or by the profile's count -- the count trusted
    only when it was exact or the whole table was read: an approximate count
    on a sample can round a nearly unique column up to unique."""
    found = graph.table(table)
    if found is not None and (found.primary_key == [column]
                              or column in found.unique_single_columns()):
        return True
    held = graph.profile_of(table, column)
    if not (held and held.row_count and held.null_rate == 0
            and held.distinct_ratio >= UNIQUE_RATIO):
        return False
    return not held.approximate_distinct or (found is not None
                                             and not source_read.is_sampled(found))


def _counted(graph: KeyGraph, table: str, counted: tuple[int, int, int] | None) -> bool | None:
    """What one pair count says: None when nothing was counted, or when, on a
    sampled table, equal counts come from values each seen about once (the
    profile's own rule: twice on average, sdg/profiling/dependencies.py).
    Unequal counts say False whatever the sample."""
    if counted is None:
        return None
    rows_both, distinct_b, distinct_pair = counted
    if distinct_b <= 0:
        return None
    if distinct_pair != distinct_b:
        return False
    found = graph.table(table)
    if found is not None and source_read.is_sampled(found) and rows_both < 2 * distinct_b:
        return None
    return True


def nests(graph: KeyGraph, inner: LevelCandidate, outer: LevelCandidate,
          pair_counts: PairCounts | None = None) -> bool | None:
    """Whether each value of `inner` falls under one value of `outer`.
    True or False when keys, the profile or a pair count say; None when
    nothing does."""
    if inner.id == outer.id:
        return True
    inner_table = graph.table(inner.table)
    is_key = inner_table is not None and inner_table.primary_key == [inner.column]
    # A table's key decides every column of its row, and every table above it.
    if is_key and (outer.table == inner.table or outer.table in _above(graph, inner.table)):
        return True
    inner_on, outer_on = _carriers(graph, inner), _carriers(graph, outer)
    shared = sorted(set(inner_on) & set(outer_on))
    for table in shared:
        a, b = outer_on[table], inner_on[table]
        if a == b:
            continue
        # Unique on a table holding both, it decides the other there.
        if _unique_on(graph, table, b):
            return True
        measured = next((d for d in graph.profile.dependencies
                         if d.table == table and d.determinant == b and d.dependent == a), None)
        if measured is not None:
            return measured.holds
    if pair_counts is not None:
        for table in shared:
            a, b = outer_on[table], inner_on[table]
            if a == b:
                continue
            said = _counted(graph, table, pair_counts(table, a, b))
            if said is not None:
                return said
    return None


def verdict(graph: KeyGraph, inner: LevelCandidate, outer: LevelCandidate,
            pair_counts: PairCounts | None = None) -> bool | None:
    """nests(), from keys and the profile first; then from a pair count,
    kept on the level; then from a count kept by an earlier proposal."""
    found = nests(graph, inner, outer)
    if found is not None:
        return found
    if pair_counts is not None:
        found = nests(graph, inner, outer, pair_counts)
        if found is not None:
            inner.nesting[outer.id] = found
            return found
    return inner.nesting.get(outer.id)


def _strip_flag(reason: str, flags: Iterable[str] = (NEST_FLAG, UNKNOWN_FLAG)) -> str:
    for flag in flags:
        reason = reason.split("; " + flag)[0]
    return reason


def flag_nesting(levels: list[LevelCandidate], graph: KeyGraph,
                 pair_counts: PairCounts | None = None) -> list[LevelCandidate]:
    """Mark each selected walk level that does not nest under the selected
    walk level before it (every walk level, when none is selected), or that
    nothing could say nests or not: unknown is said as unknown, never taken
    for crossed. The level is kept: crossed levels are still walked, one
    within the other."""
    for level in levels:
        level.reason = _strip_flag(level.reason)
    walk = sorted((lv for lv in levels if lv.kind == "walk"), key=lambda lv: lv.order)
    chosen = [lv for lv in walk if lv.selected] or walk
    for before, level in zip(chosen, chosen[1:]):
        said = verdict(graph, level, before, pair_counts)
        if said is False:
            level.reason += f"; {NEST_FLAG}{before.column}"
        elif said is None:
            level.reason += f"; {UNKNOWN_FLAG}{before.column}"
    return levels


def order_levels(levels: list[LevelCandidate], graph: KeyGraph,
                 pair_counts: PairCounts | None = None, *,
                 first: Iterable[str] = ()) -> list[LevelCandidate]:
    """Walk levels broadest first, then narrowing ones, numbered from 1.

    Walk levels that nest form chains; within a chain the level fewest others
    decide comes first (then fewer values, then the shorter path). Chains go
    coarse to fine, by their finest level's count, so a walk ends at its
    finest groups. Ids in `first` (a child's levels shared with its primary,
    in the primary's order) lead.

    The source is asked (`pair_counts`) only about neighbors in the order
    their counts give, never about every pair: a source of many levels would
    otherwise be sent a statement for each of n * n pairs. Two levels whose
    nesting nothing could say are placed by their counts, and their reasons
    say so."""
    walk = [lv for lv in levels if lv.kind == "walk"]
    narrow = [lv for lv in levels if lv.kind != "walk"]
    leading = {lid: i for i, lid in enumerate(first)}
    led = sorted((lv for lv in walk if lv.id in leading), key=lambda lv: leading[lv.id])
    rest = [lv for lv in walk if lv.id not in leading]
    for level in levels:
        level.reason = _strip_flag(level.reason, (ORDER_FLAG, NEST_FLAG, UNKNOWN_FLAG))

    if pair_counts is not None:
        provisional = sorted(rest, key=lambda lv: (lv.distinct, len(lv.path), lv.id))
        for outer, inner in zip(provisional, provisional[1:]):
            # Kept on the level, so the full pass below reads it.
            verdict(graph, inner, outer, pair_counts)
    under: dict[str, set[str]] = {lv.id: set() for lv in rest}
    unknown: dict[str, set[str]] = {lv.id: set() for lv in rest}
    for inner in rest:
        for outer in rest:
            if inner.id == outer.id:
                continue
            said = verdict(graph, inner, outer)
            if said:
                under[inner.id].add(outer.id)
            elif said is None:
                unknown[inner.id].add(outer.id)
    # Chains: levels joined by nesting either way.
    group: dict[str, int] = {}
    for start in rest:
        if start.id in group:
            continue
        number, stack = len(set(group.values())), [start.id]
        while stack:
            current = stack.pop()
            if current in group:
                continue
            group[current] = number
            stack += [lv.id for lv in rest if lv.id not in group and (
                lv.id in under[current] or current in under[lv.id])]
    chains: dict[int, list[LevelCandidate]] = {}
    for lv in rest:
        chains.setdefault(group[lv.id], []).append(lv)
    for chain in chains.values():
        chain.sort(key=lambda lv: (len(under[lv.id]), lv.distinct, len(lv.path), lv.id))
    ordered_chains = sorted(chains.values(), key=lambda m: (
        max(lv.distinct for lv in m), len(m), m[0].id))
    ordered = led + [lv for chain in ordered_chains for lv in chain]
    by_id = {lv.id: lv for lv in rest}
    for level in rest:
        # Placed against another level in a different chain by counts alone,
        # since neither was known to nest under the other.
        open_ = sorted(by_id[o].column for o in unknown[level.id]
                       if group[o] != group[level.id] and level.id in unknown[o])
        if open_:
            shown = ", ".join(open_[:ORDER_SHOWN])
            more = f" and {len(open_) - ORDER_SHOWN} more" if len(open_) > ORDER_SHOWN else ""
            level.reason += f"; {ORDER_FLAG}{shown}{more}"
    narrow.sort(key=lambda lv: (-lv.confidence, lv.distinct, lv.id))
    result = ordered + narrow
    for number, level in enumerate(result, start=1):
        level.order = number
    return flag_nesting(result, graph, pair_counts)


def renumber(levels: list[LevelCandidate]) -> list[LevelCandidate]:
    """Selected levels first, each group keeping its order; numbered from 1."""
    result = sorted(levels, key=lambda lv: (not lv.selected, lv.order, lv.id))
    for number, level in enumerate(result, start=1):
        level.order = number
    return result


def select_defaults(levels: list[LevelCandidate]) -> list[LevelCandidate]:
    """The first proposal's choice: confident walk levels in order, at most
    MAX_WALK_SELECTED, then confident narrowing levels; renumbered."""
    walked = 0
    for level in sorted(levels, key=lambda lv: lv.order):
        if level.confidence < SELECT_AT:
            level.selected = False
        elif level.kind == "walk":
            level.selected = walked < MAX_WALK_SELECTED
            walked += level.selected
        else:
            level.selected = True
    return renumber(levels)


# --- features -------------------------------------------------------------

def _earliest(graph: KeyGraph, table: str, columns: list[str]) -> str | None:
    def start(name: str) -> tuple:
        held = graph.profile_of(table, name)
        low = str(held.minimum) if held is not None and held.minimum is not None else "~"
        return (low, not name_tokens(name) & {"ORIG", "ORIGINAL", "SINCE"}, name)
    return min(columns, key=start) if columns else None


def propose_features(graph: KeyGraph, record: str, catalog: Catalog | None = None, *,
                     lines: str | None = None) -> list[FeatureCandidate]:
    """What a pattern of the record is described by: code columns (in a set
    of values), amounts (in a range), an age from a birth date and a tenure
    from the earliest start date. Personal columns are never features
    themselves; a birth date lends only the age it gives. With `lines`, the
    lines table's codes and amounts are offered too (a feature of each line),
    not selected: the person picks them."""
    found = graph.table(record)
    if found is None:
        return []
    mapped = graph.mapped(catalog)
    out: list[FeatureCandidate] = []
    for column, distinct, null_rate in _code_columns(graph, record):
        out.append(FeatureCandidate(
            id=level_id(record, column), column=column, kind="in", derive=None,
            distinct=distinct, null_rate=round(null_rate, 4), confidence=0.6,
            reason=f"{distinct} values", selected=True))
    for column in graph.amounts(record):
        held = graph.profile_of(record, column)
        named = mapped.get((record, column)) == AMOUNT_ELEMENT
        out.append(FeatureCandidate(
            id=level_id(record, column), column=column, kind="range", derive=None,
            distinct=held.distinct_count if held else 0,
            null_rate=round(held.null_rate, 4) if held else 0.0,
            confidence=0.7 if named else 0.6,
            reason="an amount" + (" (catalog)" if named else ", by its type"), selected=True))
    dates = [c.name for c in found.columns if c.is_date]
    births = [c for c in dates if mapped.get((record, c)) == BIRTH_ELEMENT
              or "BIRTH" in name_tokens(c)]
    starts = [c for c in dates if c not in births and not name_tokens(c) & END_TOKENS
              and (name_tokens(c) & TENURE_TOKENS
                   or mapped.get((record, c)) == "EFFECTIVE_DATE")]
    for derived, source, unit, why in (
            ("_age", births[0] if births else None, "years", "elapsed since a birth date"),
            ("_tenure", _earliest(graph, record, starts), "months",
             "elapsed since the earliest start date")):
        if source is None:
            continue
        held = graph.profile_of(record, source)
        out.append(FeatureCandidate(
            id=level_id(record, derived), column=derived, kind="range",
            derive={"op": "elapsed", "from": source, "unit": unit},
            distinct=held.distinct_count if held else 0,
            null_rate=round(held.null_rate, 4) if held else 0.0,
            confidence=0.7 if derived == "_age" else 0.6,
            reason=f"{why}, {source}", selected=True))
    if lines and graph.table(lines) is not None:
        for column, distinct, null_rate in _code_columns(graph, lines):
            out.append(FeatureCandidate(
                id=level_id(lines, column), column=column, kind="in", derive=None,
                distinct=distinct, null_rate=round(null_rate, 4), confidence=0.5,
                reason=f"{distinct} values, on each line ({lines})", selected=False,
                table=lines))
        for column in graph.amounts(lines):
            if graph.is_personal(lines, column):
                continue  # never a feature, so never offered as one
            held = graph.profile_of(lines, column)
            named = mapped.get((lines, column)) == AMOUNT_ELEMENT
            out.append(FeatureCandidate(
                id=level_id(lines, column), column=column, kind="range", derive=None,
                distinct=held.distinct_count if held else 0,
                null_rate=round(held.null_rate, 4) if held else 0.0, confidence=0.5,
                reason=("an amount" + (" (catalog)" if named else ", by its type")
                        + f", on each line ({lines})"), selected=False, table=lines))
    return out

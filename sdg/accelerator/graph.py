"""The source's keys as a graph: which tables point at which, and how strongly.

Every proposal reads this rather than the model and profile directly. An edge
runs from a child table to the parent it keys to, declared by the schema,
inferred by discovery, or stated by the person; it carries the share of the
child's values found in the parent and, where the profile measured it, how many
child rows each parent holds on average.

Discovery infers single-column keys only. A parent whose primary key has
several columns is reached by a cheap extension: a table carrying columns of
the same names and comparable types is a candidate child, and, when the source
can be read, one inclusion query on the shortlist decides it. Read from the
saved landscape alone (no registry), those candidates are left unchecked and
nothing is sent anywhere.

Every statement the proposer sends goes through one SourceChecks: at most
MAX_SOURCE_CHECKS a run and CHECK_SECONDS of them in all, each through
source_read with its own time and lock limits. A check the database will
not answer is noted with the tables it named; a lost connection ends the run,
since every check after it would fail the same way.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

import pyodbc

from sdg.accelerator import landscape as landscape_reading
from sdg.accelerator import source_read
from sdg.catalog.loader import load_catalog
from sdg.catalog.model import Catalog
from sdg.discovery.classify import CODE_TABLE_MAX_ROWS, tokenize
from sdg.discovery.inference import (CHECKED_VALUES, INCLUSION_THRESHOLD, MIN_DISTINCT_VALUES,
                                     _comparable)
from sdg.discovery.model import PhysicalModel, Table
from sdg.mapping.scorer import map_columns
from sdg.profiling import sample
from sdg.profiling.model import ColumnProfile, ProfileReport

logger = logging.getLogger(__name__)

# How much one personal column says that a table's rows are people. A name, a
# birth date or a national number belongs to a person only; a phone or an email
# can belong to an office; an address part (a city, a state, a postal code)
# belongs to any place, so a branch or a site with a city column is not
# person-like on that alone.
PERSON_WEIGHT_BY_DOMAIN = {"Person": 1.0, "Contact": 0.5, "Address": 0.25,
                           # A login names the staff who changed a row,
                           # not the population the rows are about.
                           "User": 0.25}

# Statements the proposer sends a source in one run, at most: composite-key
# checks and nesting counts together. Each may take up to source_read's
# timeout, and the source is often production; what the cap leaves out is
# noted, never guessed.
MAX_SOURCE_CHECKS = 50
# Seconds the checks of one run may take together, at most: the cap counts
# statements, each up to source_read.CHECK_TIMEOUT, so the time is bounded too.
# A check that would start past it is noted and not sent.
CHECK_SECONDS = 300.0

# A driver error that says the connection itself is gone or refused: SQLSTATE
# class 08, a refused login, a missing driver; or the driver's own words.
_CONNECTION_STATES = ("08", "28000", "IM002")
_CONNECTION_WORDS = re.compile(
    r"Communication link failure|Login failed|server was not found|\bTCP\b|"
    r"Named Pipes|Login timeout expired|actively refused|network-related",
    re.IGNORECASE)


def connection_lost(exc: BaseException) -> bool:
    """Whether a driver error says the connection is gone or was refused."""
    state = str(exc.args[0]) if getattr(exc, "args", None) else ""
    return state.startswith(_CONNECTION_STATES) or bool(_CONNECTION_WORDS.search(str(exc)))


@dataclass
class SourceChecks:
    """The statements one proposal sends a source: capped, each through
    source_read, each failure noted with what it was checking."""
    registry: Any
    limit: int = MAX_SOURCE_CHECKS
    seconds: float = CHECK_SECONDS
    sent: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    late: list[str] = field(default_factory=list)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    started: float | None = None

    def run(self, what: str, call: Callable[[], Any]) -> tuple[bool, Any]:
        """(checked, what `call` returned): `call` sends one statement
        through source_read. Not checked when the cap is reached, the run's
        time is spent, or the database refused the statement; each is noted.
        A lost connection raises."""
        if self.started is None:
            self.started = self.clock()
        if self.sent >= self.limit:
            self.skipped.append(what)
            return False, []
        if self.clock() - self.started >= self.seconds:
            self.late.append(what)
            return False, []
        self.sent += 1
        try:
            return True, call()
        except pyodbc.Error as exc:
            if connection_lost(exc):
                raise
            # The driver's words can quote a value from the source (a failed
            # conversion names it), so only the error's kind and codes are kept.
            said = source_read.error_said(exc)
            logger.warning("could not check %s: %s", what, said)
            self.failed.append(f"{what}: {said}")
            return False, []

    def notes(self) -> list[str]:
        out = [f"Not checked, the source refused it: {line}" for line in self.failed]
        for held, why in ((self.skipped, f"past the limit of {self.limit} statements a run"),
                          (self.late, f"past the time limit of {self.seconds:g} seconds a run")):
            if held:
                shown = "; ".join(held[:10])
                more = f" and {len(held) - 10} more" if len(held) > 10 else ""
                out.append(f"Not checked, {why}: {shown}{more}")
        return out

# The composite inclusion check: the child's distinct key tuples, each looked
# up in the parent by its whole key, as discovery's INCLUSION_SQL does for one
# column. Names come from the discovered model, bracketed; nothing else is
# written into the statement.
COMPOSITE_INCLUSION_SQL = """
SELECT COUNT(*) AS distinct_values,
       SUM(CASE WHEN p.{first} IS NULL THEN 1 ELSE 0 END) AS missing
FROM (
    SELECT DISTINCT TOP ({checked}) {child_columns}
    FROM {child_source}
    WHERE {not_null}
) AS c
LEFT JOIN {parent_table} AS p ON {on}{hint}
"""


@dataclass
class Edge:
    child_table: str
    child_columns: list[str]
    parent_table: str
    parent_columns: list[str]
    kind: str  # declared | inferred | stated
    coverage: float | None = None
    per_parent: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KeyGraph:
    model: PhysicalModel
    profile: ProfileReport
    edges: list[Edge] = field(default_factory=list)
    shortlist: list[str] = field(default_factory=list)
    # Composite-key candidates left unchecked because the source was not read,
    # and those the source confirmed (now or in an earlier proposal).
    unchecked: list[Edge] = field(default_factory=list)
    confirmed: list[Edge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _mapped: dict[int, dict[tuple[str, str], str]] = field(default_factory=dict, repr=False)
    _base: Catalog | None = field(default=None, repr=False)
    # What the graph's edges imply -- lookups by table, lines, owners, kinds --
    # worked out once and dropped whenever an edge is added: a source of
    # thousands of tables cannot be scanned from the top for each question.
    _cache: dict[Any, Any] = field(default_factory=dict, repr=False)

    def memo(self, key: Any, compute: Any) -> Any:
        """`compute()`, worked out once until the next edge is added."""
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    # --- building ---------------------------------------------------------

    @classmethod
    def from_landscape(cls, landscape: landscape_reading.Landscape, *,
                       registry: Any = None, stated: Iterable[Edge] = (),
                       confirmed: Iterable[Edge] = (),
                       checks: SourceChecks | None = None) -> "KeyGraph":
        """The graph of a landscape's keys, declared and inferred, plus any the
        person stated. With `registry` (a SourceRegistry), composite-key
        candidates are checked against the source; without it they are listed
        as unchecked and no statement is sent, and `confirmed` -- candidates
        an earlier read confirmed, kept in the plan -- are taken as they are.
        `checks` carries the run's statement cap across the graph and the
        nesting counts; one is made when only a registry is given."""
        model, profile = landscape.model, landscape.profile
        shortlist = list(landscape.summary.get("shortlisted") or
                         [t.name for t in model.tables if t.row_count > 0])
        graph = cls(model=model, profile=profile, shortlist=shortlist)
        seen: set[tuple] = set()
        for table in model.tables:
            for key in table.foreign_keys:
                graph._add(Edge(table.name, list(key.columns), key.parent_table,
                                list(key.parent_columns), key.kind, key.coverage), seen)
        candidates = composite_candidates(model, shortlist, graph.edges)
        if registry is None:
            kept = {(e.child_table, tuple(e.child_columns), e.parent_table) for e in confirmed}
            for edge in confirmed:
                graph._add(Edge(edge.child_table, list(edge.child_columns), edge.parent_table,
                                list(edge.parent_columns), "inferred", edge.coverage), seen)
                graph.confirmed.append(edge)
            candidates = [c for c in candidates
                          if (c.child_table, tuple(c.child_columns), c.parent_table) not in kept]
            graph.unchecked = candidates
            if candidates:
                graph.notes.append(
                    f"{len(candidates)} composite-key candidates were not checked: the "
                    f"proposal was made from the saved landscape without reading the source")
        else:
            if not isinstance(registry, landscape_reading.sources.SourceRegistry):
                raise TypeError("a source is read through a SourceRegistry only")
            if checks is None:
                checks = SourceChecks(registry)
            for candidate in candidates:
                checked, found = check_composite(checks, model, candidate)
                if not checked:
                    graph.unchecked.append(candidate)
                elif found is not None:
                    graph._add(found, seen)
                    graph.confirmed.append(found)
        for edge in stated:
            graph.add_stated(edge)
        return graph

    def _add(self, edge: Edge, seen: set[tuple] | None = None) -> None:
        name = (edge.child_table, tuple(edge.child_columns), edge.parent_table,
                tuple(edge.parent_columns))
        if seen is not None:
            if name in seen:
                return
            seen.add(name)
        elif any((e.child_table, tuple(e.child_columns), e.parent_table,
                  tuple(e.parent_columns)) == name for e in self.edges):
            return
        if edge.per_parent is None and len(edge.child_columns) == 1:
            ratio = next((r for r in self.profile.ratios
                          if r.child_table == edge.child_table
                          and r.child_column == edge.child_columns[0]
                          and r.parent_table == edge.parent_table
                          and r.parent_column == edge.parent_columns[0]), None)
            if ratio is not None:
                edge.per_parent = ratio.mean
        self.edges.append(edge)
        self._cache.clear()

    def add_stated(self, edge: Edge) -> None:
        """A join the person stated; refused when it names a table or column
        the model does not hold."""
        for table_name, columns in ((edge.child_table, edge.child_columns),
                                    (edge.parent_table, edge.parent_columns)):
            table = self.model.table(table_name)
            if table is None:
                raise ValueError(f"the source has no table {table_name!r}")
            missing = [c for c in columns if table.column(c) is None]
            if missing or not columns:
                raise ValueError(f"{table_name} has no column {', '.join(missing) or '(none)'}")
        if len(edge.child_columns) != len(edge.parent_columns):
            raise ValueError("a join pairs as many columns on each side")
        edge.kind = "stated"
        self._add(edge)

    # --- reading ----------------------------------------------------------

    def table(self, name: str) -> Table | None:
        return self.model.table(name)

    def rows(self, name: str) -> int:
        table = self.model.table(name)
        return table.row_count if table is not None else 0

    def _index(self) -> tuple[dict[str, list[Edge]], dict[str, list[Edge]]]:
        def build() -> tuple[dict[str, list[Edge]], dict[str, list[Edge]]]:
            up: dict[str, list[Edge]] = {}
            down: dict[str, list[Edge]] = {}
            for edge in self.edges:
                if edge.child_table != edge.parent_table:
                    up.setdefault(edge.child_table, []).append(edge)
                    down.setdefault(edge.parent_table, []).append(edge)
            return up, down
        return self.memo("index", build)

    def edges_from(self, table: str) -> list[Edge]:
        """The table's parents: the edges it keys out on."""
        return list(self._index()[0].get(table, ()))

    def edges_to(self, table: str) -> list[Edge]:
        """The table's children: the edges that key to it."""
        return list(self._index()[1].get(table, ()))

    def in_degree(self, table: str) -> int:
        """How many other tables key to this one."""
        return len({e.child_table for e in self.edges_to(table)})

    def profile_of(self, table: str, column: str) -> ColumnProfile | None:
        return self.profile.column(table, column)

    def distinct(self, table: str, column: str) -> int:
        """The column's distinct values, from the profile; the table's rows
        when the column is its single-column key and was not profiled."""
        held = self.profile_of(table, column)
        if held is not None and held.distinct_count:
            return held.distinct_count
        found = self.model.table(table)
        if found is not None and found.primary_key == [column]:
            return found.row_count
        return 0

    def distinct_estimated(self, table: str, column: str) -> bool:
        """Whether distinct() is an estimate: the profile counted it
        approximately, or did not profile it and the table's row count stood in."""
        held = self.profile_of(table, column)
        if held is not None and held.distinct_count:
            return bool(held.approximate_distinct)
        return True

    def per_parent(self, edge: Edge) -> tuple[float, bool]:
        """Child rows per parent, and whether it was measured. Unmeasured, the
        two tables' row counts give an estimate."""
        if edge.per_parent is not None:
            return edge.per_parent, True
        parent = self.rows(edge.parent_table)
        return (self.rows(edge.child_table) / parent if parent else 0.0), False

    def date_pairs(self, table: str) -> list[tuple[str, str]]:
        """The table's start and end date columns, by their names (landscape.date_pairs)."""
        found = self.memo("date_pairs",
                          lambda: landscape_reading.date_pairs(self.model, self.profile))
        return [(p["start"], p["end"]) for p in found.get(table, ())]

    def mapped(self, catalog: Catalog | None = None) -> dict[tuple[str, str], str]:
        """(table, column) to the catalog element it maps to outright; the
        base catalog when none is given."""
        if catalog is None:
            if self._base is None:
                self._base = load_catalog(landscape_reading.BASE_CATALOG)
            catalog = self._base
        held = self._mapped.get(id(catalog))
        if held is None:
            held = {(m.table, m.column): m.element
                    for m in map_columns(self.model, catalog, self.profile).mappings
                    if m.accepted and m.element}
            self._mapped[id(catalog)] = held
        return held

    def element_of(self, table: str, column: str) -> str | None:
        return self.mapped().get((table, column))

    def _base_elements(self) -> dict[str, Any]:
        self.mapped()
        base = self._base
        if base is None:
            raise RuntimeError("the base catalog was not loaded")
        return self.memo("base_elements", lambda: {e.name: e for e in base.elements})

    def is_personal(self, table: str, column: str) -> bool:
        """A column mapped to a personal element of the base catalog: its
        values are never shown and it is never a level or a label."""
        element = self._base_elements().get(self.element_of(table, column) or "")
        return element is not None and element.pii in landscape_reading.PERSONAL_CLASSES

    def person_weight(self, table: str) -> float:
        """How strongly the table's columns say its rows are people
        (PERSON_WEIGHT_BY_DOMAIN per personal column)."""
        return self.memo(("person_weight", table), lambda: self._person_weight(table))

    def _person_weight(self, table: str) -> float:
        found = self.model.table(table)
        if found is None:
            return 0.0
        elements = self._base_elements()
        weight = 0.0
        for column in found.columns:
            if self.is_personal(table, column.name):
                element = elements[self.element_of(table, column.name) or ""]
                weight += PERSON_WEIGHT_BY_DOMAIN.get(element.domain or "", 0.5)
        return weight

    def lines_of(self, table: str) -> str | None:
        """The table whose primary key is this table's key plus one more
        column, keyed back to it: its lines. The first by name when several."""
        return self.memo(("lines_of", table), lambda: self._lines_of(table))

    def _lines_of(self, table: str) -> str | None:
        found = self.model.table(table)
        if found is None or not found.primary_key:
            return None
        key = set(found.primary_key)
        for edge in sorted(self.edges_to(table), key=lambda e: e.child_table):
            other = self.model.table(edge.child_table)
            if other is None or other.row_count == 0 or set(edge.child_columns) != key:
                continue
            if len(other.primary_key) == len(key) + 1 and key <= set(other.primary_key):
                return other.name
        return None

    def is_lines(self, table: str) -> bool:
        """Whether the table is the lines of some other table."""
        return any(self.lines_of(e.parent_table) == table for e in self.edges_from(table))

    def owner_of(self, table: str) -> str | None:
        """For a table that only extends another -- its single-column key keys
        to the other's single-column key, no more rows than it, a description
        kept apart, say -- the table it extends. None for a table that stands
        on its own. Two tables extending each other: the declared direction
        wins, then the larger table, then the first by name."""
        return self.memo(("owner_of", table), lambda: self._owner_of(table))

    def _owner_of(self, table: str) -> str | None:
        found = self.model.table(table)
        if found is None or len(found.primary_key) != 1:
            return None
        for edge in sorted(self.edges_from(table), key=lambda e: (e.kind != "declared",
                                                                  e.parent_table)):
            parent = self.model.table(edge.parent_table)
            if (parent is None or edge.child_columns != found.primary_key
                    or parent.primary_key != edge.parent_columns
                    or found.row_count > parent.row_count):
                continue
            back = next((e for e in self.edges_from(parent.name)
                         if e.parent_table == table and e.child_columns == parent.primary_key),
                        None)
            if back is not None and (
                    (back.kind == "declared", parent.row_count, table)
                    > (edge.kind == "declared", found.row_count, parent.name)):
                continue
            return parent.name
        return None

    def table_kind(self, table: str) -> str:
        """code, link, event or entity.

        code: a small table others key to, a list of codes (discovery's rule).
        link: keyed to two or more tables, with a key holding one of those
        keys and a date: an association over a span of time.
        event: rows that carry a date or an amount, and nothing but their own
        lines keys to them: things that happened, at the bottom of the graph.
        entity: anything else, a population other tables point at."""
        return self.memo(("table_kind", table), lambda: self._table_kind(table))

    def _table_kind(self, table: str) -> str:
        found = self.model.table(table)
        if found is None:
            return "entity"
        if self.in_degree(table) and 0 < found.row_count < CODE_TABLE_MAX_ROWS:
            return "code"
        parents = {e.parent_table for e in self.edges_from(table)}
        key_columns = {c for e in self.edges_from(table) for c in e.child_columns}
        key = [found.column(c) for c in found.primary_key]
        if (len(parents) >= 2 and len(key) >= 2
                and any(c is not None and c.name in key_columns for c in key)
                and any(c is not None and c.is_date for c in key)):
            return "link"
        lines = self.lines_of(table)
        children = {e.child_table for e in self.edges_to(table)} - {lines}
        dated = any(c.is_date for c in found.columns)
        if not children and (dated or self.amounts(table)):
            return "event"
        return "entity"

    def amounts(self, table: str) -> list[str]:
        """Columns holding amounts: mapped to the catalog's AMOUNT, or a
        decimal with places that is no key."""
        found = self.model.table(table)
        if found is None:
            return []
        keys = set(found.primary_key) | {c for e in self.edges_from(table) for c in e.child_columns}
        out = []
        for column in found.columns:
            if column.name in keys:
                continue
            if self.element_of(table, column.name) == "AMOUNT" or (
                    column.type_name in {"decimal", "numeric", "money", "smallmoney"}
                    and (column.scale or column.type_name.endswith("money"))):
                out.append(column.name)
        return out


def name_tokens(name: str) -> set[str]:
    return set(tokenize(name))


def composite_candidates(model: PhysicalModel, shortlist: Iterable[str],
                         known: Iterable[Edge] = ()) -> list[Edge]:
    """For each parent with a primary key of several columns, the shortlisted
    tables carrying columns of the same names and comparable types: each a
    candidate child, unchecked. A table with that very key is the same grain,
    not a child, and is left out, as is a pair the model already joins."""
    held = set(shortlist)
    joined = {(e.child_table, e.parent_table, tuple(e.child_columns)) for e in known}
    found: list[Edge] = []
    for parent in model.tables:
        key = list(parent.primary_key)
        if len(key) < 2 or parent.row_count < 2 or parent.name not in held:
            continue
        for child in model.tables:
            if child.name == parent.name or child.name not in held or child.row_count == 0:
                continue
            if set(child.primary_key) == set(key):
                continue
            pairs = [(child.column(c), parent.column(c)) for c in key]
            if any(a is None or b is None or not _comparable(a, b) for a, b in pairs):
                continue
            if (child.name, parent.name, tuple(key)) in joined:
                continue
            found.append(Edge(child.name, list(key), parent.name, list(key), "inferred"))
    return found


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def check_composite(checks: SourceChecks, model: PhysicalModel,
                    candidate: Edge) -> tuple[bool, Edge | None]:
    """One inclusion query through the run's checks: the share of the child's
    distinct key tuples found in the parent, kept at discovery's threshold.
    (checked, the edge or None); not checked when the cap was reached or the
    database refused, which the checks note with both tables named."""
    child = model.table(candidate.child_table)
    parent = model.table(candidate.parent_table)
    if child is None or parent is None:
        return True, None
    columns = candidate.child_columns
    names = [f"v{i}" for i in range(len(columns))]
    sql = COMPOSITE_INCLUSION_SQL.format(
        first=_quote(candidate.parent_columns[0]),
        checked=CHECKED_VALUES,
        child_columns=", ".join(f"{_quote(c)} AS {n}" for c, n in zip(columns, names)),
        child_source=source_read.sample_source(child),
        not_null=" AND ".join(f"{_quote(c)} IS NOT NULL" for c in columns),
        parent_table=sample.qualified(parent),
        on=" AND ".join(
            f"p.{_quote(p)} = c.{n}{sample.collate_as(parent.column(p), child.column(c))}"
            for p, c, n in zip(candidate.parent_columns, columns, names)),
        hint=" OPTION (LOOP JOIN)" if parent.leads_index(candidate.parent_columns[0]) else "",
    )
    checked, rows = checks.run(
        f"composite key {child.name} ({', '.join(columns)}) against {parent.name}",
        lambda: source_read.query(checks.registry, sql, timeout=source_read.CHECK_TIMEOUT,
                                  lock_timeout_ms=source_read.CHECK_LOCK_TIMEOUT_MS))
    if not checked:
        return False, None
    if not rows:
        return True, None
    distinct = int(rows[0].get("distinct_values") or 0)
    missing = int(rows[0].get("missing") or 0)
    if distinct < MIN_DISTINCT_VALUES:
        return True, None
    coverage = (distinct - missing) / distinct
    if coverage < INCLUSION_THRESHOLD:
        return True, None
    return True, Edge(candidate.child_table, list(columns), candidate.parent_table,
                      list(candidate.parent_columns), "inferred", coverage)

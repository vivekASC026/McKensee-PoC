"""Relationships between columns.

Two analyses, both bounded so a wide table cannot blow the time budget.

A functional dependency says one column decides another: if the number of
distinct A values equals the number of distinct (A, B) pairs, then A determines
B. ZIP determining city and state is the case this exists for, and the effect
on generation is that the pair must be drawn from one record rather than
independently.

A date ordering says one date reliably precedes another, inside a row or across
a join. That is what turns a received date into "service date plus nought to
thirty days" instead of a free date in a range.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.profiling import sample
from sdg.profiling.model import ColumnProfile, DateOrdering, FunctionalDependency
from sdg.profiling.sample import SAMPLE_ROWS

# The build brief: only test pairs where both columns have fewer distinct
# values than this.
PAIRWISE_DISTINCT_LIMIT = 5_000
MAX_PAIRS_PER_TABLE = 80
MAX_DATE_PAIRS = 40
# Expressions per query. Kept modest so one statement stays readable to the
# optimizer and to anyone reading a trace.
BATCH_SIZE = 24

ORDERING_THRESHOLD = 0.98

# Buckets excluded from pairwise analysis. A system column is filled by the
# database and a reference column takes its value from whichever parent the row
# points at, so neither can be the source or the target of a generation rule.
# A dependency involving one is real but unusable, and reporting it buries the
# findings that matter.
UNGENERATED_BUCKETS = {"system", "reference"}


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _qualified(table: Table) -> str:
    return f"{_quote(table.schema_name)}.{_quote(table.table_name)}"


@dataclass
class Budget:
    """A wall-clock allowance for one table's analyses."""

    seconds: float
    started: float

    @classmethod
    def start(cls, seconds: float) -> "Budget":
        return cls(seconds=seconds, started=time.perf_counter())

    def spent(self) -> float:
        return time.perf_counter() - self.started

    def exhausted(self) -> bool:
        return self.spent() >= self.seconds

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.spent())


# ---------------------------------------------------------------------------
# Functional dependencies
# ---------------------------------------------------------------------------


def dependency_candidates(
    table: Table, profiles: dict[str, ColumnProfile]
) -> list[tuple[str, str]]:
    """Ordered pairs worth testing.

    A determines B is only possible when A has at least as many distinct values
    as B, and a key determines everything by definition, so keys are skipped:
    reporting them would bury the one real finding under a dozen trivial ones.
    """
    eligible = []
    for column in table.columns:
        profile = profiles.get(column.name)
        if profile is None or column.is_computed:
            continue
        if column.bucket in UNGENERATED_BUCKETS:
            continue
        if profile.distinct_count > PAIRWISE_DISTINCT_LIMIT or profile.distinct_count < 2:
            continue
        eligible.append((column.name, profile))

    pairs: list[tuple[str, str]] = []
    for determinant, determinant_profile in eligible:
        if determinant_profile.cardinality == "unique":
            continue
        for dependent, dependent_profile in eligible:
            if dependent == determinant:
                continue
            if dependent_profile.distinct_count > determinant_profile.distinct_count:
                continue
            pairs.append((determinant, dependent))
    # Fewest distinct values first: the cheapest and most likely findings.
    pairs.sort(key=lambda p: profiles[p[0]].distinct_count)
    return pairs


def _dependency_sql(table: Table, pairs: list[tuple[str, str]],
                    sample_rows: int = SAMPLE_ROWS) -> str:
    parts = []
    for index, (determinant, dependent) in enumerate(pairs):
        a, b = _quote(determinant), _quote(dependent)
        both = f"{a} IS NOT NULL AND {b} IS NOT NULL"
        parts.append(
            f"COUNT(DISTINCT CASE WHEN {both} THEN {a} END) AS det_{index}"
        )
        parts.append(
            f"COUNT(DISTINCT CASE WHEN {both} THEN CONCAT({a}, CHAR(1), {b}) END) "
            f"AS pair_{index}"
        )
        parts.append(f"SUM(CASE WHEN {both} THEN 1 ELSE 0 END) AS rows_{index}")
    return (f"SELECT {', '.join(parts)} "  # noqa: S608
            f"FROM {sample.source(table, rows=sample_rows)}")


def functional_dependencies(
    registry: ConnectionRegistry,
    table: Table,
    profiles: Iterable[ColumnProfile],
    connection: str = "source",
    *,
    budget: Budget | None = None,
    sample_rows: int = SAMPLE_ROWS,
) -> tuple[list[FunctionalDependency], int]:
    """Return the dependencies that hold, and how many pairs went untested.

    A large table is tested on a sample of its rows. There a determinant value
    seen only once decides its partner trivially, so a dependency is only
    trusted when its values were each seen twice on average.
    """
    sampled = sample.is_sampled(table, sample_rows)
    by_name = {p.column: p for p in profiles}
    pairs = dependency_candidates(table, by_name)
    skipped = max(0, len(pairs) - MAX_PAIRS_PER_TABLE)
    pairs = pairs[:MAX_PAIRS_PER_TABLE]

    found: list[FunctionalDependency] = []
    for start in range(0, len(pairs), BATCH_SIZE):
        if budget is not None and budget.exhausted():
            skipped += len(pairs) - start
            break
        batch = pairs[start : start + BATCH_SIZE]
        rows = registry.query(connection, _dependency_sql(table, batch, sample_rows))
        if not rows:
            continue
        row = rows[0]
        for index, (determinant, dependent) in enumerate(batch):
            determinant_count = int(row.get(f"det_{index}") or 0)
            pair_count = int(row.get(f"pair_{index}") or 0)
            if pair_count == 0:
                continue
            if sampled and int(row.get(f"rows_{index}") or 0) < 2 * determinant_count:
                continue
            found.append(
                FunctionalDependency(
                    table=table.name,
                    determinant=determinant,
                    dependent=dependent,
                    distinct_determinant=determinant_count,
                    distinct_pair=pair_count,
                    holds=determinant_count == pair_count,
                    coverage=round(determinant_count / pair_count, 6),
                )
            )
    return found, skipped


# ---------------------------------------------------------------------------
# Date orderings
# ---------------------------------------------------------------------------


def _date_columns(table: Table) -> list[Column]:
    # A time of day has no place in which date comes first, and SQL Server
    # will not compare a time with a date at all.
    return [
        c for c in table.columns
        if c.is_date and c.type_name != "time"
        and not c.is_computed and c.bucket not in UNGENERATED_BUCKETS
    ]


def _ordering_terms(prefix_a: str, a: str, prefix_b: str, b: str, index: int,
                    a_column: Column | None = None,
                    b_column: Column | None = None) -> list[str]:
    left = f"{prefix_a}{_quote(a)}"
    right = f"{prefix_b}{_quote(b)}"
    both = f"{left} IS NOT NULL AND {right} IS NOT NULL"
    # A term date of 9999-12-31 is "no end", not a gap of three million days.
    for expression, column in ((left, a_column), (right, b_column)):
        if column is not None and column.type_name != "time":
            both += f" AND {sample.real_date(expression, column)}"
    gap = f"DATEDIFF(day, {left}, {right})"
    return [
        f"SUM(CASE WHEN {both} THEN 1 ELSE 0 END) AS rows_{index}",
        f"SUM(CASE WHEN {both} AND {left} <= {right} THEN 1 ELSE 0 END) AS ordered_{index}",
        f"MIN(CASE WHEN {both} THEN {gap} END) AS gmin_{index}",
        f"MAX(CASE WHEN {both} THEN {gap} END) AS gmax_{index}",
        f"AVG(CASE WHEN {both} THEN CAST({gap} AS float) END) AS gmean_{index}",
        f"STDEV(CASE WHEN {both} THEN CAST({gap} AS float) END) AS gstd_{index}",
    ]


def _read_ordering(row: dict, index: int) -> tuple[int, int, dict] | None:
    rows_compared = int(row.get(f"rows_{index}") or 0)
    if not rows_compared:
        return None
    in_order = int(row.get(f"ordered_{index}") or 0)
    gaps = {
        "gap_min": _int_or_none(row.get(f"gmin_{index}")),
        "gap_max": _int_or_none(row.get(f"gmax_{index}")),
        "gap_mean": _float_or_none(row.get(f"gmean_{index}")),
        "gap_stddev": _float_or_none(row.get(f"gstd_{index}")),
    }
    return rows_compared, in_order, gaps


def _int_or_none(value):
    return None if value is None else int(value)


def _float_or_none(value):
    return None if value is None else round(float(value), 4)


def row_date_orderings(
    registry: ConnectionRegistry,
    table: Table,
    connection: str = "source",
    *,
    threshold: float = ORDERING_THRESHOLD,
    sample_rows: int = SAMPLE_ROWS,
) -> list[DateOrdering]:
    """Orderings between two date columns of the same row, on a sample of a
    large table's rows."""
    dates = _date_columns(table)
    pairs = [
        (a.name, b.name)
        for i, a in enumerate(dates)
        for b in dates[i + 1 :]
    ][:MAX_DATE_PAIRS]
    if not pairs:
        return []

    found: list[DateOrdering] = []
    for start in range(0, len(pairs), BATCH_SIZE // 6 or 1):
        batch = pairs[start : start + (BATCH_SIZE // 6 or 1)]
        terms: list[str] = []
        for index, (a, b) in enumerate(batch):
            terms.extend(_ordering_terms("", a, "", b, index, table.column(a), table.column(b)))
        rows = registry.query(
            connection,
            f"SELECT {', '.join(terms)} FROM {sample.source(table, rows=sample_rows)}",  # noqa: S608
        )
        if not rows:
            continue
        for index, (a, b) in enumerate(batch):
            read = _read_ordering(rows[0], index)
            if read is None:
                continue
            rows_compared, in_order, gaps = read
            share = in_order / rows_compared
            # Whichever direction holds is the one worth reporting.
            earlier, later, share = (
                (a, b, share) if share >= 0.5 else (b, a, 1 - share)
            )
            if share < threshold:
                continue
            if earlier != a:
                gaps = {
                    "gap_min": None if gaps["gap_max"] is None else -gaps["gap_max"],
                    "gap_max": None if gaps["gap_min"] is None else -gaps["gap_min"],
                    "gap_mean": None if gaps["gap_mean"] is None else -gaps["gap_mean"],
                    "gap_stddev": gaps["gap_stddev"],
                }
            found.append(
                DateOrdering(
                    kind="row",
                    table=table.name,
                    earlier=earlier,
                    later=later,
                    rows_compared=rows_compared,
                    share_in_order=round(share, 6),
                    **gaps,
                )
            )
    return found


def join_date_orderings(
    registry: ConnectionRegistry,
    model: PhysicalModel,
    child: Table,
    connection: str = "source",
    *,
    threshold: float = ORDERING_THRESHOLD,
    sample_rows: int = SAMPLE_ROWS,
) -> list[DateOrdering]:
    """Orderings between a parent's dates and a child's, across a foreign key,
    on a sample of a large child table's rows.

    This is what lets a claim's service date be anchored on its member's
    effective date rather than picked out of thin air.
    """
    found: list[DateOrdering] = []
    child_dates = _date_columns(child)
    if not child_dates:
        return found

    for foreign_key in child.foreign_keys:
        parent = model.table(foreign_key.parent_table)
        if parent is None or len(foreign_key.columns) != 1:
            continue
        parent_dates = _date_columns(parent)
        if not parent_dates:
            continue

        pairs = [(p.name, c.name) for p in parent_dates for c in child_dates][:MAX_DATE_PAIRS]
        join = (
            f"FROM {sample.source(child, 'c', rows=sample_rows)} "
            f"JOIN {_qualified(parent)} AS p "
            f"ON c.{_quote(foreign_key.columns[0])}"
            f"{sample.collate_as(parent.column(foreign_key.parent_columns[0]), child.column(foreign_key.columns[0]))}"
            f" = p.{_quote(foreign_key.parent_columns[0])}"
        )
        chunk = BATCH_SIZE // 6 or 1
        for start in range(0, len(pairs), chunk):
            batch = pairs[start : start + chunk]
            terms: list[str] = []
            for index, (parent_column, child_column) in enumerate(batch):
                terms.extend(_ordering_terms("p.", parent_column, "c.", child_column, index,
                                             parent.column(parent_column),
                                             child.column(child_column)))
            hint = sample.seek_hint(parent, foreign_key.parent_columns[0])
            rows = registry.query(connection, f"SELECT {', '.join(terms)} {join}{hint}")  # noqa: S608
            if not rows:
                continue
            for index, (parent_column, child_column) in enumerate(batch):
                read = _read_ordering(rows[0], index)
                if read is None:
                    continue
                rows_compared, in_order, gaps = read
                share = in_order / rows_compared
                if share >= threshold:
                    found.append(
                        DateOrdering(
                            kind="join",
                            table=child.name,
                            parent_table=parent.name,
                            earlier=f"{parent.name}.{parent_column}",
                            later=child_column,
                            rows_compared=rows_compared,
                            share_in_order=round(share, 6),
                            **gaps,
                        )
                    )
                elif 1 - share >= threshold:
                    # The child's date reliably precedes the parent's, which is
                    # how a coverage end date bounds a service date.
                    found.append(
                        DateOrdering(
                            kind="join",
                            table=child.name,
                            parent_table=parent.name,
                            earlier=child_column,
                            later=f"{parent.name}.{parent_column}",
                            rows_compared=rows_compared,
                            share_in_order=round(1 - share, 6),
                            gap_min=None if gaps["gap_max"] is None else -gaps["gap_max"],
                            gap_max=None if gaps["gap_min"] is None else -gaps["gap_min"],
                            gap_mean=None if gaps["gap_mean"] is None else -gaps["gap_mean"],
                            gap_stddev=gaps["gap_stddev"],
                        )
                    )
    return found

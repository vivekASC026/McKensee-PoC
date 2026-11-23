"""Detections that need to look at the data, not just at the profile.

Only one so far: whether a parent's amount column is the sum of one of its
children's. That cannot be seen in a column profile, because the evidence lives
in the join, and it is exactly the kind of rule that a person would otherwise
have to notice and write by hand.

Every parent amount is tested against every child amount across a key in one
query. A large parent is read through its fixed sample, and each sampled
parent's children are found by a seek on the child's key, so neither table is
read whole. A huge child with no index on its key is left untested: finding a
parent's children would mean reading all of them.

Read-only, like everything else in profiling.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pyodbc

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.profiling import sample

# Keys checked at once.
SUM_WORKERS = 8
# A database of thousands of tables has thousands of keys between tables with
# amounts, some of them between tables of millions of rows. Each key is read
# through a small fixed sample of parents, a check still running after a
# minute is given up, and no new check starts after the step's time is spent:
# a sum not found is a rule a person can still write, a read that never ends
# is not.
SUM_SAMPLE_ROWS = 2_000
SUM_QUERY_SECONDS = 60
SUM_MAX_MINUTES = 10

log = logging.getLogger(__name__)

# The build brief: propose the sum only when it holds for essentially every row.
EXPRESSION_MATCH_THRESHOLD = 0.999
MIN_ROWS = 20


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _qualified(table: Table) -> str:
    return f"{_quote(table.schema_name)}.{_quote(table.table_name)}"


def sum_sql(parent: Table, parent_key: str, parent_columns: list[str],
            child: Table, child_key: str, child_columns: list[str],
            *, sample_rows: int = sample.SAMPLE_ROWS) -> str:
    """Each parent row that has children, its amounts beside its children's
    sums: how many parents there were, and how many matched for each pair."""
    sums = ", ".join(
        f"SUM(c.{_quote(column)}) AS s_{j}" for j, column in enumerate(child_columns))
    matched = ", ".join(
        f"SUM(CASE WHEN p.{_quote(pc)} = t.s_{j} THEN 1 ELSE 0 END) AS m_{i}_{j}"
        for i, pc in enumerate(parent_columns) for j in range(len(child_columns)))
    return (
        f"SELECT COUNT(*) AS rows_compared, {matched} "  # noqa: S608 - names come from the catalog
        f"FROM {sample.source(parent, 'p', rows=sample_rows)} "
        f"CROSS APPLY (SELECT COUNT(*) AS n, {sums} FROM {_qualified(child)} AS c "
        f"WHERE c.{_quote(child_key)} = p.{_quote(parent_key)}"
        f"{sample.collate_as(child.column(child_key), parent.column(parent_key))}) AS t "
        f"WHERE t.n > 0{sample.seek_hint(child, child_key)}"
    )


@dataclass
class SumExpression:
    parent_table: str
    parent_column: str
    child_table: str
    child_column: str
    rows_compared: int
    matched: int

    @property
    def match_rate(self) -> float:
        return self.matched / self.rows_compared if self.rows_compared else 0.0

    def expression(self) -> str:
        return f"sum({self.child_table}.{self.child_column})"

    def detail(self) -> str:
        return (
            f"equals {self.expression()} in {self.match_rate:.1%} of "
            f"{self.rows_compared:,} parent rows that have children"
        )


def _amount_columns(table: Table) -> list[Column]:
    """Decimal columns that could hold a total. Integers and keys are skipped."""
    return [
        c for c in table.columns
        if c.type_name in {"decimal", "numeric", "money", "smallmoney"}
        and not c.is_computed
        and not c.is_primary_key
        and c.bucket not in {"system", "reference"}
    ]


def detect_sum_expressions(
    registry: ConnectionRegistry,
    model: PhysicalModel,
    connection: str = "source",
    *,
    threshold: float = EXPRESSION_MATCH_THRESHOLD,
    sample_rows: int = SUM_SAMPLE_ROWS,
    query_seconds: int = SUM_QUERY_SECONDS,
    max_minutes: float = SUM_MAX_MINUTES,
) -> list[SumExpression]:
    """Test every parent amount against every child amount across each key,
    several keys at a time: a database of thousands of tables has thousands."""
    jobs = []
    for child in model.tables:
        child_amounts = _amount_columns(child)
        if not child_amounts:
            continue
        for foreign_key in child.foreign_keys:
            if len(foreign_key.columns) != 1:
                continue
            parent = model.table(foreign_key.parent_table)
            if parent is None or parent.name == child.name:
                continue
            parent_amounts = _amount_columns(parent)
            if not parent_amounts:
                continue
            child_key = foreign_key.columns[0]
            if sample.is_huge(child) and not child.leads_index(child_key):
                continue
            jobs.append((parent, foreign_key.parent_columns[0], [c.name for c in parent_amounts],
                         child, child_key, [c.name for c in child_amounts]))

    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None

    def measure(job) -> list[SumExpression]:
        parent, parent_key, parent_names, child, child_key, child_names = job
        if deadline is not None and time.monotonic() > deadline:
            return []
        try:
            rows = registry.query(connection, sum_sql(
                parent, parent_key, parent_names, child, child_key, child_names,
                sample_rows=sample_rows,
            ), timeout=query_seconds)
        except pyodbc.Error as exc:
            log.warning("sums of %s.%s not checked against %s: %s",
                        child.name, child_key, parent.name, exc)
            return []
        if not rows:
            return []
        rows_compared = int(rows[0]["rows_compared"] or 0)
        if rows_compared < MIN_ROWS:
            return []
        out: list[SumExpression] = []
        for i, parent_column in enumerate(parent_names):
            for j, child_column in enumerate(child_names):
                candidate = SumExpression(
                    parent_table=parent.name,
                    parent_column=parent_column,
                    child_table=child.name,
                    child_column=child_column,
                    rows_compared=rows_compared,
                    matched=int(rows[0][f"m_{i}_{j}"] or 0),
                )
                if candidate.match_rate >= threshold:
                    out.append(candidate)
        return out

    if len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=SUM_WORKERS) as pool:
            measured = list(pool.map(measure, jobs))
    else:
        measured = [measure(job) for job in jobs]
    return [expression for found in measured for expression in found]

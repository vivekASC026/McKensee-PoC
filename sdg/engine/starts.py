"""Where a generated key starts, read from the target rather than remembered.

A sequence proposal records the highest value a column held when it was
profiled, and starts after it. That is right once. It is wrong every time
after: the rows the first run wrote are now in the table, the profile does not
know, and the second run begins again at the same number and collides on the
primary key.

The gap is not a day's drift, it is the run that just finished. Anything else
writing to the target between runs has the same effect, and so does generating
into a database somebody else is also loading.

So the start is resolved against the target immediately before writing. The
plan keeps what profiling found -- it is evidence about the source and still
worth reading -- and the run uses whatever the table actually holds now.

Only `sequence` is resolved. A pattern marked unique keeps its uniqueness
within the run but has no equivalent reading: there is no "next" for
`[A-Z]{3}\\d{5}`, and pretending otherwise would be worse than the honest note
in the report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pyodbc

from sdg.plan.model import Plan, PlanTable

logger = logging.getLogger(__name__)

# SQLSTATEs that mean "this column has no readable integer maximum": no such
# table (42S02) or column (42S22). TRY_CAST never raises a conversion error.
# Anything else -- permission denied (42000), a dropped connection, a lock or
# query timeout -- is not an answer, and treating it as one lets the run
# collide on the key.
_NOT_READABLE = {"42S02", "42S22"}


@dataclass
class Advance:
    """One key the target had moved past since the plan was built."""

    table: str
    column: str
    planned: int
    actual: int

    def describe(self) -> str:
        return (
            f"{self.table}.{self.column}: the plan starts at {self.planned:,} "
            f"and the table already holds {self.actual - 1:,}"
        )


def resolve(
    registry, plan: Plan, *, connection: str = "target"
) -> tuple[Plan, list[Advance]]:
    """Return the plan with every sequence start moved past what is there now.

    The plan is copied rather than edited. The one on disk is a record of what
    was decided, and a run quietly rewriting it would make the next person
    wonder where the numbers came from.
    """
    sequences = [
        (table.name, _qualified(table), field.name)
        for table in plan.tables
        if table.mode == "generate"
        for field in table.fields
        if field.generator == "sequence"
    ]
    if not sequences:
        return plan, []

    highest = _highest(registry, connection, sequences)

    moved: list[Advance] = []
    adjusted = plan.model_copy(deep=True)
    for table in adjusted.tables:
        if table.mode != "generate":
            continue
        for field in table.fields:
            if field.generator != "sequence":
                continue
            planned = int(field.params.get("start") or 1)
            found = highest.get((table.name, field.name))
            if found is None:
                continue
            start = found + int(field.params.get("step") or 1)
            if start > planned:
                field.params["start"] = start
                moved.append(Advance(table.name, field.name, planned, start))
    return adjusted, moved


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _qualified(table: PlanTable) -> str:
    if table.schema_name:
        return f"{_quote(table.schema_name)}.{_quote(table.table_name or table.name)}"
    # A plan table's name is schema.table when the schema is not held apart.
    schema, dot, name = table.name.partition(".")
    return f"{_quote(schema)}.{_quote(name)}" if dot else _quote(table.name)


def _highest(
    registry: Any, connection: str, sequences: list[tuple[str, str, str]]
) -> dict[tuple[str, str], int]:
    """The largest value each key column currently holds in the target.

    A table that does not exist yet, or a column that holds nothing numeric,
    contributes nothing: the planned start then stands, which is the right
    answer for a table nobody has written to.
    """
    found: dict[tuple[str, str], int] = {}
    with registry.connect(connection) as conn:
        cursor = conn.cursor()
        for table, source, column in sequences:
            try:
                row = cursor.execute(
                    f"SELECT MAX(TRY_CAST({_quote(column)} AS BIGINT)) FROM {source}"  # noqa: S608
                ).fetchone()
            except pyodbc.Error as exc:
                state = exc.args[0] if exc.args else ""
                if state not in _NOT_READABLE:
                    raise RuntimeError(
                        f"Could not read the highest {column} in {table} on the "
                        f"{connection} database, so the run stopped before writing "
                        f"rather than risk reusing a key. Check the connection and "
                        f"run again. ({exc})"
                    ) from exc
                # Not a readable integer key. The plan's own start stands,
                # and the write will say so if it collides.
                logger.warning(
                    "Key start for %s.%s not read from the target (SQLSTATE %s); "
                    "the planned start stands.", table, column, state,
                )
                continue
            if row and row[0] is not None:
                found[(table, column)] = int(row[0])
    return found


def summarize(moved: list[Advance]) -> list[dict[str, Any]]:
    return [
        {
            "table": advance.table,
            "column": advance.column,
            "planned": advance.planned,
            "started": advance.actual,
        }
        for advance in moved
    ]

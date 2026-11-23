"""Profile a whole database, inside a time budget.

Each table gets an allowance. Measures that every later step depends on run
first; the pairwise analyses run with whatever time is left and report what they
had to leave out. A profile that silently skipped half the schema would be worse
than one that says so.

Tables are profiled several at a time, and the whole profile has a time limit
(SDG_PROFILE_MAX_MINUTES): past it, the remaining tables still get their column
measures, and their pairwise analyses are listed as left out. A database of
thousands of tables is read in under an hour rather than overnight.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

from sdg.catalog.model import Catalog
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.errors import is_database_error
from sdg.discovery.model import PhysicalModel, Table
from sdg.profiling.columns import SAMPLE_CAP, profile_table
from sdg.profiling.dependencies import (
    Budget,
    functional_dependencies,
    join_date_orderings,
    row_date_orderings,
)
from sdg.profiling.model import (
    ColumnProfile,
    DateOrdering,
    FunctionalDependency,
    ProfileReport,
    SkippedAnalysis,
)
from sdg.profiling.personal import key_columns, withhold_personal_values
from sdg.profiling.ratio import profile_ratios
from sdg.discovery.model import now_stamp

# The build brief: 60 seconds per table against the small fixture, ten minutes
# at full volume.
SMALL_TABLE_BUDGET_SECONDS = 60.0
FULL_TABLE_BUDGET_SECONDS = 600.0
LARGE_TABLE_ROWS = 500_000
# Tables profiled at once. Each holds one connection while it reads.
PROFILE_WORKERS = 8


logger = logging.getLogger(__name__)


def _sqlstate(exc: BaseException) -> str:
    state = exc.args[0] if exc.args else ""
    return state if isinstance(state, str) and len(state) == 5 and state.isalnum() else ""


def table_budget(row_count: int) -> float:
    return FULL_TABLE_BUDGET_SECONDS if row_count >= LARGE_TABLE_ROWS else SMALL_TABLE_BUDGET_SECONDS


@dataclass
class _TableResult:
    columns: list[ColumnProfile] = field(default_factory=list)
    dependencies: list[FunctionalDependency] = field(default_factory=list)
    orderings: list[DateOrdering] = field(default_factory=list)
    skipped: list[SkippedAnalysis] = field(default_factory=list)
    seconds: float = 0.0


def _profile_one(
    registry: ConnectionRegistry,
    model: PhysicalModel,
    table: Table,
    connection: str,
    sample_cap: int,
    skip_pairwise: bool,
    deadline: float | None,
    limit_minutes: float,
) -> _TableResult:
    table_started = time.perf_counter()
    budget = Budget.start(table_budget(table.row_count))
    out = _TableResult()

    profiles = profile_table(registry, table, connection, sample_cap=sample_cap)
    out.columns = profiles

    if not table.row_count:
        # Nothing in an empty table decides or orders anything.
        pass
    elif skip_pairwise:
        out.skipped.append(
            SkippedAnalysis(
                table=table.name,
                analysis="pairwise",
                reason="pairwise analysis was turned off for this run",
            )
        )
    elif deadline is not None and time.perf_counter() > deadline:
        out.skipped.append(
            SkippedAnalysis(
                table=table.name,
                analysis="pairwise",
                reason=(
                    f"the profile's overall limit of {limit_minutes:g} minutes was reached "
                    f"(SDG_PROFILE_MAX_MINUTES)"
                ),
            )
        )
    elif budget.exhausted():
        out.skipped.append(
            SkippedAnalysis(
                table=table.name,
                analysis="pairwise",
                reason=(
                    f"per-table budget of {budget.seconds:.0f}s was spent on column "
                    f"measures"
                ),
            )
        )
    else:
        dependencies, skipped_pairs = functional_dependencies(
            registry, table, profiles, connection, budget=budget, sample_rows=sample_cap,
        )
        out.dependencies.extend(d for d in dependencies if d.holds)
        if skipped_pairs:
            out.skipped.append(
                SkippedAnalysis(
                    table=table.name,
                    analysis="functional_dependency",
                    reason=f"{skipped_pairs} column pairs left untested within the budget",
                )
            )

        if budget.exhausted():
            out.skipped.append(
                SkippedAnalysis(
                    table=table.name,
                    analysis="date_ordering",
                    reason=f"per-table budget of {budget.seconds:.0f}s was spent",
                )
            )
        else:
            out.orderings.extend(row_date_orderings(
                registry, table, connection, sample_rows=sample_cap))
            out.orderings.extend(join_date_orderings(
                registry, model, table, connection, sample_rows=sample_cap))

    out.seconds = round(time.perf_counter() - table_started, 3)
    return out


def profile(
    registry: ConnectionRegistry,
    model: PhysicalModel,
    connection: str = "source",
    *,
    sample_cap: int = SAMPLE_CAP,
    tables: Iterable[str] | None = None,
    skip_pairwise: bool = False,
    workers: int = PROFILE_WORKERS,
    max_minutes: float | None = None,
    catalog: Catalog | None = None,
) -> ProfileReport:
    """Profile the chosen tables. Personal values are withheld (personal.py)
    against `catalog`, the business pack's own unless one is given."""
    from sdg.config import profile_max_minutes

    wanted = set(tables) if tables else None
    started = time.perf_counter()
    report = ProfileReport(
        connection=registry.ref(connection).as_dict(),
        profiled=now_stamp(),
        sample_cap=sample_cap,
    )
    limit_minutes = profile_max_minutes() if max_minutes is None else max_minutes
    deadline = started + limit_minutes * 60 if limit_minutes else None

    chosen = [t for t in model.tables if not wanted or t.name in wanted]

    def one(table: Table) -> _TableResult:
        try:
            return _profile_one(registry, model, table, connection, sample_cap,
                                skip_pairwise, deadline, limit_minutes)
        except Exception as exc:
            if not is_database_error(exc):
                raise
            state = _sqlstate(exc)
            if state.startswith("08") or state in {"HYT00", "HYT01"}:
                # The connection is gone or timed out: every table after this
                # would be "skipped" too, and the report would look complete.
                raise
            # One unreadable table is reported and the others kept, rather
            # than the whole profile thrown away with it.
            logger.warning("Profiling %s failed (%s); the table is left out.",
                           table.name, type(exc).__name__)
            return _TableResult(skipped=[SkippedAnalysis(
                table=table.name,
                analysis="profile",
                # The SQLSTATE only: a driver message can quote a source value.
                reason=(f"the table could not be read (SQL Server "
                        f"{_sqlstate(exc) or type(exc).__name__}); check the login's "
                        f"access to it and profile again"),
            )])

    if workers > 1 and len(chosen) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, chosen))
    else:
        results = [one(t) for t in chosen]

    # In the model's table order, however the work was shared out.
    for table, result in zip(chosen, results):
        report.columns.extend(result.columns)
        report.dependencies.extend(result.dependencies)
        report.orderings.extend(result.orderings)
        report.skipped.extend(result.skipped)
        report.table_seconds[table.name] = result.seconds

    report.ratios = profile_ratios(
        registry, model, connection, sample_rows=sample_cap, workers=workers)
    if catalog is None:
        from sdg.catalog.loader import load_catalog

        catalog = load_catalog()
    withhold_personal_values(report, catalog, key_columns(model))
    report.seconds = round(time.perf_counter() - started, 3)
    return report

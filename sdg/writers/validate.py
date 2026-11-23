"""Check generated data against every rule the plan claimed.

The plan is a set of promises. This reads the data back out of the target and
tests each one: keys resolve, unique columns are unique, code columns hold
allowed values, references land in the reference set, null rates are close to
what was asked for, and every expression still holds.

Checks run against the rows the run wrote and nothing else, so a pre-existing
row can neither rescue nor sink a run. The run's keys are staged into a
temporary table once and every check joins to it. The obvious alternative,
listing the keys inline, turns into a quarter of a million bind parameters
spread over hundreds of statements, which is slower than the generation it is
checking.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import pyodbc

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import PhysicalModel
from sdg.plan.model import Plan, PlanTable
from sdg.writers.sqlserver import manifest_key_columns, quote

log = logging.getLogger(__name__)

# The build brief: a generated null rate has to land within five points of the
# rate the plan asked for.
NULL_RATE_TOLERANCE = 0.05
# A few hundred rows drift further than that by chance alone: 116 members asked
# for 40% blank come out between 26% and 54%. Below a few thousand rows the
# allowance widens to three times the chance drift.
NULL_RATE_CHANCE_SPREAD = 3


def null_rate_tolerance(expected: float, written: int) -> float:
    """How far a written null rate may land from the plan's, on this many rows."""
    if written <= 0:
        return NULL_RATE_TOLERANCE
    chance = math.sqrt(expected * (1 - expected) / written)
    return max(NULL_RATE_TOLERANCE, NULL_RATE_CHANCE_SPREAD * chance)
STAGE_BATCH = 10_000
# A value list longer than this is not worth checking one membership at a time.
# SQL Server takes at most 2,100 parameters in one statement.
MAX_INLINE_VALUES = 2_000

KEYS_TABLE = "#sdg_run_keys"


@dataclass
class Check:
    name: str
    kind: str
    passed: bool
    detail: str
    table: str | None = None
    column: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "passed": self.passed,
            "detail": self.detail,
            "table": self.table,
            "column": self.column,
        }


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, dict[str, int]] = {}
        for check in self.checks:
            entry = by_kind.setdefault(check.kind, {"passed": 0, "failed": 0})
            entry["passed" if check.passed else "failed"] += 1
        return {
            "checks": len(self.checks),
            "failed": len(self.failures()),
            "passed": self.passed,
            "by_kind": dict(sorted(by_kind.items())),
        }


def _qualified(table: PlanTable) -> str:
    if table.schema_name:
        return f"{quote(table.schema_name)}.{quote(table.table_name or table.name)}"
    return quote(table.name)


def _fallback_name(name: str) -> str:
    if "." in name:
        schema, _, table = name.partition(".")
        return f"{quote(schema)}.{quote(table)}"
    return quote(name)


class Scope:
    """The rows one run wrote, staged so every check can join to them."""

    def __init__(self, conn: pyodbc.Connection) -> None:
        self.conn = conn
        self.target = ""
        self.key_columns: list[str] = []
        self.staged = 0

    # -- staging -------------------------------------------------------

    def stage(self, target: str, key_columns: list[str], keys: list[Any]) -> bool:
        """Put this run's keys into a temporary table. False if there are none."""
        self.drop()
        self.target = target
        self.key_columns = list(key_columns)
        self.staged = 0
        if not key_columns or not keys:
            return False

        columns = ", ".join(quote(c) for c in key_columns)
        cursor = self.conn.cursor()
        # SELECT INTO copies the exact column types, so a char key does not
        # quietly become something that compares differently. The UNION ALL is
        # what stops it copying the IDENTITY property too, which would leave a
        # staging table that refuses the very values it exists to hold.
        cursor.execute(
            f"SELECT {columns} INTO {KEYS_TABLE} FROM ("  # noqa: S608
            f"  SELECT {columns} FROM {target} WHERE 1 = 0"
            f"  UNION ALL"
            f"  SELECT {columns} FROM {target} WHERE 1 = 0"
            f") AS shape"
        )
        statement = (
            f"INSERT INTO {KEYS_TABLE} ({columns}) "  # noqa: S608
            f"VALUES ({', '.join('?' for _ in key_columns)})"
        )
        cursor.fast_executemany = True
        rows = [
            tuple(key) if isinstance(key, (list, tuple)) else (key,) for key in keys
        ]
        for start in range(0, len(rows), STAGE_BATCH):
            cursor.executemany(statement, rows[start : start + STAGE_BATCH])
        cursor.execute(f"CREATE CLUSTERED INDEX ix ON {KEYS_TABLE} ({columns})")
        self.conn.commit()
        self.staged = len(rows)
        return True

    def drop(self) -> None:
        self.conn.cursor().execute(f"DROP TABLE IF EXISTS {KEYS_TABLE}")
        self.conn.commit()

    # -- querying ------------------------------------------------------

    def join(self, alias: str = "c") -> str:
        condition = " AND ".join(
            f"{alias}.{quote(column)} = k.{quote(column)}" for column in self.key_columns
        )
        return f"JOIN {KEYS_TABLE} AS k ON {condition}"

    def source(self, alias: str = "c") -> str:
        """FROM clause covering only the rows this run wrote."""
        return f"FROM {self.target} AS {alias} {self.join(alias)}"

    def scalar(self, sql: str, params: list[Any] | None = None) -> int:
        cursor = self.conn.cursor()
        cursor.execute(sql, *params) if params else cursor.execute(sql)
        row = cursor.fetchone()
        return int(row[0] or 0) if row else 0


def validate_run(
    registry: ConnectionRegistry,
    plan: Plan,
    model: PhysicalModel,
    manifest: dict[str, Any],
    *,
    connection: str = "target",
    reference_sets: dict[str, Iterable[Any]] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    keys_by_table = manifest.get("keys", {})
    key_columns = {
        entry["table"]: manifest_key_columns(entry)
        for entry in manifest.get("tables", [])
    }
    written = {
        entry["table"]: entry["rows_written"] for entry in manifest.get("tables", [])
    }

    history = _StoreCombinations(registry)
    with registry.connect(connection) as conn:
        scope = Scope(conn)
        try:
            for plan_table in plan.tables_in_order():
                name = plan_table.name
                if name not in written:
                    continue
                target = _qualified(plan_table)
                columns = key_columns.get(name) or []
                keys = keys_by_table.get(name, [])

                if not scope.stage(target, columns, keys):
                    report.checks.append(
                        Check(
                            name=f"{name} can be checked",
                            kind="scope",
                            passed=False,
                            detail=(
                                "the manifest records no keys for this table, so the "
                                "rows this run wrote cannot be told apart from the "
                                "rows that were already there"
                            ),
                            table=name,
                        )
                    )
                    continue

                _check_row_count(report, scope, plan_table, written[name])
                _check_references(report, scope, plan, plan_table)
                _check_uniqueness(report, scope, plan_table, target, model)
                _check_domains(report, scope, plan_table)
                _check_null_rates(report, scope, plan_table, written[name])
                _check_reference_sets(report, scope, plan_table, reference_sets or {})
                _check_expressions(report, scope, plan, plan_table)
                _check_required_codes(report, scope, plan, plan_table, model)
                _check_combinations(report, scope, plan, plan_table, model, history)
        finally:
            scope.drop()

    return report


class _StoreCombinations:
    """The combinations the pattern store holds, read once per set of columns."""

    def __init__(self, registry: ConnectionRegistry) -> None:
        self.registry = registry
        self._held: dict[tuple, set[tuple[str, ...]] | None] = {}
        # Why the store could not be read for a set of elements, where it
        # exists but failed: the check says so instead of passing unseen.
        self._failed: dict[tuple, str] = {}

    def held(self, elements: list[str]) -> tuple[str, set[tuple[str, ...]]] | None:
        """The store table holding all these elements, and their combinations."""
        from sdg.plan.combinations import STORE_ELEMENTS, held_combinations

        for store, named in STORE_ELEMENTS.items():
            column_of = {e: c for c, e in named.items()}
            if all(e in column_of for e in elements):
                key = (store, tuple(elements))
                if key not in self._held:
                    try:
                        self._held[key] = held_combinations(
                            self.registry, store, [column_of[e] for e in elements])
                    except KeyError:
                        # No pattern store connection: nothing to compare with.
                        self._held[key] = None
                    except Exception as exc:  # noqa: BLE001 -- said, not swallowed
                        self._held[key] = None
                        # No such store table here is nothing to compare with;
                        # any other failure is said, in the log and the report.
                        if not _missing_table(exc):
                            log.warning("The pattern store %s could not be read to "
                                        "check combinations (%s).", store,
                                        type(exc).__name__, exc_info=True)
                            self._failed[key] = type(exc).__name__
                found = self._held[key]
                return (store, found) if found is not None else None
        return None

    def failure(self, elements: list[str]) -> tuple[str, str] | None:
        """The store, and the error, where reading it for these elements failed."""
        for (store, named), reason in self._failed.items():
            if list(named) == list(elements):
                return store, reason
        return None


def _missing_table(exc: Exception) -> bool:
    """Whether a driver error says the table does not exist (SQLSTATE 42S02)."""
    return (isinstance(exc, pyodbc.Error) and bool(exc.args)
            and str(exc.args[0]) == "42S02")


def _check_combinations(report, scope: Scope, plan, plan_table, model,
                        history: _StoreCombinations) -> None:
    """Every combination of dependent features the run wrote is one the store holds.

    A diagnosis with a procedure no claim carries, a plan with another
    product's code, a married child: valid values in a combination that does
    not occur. For each group the plan draws as combinations, the columns in
    this table and in the parent its rows belong to are read back together.
    """
    from sdg.plan.build import parent_links

    by_name = {t.name: t for t in plan.tables}
    keys = sorted({f.params.get("record_key") for f in plan_table.fields
                   if str(f.params.get("record_key") or "").startswith(
                       ("combo:", "pattern_combo:"))})
    for key in keys:
        own = [f for f in plan_table.fields if f.params.get("record_key") == key]
        columns = [(f.params["field"], f"c.{quote(f.name)}") for f in own]
        joins = []
        for number, (parent_name, child_cols, parent_cols) in enumerate(
                parent_links(plan_table, model)):
            parent = by_name.get(parent_name)
            theirs = [f for f in (parent.fields if parent else [])
                      if f.params.get("record_key") == key]
            if not theirs:
                continue
            alias = f"p{number}"
            on = " AND ".join(f"c.{quote(a)} = {alias}.{quote(b)}"
                              for a, b in zip(child_cols, parent_cols))
            joins.append(f"JOIN {_qualified(parent)} AS {alias} ON {on}")
            columns += [(f.params["field"], f"{alias}.{quote(f.name)}") for f in theirs]
        if len(columns) < 2:
            continue
        elements = [e for e, _ in columns]
        found = history.held(elements)
        if found is None:
            failed = history.failure(elements)
            if failed is not None:
                report.checks.append(
                    Check(
                        name=f"{plan_table.name} {' + '.join(elements)} combinations occur in the store",
                        kind="combination",
                        passed=False,
                        detail=(
                            f"not checked: the pattern store {failed[0]} could not be read "
                            f"({failed[1]}); see the server log, then validate again"
                        ),
                        table=plan_table.name,
                        column=", ".join(f.name for f in own),
                    )
                )
            continue
        _store, known = found
        select = ", ".join(f"LTRIM(RTRIM(CAST({c} AS NVARCHAR(100))))" for _, c in columns)
        cursor = scope.conn.cursor()
        cursor.execute(f"SELECT DISTINCT {select} {scope.source('c')} "  # noqa: S608
                       + " ".join(joins))
        written = [tuple(str(v) for v in row) for row in cursor.fetchall()
                   if all(v not in (None, "") for v in row)]
        unseen = [combo for combo in written if combo not in known]
        report.checks.append(
            Check(
                name=f"{plan_table.name} {' + '.join(elements)} combinations occur in the store",
                kind="combination",
                passed=not unseen,
                detail=(
                    f"all {len(written)} combinations of {', '.join(elements)} written "
                    f"are ones the store holds"
                    if not unseen else
                    f"{len(unseen)} of {len(written)} combinations of {', '.join(elements)} "
                    f"never occur in the store, e.g. "
                    + "; ".join(" / ".join(c) for c in unseen[:5])
                ),
                table=plan_table.name,
                column=", ".join(f.name for f in own),
            )
        )


def _check_row_count(report, scope: Scope, plan_table, expected) -> None:
    actual = scope.scalar(f"SELECT COUNT_BIG(*) {scope.source()}")  # noqa: S608
    report.checks.append(
        Check(
            name=f"{plan_table.name} row count",
            kind="row_count",
            passed=actual == expected,
            detail=f"{actual:,} rows found, {expected:,} written",
            table=plan_table.name,
        )
    )


def _check_references(report, scope: Scope, plan, plan_table) -> None:
    """Every reference must resolve, declared or inferred."""
    for plan_field in plan_table.fields:
        if plan_field.generator != "reference":
            continue
        parent_name = plan_field.params["parent_table"]
        parent_column = plan_field.params["parent_column"]
        parent = plan.table(parent_name)
        parent_target = _qualified(parent) if parent else _fallback_name(parent_name)

        orphans = scope.scalar(
            # An empty link points nowhere, which a nullable key allows.
            f"SELECT COUNT_BIG(*) {scope.source()} "  # noqa: S608
            f"WHERE c.{quote(plan_field.name)} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {parent_target} AS p "
            f"WHERE p.{quote(parent_column)} = c.{quote(plan_field.name)})"
        )
        report.checks.append(
            Check(
                name=f"{plan_table.name}.{plan_field.name} resolves to {parent_name}",
                kind="referential_integrity",
                passed=orphans == 0,
                detail=f"{orphans:,} rows point at a key that does not exist",
                table=plan_table.name,
                column=plan_field.name,
            )
        )


def _check_uniqueness(report, scope: Scope, plan_table, target, model) -> None:
    """Uniqueness covers the whole table, not just the run.

    A generated key that collides with a row that was already there is exactly
    the failure worth catching, and scoping the check to the run would hide it.
    """
    physical = model.table(plan_table.name)
    if physical is None:
        return
    for constraint in physical.unique_constraints:
        columns = ", ".join(quote(c) for c in constraint.columns)
        duplicates = scope.scalar(
            f"SELECT COUNT_BIG(*) FROM (SELECT {columns} FROM {target} "  # noqa: S608
            f"GROUP BY {columns} HAVING COUNT_BIG(*) > 1) AS d"
        )
        report.checks.append(
            Check(
                name=f"{plan_table.name} ({', '.join(constraint.columns)}) is unique",
                kind="uniqueness",
                passed=duplicates == 0,
                detail=f"{duplicates:,} duplicated key{'s' if duplicates != 1 else ''}",
                table=plan_table.name,
                column=", ".join(constraint.columns),
            )
        )


def _values_of(plan_field) -> list[str]:
    values = plan_field.params.get("values") or []
    record_field = plan_field.params.get("field")
    if record_field:
        values = [v.get(record_field) for v in values if isinstance(v, dict)]
    # Records of a combination repeat a value many times; each is asked once.
    return sorted({str(v) for v in values if v is not None})


def _count_outside(scope: Scope, column: str, values: list[str]) -> int:
    placeholders = ", ".join("?" for _ in values)
    return scope.scalar(
        f"SELECT COUNT_BIG(*) {scope.source()} "  # noqa: S608
        f"WHERE c.{quote(column)} IS NOT NULL "
        f"AND CAST(c.{quote(column)} AS nvarchar(400)) NOT IN ({placeholders})",
        values,
    )


def _check_domains(report, scope: Scope, plan_table) -> None:
    """A lookup column must hold only values the plan listed."""
    for plan_field in plan_table.fields:
        if plan_field.generator != "lookup":
            continue
        values = _values_of(plan_field)
        if not values or len(values) > MAX_INLINE_VALUES:
            continue
        outside = _count_outside(scope, plan_field.name, values)
        report.checks.append(
            Check(
                name=f"{plan_table.name}.{plan_field.name} stays inside its value list",
                kind="domain",
                passed=outside == 0,
                detail=f"{outside:,} values outside the {len(values):,} allowed",
                table=plan_table.name,
                column=plan_field.name,
            )
        )


def _check_null_rates(report, scope: Scope, plan_table, written) -> None:
    for plan_field in plan_table.fields:
        if plan_field.generator == "skip":
            continue
        column = quote(plan_field.name)
        nulls = scope.scalar(
            f"SELECT SUM(CASE WHEN c.{column} IS NULL THEN 1 ELSE 0 END) "  # noqa: S608
            f"{scope.source()}"
        )
        actual = nulls / written if written else 0.0
        expected = plan_field.null_rate

        if expected is None:
            if not plan_field.nullable:
                report.checks.append(
                    Check(
                        name=f"{plan_table.name}.{plan_field.name} has no nulls",
                        kind="nullability",
                        passed=nulls == 0,
                        detail=f"{nulls:,} nulls in a column declared not null",
                        table=plan_table.name,
                        column=plan_field.name,
                    )
                )
            continue

        tolerance = null_rate_tolerance(expected, written)
        report.checks.append(
            Check(
                name=f"{plan_table.name}.{plan_field.name} null rate",
                kind="null_rate",
                passed=abs(actual - expected) <= tolerance,
                detail=(
                    f"{actual:.1%} null against the {expected:.1%} the plan asked for "
                    f"(tolerance {tolerance:.0%})"
                ),
                table=plan_table.name,
                column=plan_field.name,
            )
        )


def _check_reference_sets(report, scope: Scope, plan_table, reference_sets) -> None:
    """A reference into a reuse table must land inside the set that was loaded."""
    for plan_field in plan_table.fields:
        if plan_field.generator != "reference":
            continue
        loaded = reference_sets.get(plan_field.params["parent_table"])
        if not loaded:
            continue
        allowed = [str(v) for v in loaded]
        if len(allowed) > MAX_INLINE_VALUES:
            continue
        outside = _count_outside(scope, plan_field.name, allowed)
        report.checks.append(
            Check(
                name=f"{plan_table.name}.{plan_field.name} stays inside the reference set",
                kind="reference_set",
                passed=outside == 0,
                detail=f"{outside:,} values outside the {len(allowed):,} keys loaded",
                table=plan_table.name,
                column=plan_field.name,
            )
        )


# The codes a claim of each 837 type must carry. A dental claim need not name a
# diagnosis, and an institutional line is billed by revenue code, so its
# procedure may be blank; the others may not.
REQUIRED_CODES = {"DIAGNOSIS_CODE": ("P", "I"), "PROCEDURE_CODE": ("P", "D")}
CLAIM_TYPE_NAMES = {"P": "professional", "I": "institutional", "D": "dental"}


def _check_required_codes(report, scope: Scope, plan, plan_table, model) -> None:
    """A code a claim's type requires is never left blank."""
    from sdg.plan.build import parent_links

    by_name = {t.name: t for t in plan.tables}
    claim_type, join = None, ""
    own = next((f for f in plan_table.fields if f.element == "CLAIM_TYPE"), None)
    if own is not None:
        claim_type = f"c.{quote(own.name)}"
    else:
        for parent_name, child_cols, parent_cols in parent_links(plan_table, model):
            parent = by_name.get(parent_name)
            theirs = next((f for f in (parent.fields if parent else [])
                           if f.element == "CLAIM_TYPE"), None)
            if theirs is None:
                continue
            on = " AND ".join(f"c.{quote(a)} = p.{quote(b)}"
                              for a, b in zip(child_cols, parent_cols))
            claim_type, join = f"p.{quote(theirs.name)}", f"JOIN {_qualified(parent)} AS p ON {on}"
            break
    if claim_type is None:
        return
    for plan_field in plan_table.fields:
        types = REQUIRED_CODES.get(plan_field.element or "")
        if not types or plan_field.generator == "skip":
            continue
        column = f"c.{quote(plan_field.name)}"
        blank = scope.scalar(
            f"SELECT COUNT_BIG(*) {scope.source('c')} {join} "  # noqa: S608
            f"WHERE LTRIM(RTRIM(CAST({claim_type} AS nvarchar(20)))) IN "
            f"({', '.join('?' for _ in types)}) "
            f"AND NULLIF(LTRIM(RTRIM(CAST({column} AS nvarchar(100)))), '') IS NULL",
            list(types),
        )
        named = " and ".join(CLAIM_TYPE_NAMES[t] for t in types)
        report.checks.append(
            Check(
                name=f"{plan_table.name}.{plan_field.name} is filled on {named} claims",
                kind="required_code",
                passed=blank == 0,
                detail=(f"{blank:,} {named} rows with no "
                        f"{plan_field.element.replace('_', ' ').lower()}"),
                table=plan_table.name,
                column=plan_field.name,
            )
        )


def _check_expressions(report, scope: Scope, plan, plan_table) -> None:
    """An expression the plan promised has to hold in the data that was written."""
    for plan_field in plan_table.fields:
        if plan_field.generator != "expression":
            continue
        expression = plan_field.params.get("expression", "")
        if not expression.startswith("sum(") or not expression.endswith(")"):
            continue
        child_name, _, child_column = expression[4:-1].rpartition(".")
        child = plan.table(child_name)
        if child is None:
            continue

        link = next(
            (
                f for f in child.fields
                if f.generator == "reference"
                and f.params.get("parent_table") == plan_table.name
            ),
            None,
        )
        if link is None:
            continue
        child_target = _qualified(child)
        parent_key = link.params["parent_column"]

        mismatched = scope.scalar(
            f"SELECT COUNT_BIG(*) {scope.source()} "  # noqa: S608
            f"WHERE c.{quote(plan_field.name)} <> ("
            f"SELECT SUM(ch.{quote(child_column)}) FROM {child_target} AS ch "
            f"WHERE ch.{quote(link.name)} = c.{quote(parent_key)})"
        )
        report.checks.append(
            Check(
                name=f"{plan_table.name}.{plan_field.name} equals {expression}",
                kind="expression",
                passed=mismatched == 0,
                detail=f"{mismatched:,} rows where the expression does not hold",
                table=plan_table.name,
                column=plan_field.name,
            )
        )

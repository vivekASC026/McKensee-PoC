"""Write generated rows into SQL Server.

Rows go in in generation order, batched, through parameterized inserts with
fast_executemany. Three things need care.

Identity columns. When the plan generates a key because a child in the same run
references it, the load turns on IDENTITY_INSERT for that table. When it does
not, the column is left out of the INSERT and the database assigns it.

Triggers. They stay enabled by default, because a trigger is part of what the
target does with a row and turning it off silently would change the result.
There is an option to disable and re-enable them, and whichever was done is
recorded in the manifest.

Cleanup. Every run records the keys it wrote, so the rows it added can be
removed later without touching anything that was already there.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pyodbc

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import PhysicalModel
from sdg.engine.execute import RunResult
from sdg.plan.model import Plan, PlanTable

log = logging.getLogger(__name__)

BATCH_SIZE = 5_000


def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def qualified(table: PlanTable) -> str:
    if table.schema_name:
        return f"{quote(table.schema_name)}.{quote(table.table_name or table.name)}"
    return quote(table.name)


@dataclass
class TableWrite:
    table: str
    rows_written: int = 0
    seconds: float = 0.0
    identity_insert: bool = False
    triggers_disabled: bool = False
    # The whole primary key, so a table keyed on (claim, line number) can be
    # scoped and cleaned up as exactly as one keyed on a single column.
    key_columns: list[str] = field(default_factory=list)
    key_values: list[tuple] = field(default_factory=list)
    # Triggers that were live during the load. What they wrote elsewhere is not
    # this run's to undo, but it is this run's to report.
    triggers_fired: list[str] = field(default_factory=list)

    @property
    def key_column(self) -> str | None:
        return self.key_columns[0] if len(self.key_columns) == 1 else None

    def as_record(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rows_written": self.rows_written,
            "seconds": round(self.seconds, 3),
            "identity_insert": self.identity_insert,
            "triggers_disabled": self.triggers_disabled,
            "key_columns": list(self.key_columns),
            "key_count": len(self.key_values),
            "triggers_fired": list(self.triggers_fired),
        }


@dataclass
class WriteReport:
    tables: list[TableWrite] = field(default_factory=list)
    seconds: float = 0.0

    def total_rows(self) -> int:
        return sum(t.rows_written for t in self.tables)

    def by_table(self) -> dict[str, TableWrite]:
        return {t.table: t for t in self.tables}


def needs_identity_insert(plan_table: PlanTable, model: PhysicalModel) -> bool:
    """True when the plan generates a value for an identity column."""
    physical = model.table(plan_table.name)
    if physical is None:
        return False
    return any(
        (column := physical.column(f.name)) is not None
        and column.is_identity
        and f.generator != "skip"
        for f in plan_table.fields
    )


def adapt(value: Any) -> Any:
    """Convert a generated value to something pyodbc will bind."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, bool):
        return 1 if value else 0
    return value


class TooLong(ValueError):
    """A generated value longer than the column it is going into."""


def check_widths(plan: Plan, result: RunResult, model: PhysicalModel) -> None:
    """Refuse a load in which any text value is longer than its column.

    Said with the table, the column, its width, the value and the rule that
    made it, which is what anyone needs to change it -- and said before the
    first row is written, so nothing is left half loaded.
    """
    from sdg.writers.export import _text

    problems: list[str] = []
    for plan_table in plan.tables_in_order():
        table_result = result.tables.get(plan_table.name)
        physical = model.table(plan_table.name)
        if table_result is None or not table_result.rows or physical is None:
            continue
        for plan_field in plan_table.fields:
            if plan_field.generator == "skip":
                continue
            column = physical.column(plan_field.name)
            if column is None or not column.is_character:
                continue
            width = column.character_length()
            if not width:
                continue
            for row in table_result.rows:
                value = row.get(plan_field.name)
                if value is None:
                    continue
                text = _text(value)
                if len(text) > width:
                    problems.append(
                        f"{plan_table.name}.{plan_field.name} is {column.type_name}({width}) "
                        f"and the {plan_field.generator} rule made {text!r} "
                        f"({len(text)} characters)"
                    )
                    break
    if problems:
        raise TooLong(
            "Nothing was written: some values are longer than their columns. "
            + "; ".join(problems[:5])
            + (f"; and {len(problems) - 5} more" if len(problems) > 5 else "")
            + ". Change the rule for each in Schema or Review, then generate again."
        )


class PartialWrite(Exception):
    """A load that failed after committing whole tables.

    Each table is committed on its own, so a failure partway leaves the tables
    before it in the database. The report of what was committed travels with
    the error, because without it there is no manifest and nothing can find
    those rows again to remove them.
    """

    def __init__(self, cause: Exception, report: "WriteReport") -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.report = report


class DatabaseGenerationOff(RuntimeError):
    """This installation does not write generated rows into a database."""


class WrongDatabase(ValueError):
    """A run's rows were written somewhere other than where cleanup would delete."""


def check_cleanup_target(
    registry: ConnectionRegistry, manifest: dict[str, Any], connection: str = "target"
) -> str:
    """The database cleanup would delete from, refused unless the run wrote there.

    The manifest's keys say which rows to delete, not where they are: run
    against another database, the same keys find someone else's rows.
    """
    database = registry.database_for(connection)
    run = manifest.get("run") or {}
    written_to = str(run.get("database") or "").strip()
    if not written_to:
        raise WrongDatabase(
            "This run's record does not say which database its rows were written to, "
            f"so cleanup will not delete from {database}. Nothing was deleted.")
    if written_to.upper() != database.strip().upper():
        raise WrongDatabase(
            f"This run wrote its rows to {written_to}, but the target is now {database}. "
            f"Nothing was deleted. Choose {written_to} as the target and run cleanup again.")
    server = str(run.get("server") or "").strip()
    settings = registry.settings_for(connection) if server else None
    current = settings.endpoint if settings else ""
    # A record that names the host alone means port 1433, as the endpoint is
    # written. A record from before ports were recorded (cda42c7..17da574)
    # reads the same way: right for a 1433 run, but it cannot tell another
    # port's run apart. A record from before servers were recorded is checked
    # by database alone.
    if server and server.upper() != current.strip().upper():
        if "," not in server and server.upper() == settings.server.strip().upper():
            raise WrongDatabase(
                f"This run's record names {server} without a port, which means port 1433, "
                f"but the target is now on {current}. Nothing was deleted. Connect to "
                f"{server} on port 1433 and run cleanup again.")
        raise WrongDatabase(
            f"This run wrote its rows to {written_to} on {server}, but the target is now "
            f"on {current}. Nothing was deleted. Connect to {server} and run cleanup again.")
    return database


def write(
    registry: ConnectionRegistry,
    plan: Plan,
    result: RunResult,
    model: PhysicalModel,
    *,
    connection: str = "target",
    disable_triggers: bool = False,
    batch_size: int = BATCH_SIZE,
) -> WriteReport:
    """Insert every generated table, parents first."""
    from sdg.config import DATABASE_GENERATION_OFF, database_generation

    # The one place generated rows go into a database, so turning it off here
    # turns it off for the screens and the command line alike.
    if not database_generation():
        raise DatabaseGenerationOff(DATABASE_GENERATION_OFF)
    from sdg.config import refuse_protected

    refuse_protected(registry.database_for(connection), "write into")
    # Before anything is written: a value too long for its column fails as
    # "String data, right truncation" with no column named, after the tables
    # before it are already committed.
    check_widths(plan, result, model)
    report = WriteReport()
    started = time.perf_counter()

    try:
        with registry.connect(connection) as conn:
            for plan_table in plan.tables_in_order():
                table_result = result.tables.get(plan_table.name)
                if table_result is None or not table_result.rows:
                    continue
                try:
                    report.tables.append(
                        _write_table(
                            conn, plan_table, table_result.rows, model,
                            disable_triggers=disable_triggers, batch_size=batch_size,
                        )
                    )
                except _CommittedThenFailed as done:
                    # The rows are in: recorded, so cleanup can find them,
                    # before the switch-back failure stops the load.
                    report.tables.append(done.record)
                    raise done.error from done
    except Exception as exc:
        report.seconds = time.perf_counter() - started
        if report.tables:
            raise PartialWrite(exc, report) from exc
        raise

    report.seconds = time.perf_counter() - started
    return report


def _write_table(
    conn: pyodbc.Connection,
    plan_table: PlanTable,
    rows: list[dict[str, Any]],
    model: PhysicalModel,
    *,
    disable_triggers: bool,
    batch_size: int,
) -> TableWrite:
    started = time.perf_counter()
    columns = [f.name for f in plan_table.fields if f.generator != "skip"]
    target = qualified(plan_table)
    identity = needs_identity_insert(plan_table, model)

    record = TableWrite(
        table=plan_table.name,
        identity_insert=identity,
        triggers_disabled=disable_triggers,
    )
    physical = model.table(plan_table.name)
    if physical and physical.primary_key:
        record.key_columns = list(physical.primary_key)
    if physical and physical.triggers and not disable_triggers:
        record.triggers_fired = list(physical.triggers)

    cursor = conn.cursor()
    cursor.fast_executemany = True

    statement = (
        f"INSERT INTO {target} ({', '.join(quote(c) for c in columns)}) "  # noqa: S608
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    # The switches are inside the try, so a failure in the second still puts
    # the first back.
    try:
        if disable_triggers:
            cursor.execute(f"ALTER TABLE {target} DISABLE TRIGGER ALL")
        if identity:
            cursor.execute(f"SET IDENTITY_INSERT {target} ON")
        for start in range(0, len(rows), batch_size):
            batch = [
                tuple(adapt(row.get(column)) for column in columns)
                for row in rows[start : start + batch_size]
            ]
            cursor.executemany(statement, batch)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except pyodbc.Error as exc:
            log.error("Could not roll back the write into %s: %s", plan_table.name, exc)
        _restore(conn, cursor, target, plan_table.name, identity=identity,
                 triggers=disable_triggers, failed=True)
        raise

    record.rows_written = len(rows)
    if record.key_columns:
        record.key_values = [
            tuple(row.get(column) for column in record.key_columns) for row in rows
        ]
    try:
        _restore(conn, cursor, target, plan_table.name, identity=identity,
                 triggers=disable_triggers, failed=False)
    except pyodbc.Error as exc:
        record.seconds = time.perf_counter() - started
        raise _CommittedThenFailed(exc, record) from exc
    record.seconds = time.perf_counter() - started
    return record


class _CommittedThenFailed(Exception):
    """A table's rows were committed, then turning its switches back failed."""

    def __init__(self, error: Exception, record: TableWrite):
        super().__init__(str(error))
        self.error = error
        self.record = record


def _restore(
    conn: pyodbc.Connection,
    cursor: pyodbc.Cursor,
    target: str,
    table: str,
    *,
    identity: bool,
    triggers: bool,
    failed: bool,
) -> None:
    """Turn IDENTITY_INSERT off and the triggers back on.

    Each step is tried even when the one before it fails. After a failed write
    a cleanup error is logged rather than raised, so the error that stopped the
    write is the one the caller sees; the rollback has already undone the
    DISABLE TRIGGER, and ENABLE is repeated in case it had not. After a good
    write a cleanup error is raised, because the table is left switched.
    """
    steps = []
    if identity:
        steps.append(("turn IDENTITY_INSERT off", f"SET IDENTITY_INSERT {target} OFF"))
    if triggers:
        steps.append(("re-enable the triggers", f"ALTER TABLE {target} ENABLE TRIGGER ALL"))
    if not steps:
        return
    first: Exception | None = None
    for what, sql in steps:
        try:
            cursor.execute(sql)
        except pyodbc.Error as exc:
            log.error("Could not %s on %s after %s. Check the table before the "
                      "next load. (%s)", what, table,
                      "a failed write" if failed else "the write", exc)
            first = first or exc
    try:
        conn.commit()
    except pyodbc.Error as exc:
        log.error("Could not commit the cleanup on %s. Check its triggers and "
                  "IDENTITY_INSERT before the next load. (%s)", table, exc)
        first = first or exc
    if first is not None and not failed:
        raise first


def key_predicate(key_columns: list[str], keys: list) -> tuple[str, list[Any]]:
    """A WHERE clause matching exactly the given key values.

    A single-column key becomes an IN list. A composite key becomes a chain of
    equality groups, because T-SQL has no row-value IN.
    """
    if len(key_columns) == 1:
        placeholders = ", ".join("?" for _ in keys)
        values = [k[0] if isinstance(k, (list, tuple)) else k for k in keys]
        return f"{quote(key_columns[0])} IN ({placeholders})", values

    group = "(" + " AND ".join(f"{quote(c)} = ?" for c in key_columns) + ")"
    clause = " OR ".join(group for _ in keys)
    values = [value for key in keys for value in key]
    return f"({clause})", values


def manifest_key_columns(record: dict[str, Any]) -> list[str]:
    """Key columns from a manifest entry, tolerating the single-column form."""
    if record.get("key_columns"):
        return list(record["key_columns"])
    if record.get("key_column"):
        return [record["key_column"]]
    return []


CLEANUP_TABLE = "#sdg_cleanup_keys"
STAGE_BATCH = 10_000


def _stage_keys(
    conn: pyodbc.Connection, target: str, columns: list[str], keys: list
) -> None:
    """Put the keys to delete into a temporary table.

    The same reason the validator stages rather than lists: a quarter of a
    million keys spread over hundreds of statements is slower than the
    generation that produced them. The UNION ALL drops the IDENTITY property
    that SELECT INTO would otherwise copy.
    """
    quoted = ", ".join(quote(c) for c in columns)
    cursor = conn.cursor()
    cursor.execute(f"DROP TABLE IF EXISTS {CLEANUP_TABLE}")
    cursor.execute(
        f"SELECT {quoted} INTO {CLEANUP_TABLE} FROM ("  # noqa: S608
        f"  SELECT {quoted} FROM {target} WHERE 1 = 0"
        f"  UNION ALL"
        f"  SELECT {quoted} FROM {target} WHERE 1 = 0"
        f") AS shape"
    )
    statement = (
        f"INSERT INTO {CLEANUP_TABLE} ({quoted}) "  # noqa: S608
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    cursor.fast_executemany = True
    rows = [tuple(k) if isinstance(k, (list, tuple)) else (k,) for k in keys]
    for start in range(0, len(rows), STAGE_BATCH):
        cursor.executemany(statement, rows[start : start + STAGE_BATCH])
    cursor.execute(f"CREATE CLUSTERED INDEX ix ON {CLEANUP_TABLE} ({quoted})")
    conn.commit()


def cleanup(
    registry: ConnectionRegistry,
    manifest: dict[str, Any],
    *,
    connection: str = "target",
) -> dict[str, int]:
    """Delete exactly the rows a run wrote, children first.

    The keys come from the manifest rather than from a date range or a marker
    column, so rows that were already in the table are never touched.

    What a trigger wrote into some other table is not undone. Those rows are
    not this run's to delete, and `trigger_side_effects` reports them so the
    difference is visible rather than surprising.
    """
    from sdg.config import refuse_protected

    refuse_protected(registry.database_for(connection), "delete from")
    check_cleanup_target(registry, manifest, connection)
    order = list(manifest.get("generation_order", []))
    by_table = {t["table"]: t for t in manifest.get("tables", [])}
    keys_by_table = manifest.get("keys", {})

    deleted: dict[str, int] = {}
    with registry.connect(connection) as conn:
        cursor = conn.cursor()
        try:
            # Reverse order: a child has to go before the parent it points at.
            for name in reversed(order):
                record = by_table.get(name)
                keys = keys_by_table.get(name)
                columns = manifest_key_columns(record or {})
                if not record or not columns or not keys:
                    continue
                target = _qualified_name(name)
                _stage_keys(conn, target, columns, keys)
                condition = " AND ".join(
                    f"t.{quote(c)} = k.{quote(c)}" for c in columns
                )
                cursor.execute(
                    f"DELETE t FROM {target} AS t "  # noqa: S608
                    f"JOIN {CLEANUP_TABLE} AS k ON {condition}"
                )
                deleted[name] = cursor.rowcount if cursor.rowcount > 0 else 0
                conn.commit()
        finally:
            cursor.execute(f"DROP TABLE IF EXISTS {CLEANUP_TABLE}")
            conn.commit()
    return deleted


def trigger_side_effects(manifest: dict[str, Any]) -> list[str]:
    """Triggers that fired during the load, whose writes cleanup will not undo."""
    fired: list[str] = []
    for record in manifest.get("tables", []):
        for trigger in record.get("triggers_fired", []):
            fired.append(f"{trigger} on {record['table']}")
    return fired


def _qualified_name(name: str) -> str:
    if "." in name:
        schema, _, table = name.partition(".")
        return f"{quote(schema)}.{quote(table)}"
    return quote(name)

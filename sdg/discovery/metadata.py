"""Read structure out of SQL Server.

Everything comes from the sys.* catalog views rather than INFORMATION_SCHEMA,
because identity, computed, and default information is not in the standard
views and the plan needs all three.

Nothing here writes to the source.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import (
    CheckConstraint,
    Column,
    ForeignKey,
    PhysicalModel,
    Table,
    UniqueConstraint,
    now_stamp,
)

TABLES_SQL = """
SELECT s.name AS schema_name,
       t.name AS table_name,
       CAST(ISNULL(p.rows, 0) AS bigint) AS row_count
FROM sys.tables AS t
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
OUTER APPLY (
    -- Every partition's rows: a partitioned table is the sum of them.
    SELECT SUM(pt.rows) AS rows
    FROM sys.partitions AS pt
    WHERE pt.object_id = t.object_id AND pt.index_id IN (0, 1)
) AS p
WHERE t.is_ms_shipped = 0 AND t.type = 'U'
ORDER BY s.name, t.name
"""

COLUMNS_SQL = """
SELECT s.name AS schema_name,
       t.name AS table_name,
       c.name AS column_name,
       c.column_id,
       ty.name AS type_name,
       c.max_length,
       c.precision,
       c.scale,
       c.is_nullable,
       c.is_identity,
       c.is_computed,
       cc.definition AS computed_definition,
       dc.definition AS default_definition,
       c.collation_name
FROM sys.columns AS c
JOIN sys.tables AS t ON t.object_id = c.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.computed_columns AS cc
       ON cc.object_id = c.object_id AND cc.column_id = c.column_id
LEFT JOIN sys.default_constraints AS dc ON dc.object_id = c.default_object_id
WHERE t.is_ms_shipped = 0 AND t.type = 'U'
ORDER BY s.name, t.name, c.column_id
"""

INDEXES_SQL = """
SELECT s.name AS schema_name,
       t.name AS table_name,
       i.name AS index_name,
       i.is_primary_key,
       i.is_unique_constraint,
       i.is_unique,
       c.name AS column_name,
       ic.key_ordinal
FROM sys.indexes AS i
JOIN sys.tables AS t ON t.object_id = i.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.index_columns AS ic
       ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns AS c
       ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
  AND ic.is_included_column = 0
  -- A filtered or disabled unique index proves nothing about every row.
  AND i.has_filter = 0 AND i.is_disabled = 0
  AND (i.is_primary_key = 1 OR i.is_unique_constraint = 1 OR i.is_unique = 1)
ORDER BY s.name, t.name, i.name, ic.key_ordinal
"""

# Every index's first column, unique or not: what a lookup can seek on.
INDEX_LEADS_SQL = """
SELECT DISTINCT s.name AS schema_name, t.name AS table_name, c.name AS column_name
FROM sys.indexes AS i
JOIN sys.tables AS t ON t.object_id = i.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.index_columns AS ic
       ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal = 1
JOIN sys.columns AS c
       ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0 AND i.is_disabled = 0 AND i.is_hypothetical = 0
"""

FOREIGN_KEYS_SQL = """
SELECT fk.name AS constraint_name,
       cs.name AS child_schema,
       ct.name AS child_table,
       cc.name AS child_column,
       ps.name AS parent_schema,
       pt.name AS parent_table,
       pc.name AS parent_column,
       fkc.constraint_column_id
FROM sys.foreign_keys AS fk
JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables AS ct ON ct.object_id = fk.parent_object_id
JOIN sys.schemas AS cs ON cs.schema_id = ct.schema_id
JOIN sys.tables AS pt ON pt.object_id = fk.referenced_object_id
JOIN sys.schemas AS ps ON ps.schema_id = pt.schema_id
JOIN sys.columns AS cc
       ON cc.object_id = fkc.parent_object_id AND cc.column_id = fkc.parent_column_id
JOIN sys.columns AS pc
       ON pc.object_id = fkc.referenced_object_id AND pc.column_id = fkc.referenced_column_id
ORDER BY fk.name, fkc.constraint_column_id
"""

CHECKS_SQL = """
SELECT s.name AS schema_name,
       t.name AS table_name,
       ck.name AS constraint_name,
       ck.definition,
       c.name AS column_name
FROM sys.check_constraints AS ck
JOIN sys.tables AS t ON t.object_id = ck.parent_object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
LEFT JOIN sys.columns AS c
       ON c.object_id = ck.parent_object_id AND c.column_id = ck.parent_column_id
WHERE ck.is_disabled = 0
ORDER BY s.name, t.name, ck.name
"""

TRIGGERS_SQL = """
SELECT s.name AS schema_name,
       t.name AS table_name,
       tr.name AS trigger_name
FROM sys.triggers AS tr
JOIN sys.tables AS t ON t.object_id = tr.parent_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
WHERE tr.parent_class = 1 AND tr.is_disabled = 0
ORDER BY s.name, t.name, tr.name
"""

# SQL Server stores `IN ('a','b')` as an OR chain of equality tests, so both
# forms have to be recognized.
_EQUALITY = re.compile(r"\[?(\w+)\]?\s*=\s*N?'((?:[^']|'')*)'")
_IN_LIST = re.compile(r"\[?(\w+)\]?\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_QUOTED = re.compile(r"N?'((?:[^']|'')*)'")


def parse_check_values(definition: str) -> tuple[str | None, list[str] | None]:
    """Pull the allowed value list out of a check constraint, if it is one.

    Returns (column, values). Either may be None when the constraint is not a
    simple membership test.
    """
    if not definition:
        return None, None

    match = _IN_LIST.search(definition)
    if match:
        column = match.group(1)
        values = [v.group(1).replace("''", "'") for v in _QUOTED.finditer(match.group(2))]
        return (column, sorted(set(values))) if values else (column, None)

    pairs = _EQUALITY.findall(definition)
    if not pairs:
        return None, None
    columns = {c for c, _ in pairs}
    if len(columns) != 1:
        # A constraint spanning several columns is not a value list.
        return None, None
    column = columns.pop()
    # Only an OR chain is a membership test; AND means a range or a rule.
    if " AND " in definition.upper():
        return column, None
    values = sorted({v.replace("''", "'") for _, v in pairs})
    return column, values


def _qualified(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def extract(
    registry: ConnectionRegistry,
    connection: str = "source",
    *,
    include_schemas: Iterable[str] | None = None,
    exclude_tables: Iterable[str] | None = None,
    exclude_schemas: Iterable[str] | None = None,
) -> PhysicalModel:
    """Read the whole structure of one database."""
    schemas = {s.lower() for s in include_schemas} if include_schemas else None
    skipped = {s.lower() for s in exclude_schemas} if exclude_schemas else set()
    excluded = {t.upper() for t in exclude_tables} if exclude_tables else set()

    def wanted(schema: str, table: str) -> bool:
        if schemas and schema.lower() not in schemas:
            return False
        if schema.lower() in skipped:
            return False
        return table.upper() not in excluded and _qualified(schema, table).upper() not in excluded

    tables: dict[str, Table] = {}
    for row in registry.query(connection, TABLES_SQL):
        if not wanted(row["schema_name"], row["table_name"]):
            continue
        name = _qualified(row["schema_name"], row["table_name"])
        tables[name] = Table(
            name=name,
            schema_name=row["schema_name"],
            table_name=row["table_name"],
            row_count=int(row["row_count"] or 0),
        )

    _add_columns(registry, connection, tables)
    _add_indexes(registry, connection, tables)
    _add_index_leads(registry, connection, tables)
    _add_foreign_keys(registry, connection, tables)
    _add_checks(registry, connection, tables)
    _add_triggers(registry, connection, tables)

    return PhysicalModel(
        connection=registry.ref(connection).as_dict(),
        discovered=now_stamp(),
        tables=[tables[name] for name in sorted(tables)],
    )


def _add_columns(registry: ConnectionRegistry, connection: str, tables: dict[str, Table]) -> None:
    for row in registry.query(connection, COLUMNS_SQL):
        name = _qualified(row["schema_name"], row["table_name"])
        table = tables.get(name)
        if table is None:
            continue
        table.columns.append(
            Column(
                name=row["column_name"],
                ordinal=int(row["column_id"]),
                type_name=row["type_name"],
                max_length=int(row["max_length"]) if row["max_length"] is not None else None,
                precision=int(row["precision"]) if row["precision"] is not None else None,
                scale=int(row["scale"]) if row["scale"] is not None else None,
                nullable=bool(row["is_nullable"]),
                is_identity=bool(row["is_identity"]),
                is_computed=bool(row["is_computed"]),
                computed_definition=row["computed_definition"],
                default_definition=row["default_definition"],
                collation=row.get("collation_name"),
            )
        )


def _add_indexes(registry: ConnectionRegistry, connection: str, tables: dict[str, Table]) -> None:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in registry.query(connection, INDEXES_SQL):
        name = _qualified(row["schema_name"], row["table_name"])
        if name not in tables:
            continue
        key = (name, row["index_name"])
        entry = grouped.setdefault(
            key,
            {
                "table": name,
                "index": row["index_name"],
                "is_primary_key": bool(row["is_primary_key"]),
                "columns": [],
            },
        )
        entry["columns"].append((int(row["key_ordinal"]), row["column_name"]))

    for entry in grouped.values():
        table = tables[entry["table"]]
        columns = [c for _, c in sorted(entry["columns"])]
        table.unique_constraints.append(
            UniqueConstraint(
                name=entry["index"],
                columns=columns,
                is_primary_key=entry["is_primary_key"],
            )
        )
        if entry["is_primary_key"]:
            table.primary_key = columns
            for position, column_name in enumerate(columns, start=1):
                column = table.column(column_name)
                if column:
                    column.is_primary_key = True
                    column.primary_key_ordinal = position
        if len(columns) == 1:
            column = table.column(columns[0])
            if column:
                column.is_unique = True


def _add_index_leads(
    registry: ConnectionRegistry, connection: str, tables: dict[str, Table]
) -> None:
    for row in registry.query(connection, INDEX_LEADS_SQL):
        table = tables.get(_qualified(row["schema_name"], row["table_name"]))
        if table is not None and row["column_name"] not in table.indexed:
            table.indexed.append(row["column_name"])


def _add_foreign_keys(
    registry: ConnectionRegistry, connection: str, tables: dict[str, Table]
) -> None:
    grouped: dict[str, dict[str, Any]] = {}
    for row in registry.query(connection, FOREIGN_KEYS_SQL):
        child = _qualified(row["child_schema"], row["child_table"])
        parent = _qualified(row["parent_schema"], row["parent_table"])
        if child not in tables or parent not in tables:
            continue
        entry = grouped.setdefault(
            row["constraint_name"],
            {"child": child, "parent": parent, "pairs": []},
        )
        entry["pairs"].append(
            (int(row["constraint_column_id"]), row["child_column"], row["parent_column"])
        )

    for constraint_name, entry in grouped.items():
        pairs = sorted(entry["pairs"])
        tables[entry["child"]].foreign_keys.append(
            ForeignKey(
                name=constraint_name,
                columns=[c for _, c, _ in pairs],
                parent_table=entry["parent"],
                parent_columns=[p for _, _, p in pairs],
                kind="declared",
            )
        )


def _add_checks(registry: ConnectionRegistry, connection: str, tables: dict[str, Table]) -> None:
    for row in registry.query(connection, CHECKS_SQL):
        name = _qualified(row["schema_name"], row["table_name"])
        table = tables.get(name)
        if table is None:
            continue
        definition = row["definition"] or ""
        parsed_column, values = parse_check_values(definition)
        column_name = row["column_name"] or parsed_column
        table.check_constraints.append(
            CheckConstraint(
                name=row["constraint_name"],
                definition=definition,
                column=column_name,
                values=values,
            )
        )
        if column_name:
            column = table.column(column_name)
            if column:
                column.check_definition = definition
                if values:
                    column.check_values = values


def _add_triggers(registry: ConnectionRegistry, connection: str, tables: dict[str, Table]) -> None:
    for row in registry.query(connection, TRIGGERS_SQL):
        name = _qualified(row["schema_name"], row["table_name"])
        table = tables.get(name)
        if table is not None:
            table.triggers.append(row["trigger_name"])

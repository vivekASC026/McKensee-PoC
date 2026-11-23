"""The SQL a built pack carries: its store tables, their indexes, its extract.

Everything here is written from an approved plan and the source's model,
never from a value of any source column:

- `store_tables_sql`: one `CREATE TABLE` per denorm table, add-only (a table
  that exists is left as it is), written for schema sdm (the store's own
  schema replaces it when it is run: seed/bootstrap.in_schema). It refuses
  to run in the source database and in every protected one: the store is
  never a source.
- `store_indexes_sql`: an index on each table's record key and on each walk
  level's key, each made only when absent, behind the same guards.
- `extract_sql`: one `-- table: <name>` block per denorm table and one
  SELECT in each, the runner's rule (sdg/extract/run.py `_statements`). A
  block reads from the record table (the lines table for a child at lines
  grain) and LEFT JOINs along each selected level's path; a picked link
  table is read through a ROW_NUMBER() pick of the row in force today with
  the latest start (ties: an open end first, then the later end, then the
  link's key). A join that could repeat records is refused: one up a path
  or to a label must meet the target's primary key or a unique index in the
  model, and a link entered without dates needs the person's acceptance of
  the grain change (plan.accept_grain). Its WHERE is the selected criteria
  (criteria.where_sql); the child's also holds an EXISTS on the primary's
  criteria, so the child follows the primary's scope (refused when the child
  links to the primary by more than one key and the person stated none). A
  withheld column is selected as CAST(NULL AS <type>), never as itself, and
  the runner writes it NULL again (pack.json extract.withheld and
  extract.kept). Each SELECT ends with OPTION (MAXDOP 2), so a full read of a
  large source takes at most two of its cores.
- `readme_txt`: how the person installs the pack.

Identifiers come from the model only, each bracket-quoted with any `]`
doubled; a denorm name is a plain name (packs.plain_name); a store type is
one of SQL Server's own types. A value in a criterion is a literal made by
criteria.literal alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sdg import packs
from sdg.accelerator import criteria as criteria_rules
from sdg.accelerator import structure as structure_rules
from sdg.accelerator.graph import KeyGraph
from sdg.accelerator.hierarchy import Join, LevelCandidate
from sdg.accelerator.structure import DenormColumn, DenormTable
from sdg.discovery.model import Column, Table

if TYPE_CHECKING:
    from sdg.accelerator.plan import SourcePlan

# The schema every store script is written for (seed/bootstrap.in_schema).
STORE_SCHEMA = "sdm"
SOURCE_REFUSAL = ("This script runs in the SDM platform database, never in a source or "
                  "a protected database. Nothing was changed. Choose the SDM platform "
                  "database and run it again.")
# A database name as a guard names it: what SQL Server allows unquoted, and
# the few characters a name in .env may hold besides.
_DATABASE = re.compile(r"[A-Za-z0-9_@#$.\- ]{1,128}")

# SQL Server's own types: anything else (an alias type of the source's own)
# does not exist in the store, and is refused.
_PLAIN_TYPES = frozenset({
    "bigint", "int", "smallint", "tinyint", "bit", "money", "smallmoney", "float", "real",
    "date", "datetime", "smalldatetime", "uniqueidentifier", "text", "ntext", "image", "xml",
    "sql_variant", "hierarchyid", "geography", "geometry"})
_SIZED_TYPES = frozenset({"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"})
_SCALED_TYPES = frozenset({"decimal", "numeric"})
_FRACTION_TYPES = frozenset({"datetime2", "time", "datetimeoffset"})
# A row version cannot be written into: its eight bytes are kept instead.
_ROW_VERSION = frozenset({"timestamp", "rowversion"})
_TYPE = re.compile(r"[a-z0-9_]+(\((\d{1,4}|max)(,\d{1,2})?\))?")
# Types the extract's driver cannot read back (ODBC -150, -151, -155): a
# column of one is refused when the plan is built, not after approval.
_UNREADABLE = frozenset({"sql_variant", "hierarchyid", "geography", "geometry",
                         "datetimeoffset"})
# Types SQL Server cannot order by: left out of a pick's tie-break.
_UNORDERABLE = frozenset({"text", "ntext", "image", "xml", "geography", "geometry"})
# Type families a join may pair; two columns of different families are refused.
_CHARACTER = frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext"})
_NUMBER = frozenset({"bigint", "int", "smallint", "tinyint", "bit", "decimal", "numeric",
                     "money", "smallmoney", "float", "real"})
# A collation name as the model holds it.
_COLLATION = re.compile(r"[A-Za-z0-9_]{1,128}")

TODAY = "CAST(GETDATE() AS date)"
TOMORROW = criteria_rules.TOMORROW
PICK_COLUMN = "sdm_pick"
# Index names SQL Server takes: 128 characters at most.
NAME_MAX = 128
# How many of the source's cores one extract statement may use.
MAXDOP = 2
# The most bytes a nonclustered index key may hold in SQL Server.
INDEX_KEY_BYTES = 1700


class SqlRefused(ValueError):
    """SQL that cannot be written from the plan as it stands, said plainly."""


# --- names and types -----------------------------------------------------------

def quote(name: str) -> str:
    """A name bracket-quoted, any `]` in it doubled."""
    return "[" + name.replace("]", "]]") + "]"


def _table(graph: KeyGraph, name: str) -> Table:
    found = graph.table(name)
    if found is None:
        raise SqlRefused(f"the source has no table {name!r}")
    return found


def _column(graph: KeyGraph, table: str, column: str) -> Column:
    found = _table(graph, table).column(column)
    if found is None:
        raise SqlRefused(f"{table} has no column {column!r}")
    return found


def table_ref(table: Table) -> str:
    """A source table as SQL: its schema and name, as the model holds them."""
    return f"{quote(table.schema_name)}.{quote(table.table_name)}"


def store_type(column: Column) -> str:
    """The store column's type, from the source column's: one of SQL Server's
    own types, refused otherwise."""
    name = column.type_name.lower()
    if name in _UNREADABLE:
        raise SqlRefused(f"{column.name} is of type {name}, which the extract cannot read: "
                         f"leave it out of the plan")
    if name in _PLAIN_TYPES:
        out = name
    elif name in _ROW_VERSION:
        out = "binary(8)"
    elif name in _SCALED_TYPES:
        if column.precision is None or column.scale is None:
            raise SqlRefused(f"{column.name} is {name} with no precision in the model")
        out = f"{name}({int(column.precision)},{int(column.scale)})"
    elif name in _SIZED_TYPES:
        if column.max_length is None:
            raise SqlRefused(f"{column.name} is {name} with no length in the model")
        if column.max_length < 0:
            out = f"{name}(max)"
        else:
            length = column.max_length // 2 if name in ("nchar", "nvarchar") else column.max_length
            out = f"{name}({max(1, int(length))})"
    elif name in _FRACTION_TYPES:
        out = name if column.scale is None else f"{name}({int(column.scale)})"
    else:
        raise SqlRefused(f"{column.name} is of type {column.type_name!r}, not one of SQL "
                         f"Server's own: name its store type by hand in the pack")
    if not _TYPE.fullmatch(out):
        raise SqlRefused(f"{column.name}: {out!r} is not a type the store takes")
    return out


def denorm_type(held: DenormColumn, graph: KeyGraph) -> str:
    """A denorm column's store type: a surrogate's own, else the source's."""
    if held.privacy == "surrogate":
        return structure_rules.SURROGATE_TYPE
    return store_type(_column(graph, held.source_table, held.source_column))


def _store_name(name: str, what: str) -> str:
    return packs.plain_name(name, what)


def _comment(text: Any) -> str:
    """Text safe inside a -- comment: one line, printable characters only."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())


def _database_literal(name: str) -> str:
    if not _DATABASE.fullmatch(name or ""):
        raise SqlRefused(f"{name!r} is not a database name a guard can hold")
    return "N'" + name.replace("'", "''") + "'"


def guards(databases: list[str]) -> str:
    """The first batches of every store script: refused in each database
    named here (the source, the protected ones), written in by name when the
    pack is built where it runs."""
    out: list[str] = []
    others = sorted({d.strip() for d in databases if d and d.strip()}, key=str.upper)
    if others:
        listed = ", ".join(_database_literal(d) for d in others)
        out += [f"IF DB_NAME() IN ({listed})",
                f"    THROW 50000, N'{SOURCE_REFUSAL}', 1;",
                "GO",
                f"IF DB_NAME() IN ({listed})",
                "    SET NOEXEC ON;",
                "GO"]
    return "\n".join(out)


# --- the store tables ------------------------------------------------------------

def _role_key(plan: "SourcePlan", denorm: DenormTable) -> list[DenormColumn]:
    """The denorm table's own key: the record's key (the lines' too, at lines
    grain)."""
    kind = plan.record_kinds.get(denorm.role) or {}
    keys = set(kind.get("key_columns") or [])
    record = kind.get("table")
    out = [c for c in denorm.columns if c.serves == "key" and c.source_table == record
           and c.source_column in keys]
    return out + [c for c in denorm.columns if c.serves == "lines_key"]


def read_from(plan: "SourcePlan", denorm: DenormTable, held: DenormColumn) -> tuple[str, str]:
    """The source (table, column) a denorm column is selected from: at lines
    grain the record's key is read from the lines, which carry it."""
    kind = plan.record_kinds.get(denorm.role) or {}
    record, lines = kind.get("table"), kind.get("lines_table")
    if lines and held.serves == "key" and held.source_table == record \
            and held.source_column in set(kind.get("key_columns") or []):
        return lines, held.source_column
    return held.source_table, held.source_column


# Types an index cannot have as its key.
_UNINDEXABLE = frozenset({"text", "ntext", "image", "xml", "geography", "geometry"})


def store_tables_sql(plan: "SourcePlan", graph: KeyGraph, databases: list[str]) -> str:
    """`CREATE TABLE` for each denorm table, add-only, for schema sdm, behind
    the guards on each of `databases`."""
    lines = [
        f"-- store_tables.sql ({_comment(plan.pack.get('id'))} pack): the tables pattern "
        f"analysis reads.",
        f"-- Written by the source accelerator from {_comment(plan.source)}'s approved plan.",
        "-- Add-only: a table that exists is left as it is. Written for schema sdm.",
        "--",
        "-- Never in the source or a protected database. The first batch of",
        "-- each pair stops the script there with an error; the second keeps every",
        "-- batch after it from running (SET NOEXEC ON lasts for the session: reconnect",
        "-- after).",
        guards(databases),
        ""]
    for denorm in plan.structure:
        name = _store_name(denorm.name, "a denorm table")
        required = {id(c) for c in _role_key(plan, denorm)}
        rows = []
        for held in denorm.columns:
            column = _store_name(held.name, f"{name} column")
            source = _column(graph, *read_from(plan, denorm, held))
            not_null = (id(held) in required and held.privacy == "kept"
                        and not source.nullable)
            rows.append(f"    {quote(column)} {denorm_type(held, graph)} "
                        f"{'NOT NULL' if not_null else 'NULL'}")
        lines += [f"-- {name}: one row per {'line' if denorm.grain == 'lines' else 'record'}, "
                  f"the {_comment(denorm.role)} record kind.",
                  f"IF OBJECT_ID('{STORE_SCHEMA}.{name}', 'U') IS NULL",
                  f"CREATE TABLE {STORE_SCHEMA}.{name} (",
                  ",\n".join(rows),
                  ");",
                  "GO",
                  "-- A table made by an earlier build gains the columns it lacks, added NULL;",
                  "-- a column changed or dropped since is the DBA's to change."]
        for held in denorm.columns:
            column = _store_name(held.name, f"{name} column")
            lines += [f"IF COL_LENGTH('{STORE_SCHEMA}.{name}', '{column}') IS NULL",
                      f"    ALTER TABLE {STORE_SCHEMA}.{name} ADD {quote(column)} "
                      f"{denorm_type(held, graph)} NULL;",
                      "GO"]
        lines.append("")
    return "\n".join(lines)


def _index_name(table: str, columns: list[str]) -> str:
    name = f"ix_{table}_{'_'.join(columns)}".lower()
    if len(name) <= NAME_MAX:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[:NAME_MAX - 9]}_{digest}"


_FIXED_BYTES = {"bigint": 8, "int": 4, "smallint": 2, "tinyint": 1, "bit": 1, "money": 8,
                "smallmoney": 4, "float": 8, "real": 4, "date": 3, "datetime": 8,
                "smalldatetime": 4, "datetime2": 8, "time": 5, "datetimeoffset": 10,
                "uniqueidentifier": 16, "decimal": 17, "numeric": 17}


def key_bytes(sql_type: str) -> int | None:
    """The most bytes a value of a store type takes in an index key; None
    for a type no index takes as its key, or one not known here."""
    held = sql_type.lower().replace(" ", "")
    base = held.split("(", 1)[0]
    if base in _UNINDEXABLE or "(max)" in held:
        return None
    if base in _SIZED_TYPES:
        found = re.fullmatch(r"[a-z]+\((\d+)\)", held)
        if found is None:
            return None
        return int(found.group(1)) * (2 if base in ("nchar", "nvarchar") else 1)
    return _FIXED_BYTES.get(base)


def store_indexes_sql(plan: "SourcePlan", databases: list[str]) -> str:
    """An index on each denorm table's record key and on each selected walk
    level's key, each made only when absent; a key wider than SQL Server
    takes (INDEX_KEY_BYTES) is left out, with a comment saying so."""
    lines = [
        f"-- store_indexes.sql ({_comment(plan.pack.get('id'))} pack): indexes for the reads "
        f"pattern analysis makes.",
        "-- Optional, and each made only when absent. Written for schema sdm. Run it after",
        "-- the first extract has filled the tables.",
        guards(databases),
        ""]
    for denorm in plan.structure:
        name = _store_name(denorm.name, "a denorm table")
        wanted: list[list[str]] = []
        key = [c.name for c in _role_key(plan, denorm)]
        if key:
            wanted.append(key)
        walks = {(lv.table, lv.column) for lv in plan.levels(denorm.role)
                 if lv.selected and lv.kind == "walk"}
        for held in denorm.columns:
            if held.serves == "level_key" and (held.source_table, held.source_column) in walks:
                if [held.name] not in wanted:
                    wanted.append([held.name])
        types = {c.name: c.sql_type.lower() for c in denorm.columns}
        for columns in wanted:
            for column in columns:
                _store_name(column, f"{name} column")
            sizes = [key_bytes(types.get(c, "")) for c in columns]
            if any(size is None for size in sizes):
                lines.append(f"-- {', '.join(columns)}: of a type no index takes as its key, "
                             f"so not indexed.")
                continue
            if sum(sizes) > INDEX_KEY_BYTES:
                lines.append(f"-- {', '.join(columns)}: up to {sum(sizes)} bytes, more than the "
                             f"{INDEX_KEY_BYTES} an index key holds, so not indexed.")
                continue
            index = _index_name(name, columns)
            lines += [f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'{index}'",
                      f"               AND object_id = OBJECT_ID(N'{STORE_SCHEMA}.{name}'))",
                      f"    CREATE INDEX {quote(index)} ON {STORE_SCHEMA}.{name} "
                      f"({', '.join(quote(c) for c in columns)});",
                      "GO"]
        lines.append("")
    return "\n".join(lines)


# --- the extract -----------------------------------------------------------------

def _down(graph: KeyGraph, join: Join) -> bool:
    return structure_rules._down(graph, join)


def _unique_on(table: Table, columns: list[str]) -> bool:
    """Whether a row of `table` is found by these columns alone: they hold
    its primary key or one of its unique constraints or indexes."""
    held = {c.upper() for c in columns}
    keys = [list(table.primary_key)] + [list(u.columns) for u in table.unique_constraints]
    keys += [[c.name] for c in table.columns if c.is_unique]
    return any(key and {c.upper() for c in key} <= held for key in keys)


def _refuse_unless_unique(graph: KeyGraph, table: str, columns: list[str], came: str) -> None:
    if not _unique_on(_table(graph, table), columns):
        raise SqlRefused(
            f"{came} meets {table} on {', '.join(columns)}, which is neither its primary key "
            f"nor a unique index in the model: a row could meet several of its rows, and the "
            f"table would repeat records. Deselect the level, or have the key declared in the "
            f"source and read the landscape again.")


@dataclass
class _Statement:
    """One block's FROM: aliases by the joins that reach them, the picks'
    CTEs, and the LEFT JOINs in the order they were needed."""
    graph: KeyGraph
    picks: dict[str, dict[str, Any]]
    # The columns read from each table, so a pick carries only those.
    needed: dict[str, set[str]] = field(default_factory=dict)
    ctes: dict[tuple, tuple[str, str]] = field(default_factory=dict)
    # Link tables entered without dates whose repeated records the person
    # accepted (plan.grain_accepted, for the block's role).
    accepted: set[str] = field(default_factory=set)

    def cte(self, join: Join) -> str:
        """The pick of a link table entered down by `join`: its rows in force
        today, the latest start first, one row per key it is entered by."""
        key = (join.to_table, tuple(join.to_columns))
        if key not in self.ctes:
            pick = self.picks[join.to_table]
            table = _table(self.graph, join.to_table)
            start = _column(self.graph, join.to_table, pick["start"])
            end = _column(self.graph, join.to_table, pick["end"])
            if not (start.is_date and end.is_date):
                raise SqlRefused(f"{join.to_table}: a pick reads date columns")
            partition = ", ".join(f"b.{quote(_column(self.graph, join.to_table, c).name)}"
                                  for c in join.to_columns)
            carried = sorted({*join.to_columns, start.name, end.name,
                              *self.needed.get(join.to_table, set())})
            # Ties on the latest start are broken the same way every run: an
            # open end first, then the later end, then the link table's own
            # key -- or, where it has none, every column the pick carries, so
            # rows tied on all of them give the same values whichever is taken.
            tie_columns = [c for c in (list(table.primary_key) or carried)
                           if _column(self.graph, join.to_table, c).type_name.lower()
                           not in _UNORDERABLE]
            ties = [f"CASE WHEN b.{quote(end.name)} IS NULL THEN 0 ELSE 1 END",
                    f"b.{quote(end.name)} DESC"] + [
                f"b.{quote(_column(self.graph, join.to_table, c).name)}"
                for c in tie_columns
                if c not in (start.name, end.name, *join.to_columns)]
            note = ("" if table.primary_key else
                    f"    -- {_comment(join.to_table)} has no primary key: ties are broken by "
                    f"every column read.\n")
            listed = ", ".join(f"b.{quote(_column(self.graph, join.to_table, c).name)}"
                               for c in carried)
            name = f"pick_{len(self.ctes) + 1}"
            body = (f"{name} AS (\n"
                    f"{note}"
                    f"    SELECT {listed},\n"
                    f"           ROW_NUMBER() OVER (PARTITION BY {partition} "
                    f"ORDER BY b.{quote(start.name)} DESC, {', '.join(ties)}) "
                    f"AS {PICK_COLUMN}\n"
                    f"    FROM {table_ref(table)} AS b\n"
                    f"    WHERE b.{quote(start.name)} < {TOMORROW}\n"
                    f"      AND (b.{quote(end.name)} IS NULL OR b.{quote(end.name)} >= {TODAY})\n"
                    f")")
            self.ctes[key] = (name, body)
        return self.ctes[key][0]


def _collated(graph: KeyGraph, table: str, mine: str, base_table: str | None,
              theirs: str) -> str:
    """What the base's side of one join pair needs: nothing, or a COLLATE to
    the joined column's collation. Refused when the two are of different
    type families (a number against text fails on the first value that is
    not a number, or depends on the order SQL Server converts in)."""
    if base_table is None:
        return ""
    joined, base = _column(graph, table, mine), _column(graph, base_table, theirs)
    kinds = {c.type_name.lower() for c in (joined, base)}
    if kinds & _CHARACTER and kinds & _NUMBER:
        raise SqlRefused(f"{base_table}.{base.name} ({base.type_name}) and {table}.{joined.name} "
                         f"({joined.type_name}) are of different types, so they cannot be joined "
                         f"safely: state another join, or deselect the level")
    if (joined.type_name.lower() in _CHARACTER and base.type_name.lower() in _CHARACTER
            and joined.collation and base.collation and joined.collation != base.collation):
        if not _COLLATION.fullmatch(joined.collation):
            raise SqlRefused(f"{table}.{joined.name} has a collation the extract cannot name")
        return f" COLLATE {joined.collation}"
    return ""


@dataclass
class _From:
    """The FROM of one SELECT: the first table, then each LEFT JOIN."""
    statement: _Statement
    prefix: str
    first: str
    by_path: dict[tuple, str] = field(default_factory=dict)
    by_table: dict[str, str] = field(default_factory=dict)
    times: dict[str, int] = field(default_factory=dict)
    joins: list[str] = field(default_factory=list)
    count: int = 0

    def __post_init__(self) -> None:
        self.start = self._alias(self.first)

    def _alias(self, table: str) -> str:
        alias = f"{self.prefix}{self.count}"
        self.count += 1
        self.by_table.setdefault(table, alias)
        self.times[table] = self.times.get(table, 0) + 1
        return alias

    def alias_of(self, table: str) -> str:
        """The one alias of a table read once; refused when it is read along
        two paths, where a column of it could be either's."""
        if self.times.get(table, 0) > 1:
            raise SqlRefused(f"{table} is reached along two paths, so its columns could come "
                             f"from either: deselect one of the levels through it")
        alias = self.by_table.get(table)
        if alias is None:
            raise SqlRefused(f"{table} is not read by the statement")
        return alias

    def join(self, table: str, on: list[tuple[str, str]], base: str, *,
             via: str | None = None, picked: bool = False, path: tuple = (),
             base_table: str | None = None) -> str:
        """LEFT JOIN `table` (or `via`, a pick's CTE), on (its column, the
        base's column) pairs; the alias, reused for the same path. Given the
        base's table, each pair is checked: columns of different type families
        are refused, and two text columns of different collations are compared
        in the joined column's."""
        if path and path in self.by_path:
            return self.by_path[path]
        alias = self._alias(table)
        graph = self.statement.graph
        source = via or table_ref(_table(graph, table))
        parts = [f"{alias}.{quote(_column(graph, table, mine).name)} = "
                 f"{base}.{quote(theirs)}{_collated(graph, table, mine, base_table, theirs)}"
                 for mine, theirs in on]
        if picked:
            parts.append(f"{alias}.{PICK_COLUMN} = 1")
        self.joins.append(f"LEFT JOIN {source} AS {alias} ON {' AND '.join(parts)}")
        if path:
            self.by_path[path] = alias
        return alias

    def reach(self, path: list[Join], base: str, base_table: str, *,
              inside_exists: bool = False) -> str:
        """Follow a path of joins from `base` (an alias of `base_table`); the
        alias of the table it ends at. A join up the path must meet the
        target's key; one down into a link table is read through its pick,
        or, with no dates to pick by, refused unless the person accepted the
        grain change -- or `inside_exists`, where repeated rows change
        nothing."""
        alias, table = base, base_table
        graph = self.statement.graph
        walked: tuple = (base,)
        for join in path:
            if join.from_table != table:
                raise SqlRefused(f"a path leaves {join.from_table}, not {table}")
            if len(join.from_columns) != len(join.to_columns) or not join.from_columns:
                raise SqlRefused(f"a join from {join.from_table} pairs no columns")
            walked += ((join.from_table, tuple(join.from_columns), join.to_table,
                        tuple(join.to_columns)),)
            on = [(mine, _column(graph, join.from_table, theirs).name)
                  for mine, theirs in zip(join.to_columns, join.from_columns)]
            down = _down(graph, join)
            picked = down and join.to_table in self.statement.picks
            if not down:
                _refuse_unless_unique(graph, join.to_table, list(join.to_columns),
                                      join.from_table)
            elif not picked and not inside_exists \
                    and join.to_table not in self.statement.accepted:
                raise SqlRefused(
                    f"{join.to_table} links without dates, so each record would appear once "
                    f"per row of it: accept the grain change in Structure, or deselect the "
                    f"level through it.")
            via = self.statement.cte(join) if picked else None
            alias = self.join(join.to_table, on, alias, via=via, picked=picked, path=walked,
                              base_table=table)
            table = join.to_table
        return alias

    def sql(self) -> str:
        table = _table(self.statement.graph, self.first)
        return "\n".join([f"FROM {table_ref(table)} AS {self.start}"] + self.joins)


def _levels(plan: "SourcePlan", role: str) -> list[LevelCandidate]:
    return sorted((lv for lv in plan.levels(role) if lv.selected), key=lambda lv: lv.order)


def _record_from(plan: "SourcePlan", role: str, statement: _Statement,
                 prefix: str) -> tuple[_From, str]:
    """The FROM of a role's block: its lines (at lines grain) joined to its
    record, then every selected level's path and label. Returns it with the
    record's alias."""
    kind = plan.record_kinds[role]
    record, lines = kind["table"], kind.get("lines_table")
    graph = statement.graph
    if lines:
        built = _From(statement, prefix, lines)
        keys = [_column(graph, record, c).name for c in kind.get("key_columns") or []]
        if not keys:
            raise SqlRefused(f"{record} has no key to join its lines on")
        # Each line meets one record, or the lines would multiply.
        _refuse_unless_unique(graph, record, keys, lines)
        on = [(c, _column(graph, lines, c).name) for c in keys]
        record_alias = built.join(record, on, built.start, path=(built.start, "record"),
                                  base_table=lines)
    else:
        built = _From(statement, prefix, record)
        record_alias = built.start
    for level in _levels(plan, role):
        at = built.reach(level.path, record_alias, record)
        if level.label_table and level.label_table != level.table:
            join = level.label_join
            if join is None or join.from_table != level.label_table \
                    or join.to_table != level.table:
                raise SqlRefused(f"{level.id}: its label's table has no join to the level's")
            _refuse_unless_unique(graph, level.label_table, list(join.from_columns),
                                  f"{level.id}'s label")
            on = [(mine, _column(graph, level.table, theirs).name)
                  for mine, theirs in zip(join.from_columns, join.to_columns)]
            built.join(level.label_table, on, at, path=(at, "label", level.label_table),
                       base_table=level.table)
    return built, record_alias


def _select(plan: "SourcePlan", denorm: DenormTable, built: _From, record_alias: str,
            graph: KeyGraph) -> list[str]:
    kind = plan.record_kinds[denorm.role]
    record, lines = kind["table"], kind.get("lines_table")
    keys = set(kind.get("key_columns") or [])
    out = []
    for held in denorm.columns:
        name = quote(_store_name(held.name, f"{denorm.name} column"))
        source = _column(graph, *read_from(plan, denorm, held))
        if held.privacy == "withheld":
            out.append(f"CAST(NULL AS {denorm_type(held, graph)}) AS {name}")
            continue
        if held.privacy not in ("kept", "surrogate"):
            raise SqlRefused(f"{denorm.name}.{held.name} has no privacy class")
        if lines and held.serves == "key" and held.source_table == record \
                and held.source_column in keys:
            alias = built.start  # the lines carry the record's key
        elif held.serves == "lines_key":
            alias = built.start
        elif held.source_table == record:
            alias = record_alias
        else:
            alias = built.alias_of(held.source_table)
        out.append(f"{alias}.{quote(source.name)} AS {name}")
    return out


def _criteria_aliases(built: _From, record: str, record_alias: str,
                      chosen: list[Any]) -> dict[str, str]:
    aliases = {c.table: built.alias_of(c.table) for c in chosen
               if c.selected and c.table != record}
    aliases[record] = record_alias
    return aliases


def _primary_scope(plan: "SourcePlan", graph: KeyGraph, statement: _Statement,
                   child_alias: str) -> str | None:
    """EXISTS on the primary's selected criteria, the child's record matched
    to its primary by the columns the two join on; None when the primary
    has none selected."""
    chosen = [c for c in plan.criteria.get("primary", []) if c.selected]
    if not chosen:
        return None
    pairs = structure_rules.join_pairs(plan, graph)
    if not pairs:
        raise SqlRefused("the child does not join the primary, so it cannot follow its "
                         "criteria")
    edges = structure_rules.primary_edges(plan, graph)
    stated = structure_rules.stated_to_primary(plan)
    named = sum(e.kind == "stated" or (tuple(e.child_columns), tuple(e.parent_columns)) in stated
                for e in edges)
    if len(edges) > 1 and named != 1:
        said = "; ".join(", ".join(e.child_columns) for e in edges)
        raise SqlRefused(
            f"the child links to the primary by {len(edges)} keys ({said}), so which primary "
            f"record each child follows is not known: state the one join it follows, then "
            f"build again.")
    record = plan.record_kinds["primary"]["table"]
    built = _From(statement, "p", record)
    for criterion in chosen:
        if criterion.table == record or criterion.table in built.by_table:
            continue
        level = next((lv for lv in _levels(plan, "primary")
                      if any(j.to_table == criterion.table for j in lv.path)), None)
        if level is None:
            raise SqlRefused(f"{criterion.id}: no selected level of the primary reaches "
                             f"{criterion.table}")
        upto = next(i for i, j in enumerate(level.path) if j.to_table == criterion.table)
        built.reach(level.path[:upto + 1], built.start, record, inside_exists=True)
    on = " AND ".join(
        f"{built.start}.{quote(_column(graph, p[0], p[1]).name)} = "
        f"{child_alias}.{quote(_column(graph, c[0], c[1]).name)}" for p, c in pairs)
    try:
        where = criteria_rules.where_sql(
            chosen, _criteria_aliases(built, record, built.start, chosen), graph)
    except ValueError as exc:
        raise SqlRefused(str(exc)) from exc
    inner = built.sql().replace("\n", "\n        ")
    return f"EXISTS (\n        SELECT 1\n        {inner}\n        WHERE {on} AND {where}\n    )"


def _needed(plan: "SourcePlan", denorm: DenormTable) -> dict[str, set[str]]:
    """Every column the block reads from each table: what it selects, what
    its joins leave and enter a table by, and what the criteria (its own and,
    for the child, the primary's) compare."""
    out: dict[str, set[str]] = {}
    for held in denorm.columns:
        out.setdefault(held.source_table, set()).add(held.source_column)
    for role in [denorm.role] + (["primary"] if denorm.role == "child" else []):
        for level in plan.levels(role):
            if not level.selected:
                continue
            for join in level.path:
                out.setdefault(join.from_table, set()).update(join.from_columns)
                out.setdefault(join.to_table, set()).update(join.to_columns)
        for criterion in plan.criteria.get(role, []):
            if criterion.selected:
                out.setdefault(criterion.table, set()).update(criterion.columns)
    return out


def store_layout(plan: "SourcePlan", graph: KeyGraph) -> list[dict[str, Any]]:
    """Each denorm table as built: its columns with their store types, privacy
    and what each serves, its key at its grain, and (for the child) the
    column pairs it meets the primary's table on. What a load creates,
    compares the store against and checks the rows by."""
    names = {d.role: _store_name(d.name, "a denorm table") for d in plan.structure}
    out = []
    for denorm in plan.structure:
        name = names[denorm.role]
        columns = [{"name": _store_name(c.name, f"{name} column"),
                    "type": denorm_type(c, graph), "privacy": c.privacy, "serves": c.serves}
                   for c in denorm.columns]
        meets: list[list[str]] = []
        if denorm.role == "child" and "primary" in names:
            primary = next(d for d in plan.structure if d.role == "primary")
            for (p_table, p_column), (c_table, c_column) in structure_rules.join_pairs(plan,
                                                                                  graph):
                ours = [c for c in denorm.columns
                        if (c.source_table, c.source_column) == (c_table, c_column)]
                theirs = [c for c in primary.columns
                          if (c.source_table, c.source_column) == (p_table, p_column)]
                if ours and theirs:
                    meets.append([theirs[0].name, ours[0].name])
        out.append({"name": name, "role": denorm.role, "grain": denorm.grain,
                    "key": [c.name for c in _role_key(plan, denorm)], "columns": columns,
                    "primary": names.get("primary") if meets else None, "meets": meets})
    return out


def source_reads(plan: "SourcePlan", graph: KeyGraph) -> dict[str, dict[str, Any]]:
    """Every source column the extract reads, by table (its schema and name,
    and each column's type as the source held it when the plan was built:
    [type, max_length, precision, scale]): what a load checks the source
    against before it reads."""
    wanted: dict[str, set[str]] = {}
    for denorm in plan.structure:
        for table, columns in _needed(plan, denorm).items():
            wanted.setdefault(table, set()).update(columns)
        for held in denorm.columns:
            table, column = read_from(plan, denorm, held)
            wanted.setdefault(table, set()).add(column)
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(wanted):
        table = _table(graph, name)
        held = out[name] = {"schema": table.schema_name, "table": table.table_name,
                            "columns": {}}
        for column in sorted(wanted[name]):
            found = _column(graph, name, column)
            held["columns"][found.name] = [found.type_name, found.max_length,
                                           found.precision, found.scale]
    return out


def block_sql(plan: "SourcePlan", denorm: DenormTable, graph: KeyGraph) -> str:
    """One denorm table's SELECT."""
    role = denorm.role
    kind = plan.record_kinds[role]
    record = kind["table"]
    picks = {}
    for pick in denorm.pick:
        _column(graph, pick["table"], pick["start"])
        _column(graph, pick["table"], pick["end"])
        picks[pick["table"]] = dict(pick)
    if role == "child":
        primary = next((t for t in plan.structure if t.role == "primary"), None)
        for pick in (primary.pick if primary is not None else []):
            picks.setdefault(pick["table"], dict(pick))
    statement = _Statement(graph, picks, _needed(plan, denorm),
                           accepted=set(plan.grain_accepted.get(role, [])))
    built, record_alias = _record_from(plan, role, statement, "t")
    columns = _select(plan, denorm, built, record_alias, graph)
    conditions = []
    own = [c for c in plan.criteria.get(role, []) if c.selected]
    try:
        where = criteria_rules.where_sql(
            own, _criteria_aliases(built, record, record_alias, own), graph)
    except ValueError as exc:
        raise SqlRefused(str(exc)) from exc
    if where:
        conditions.append(where)
    if role == "child":
        scope = _primary_scope(plan, graph, statement, record_alias)
        if scope:
            conditions.append(scope)
    parts = []
    if statement.ctes:
        parts.append("WITH " + ",\n".join(body for _, body in statement.ctes.values()))
    parts.append("SELECT " + ",\n       ".join(columns))
    parts.append(built.sql())
    if conditions:
        parts.append("WHERE " + "\n  AND ".join(conditions))
    parts.append(f"OPTION (MAXDOP {MAXDOP})")
    return "\n".join(parts) + ";"


def privacy_summary(plan: "SourcePlan") -> list[str]:
    """What each table keeps, withholds and writes as a surrogate, by name."""
    out = []
    for denorm in plan.structure:
        by = {p: [c.name for c in denorm.columns if c.privacy == p]
              for p in structure_rules.PRIVACY}
        out.append(f"{denorm.name}: {len(by['kept'])} kept, {len(by['withheld'])} withheld "
                   f"(written NULL), {len(by['surrogate'])} surrogate (keyed with "
                   f"SDG_EXTRACT_KEY).")
        if by["withheld"]:
            out.append(f"  withheld: {', '.join(by['withheld'])}")
        if by["surrogate"]:
            out.append(f"  surrogate: {', '.join(by['surrogate'])}")
    return out


def extract_sql(plan: "SourcePlan", graph: KeyGraph, *, approved_by: str, approved_at: Any,
                plan_hash: str) -> str:
    """The pack's extract for the source: a header, then one block per denorm
    table, one statement in each."""
    if not re.fullmatch(r"[0-9a-f]{64}", plan_hash or ""):
        raise SqlRefused("the plan's hash is not a SHA-256")
    lines = [
        f"-- {_comment(plan.source)}: what an extract reads out of the source into the "
        f"{_comment(plan.pack.get('id'))} pack's tables.",
        "-- Written by the source accelerator from the approved plan; edit it here, as any",
        "-- pack's extract is edited. The runner writes every column the plan did not",
        "-- keep as NULL whatever this file selects (pack.json extract.kept, withheld).",
        "--",
        f"-- Source: {_comment(plan.source)}",
        f"-- Plan: {plan_hash}",
        f"-- Approved by {_comment(approved_by)} on {_comment(approved_at)}",
        "-- Privacy:"]
    lines += [f"--   {_comment(line)}" for line in privacy_summary(plan)]
    lines.append("")
    for denorm in plan.structure:
        name = _store_name(denorm.name, "a denorm table")
        lines += [f"-- table: {name}", block_sql(plan, denorm, graph), ""]
    return "\n".join(lines)


# --- the readme --------------------------------------------------------------------

def readme_txt(plan: "SourcePlan", *, folder_name: str, packs_dir: str) -> str:
    """How the person installs the pack, step by step."""
    pack_id = plan.pack.get("id") or folder_name
    source = plan.source
    tables = ", ".join(t.name for t in plan.structure)
    return "\n".join([
        f"{plan.pack.get('name') or pack_id} ({pack_id}): a business pack written by the "
        f"source accelerator",
        f"from the approved plan for the source {source}.",
        "",
        f"It holds the tables pattern analysis reads ({tables}), the SQL that creates",
        "them, the extract that fills them from the source, and the words the screens use.",
        "Pattern analysis is offered; writing files from a pattern is not.",
        "",
        "To install it:",
        "",
        "1. In the .env file, point SDM at this pack and the source:",
        f"     SDG_PACKS_DIR={packs_dir}",
        f"     SDG_PACK={pack_id}",
        f"     SDG_EXTRACT_SOURCE={source}",
        f"   The extract reads the source through SDG_SOURCE_{source}_* (as Source",
        "   setup reads it: the source's own server, database and login). It writes",
        "   through SDG_DENORM_*. It runs as the Load tab runs: the build checked",
        "   against the store, the tables loaded beside the live ones and swapped in.",
        "   Set SDG_EXTRACT_KEY (16 characters at least, kept private, the same for",
        "   every extract) when any column is written as a surrogate.",
        "",
        "2. Create the tables in the SDM platform database: run",
        "     scripts\\customer\\setup_tables.cmd",
        "   or hand sql\\store_tables.sql to the DBA. It refuses to run in the source or",
        "   a protected database, and creates only what is missing.",
        "",
        "3. Fill them: run",
        "     scripts\\customer\\extract.cmd",
        f"   It reads extract\\{source}.sql. The extract is a full read of every table it",
        "   names, each statement on at most two of the source's cores, and it gives up",
        "   on a lock held too long rather than wait: run it off-peak, or against a",
        "   replica of the source.",
        "",
        "4. After the first extract, hand sql\\store_indexes.sql to the DBA (or run it",
        "   in the SDM platform database): the indexes pattern analysis reads by. It",
        "   has the same guards, and makes only the indexes missing.",
        "",
        "5. Restart SDM, so it reads the new pack.",
        "",
        "Good to know:",
        "- A criterion on a linked table (one a level is reached through) acts as an",
        "  inner join: it keeps only the records that have a row there meeting it; the",
        "  others are left out of the extract, not kept with their levels empty.",
        "- The child's records follow the primary's criteria: the records of a primary",
        "  record left out (one whose span has ended, say) are left out too.",
        "- Building again adds a new column to a table made before (NULL). A column",
        "  whose type changed, or one no longer used, is the DBA's to alter or drop.",
        f"- Building again moves this folder aside first, as {pack_id}.previous-<date and",
        "  time>, beside it, with any edits made in it. A build never removes one: old",
        "  ones can be removed by hand once they are no longer wanted.",
        "",
    ])

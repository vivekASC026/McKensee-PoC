"""A renamed copy of the demo source, with no foreign keys: SDMT_RENAMED_SRC.

    python -m seed.renamed_source                 (copies SDG_DB_SOURCE)
    python -m seed.renamed_source --from NAME     (copies another source)
    python -m seed.renamed_source --allow-large   (copies tables over MAX_COPY_ROWS too)
    python -m seed.renamed_source --drop          (drops SDMT_RENAMED_SRC)

The accelerator has to find a source's structure from its data, not from
names it has seen before. This builds the test of that: every table and
column of the source is renamed, its rows copied, and no foreign key
declared, so `sdg accelerate landscape SDMT_RENAMED_SRC` must infer every key
and `propose` must still find the same record kinds and levels.

The rename is token-wise (sdg.discovery.classify.tokenize): each name token
maps to a made-up word chosen by a stable hash, so a token two names share
stays shared (a key and the column pointing at it keep matching names) and no
source-specific name survives. Generic tokens -- the words the tool reads a
column's meaning from (ID, CK, DT, NAME, DESC, EFF, TERM, TYPE, the base
catalog's aliases) -- are kept, so what is tested is structure, not whether a
word for "date" is recognized. The map is written to
tests/fixtures/accelerator/renamed-map.json:
    {"tables": {old: new}, "columns": {"old.table.column": new}}

The copy is faithful to what the accelerator reads: types, identity columns
with their seed and step, computed columns (their definitions renamed),
primary keys clustered or not as the source has them, unique constraints and
the other indexes. A filtered index is left out and said so, since its filter
is not part of what is read.

The source is only read: its structure through the accelerator's read-only
source registry, its rows by INSERT ... SELECT run in SDMT_RENAMED_SRC. A
protected source is never copied from, and a table over MAX_COPY_ROWS rows
only with --allow-large. Only SDMT_RENAMED_SRC is created or dropped, on a
demo machine whose server is this machine (sdg.config.LOCAL_SERVERS) only,
and only when it is the database this script made (it carries a marker
saying so).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyodbc

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdg.accelerator import hierarchy, landscape, source_read, sources  # noqa: E402
from sdg.catalog.loader import load_catalog  # noqa: E402
from sdg.config import (  # noqa: E402
    LOCAL_SERVERS, ProtectedDatabase, get_settings, not_a_demo_machine, refuse_protected,
)
from sdg.discovery import inference  # noqa: E402
from sdg.discovery.classify import tokenize  # noqa: E402
from sdg.discovery.model import PhysicalModel, Table  # noqa: E402
from sdg.discovery.run import discover  # noqa: E402
from sdg.mapping import matchers  # noqa: E402

TARGET = "SDMT_RENAMED_SRC"
MAP_FILE = ROOT / "tests" / "fixtures" / "accelerator" / "renamed-map.json"
# Set on the database when it is made; --drop and a rebuild drop only a
# database carrying it.
MARKER_NAME = "sdm_made_by"
MARKER_VALUE = "seed.renamed_source"
SYSTEM = {"MASTER", "MODEL", "MSDB", "TEMPDB"}
# A source table holding more rows than this is copied only with --allow-large:
# the copy is a test fixture, and a production-sized table is not one.
MAX_COPY_ROWS = 5_000_000
# SQL Server's longest name.
MAX_NAME = 128
# SQL Server's own types: a column of any other type (an alias type made in
# the source) cannot be recreated from its name alone.
SYSTEM_TYPES = frozenset({
    "bigint", "binary", "bit", "char", "date", "datetime", "datetime2", "datetimeoffset",
    "decimal", "float", "geography", "geometry", "hierarchyid", "image", "int", "money",
    "nchar", "ntext", "numeric", "nvarchar", "real", "rowversion", "smalldatetime", "smallint",
    "smallmoney", "sql_variant", "sysname", "text", "time", "timestamp", "tinyint",
    "uniqueidentifier", "varbinary", "varchar", "xml",
})

# Made-up words: two consonant-vowel syllables, like the four-letter prefixes
# many sources use, and nothing a real name is likely to be.
_CONSONANTS = "BDFGKLMNPRSTVZ"
_VOWELS = "AEIOU"
VOCABULARY = [a + b + c + d for a in _CONSONANTS for b in _VOWELS
              for c in _CONSONANTS for d in _VOWELS]


class Refused(ValueError):
    """A database this script may not build, drop or copy from."""


# --- the rename map (pure: a model in, a map out) -------------------------

def generic_tokens() -> set[str]:
    """The tokens kept as they are: the words key inference, the mapping
    scorer, the landscape and the proposer read a column's meaning from, and
    every token of the base catalog's aliases."""
    kept = set(inference.GENERIC_TOKENS) | set(matchers.GENERIC_TOKENS)
    kept |= (hierarchy.LABEL_TOKENS | hierarchy.KIND_TOKENS | hierarchy.INDICATOR_TOKENS
             | hierarchy.STATUS_TOKENS | hierarchy.TENURE_TOKENS)
    kept |= (landscape.START_TOKENS | landscape.END_TOKENS | landscape.BIRTH_TOKENS
             | landscape.DENY_TOKENS)
    for element in load_catalog(landscape.BASE_CATALOG).elements:
        for alias in [element.name, *element.aliases]:
            kept.update(tokenize(alias))
    return kept


def _names(model: PhysicalModel) -> list[str]:
    return [t.table_name for t in model.tables] + [c.name for _, c in model.columns()]


def token_map(model: PhysicalModel) -> dict[str, str]:
    """Each non-generic token of the model's table and column names, to a
    word of VOCABULARY picked by a stable hash of the token. Tokens are taken
    in sorted order and a word already taken, generic, or itself a token of
    the model moves to the next word, so the map is one-to-one and the same
    for the same names."""
    generic = generic_tokens()
    tokens = sorted({tok for name in _names(model) for tok in tokenize(name)})
    barred = generic | set(tokens)
    taken: set[str] = set()
    out: dict[str, str] = {}
    for token in tokens:
        if token in generic:
            continue
        at = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        for step in range(len(VOCABULARY)):
            word = VOCABULARY[(at + step) % len(VOCABULARY)]
            if word not in taken and word not in barred:
                break
        else:
            raise Refused(f"the vocabulary of {len(VOCABULARY)} words ran out")
        taken.add(word)
        out[token] = word
    return out


def _renamed(name: str, tokens: dict[str, str]) -> str:
    return "_".join(tokens.get(tok, tok) for tok in tokenize(name))


def rename_map(model: PhysicalModel, *, strict: bool = True) -> dict[str, dict[str, str]]:
    """{"tables": {schema.old: schema.new}, "columns": {schema.old.column: new}}.

    Refused when two names would come out the same, or -- when `strict`, as
    for the database this script builds -- a name would come out as it was
    (made only of generic tokens): no source name survives. Not strict, a
    name of generic tokens alone is kept as it is (a test renaming another
    source in memory)."""
    tokens = token_map(model)
    tables: dict[str, str] = {}
    columns: dict[str, str] = {}
    problems: list[str] = []
    for table in model.tables:
        new_table = _renamed(table.table_name, tokens)
        if strict and new_table.upper() == table.table_name.upper():
            problems.append(f"table {table.name} keeps its name")
        tables[table.name] = f"{table.schema_name}.{new_table}"
        seen: dict[str, str] = {}
        for column in table.columns:
            new = _renamed(column.name, tokens)
            if strict and new.upper() == column.name.upper():
                problems.append(f"column {table.name}.{column.name} keeps its name")
            if new.upper() in seen:
                problems.append(f"{table.name}: {seen[new.upper()]} and {column.name} "
                                f"would both be {new}")
            seen[new.upper()] = column.name
            columns[f"{table.name}.{column.name}"] = new
    if len({v.upper() for v in tables.values()}) != len(tables):
        problems.append("two tables would get the same name")
    if problems:
        raise Refused("the rename is not one-to-one or leaves a source name: "
                      + "; ".join(problems))
    return {"tables": tables, "columns": columns}


# --- SQL ------------------------------------------------------------------

def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


_TYPE = re.compile(r"[a-z][a-z0-9_]{0,30}")
_COLLATION = re.compile(r"[A-Za-z0-9_]{1,128}")


def sql_type(column: Any) -> str:
    """The column's type as CREATE TABLE takes it, from the model."""
    name = (column.type_name or "").lower()
    if not _TYPE.fullmatch(name):
        raise Refused(f"{column.name}: {column.type_name!r} is not a type name")
    if name not in SYSTEM_TYPES:
        raise Refused(f"{column.name}: {column.type_name!r} is an alias type made in the source; "
                      f"the copy cannot recreate it from its name")
    if name in {"timestamp", "rowversion"}:
        # The database fills these itself; the copy keeps the bytes.
        return "binary(8)"
    if name in {"binary", "varbinary"}:
        size = column.max_length
        if size is None or size < 0:
            if name == "binary":
                # binary has a fixed length; there is no binary(max).
                raise Refused(f"{column.name}: binary with no length in the model")
            return "varbinary(max)"
        return f"{name}({int(size)})"
    if name in {"datetime2", "time", "datetimeoffset"} and column.scale is not None:
        return f"{name}({int(column.scale)})"
    described = column.type_description()
    if column.is_character and column.collation:
        if not _COLLATION.fullmatch(column.collation):
            raise Refused(f"{column.name}: {column.collation!r} is not a collation name")
        described += f" COLLATE {column.collation}"
    return described


def object_name(prefix: str, name: str, suffix: str = "") -> str:
    """A constraint's or an index's name, MAX_NAME characters at most: a long
    one is cut and ends in a short hash of the whole, so two stay apart."""
    whole = f"{prefix}{name}{suffix}"
    if len(whole) <= MAX_NAME:
        return whole
    tag = "_" + hashlib.sha256(whole.encode("utf-8")).hexdigest()[:8]
    return whole[:MAX_NAME - len(tag)] + tag


@dataclass
class Index:
    """A non-unique rowstore index of the source, by its key columns."""
    name: str
    clustered: bool
    keys: list[tuple[str, bool]] = field(default_factory=list)  # (column, descending)
    included: list[str] = field(default_factory=list)
    filtered: bool = False


@dataclass
class Extras:
    """What the copy needs beyond the model: identity seeds and steps,
    persisted computed columns, whether each primary key is clustered, and
    the non-unique indexes. Keyed by the model's table name (schema.table)."""
    identity: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    persisted: set[tuple[str, str]] = field(default_factory=set)
    clustered_key: dict[str, bool] = field(default_factory=dict)
    indexes: dict[str, list[Index]] = field(default_factory=dict)


IDENTITY_SQL = """
SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
       CAST(ic.seed_value AS decimal(38, 0)) AS seed_value,
       CAST(ic.increment_value AS decimal(38, 0)) AS increment_value
FROM sys.identity_columns AS ic
JOIN sys.tables AS t ON t.object_id = ic.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
"""

PERSISTED_SQL = """
SELECT s.name AS schema_name, t.name AS table_name, cc.name AS column_name
FROM sys.computed_columns AS cc
JOIN sys.tables AS t ON t.object_id = cc.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0 AND cc.is_persisted = 1
"""

INDEX_SQL = """
SELECT s.name AS schema_name, t.name AS table_name, i.name AS index_name,
       i.type AS index_type, i.is_primary_key, i.is_unique, i.has_filter,
       c.name AS column_name, ic.key_ordinal, ic.is_descending_key, ic.is_included_column
FROM sys.indexes AS i
JOIN sys.tables AS t ON t.object_id = i.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0 AND i.type IN (1, 2) AND i.is_hypothetical = 0 AND i.is_disabled = 0
ORDER BY s.name, t.name, i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id
"""


def read_extras(registry: Any) -> Extras:
    """The identity seeds, persisted computed columns and indexes of the
    source, through the accelerator's read-only path."""
    extras = Extras()
    for row in source_read.query(registry, IDENTITY_SQL):
        key = (f"{row['schema_name']}.{row['table_name']}", row["column_name"])
        extras.identity[key] = (int(row["seed_value"] or 1), int(row["increment_value"] or 1))
    for row in source_read.query(registry, PERSISTED_SQL):
        extras.persisted.add((f"{row['schema_name']}.{row['table_name']}", row["column_name"]))
    held: dict[tuple[str, str], Index] = {}
    for row in source_read.query(registry, INDEX_SQL):
        table = f"{row['schema_name']}.{row['table_name']}"
        clustered = int(row["index_type"]) == 1
        if row["is_primary_key"]:
            extras.clustered_key[table] = clustered
            continue
        if row["is_unique"]:
            continue  # a unique constraint or index: in the model already
        index = held.get((table, row["index_name"]))
        if index is None:
            index = Index(name=row["index_name"], clustered=clustered,
                          filtered=bool(row["has_filter"]))
            held[(table, row["index_name"])] = index
            extras.indexes.setdefault(table, []).append(index)
        if row["is_included_column"]:
            index.included.append(row["column_name"])
        else:
            index.keys.append((row["column_name"], bool(row["is_descending_key"])))
    return extras


# A name in a definition: bracketed (its inside in group 1) or bare.
_NAME_IN_DEFINITION = re.compile(r"\[((?:[^\]]|\]\])+)\]|(?<![\w@#$])[A-Za-z_][A-Za-z0-9_@#$]*")
_QUOTED = re.compile(r"(N?'(?:[^']|'')*')")


def rename_definition(definition: str, column_name: dict[str, str]) -> str:
    """A computed column's definition with each of the table's column names,
    bracketed or bare, renamed; quoted text and every other word kept."""
    by_upper = {old.upper(): new for old, new in column_name.items()}

    def renamed(found: re.Match) -> str:
        name = found.group(1).replace("]]", "]") if found.group(1) is not None else found.group(0)
        new = by_upper.get(name.upper())
        return quote(new) if new else found.group(0)

    # _QUOTED.split alternates plain text and quoted text; only the plain is read.
    parts = _QUOTED.split(definition)
    return "".join(part if number % 2 else _NAME_IN_DEFINITION.sub(renamed, part)
                   for number, part in enumerate(parts))


def create_table_sql(table: Table, names: dict[str, dict[str, str]],
                     extras: Extras | None = None) -> str:
    """CREATE TABLE with the model's types, identity columns, computed
    columns, primary key and unique constraints, renamed, and no foreign key."""
    extras = extras or Extras()
    schema, _, new_name = names["tables"][table.name].partition(".")
    column_name = {c.name: names["columns"][f"{table.name}.{c.name}"] for c in table.columns}
    lines = []
    for column in table.columns:
        new = quote(column_name[column.name])
        if column.is_computed:
            if not column.computed_definition:
                raise Refused(f"{table.name}.{column.name} is computed but its definition was "
                              f"not read")
            persisted = " PERSISTED" if (table.name, column.name) in extras.persisted else ""
            lines.append(f"    {new} AS {rename_definition(column.computed_definition, column_name)}"
                         f"{persisted}")
            continue
        null = "NOT NULL" if (not column.nullable or column.name in table.primary_key) else "NULL"
        identity = ""
        if column.is_identity:
            seed, step = extras.identity.get((table.name, column.name), (1, 1))
            identity = f" IDENTITY({int(seed)}, {int(step)})"
        lines.append(f"    {new} {sql_type(column)}{identity} {null}")
    if table.primary_key:
        keys = ", ".join(quote(column_name[c]) for c in table.primary_key)
        kind = "CLUSTERED" if extras.clustered_key.get(table.name, True) else "NONCLUSTERED"
        lines.append(f"    CONSTRAINT {quote(object_name('PK_', new_name))} PRIMARY KEY {kind} "
                     f"({keys})")
    unique = [u for u in table.unique_constraints
              if not u.is_primary_key and list(u.columns) != list(table.primary_key)]
    for number, constraint in enumerate(unique, start=1):
        keys = ", ".join(quote(column_name[c]) for c in constraint.columns)
        lines.append(f"    CONSTRAINT {quote(object_name('UQ_', new_name, f'_{number}'))} "
                     f"UNIQUE NONCLUSTERED ({keys})")
    return (f"CREATE TABLE {quote(schema)}.{quote(new_name)} (\n" + ",\n".join(lines) + "\n)")


def create_index_sql(table: Table, names: dict[str, dict[str, str]], index: Index,
                     number: int) -> str | None:
    """CREATE INDEX for one of the source's non-unique indexes, renamed;
    None for a filtered one (its filter is not read)."""
    if index.filtered or not index.keys:
        return None
    schema, _, new_name = names["tables"][table.name].partition(".")

    def renamed(column: str) -> str:
        return quote(names["columns"][f"{table.name}.{column}"])

    keys = ", ".join(f"{renamed(c)} {'DESC' if desc else 'ASC'}" for c, desc in index.keys)
    include = (f" INCLUDE ({', '.join(renamed(c) for c in index.included)})"
               if index.included else "")
    kind = "CLUSTERED" if index.clustered else "NONCLUSTERED"
    return (f"CREATE {kind} INDEX {quote(object_name('IX_', new_name, f'_{number}'))} "
            f"ON {quote(schema)}.{quote(new_name)} ({keys}){include}")


def copy_rows_sql(table: Table, names: dict[str, dict[str, str]], source_db: str) -> str:
    """INSERT ... SELECT from the source's table into its renamed copy, run
    in the target: the source is named in the FROM only, and only read.
    Computed columns are left to the copy to compute; identity values are
    copied as they are."""
    schema, _, new_name = names["tables"][table.name].partition(".")
    target = f"{quote(schema)}.{quote(new_name)}"
    columns = [c for c in table.columns if not c.is_computed]
    into = ", ".join(quote(names["columns"][f"{table.name}.{c.name}"]) for c in columns)
    read = ", ".join(quote(c.name) for c in columns)
    insert = (f"INSERT INTO {target} WITH (TABLOCK) ({into})\n"
              f"SELECT {read} FROM {quote(source_db)}.{quote(table.schema_name)}."
              f"{quote(table.table_name)}")
    if not any(c.is_identity for c in table.columns):
        return insert
    return (f"SET IDENTITY_INSERT {target} ON;\n{insert};\n"
            f"SET IDENTITY_INSERT {target} OFF")


# --- the guard ------------------------------------------------------------

def is_local(server: str | None) -> bool:
    """Whether a server name is this machine (an instance or port after it
    allowed), by sdg.config.LOCAL_SERVERS."""
    host = (server or "").strip().lower().split("\\")[0].split(",")[0]
    return host.removeprefix("tcp:") in LOCAL_SERVERS


def check(source_db: str | None = None) -> None:
    """Refuse before anything is created or dropped: off a demo machine, on a
    server that is not this machine (CREATE and DROP DATABASE never run on a
    shared one), or when the target is protected, a system database, one this
    installation is configured to use, or the source itself."""
    reason = not_a_demo_machine()
    if reason:
        raise Refused(f"This machine is {reason}: {TARGET} is created and dropped here. "
                      f"Nothing was changed.")
    server = get_settings().mssql.server
    if not is_local(server):
        raise Refused(f"The configured server {server} is not this machine: {TARGET} is "
                      f"created and dropped on a local server only. Nothing was changed.")
    refuse_protected(TARGET, "drop and rebuild")
    settings = get_settings()
    configured = {settings.source_db, settings.ref_db, settings.denorm_db, settings.target_db}
    if TARGET in SYSTEM or TARGET in {d.upper() for d in configured if d}:
        raise Refused(f"{TARGET} is a system database or one this installation is configured "
                      f"to use; it is never dropped.")
    if source_db is not None and source_db.strip().upper() == TARGET:
        raise Refused(f"{TARGET} cannot be its own source.")


def _connect(database: str) -> pyodbc.Connection:
    return pyodbc.connect(get_settings().mssql.connection_string(database), autocommit=True)


def _exists(master: pyodbc.Connection) -> bool:
    return master.execute("SELECT DB_ID(?) AS id", TARGET).fetchone()[0] is not None


def _is_ours(master: pyodbc.Connection) -> bool:
    row = master.execute(
        f"SELECT CAST(value AS nvarchar(200)) FROM {quote(TARGET)}.sys.extended_properties "
        f"WHERE class = 0 AND name = ?", MARKER_NAME).fetchone()
    return row is not None and row[0] == MARKER_VALUE


def _drop_ours(master: pyodbc.Connection) -> bool:
    """Drop the target when it is there and carries the marker; refuse when
    it is there without it. Whether anything was dropped."""
    if not _exists(master):
        return False
    if not _is_ours(master):
        raise Refused(f"{TARGET} is there but was not made by this script (no "
                      f"{MARKER_NAME} marker); it was left alone.")
    master.execute(f"ALTER DATABASE {quote(TARGET)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                   f"DROP DATABASE {quote(TARGET)}")
    return True


def drop() -> bool:
    """Drop SDMT_RENAMED_SRC, and nothing else. Whether it was there."""
    check()
    with _connect("master") as master:
        return _drop_ours(master)


# --- the build ------------------------------------------------------------

def refuse_large(model: PhysicalModel, allow_large: bool = False) -> None:
    """Refuse a source with a table over MAX_COPY_ROWS rows, unless allowed."""
    large = sorted(f"{t.name} ({t.row_count:,} rows)" for t in model.tables
                   if t.row_count > MAX_COPY_ROWS)
    if large and not allow_large:
        raise Refused(f"{len(large)} source tables hold more than {MAX_COPY_ROWS:,} rows: "
                      f"{', '.join(large[:5])}. The copy is a test fixture; pass --allow-large "
                      f"to copy them anyway. Nothing was changed.")


def read_model(source: str, *, allow_large: bool = False) -> tuple[PhysicalModel, str, Extras]:
    """The source's structure, read through the accelerator's read-only
    registry (declared keys are not needed: none is copied), its database,
    and what the copy needs beyond the model. Refused, before anything is
    read, for a protected source, one on another server, or a non-local one."""
    settings, database = sources.source_settings(source)
    refuse_protected(source, "copy rows from")
    refuse_protected(database, "copy rows from")
    here = get_settings().mssql
    if (settings.server.lower(), settings.port) != (here.server.lower(), here.port):
        raise Refused(f"{source} is on {settings.server}:{settings.port}, not on the configured "
                      f"server {here.server}:{here.port}; the copy reads it from there.")
    check(database)
    registry = sources.registry_for(source, allow_write_login=True)
    model = discover(registry, sources.SOURCE, infer_keys=False).model
    if not model.tables:
        raise Refused(f"{source} has no tables to copy")
    refuse_large(model, allow_large)
    return model, database, read_extras(registry)


def build(source: str, map_path: Path = MAP_FILE, *, allow_large: bool = False) -> dict[str, int]:
    """Create SDMT_RENAMED_SRC from the source and write the map. The rows
    copied, by renamed table."""
    check(source)
    model, source_db, extras = read_model(source, allow_large=allow_large)
    names = rename_map(model)
    with _connect("master") as master:
        _drop_ours(master)
        master.execute(f"CREATE DATABASE {quote(TARGET)}")
        master.execute(f"ALTER DATABASE {quote(TARGET)} SET RECOVERY SIMPLE")
    counts: dict[str, int] = {}
    with _connect(TARGET) as target:
        here = target.execute("SELECT DB_NAME()").fetchone()[0]
        if here != TARGET:
            raise Refused(f"connected to {here}, not {TARGET}; nothing was written")
        target.execute("EXEC sys.sp_addextendedproperty @name = ?, @value = ?",
                       MARKER_NAME, MARKER_VALUE)
        for schema in sorted({t.schema_name for t in model.tables}):
            if target.execute("SELECT SCHEMA_ID(?)", schema).fetchone()[0] is None:
                target.execute("DECLARE @s nvarchar(400) = N'CREATE SCHEMA ' + QUOTENAME(?); "
                               "EXEC (@s)", schema)
        for table in model.tables:
            target.execute(create_table_sql(table, names, extras))
        for table in model.tables:
            new = names["tables"][table.name]
            try:
                target.execute(copy_rows_sql(table, names, source_db))
            except pyodbc.Error as exc:
                # Only the error's kind and codes: the driver's words can
                # quote a row's value.
                raise Refused(f"copying {table.name} into {new} failed: "
                              f"{source_read.error_said(exc)}") from exc
            schema, _, new_name = new.partition(".")
            counts[new] = target.execute(
                f"SELECT COUNT_BIG(*) FROM {quote(schema)}.{quote(new_name)}").fetchone()[0]
        # Indexes after the rows: built once over them, not kept up row by row.
        for table in model.tables:
            for number, index in enumerate(extras.indexes.get(table.name, []), start=1):
                statement = create_index_sql(table, names, index, number)
                if statement is None:
                    print(f"left out {table.name} index {index.name}: a filtered index")
                    continue
                target.execute(statement)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(json.dumps(names, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", default=None,
                        help="the source to copy, by its accelerator name (SDG_DB_SOURCE)")
    parser.add_argument("--map", dest="map_path", type=Path, default=MAP_FILE,
                        help="where the rename map is written")
    parser.add_argument("--drop", action="store_true", help=f"drop {TARGET} and stop")
    parser.add_argument("--allow-large", action="store_true",
                        help=f"copy source tables over {MAX_COPY_ROWS:,} rows too")
    args = parser.parse_args(argv)
    try:
        if args.drop:
            print(f"dropped {TARGET}" if drop() else f"{TARGET} is not there")
            return 0
        source = sources.plain_name(args.source or get_settings().source_db)
        counts = build(source, args.map_path, allow_large=args.allow_large)
    except (Refused, ProtectedDatabase, sources.SourceError) as refused:
        parser.error(str(refused))
    for table, count in counts.items():
        print(f"{table:40} {count:>10,}")
    print(f"wrote {args.map_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Load a built pack's tables: create them in the store, read the source into
them, and check what landed.

Phase 2 ends with a pack folder and a runbook. This puts the runbook's two
steps on the screen. The tables are created from the build's own
store_tables.sql, and the rows come from the extract, the one path that reads
a source SDM protects (sdg.extract.run.extract). The extract is handed the
built pack itself, so a load needs no restart; analysis does.

What runs on the store from a file runs only as built: store_tables.sql,
store_indexes.sql and the extract SQL must match the hashes the build wrote
into its marker, checked on the text that runs, read once. An edited store
script is the DBA's to run; an edited extract runs only by hand, through the
extract command, as the health extract is edited today. Every statement must
also read and nothing else (source_read.refuse_unless_read), and every row is
nulled by the pack's kept and withheld columns. A load needs the plan
approved now, as a build does.

Before a load reads a row, the source is asked whether every column the build
reads is still there with the type it had. A load into a table that lacks a
column is refused. Afterwards, the tables are checked by counts alone: rows,
the key unique at its grain, each level and feature filled, and lines that
meet their record. No value is read back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pyodbc

from sdg import packs
from sdg.accelerator import approval, builder, sources, sqlgen
from sdg.accelerator.source_read import query, refuse_unless_read
from sdg.discovery.connections import ConnectionRegistry

log = logging.getLogger(__name__)

STORE_TABLES = "sql/store_tables.sql"
STORE_INDEXES = "sql/store_indexes.sql"
# Problems a refusal names before it says "and N more".
SHOWN = 8
# The extract_log rows shown, newest first.
LAST_LOADS = 12
# A staged load: each table is filled beside the live one, then the two are
# swapped by name in one short transaction, so analysis reads the old rows
# until the new ones are all in place.
STAGED, RETIRED = "__new", "__old"
NOT_PLACED = ("Loaded, then not put in place: another table did not load, or the load was "
              "stopped. Its rows are as they were.")
# Columns whose filled share a load checks: what analysis groups and measures by.
CHECKED = ("level_key", "level_label", "feature")

DRIFT_SQL = """
SELECT c.name AS column_name, ty.name AS type_name, c.max_length, c.precision, c.scale
FROM sys.columns AS c
JOIN sys.tables AS t ON t.object_id = c.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
WHERE s.name = ? AND t.name = ?
"""

STORE_COLUMNS_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
       NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = ?
"""

STORE_RIGHTS_SQL = ("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE'), "
                    "HAS_PERMS_BY_NAME(?, 'SCHEMA', 'ALTER')")

LOG_SQL = ("SELECT TOP (?) table_name, row_count, error, finished_at FROM {log} "
           "WHERE source = ? ORDER BY finished_at DESC, table_name")


class LoadRefused(ValueError):
    """A load, or creating its tables, refused; said in the person's words."""

    def __init__(self, message: str, kind: str = "load"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Built:
    """The pack a source's plan built, as it is on disk now."""

    source: str
    pack_id: str
    folder: Path
    marker: dict[str, Any]
    edited: list[str] = field(default_factory=list)
    pack: Any = None

    @property
    def tables(self) -> list[dict[str, Any]]:
        return list(self.marker.get("store") or [])

    def view(self) -> dict[str, Any]:
        return {"pack_id": self.pack_id, "folder": str(self.folder),
                "built_by": self.marker.get("built_by"), "built_at": self.marker.get("built_at"),
                "edited": list(self.edited)}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(path: Path) -> str | None:
    """A built file's text as written (no newline translation), or None."""
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None  # not the text the build wrote: edited


def built(source: str) -> Built:
    """The source's built pack: its folder, its marker, the files edited since
    the build, and the pack read now from that folder. Refused when the pack
    was never built, was built from another source, is not the build the
    store records, or was built before a load could be made from the screen."""
    source = sources.plain_name(source)
    try:
        folder = builder.built_folder(source)
    except FileNotFoundError as exc:
        raise LoadRefused(f"{source} has no plan yet: read it and propose under Sources "
                          f"first.", "not_built") from exc
    if builder._linked(folder):
        raise LoadRefused(f"{folder} is a link to somewhere else, so nothing in it is run.")
    marker_text = _text(folder / builder.MARKER)
    if marker_text is None:
        raise LoadRefused(f"{source}'s pack has not been built: build it under Build first.",
                          "not_built")
    try:
        marker = json.loads(marker_text)
    except ValueError as exc:
        raise LoadRefused(f"{folder / builder.MARKER} is not the build's record: build "
                          f"again.") from exc
    if not isinstance(marker, dict) or marker.get("source") != source:
        raise LoadRefused(f"The pack in {folder} was not built from {source}: build {source}'s "
                          f"plan again, or name its pack another id.")
    if not marker.get("store") or not marker.get("reads") or not marker.get("files"):
        raise LoadRefused(f"The pack in {folder} was built before loading from this screen was "
                          f"possible: build it again.", "not_built")
    sealed = builder.sealed(marker["reads"], marker["store"])
    if builder.built_hash(marker["files"], sealed) != marker.get("built_hash"):
        raise LoadRefused(f"{folder / builder.MARKER} does not agree with itself: build "
                          f"again.", "not_built")
    row = approval.record(source) or {}
    if not row.get("built_hash") or row.get("built_hash") != marker.get("built_hash"):
        raise LoadRefused(f"The pack in {folder} is not the build the store records for "
                          f"{source}: build it again.", "not_built")
    if not row.get("built_plan_hash") or row.get("built_plan_hash") != row.get("plan_hash"):
        raise LoadRefused(f"{source}'s plan changed after the pack was built (it was approved "
                          f"again since): build it again, so the pack is the plan as approved.",
                          "not_built")
    if row.get("status") != "approved":
        raise LoadRefused(f"{source}'s plan is not approved now (it was withdrawn or changed "
                          f"since the build), so its pack is not created or loaded from here. "
                          f"Approve it under Approval, and build again if it changed.",
                          "approval")
    edited = sorted(name for name, digest in marker["files"].items()
                    if (text := _text(folder / name)) is None or _sha(text) != digest)
    # The privacy classes a load writes every row under come from pack.json:
    # read once, and used as the text whose hash was checked.
    pack_text = _text(folder / "pack.json")
    if pack_text is None or _sha(pack_text) != marker["files"].get("pack.json"):
        pack_text = None
        if "pack.json" not in edited:
            edited = sorted({*edited, "pack.json"})
    try:
        pack = packs.read_folder(folder, folder.name, pack_text)
    except packs.PackError as exc:
        raise LoadRefused(f"The pack in {folder} does not read: {exc}") from exc
    return Built(source=source, pack_id=folder.name, folder=folder, marker=marker,
                 edited=edited, pack=pack)


# --- the store ---------------------------------------------------------------

def _store() -> ConnectionRegistry:
    """The store, as configured; connecting refuses a protected database."""
    return ConnectionRegistry()


def _schema(registry: ConnectionRegistry) -> str:
    return registry.settings.denorm_schema


def _same_type(wanted: str, row: dict[str, Any]) -> bool:
    """Whether a store column's type is the one the build asked for: the base
    type, the length of a text or binary type, the precision and scale of a
    decimal one."""
    held = wanted.lower().replace(" ", "")
    base = held.split("(", 1)[0]
    if base != str(row["DATA_TYPE"]).lower():
        return False
    sized = held[len(base):].strip("()")
    if not sized:
        return True
    if base in ("decimal", "numeric"):
        parts = [p for p in sized.split(",") if p]
        return [str(row["NUMERIC_PRECISION"]), str(row["NUMERIC_SCALE"])][:len(parts)] == parts
    if base in ("char", "nchar", "varchar", "nvarchar", "binary", "varbinary"):
        length = row["CHARACTER_MAXIMUM_LENGTH"]
        return sized == ("max" if length == -1 else str(length))
    if base in ("datetime2", "time", "datetimeoffset"):
        return sized == str(row.get("DATETIME_PRECISION"))
    return True


def store_state(held: Built) -> dict[str, Any]:
    """Each denorm table of the build as the store has it: missing, lacking
    columns, or ready (a type that differs and a column the build does not
    name are said, never changed), and what the store login may do."""
    registry = _store()
    schema = _schema(registry)
    with registry.connect("denorm") as conn:
        cursor = conn.cursor()
        cursor.execute(STORE_COLUMNS_SQL, schema)
        found: dict[str, dict[str, dict[str, Any]]] = {}
        names = [d[0] for d in cursor.description]
        for values in cursor.fetchall():
            row = dict(zip(names, values))
            found.setdefault(row["TABLE_NAME"].lower(), {})[row["COLUMN_NAME"].lower()] = row
        cursor.execute(STORE_RIGHTS_SQL, schema)
        rights = cursor.fetchone()
        others, ours = _filled_by(cursor, schema, held)
    tables = []
    for table in held.tables:
        there = found.get(table["name"].lower())
        lacking, differs = [], []
        if there is not None:
            for column in table["columns"]:
                row = there.get(column["name"].lower())
                if row is None:
                    lacking.append(column["name"])
                elif not _same_type(column["type"], row):
                    differs.append(column["name"])
        named = {c["name"].lower() for c in table["columns"]}
        extra = sorted(r["COLUMN_NAME"] for k, r in (there or {}).items() if k not in named)
        # A table filled from another source, or one this source never
        # filled that has columns the build does not name or of another type,
        # is someone else's: never altered or refilled from here. One this
        # source filled keeps a column an earlier build had (said, not changed).
        mine = table["name"].lower() in ours
        foreign = there is not None and (table["name"].lower() in others
                                         or (bool(extra or differs) and not mine))
        # A column of another type in a table of its own is the DBA's to
        # alter: the add-only script cannot, and a load into it would fail.
        tables.append({"name": table["name"], "role": table["role"],
                       "exists": there is not None, "lacking": lacking, "differs": differs,
                       "extra": extra, "other_source": table["name"].lower() in others,
                       "foreign": foreign,
                       "ready": there is not None and not lacking and not foreign
                       and not differs})
    may_create = bool(rights and rights[0] == 1 and rights[1] == 1)
    # Columns an earlier build left in this source's own tables: the text to
    # give the DBA, never run from here.
    q = sqlgen.quote
    drops = [f"ALTER TABLE {q(schema)}.{q(t['name'])} DROP COLUMN {q(column)};"
             for t in tables if t["exists"] and not t["foreign"] for column in t["extra"]]
    return {"schema": schema, "database": registry.database_for("denorm"),
            "tables": tables, "ready": all(t["ready"] for t in tables),
            "may_create": may_create, "drops": drops}


def _filled_by(cursor: Any, schema: str, held: Built) -> tuple[set[str], set[str]]:
    """The build's tables (lower case) the extract log says another source
    filled, and those this source filled; none when the store has no log."""
    cursor.execute("SELECT OBJECT_ID(?, 'U')", f"{schema}.extract_log")
    if cursor.fetchone()[0] is None or not held.tables:
        return set(), set()
    names = [t["name"] for t in held.tables]
    marks = ", ".join("?" for _ in names)
    cursor.execute(f"SELECT DISTINCT table_name, CASE WHEN source = ? THEN 1 ELSE 0 END "
                   f"FROM {sqlgen.quote(schema)}.[extract_log] WHERE table_name IN ({marks})",
                   held.source, *names)
    others, ours = set(), set()
    for table, same in cursor.fetchall():
        (ours if same else others).add(str(table).lower())
    return others, ours


def _refuse_foreign(state: dict[str, Any]) -> None:
    """Refuse when a table the build names is in the store as someone else's."""
    foreign = [t for t in state["tables"] if t["foreign"]]
    if not foreign:
        return
    said = []
    for t in foreign:
        why = ("filled from another source" if t["other_source"] else
               f"has columns the build does not name ({', '.join(t['extra'][:5])})" if t["extra"]
               else f"has columns of another type ({', '.join(t['differs'][:5])})")
        said.append(f"{state['schema']}.{t['name']} {why}")
    raise LoadRefused("; ".join(said) + ". It is not this build's table, so nothing is changed "
                      "in it from here: name the pack's tables differently, or ask the DBA.",
                      "foreign")


def _source_database(source: str) -> str:
    return sources._setting(source, "DATABASE", source)


def _refuse_store(registry: ConnectionRegistry, source: str) -> None:
    """The store must be schema sdm, and never the source's own database."""
    schema = _schema(registry)
    if schema.lower() != sqlgen.STORE_SCHEMA:
        raise LoadRefused(f"The store's schema is {schema}, but the built tables are written "
                          f"for {sqlgen.STORE_SCHEMA}. Set SDG_DENORM_SCHEMA={sqlgen.STORE_SCHEMA}, "
                          f"or give the scripts to the DBA.")
    if registry.database_for("denorm").strip().lower() == _source_database(source).strip().lower():
        raise LoadRefused(f"The store and {source} are one database: the store's tables are "
                          f"never made in the source.")


def _batches(text: str) -> list[str]:
    """A script's batches, split on its GO lines."""
    out, lines = [], []
    for line in text.splitlines():
        if line.strip().upper() == "GO":
            out.append("\n".join(lines))
            lines = []
        else:
            lines.append(line)
    out.append("\n".join(lines))
    return [b for b in out if b.strip() and any(not ln.strip().startswith("--") and ln.strip()
                                                for ln in b.splitlines())]


def as_built(held: Built, name: str, kind: str = "dba") -> str:
    """A built file's text, read now, and refused unless it is exactly the
    text the build wrote: what runs is what was checked, read once."""
    text = _text(held.folder / name)
    if text is None or _sha(text) != held.marker["files"].get(name):
        raise LoadRefused(f"{name} was edited after the build, so it is not run from here: "
                          + ("give it to the DBA to run, or build again." if kind == "dba" else
                             "build again, or run it by hand with the extract command."), kind)
    return text


def _run_script(held: Built, name: str, registry: ConnectionRegistry) -> None:
    """Run one of the build's store scripts, exactly as built, batch by batch
    on one session (its guards stop it in a refused database)."""
    text = as_built(held, name)
    with registry.connect("denorm", autocommit=True) as conn:
        cursor = conn.cursor()
        for batch in _batches(text):
            try:
                cursor.execute(batch)
            except pyodbc.Error as exc:
                if sqlgen.SOURCE_REFUSAL in str(exc):
                    raise LoadRefused(f"{name} refused this database ("
                                      f"{registry.database_for('denorm')}): it is the source or a "
                                      f"protected one. Nothing more was run.") from exc
                raise


def create_tables(source: str) -> dict[str, Any]:
    """Create the build's tables in the store, add-only (store_tables.sql as
    built: a table that exists gains the columns it lacks, added NULL), and
    say the store's state after."""
    held = built(source)
    registry = _store()
    _refuse_store(registry, held.source)
    state = store_state(held)
    _refuse_foreign(state)
    if not state["may_create"]:
        raise LoadRefused(f"The store's login may not create tables in {state['schema']}: give "
                          f"{STORE_TABLES} to the DBA to run, then come back here to load.", "dba")
    # Never while an extract fills the store (screen or command line): its
    # lock is taken for the length of the script.
    from sdg.extract.run import StoreBusy, hold_store, release_store

    with registry.connect("denorm") as session:
        try:
            hold_store(session)
        except StoreBusy as exc:
            raise LoadRefused(str(exc).replace("extract again", "create the tables again")
                              .replace("Nothing was extracted.", "Nothing was created."),
                              "busy") from exc
        try:
            _run_script(held, STORE_TABLES, registry)
        finally:
            release_store(session)
    return {"built": held.view(), "store": store_state(held)}


# --- the source ----------------------------------------------------------------

def drift(held: Built, registry: sources.SourceRegistry) -> list[str]:
    """Each column the build reads that the source no longer has, or has with
    another type, since the plan was built. One catalog read per table."""
    problems = []
    for name, table in sorted(held.marker["reads"].items()):
        rows = query(registry, DRIFT_SQL, (table["schema"], table["table"]))
        if not rows:
            problems.append(f"{name} is not in {held.source} any more.")
            continue
        live = {str(r["column_name"]).upper(): [r["type_name"], r["max_length"], r["precision"],
                                                 r["scale"]] for r in rows}
        for column, was in sorted(table["columns"].items()):
            now = live.get(column.upper())
            if now is None:
                problems.append(f"{name}.{column} is not in {held.source} any more.")
            elif [str(v) for v in now] != [str(v) for v in was]:
                problems.append(f"{name}.{column} changed type in {held.source} "
                                f"({was[0]} to {now[0]}).")
    return problems


def _listed(problems: list[str]) -> str:
    shown = " ".join(problems[:SHOWN])
    more = len(problems) - SHOWN
    return shown + (f" And {more} more." if more > 0 else "")


# --- the load ----------------------------------------------------------------------

def _filled_sql(schema: str, table: dict[str, Any], columns: list[str]) -> str:
    q = sqlgen.quote
    counts = ", ".join(f"COUNT_BIG({q(c)})" for c in columns)
    return f"SELECT COUNT_BIG(*){', ' + counts if counts else ''} FROM {q(schema)}.{q(table['name'])}"


# The checks a staged load must pass before its tables are put in place: a
# table with no rows, or with a key on more than one row, never replaces the
# rows analysis reads.
BLOCKING = ("rows", "key")


def checks(held: Built, at: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The loaded tables checked by counts alone: rows, the key unique at the
    grain, each level and feature filled, and lines that meet their record.
    Withheld columns are not checked: they are empty by design. `at` reads
    each table under another name (a staged copy), said under its own."""
    at = at or {}
    registry = _store()
    schema = _schema(registry)
    q = sqlgen.quote
    out: list[dict[str, Any]] = []

    def said(check: str, table: str, passed: bool, words: str) -> None:
        out.append({"check": check, "table": table, "passed": passed, "said": words})

    with registry.connect("denorm") as conn:
        cursor = conn.cursor()
        for table in held.tables:
            name = table["name"]
            for column in [table["name"], *(c["name"] for c in table["columns"]), *table["key"],
                           *([table["primary"]] if table.get("primary") else []),
                           *(c for pair in table.get("meets") or [] for c in pair)]:
                packs.plain_name(column, f"{name} column")
            where = f"{q(schema)}.{q(at.get(name, name))}"
            filled = [c["name"] for c in table["columns"]
                      if c["serves"] in CHECKED and c["privacy"] != "withheld"]
            counts = cursor.execute(_filled_sql(schema, {**table, "name": at.get(name, name)},
                                                filled)).fetchone()
            rows = int(counts[0])
            said("rows", name, rows > 0, f"{rows:,} rows." if rows else "No rows were loaded.")
            if not rows:
                continue
            if table["key"]:
                keys = ", ".join(q(c) for c in table["key"])
                twice = int(cursor.execute(
                    f"SELECT COUNT_BIG(*) FROM (SELECT {keys} FROM {where} GROUP BY {keys} "
                    f"HAVING COUNT_BIG(*) > 1) AS d").fetchone()[0])
                said("key", name, twice == 0,
                     f"One row per {', '.join(table['key'])}." if not twice else
                     f"{twice:,} values of {', '.join(table['key'])} have more than one row.")
            for column, count in zip(filled, counts[1:]):
                share = int(count) / rows
                said("filled", name, int(count) > 0,
                     f"{column}: {share:.0%} of rows have a value." if count else
                     f"{column} has no value in any row.")
            if table.get("meets") and table.get("primary"):
                primary = table["primary"]
                on = " AND ".join(f"p.{q(p)} = c.{q(c)}" for p, c in table["meets"])
                alone = int(cursor.execute(
                    f"SELECT COUNT_BIG(*) FROM {where} AS c WHERE NOT EXISTS (SELECT 1 FROM "
                    f"{q(schema)}.{q(at.get(primary, primary))} AS p WHERE {on})").fetchone()[0])
                said("meets", name, alone == 0,
                     f"Every row meets a row of {primary}." if not alone else
                     f"{alone:,} rows ({alone / rows:.1%}) meet no row of {primary}.")
    return out


def last_loads(source: str) -> list[dict[str, Any]]:
    """The source's latest extract_log rows, newest first; none when the store
    has no log yet."""
    registry = _store()
    schema = _schema(registry)
    with registry.connect("denorm") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT OBJECT_ID(?, 'U')", f"{schema}.extract_log")
        if cursor.fetchone()[0] is None:
            return []
        cursor.execute(LOG_SQL.format(log=f"{sqlgen.quote(schema)}.[extract_log]"),
                       LAST_LOADS, source)
        return [{"table": r[0], "rows": int(r[1]), "error": r[2] or None,
                 "finished_at": r[3].isoformat(timespec="seconds") if r[3] else None}
                for r in cursor.fetchall()]


def status(source: str) -> dict[str, Any]:
    """What the Load tab shows: the built pack, the store's tables, whether
    SDM is running on that pack, and the latest loads."""
    held = built(source)
    running = packs.active_id()
    return {"source": held.source, "built": held.view(), "store": store_state(held),
            "running_pack": running, "analyzing": running == held.pack_id,
            "loads": last_loads(held.source)}


Told = Callable[[int, int, str], None]


def _archive_earlier_builds(held: Built, notes: list[str]) -> int | None:
    """Once a build's tables are loaded, the patterns found under the pack's
    earlier builds no longer describe them: archived, kept read-only. On the
    first load of a build, every current pattern of the pack is (one made
    after a restart on the build but before its tables were loaded describes
    the earlier rows); a refresh of the build archives only another build's."""
    from sdg.analyzer import db

    version = held.marker["built_hash"]
    try:
        first = approval.loaded_hash(held.source) != version
        archived = db.archive_other_builds(held.pack_id, version, every=first)
        if archived is not None:
            # Recorded only once archiving ran: on a store that cannot archive
            # yet, the first load after the table setup still archives all.
            approval.record_load(held.source, version)
    except pyodbc.Error as exc:
        from sdg.accelerator.source_read import error_said

        log.warning("Patterns of %s's earlier builds not archived: %s", held.source,
                    error_said(exc))
        notes.append(f"Patterns found under an earlier build were not archived "
                     f"({error_said(exc)}).")
        return None
    if archived is None:
        notes.append("Run the table setup to add pattern archiving: patterns found under an "
                     "earlier build of this pack are not archived until it is.")
    elif archived:
        notes.append(f"{archived} {'pattern' if archived == 1 else 'patterns'} found under an "
                     f"earlier build of the pack {'was' if archived == 1 else 'were'} "
                     f"archived, kept read-only.")
    return archived


# How long the swap waits on a reader of a live table before it gives up and
# leaves every table as it was: a long analysis query is never queued behind.
SWAP_LOCK_TIMEOUT_MS = 30_000
# Tries at the swap before the loaded rows are given up: a reader that holds
# a table past one wait (a long analysis) is waited out a few times over,
# with a pause between, so a long load is not thrown away for one query.
SWAP_ATTEMPTS = 4
SWAP_PAUSE_SECONDS = 5

# What a table carries of its own that a copy made beside it would not: its
# own grants or denials, triggers, row-level security, masked columns,
# constraints (a key, a default, a check, a foreign key either way), and
# indexes the copy cannot make the same (columnstore, XML, spatial, hash,
# partitioned or compressed ones). A table with any is replaced where it
# stands, never swapped.
OWN_RULES_SQL = """
SELECT (SELECT COUNT(*) FROM sys.database_permissions WHERE class = 1 AND major_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.triggers WHERE parent_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.security_predicates WHERE target_object_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.masked_columns WHERE object_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.indexes WHERE object_id = OBJECT_ID(?) AND type > 2)
     + (SELECT COUNT(*) FROM sys.indexes AS i JOIN sys.partition_schemes AS ps
          ON ps.data_space_id = i.data_space_id WHERE i.object_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.partitions WHERE object_id = OBJECT_ID(?)
          AND data_compression > 0)
     + (SELECT COUNT(*) FROM sys.key_constraints WHERE parent_object_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.default_constraints WHERE parent_object_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.check_constraints WHERE parent_object_id = OBJECT_ID(?))
     + (SELECT COUNT(*) FROM sys.foreign_keys WHERE parent_object_id = OBJECT_ID(?)
          OR referenced_object_id = OBJECT_ID(?))
"""

# The live table's indexes, to make the same on the copy before it goes live.
INDEXES_SQL = """
SELECT i.name, i.type, i.is_unique, i.has_filter, c.name, ic.is_descending_key,
       ic.is_included_column, ic.key_ordinal, ic.index_column_id
FROM sys.indexes AS i
JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = OBJECT_ID(?) AND i.type IN (1, 2) AND i.is_primary_key = 0
  AND i.is_unique_constraint = 0 AND i.is_hypothetical = 0
ORDER BY i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id
"""


def blocking(held: Built, store: ConnectionRegistry, copies: dict[str, str]) -> list[str]:
    """What keeps the staged copies from going live: a key on two rows, or a
    copy with no rows where the live table has rows to lose. An empty source
    table replaces a live table that is new or was empty too; its check
    still says no rows were loaded."""
    failed = []
    for c in checks(held, copies):
        if c["check"] not in BLOCKING or c["passed"]:
            continue
        if c["check"] == "rows" and not _live_rows(store, c["table"]):
            continue
        failed.append(f"{c['table']}: {c['said']}")
    return failed


def _live_rows(store: ConnectionRegistry, name: str) -> bool:
    """Whether the live table holds any row: a load with no rows replaces
    it only when it has none to lose."""
    schema = _schema(store)
    packs.plain_name(name, "a denorm table")
    with store.connect("denorm") as conn:
        cursor = conn.cursor()
        if cursor.execute("SELECT OBJECT_ID(?, 'U')", f"{schema}.{name}").fetchone()[0] is None:
            return False
        return cursor.execute(
            f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {sqlgen.quote(schema)}."
            f"{sqlgen.quote(name)}) THEN 1 ELSE 0 END").fetchone()[0] == 1


def carries_own_rules(schema: str, names: list[str]) -> list[str]:
    """The build's tables that carry rules of their own a staged copy would
    lose (grants, triggers, row-level security, masking)."""
    registry = _store()
    out = []
    with registry.connect("denorm") as conn:
        cursor = conn.cursor()
        for name in names:
            at = f"{schema}.{name}"
            if int(cursor.execute(OWN_RULES_SQL, *([at] * 12)).fetchone()[0] or 0):
                out.append(name)
    return out


class _Stage:
    """A staged load's two steps on the store session that holds the extract
    lock: make an empty copy of each live table beside it (its columns, no
    rows); once every table has loaded, give each copy the live table's
    indexes and swap it in by name in one transaction. Anything less -- a
    table that failed, a stop, a swap that could not get its locks -- drops
    the copies, and every live table keeps its rows. Neither step raises
    after the copies exist: what went wrong is said in `problem`."""

    def __init__(self, schema: str, names: list[str],
                 verify: Callable[[dict[str, str]], list[str]] | None = None,
                 stop: Callable[[], bool] | None = None):
        import secrets

        for name in names:
            packs.plain_name(name, "a denorm table")
        self.schema, self.names, self.problem = schema, list(names), ""
        self.notes: list[str] = []
        # Set once the swap has committed: the new rows are live, whatever fails after.
        self.placed = False
        # What the copies must pass before they go live (BLOCKING checks).
        self.verify = verify
        # Asked between tries at the swap: a stop gives the copies up.
        self.stop = stop
        self.stopped = False
        # Each load's copies have names of their own: a statement cached from
        # an earlier load names its copy, which the swap made the live table,
        # and must never be run against it again.
        token = secrets.token_hex(4)
        self.staged = {name: packs.plain_name(f"{name}{STAGED}_{token}", "a staged table")
                       for name in names}

    def _left_behind(self, cursor: Any, name: str) -> list[str]:
        """Copies an earlier load of `name` left: exactly `<name>__new` or
        `<name>__new_<8 hex>`, nothing else."""
        import re

        shape = re.compile(re.escape(name + STAGED) + r"(_[0-9a-f]{8})?", re.IGNORECASE)
        like = (name + STAGED).replace("[", "[[]").replace("_", "[_]") + "%"
        rows = cursor.execute("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID(?) "
                              "AND name LIKE ?", self.schema, like).fetchall()
        return [r[0] for r in rows if shape.fullmatch(r[0])]

    def _at(self, name: str) -> str:
        return f"{sqlgen.quote(self.schema)}.{sqlgen.quote(name)}"

    def _drop(self, cursor: Any, name: str) -> None:
        cursor.execute(f"DROP TABLE IF EXISTS {self._at(name)}")

    def prepare(self, write: Any) -> None:
        cursor = write.cursor()
        for name in self.names:
            # What an earlier load left: its copy, and a retired table once
            # the live one is there again (the swap is one transaction).
            for left in self._left_behind(cursor, name):
                self._drop(cursor, left)
            live = cursor.execute("SELECT OBJECT_ID(?, 'U')",
                                  f"{self.schema}.{name}").fetchone()[0]
            if live is not None:
                self._drop(cursor, name + RETIRED)
            cursor.execute(f"SELECT TOP 0 * INTO {self._at(self.staged[name])} "
                           f"FROM {self._at(name)}")
        write.commit()

    def _indexes(self, cursor: Any, name: str) -> None:
        """The live table's indexes, made the same on its copy."""
        found: dict[str, dict[str, Any]] = {}
        for index, kind, unique, filtered, column, desc, included, _, _ in cursor.execute(
                INDEXES_SQL, f"{self.schema}.{name}").fetchall():
            held = found.setdefault(index, {"kind": kind, "unique": unique,
                                            "filtered": filtered, "keys": [], "include": []})
            q = sqlgen.quote(column)
            (held["include"] if included else held["keys"]).append(
                q if included else f"{q} {'DESC' if desc else 'ASC'}")
        for index, held in found.items():
            if held["filtered"] or not held["keys"]:
                self.notes.append(f"Index {index} on {name} is filtered, so it was not made "
                                  f"on the new rows: give it to the DBA to make again.")
                continue
            include = f" INCLUDE ({', '.join(held['include'])})" if held["include"] else ""
            cursor.execute(
                f"CREATE {'UNIQUE ' if held['unique'] else ''}"
                f"{'CLUSTERED' if held['kind'] == 1 else 'NONCLUSTERED'} INDEX "
                f"{sqlgen.quote(index)} ON {self._at(self.staged[name])} "
                f"({', '.join(held['keys'])}){include}")

    def finish(self, write: Any, result: Any) -> None:
        from sdg.accelerator.source_read import error_said

        try:
            self._finish(write, result)
        except Exception as exc:  # said, never raised: the extract's result stands
            log.warning("A staged load of %s did not finish its swap: %s",
                        ", ".join(self.names), error_said(exc))
            if self.placed:
                return  # the new rows are live; only the clean-up after it failed
            self.problem = self.problem or (
                f"The loaded tables could not be put in place ({error_said(exc)}); every table "
                f"keeps its rows.")
            self._not_placed(result)
            try:
                write.rollback()
                cursor = write.cursor()
                for name in self.names:
                    self._drop(cursor, self.staged[name])
                write.commit()
            except Exception as again:
                log.warning("The staged copies were not dropped (%s); the next load drops "
                            "them.", error_said(again))

    @staticmethod
    def _not_placed(result: Any) -> None:
        for table in result.tables:
            if not table.error:
                table.error = NOT_PLACED
            if table.error == NOT_PLACED:
                table.rows = table.skipped = 0

    def _finish(self, write: Any, result: Any) -> None:
        from sdg.accelerator.source_read import error_said

        loaded = {t.table for t in result.tables if not t.error}
        cursor = write.cursor()
        if loaded != set(self.names):
            self._not_placed(result)
            for name in self.names:
                self._drop(cursor, self.staged[name])
            write.commit()
            return
        if self.verify is not None:
            failed = self.verify(self.staged)
            if failed:
                self.problem = ("The new rows were not put in place, because they did not pass "
                                "their checks: " + " ".join(failed) + " Every table keeps the "
                                "rows it had.")
                self._not_placed(result)
                for name in self.names:
                    self._drop(cursor, self.staged[name])
                write.commit()
                return
        for name in self.names:
            self._indexes(cursor, name)
        write.commit()
        for attempt in range(1, SWAP_ATTEMPTS + 1):
            cursor.execute(f"SET LOCK_TIMEOUT {int(SWAP_LOCK_TIMEOUT_MS)}")
            try:
                for name in self.names:
                    cursor.execute("EXEC sp_rename ?, ?", f"{self.schema}.{name}",
                                   name + RETIRED)
                    cursor.execute("EXEC sp_rename ?, ?", f"{self.schema}.{self.staged[name]}",
                                   name)
                write.commit()
                self.placed = True
                break
            except pyodbc.Error as exc:
                write.rollback()
                waited = "(1222)" in str(exc)
                stopped = self.stop is not None and self.stop()
                self.stopped = waited and stopped
                if waited and attempt < SWAP_ATTEMPTS and not stopped:
                    # The copies stay, loaded: the swap is tried again once
                    # the reader has had a moment to finish.
                    log.info("A reader held a table at the swap (try %d of %d); trying again.",
                             attempt, SWAP_ATTEMPTS)
                    cursor.execute("SET LOCK_TIMEOUT -1")
                    time.sleep(SWAP_PAUSE_SECONDS)
                    continue
                self.problem = (
                    "The loaded tables were not put in place: the load was stopped while a "
                    "reader held a table. Every table keeps its rows." if waited and stopped else
                    f"The loaded tables were not put in place: a reader held a table through "
                    f"{SWAP_ATTEMPTS} waits of {SWAP_LOCK_TIMEOUT_MS // 1000} seconds. Every "
                    f"table keeps its rows; load again when the analysis on it has finished."
                    if waited else
                    f"The loaded tables could not be put in place ({error_said(exc)}); every "
                    f"table keeps its rows.")
                self._not_placed(result)
                for name in self.names:
                    self._drop(cursor, self.staged[name])
                write.commit()
                return
            finally:
                cursor.execute("SET LOCK_TIMEOUT -1")
        for name in self.names:
            try:
                self._drop(cursor, name + RETIRED)
                write.commit()
            except pyodbc.Error as exc:
                write.rollback()
                log.warning("%s%s was not dropped (%s); the next load drops it.", name,
                            RETIRED, error_said(exc))


def refuse_loading(source: str) -> Built:
    """What a load would refuse before it reads anything: a pack not built as
    the store records, a store that is the source's or not schema sdm, or
    tables that are missing or lack a column."""
    held = built(source)
    if "pack.json" in held.edited:
        raise LoadRefused("pack.json was edited after the build. It holds the privacy classes "
                          "every row is loaded under, so the load runs only on the pack as "
                          "approved: build again.", "edited")
    extract_file = f"extract/{held.source}.sql"
    if extract_file in held.edited:
        raise LoadRefused(f"{extract_file} was edited after the build. From here the extract "
                          f"runs only as approved and built: build again, or run the edited "
                          f"file by hand with the extract command.", "edited")
    _refuse_store(_store(), held.source)
    state = store_state(held)
    _refuse_foreign(state)
    retyped = [t for t in state["tables"] if t["exists"] and t["differs"]]
    if retyped:
        raise LoadRefused("; ".join(f"{state['schema']}.{t['name']} has "
                                    f"{', '.join(t['differs'][:5])} of another type than the build"
                                    for t in retyped)
                          + ". Ask the DBA to alter those columns to the types in "
                          f"{STORE_TABLES}, then load.", "tables")
    if not state["ready"]:
        missing = [t["name"] for t in state["tables"] if not t["ready"]]
        raise LoadRefused(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not "
                          f"ready in the store: create the tables first.", "tables")
    return held


def load(source: str, told: Told, stop: Callable[[], bool], *,
         checker: sources.SourceRegistry | None = None,
         allow_write_login: bool = False) -> dict[str, Any]:
    """Read the source into the built tables (the extract, handed the built
    pack), then make the indexes the build names and check the tables. The
    source's login is checked (`checker`, a registry that has checked it
    already, or one made here) and the source asked whether it still has
    every column the build reads, before any row is read."""
    from sdg.extract.run import (NOT_RUN, STOPPED_PART_WAY, StoreBusy, extract,
                                 parse_definitions)

    told(0, 1, "Checking the source and the store")
    held = refuse_loading(source)
    source = held.source
    store = _store()
    if checker is None:
        checker = sources.registry_for(source, allow_write_login=allow_write_login)
    changed = drift(held, checker)
    if changed:
        raise LoadRefused("The source changed since the plan was built, so nothing was loaded. "
                          + _listed(changed) + " Read the source again under Sources, then "
                          "submit, approve and build.", "drift")
    settings, database = sources.source_settings(source)
    registry = ConnectionRegistry(reads_production=True)
    registry.register_connection("source", settings, database)
    names = [t["name"] for t in held.tables]
    finished: list[str] = []

    def progress(table: str, rows: int, done: bool, **_: Any) -> None:
        if done and table not in finished:
            finished.append(table)
        told(len(finished), len(names), f"{table}: {rows:,} rows" + ("" if done else " so far"))

    # The extract as built, read once: the statements that run are the ones
    # whose hash was checked, each filling one of the build's own tables.
    statements = parse_definitions(as_built(held, f"extract/{source}.sql", "edited"),
                                   f"extract/{source}.sql")
    if sorted(statements) != sorted(names):
        raise LoadRefused(f"extract/{source}.sql fills {', '.join(sorted(statements))}, not the "
                          f"build's tables ({', '.join(sorted(names))}): build again.", "edited")
    own = carries_own_rules(_schema(store), names)
    staged = store_state(held)["may_create"] and not own
    def verify(copies: dict[str, str]) -> list[str]:
        return blocking(held, store, copies)

    stage = _Stage(_schema(store), names, verify, stop) if staged else None
    told(0, len(names), "Loading")
    try:
        result = extract(source, registry=registry, pack=held.pack, vet=refuse_unless_read,
                         stop=stop, stop_on_error=False, progress=progress,
                         definitions=statements,
                         into=stage.staged if stage else None,
                         prepare=stage.prepare if stage else None,
                         finish=stage.finish if stage else None)
    except StoreBusy as exc:
        raise LoadRefused(str(exc), "busy") from exc
    done = result.as_dict()
    failed = [t for t in result.tables if t.error]
    notes = [] if staged else [
        (f"{', '.join(own)} {'carries' if len(own) == 1 else 'carry'} permissions, rules or "
         f"indexes of its own" if own else "This login may not create tables")
        + ", so each table was replaced where it stands: analysis of a table waited while it "
        "loaded."]
    if stage:
        notes.extend(stage.notes)
        if stage.problem:
            notes.append(stage.problem)
    elif any(t.error for t in result.tables) and any(not t.error for t in result.tables):
        notes.append("Some tables were replaced and some were not, so the tables now hold rows "
                     "from different loads: load again before analyzing them.")
    archived = None
    if not failed:
        archived = _archive_earlier_builds(held, notes)
        try:
            _run_script(held, STORE_INDEXES, store)
        except LoadRefused as exc:
            notes.append(f"Indexes not made: {exc}")
        except pyodbc.Error as exc:
            from sdg.accelerator.source_read import error_said

            log.warning("Indexes for %s not made: %s", source, error_said(exc))
            notes.append(f"Indexes not made ({error_said(exc)}): give {STORE_INDEXES} to the "
                         f"DBA.")
    told(len(names), len(names), "Checking the tables")
    return {"source": source, "built": held.view(), "load": done, "archived": archived,
            "staged": staged,
            "stopped": any(t.error in (NOT_RUN, STOPPED_PART_WAY) for t in failed)
            or bool(stage and stage.stopped),
            "failed": len(failed), "checks": checks(held), "notes": notes}

"""Run an extract: read a source, fill the analysis store.

One definition per source names the SELECT that produces each denorm table.
The SELECT runs on the source connection; the rows land on the denorm
connection. Both are SQL Server and usually the same server, but they are two
connections on purpose -- the store is not required to live beside the source,
and writing through a connection the tool controls is what keeps the source
read-only.

Rows move in blocks rather than one statement per row. A member book of any
size is hundreds of thousands of rows across sixty columns, and the difference
between `executemany` with a block and a row at a time is minutes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from sdg.discovery.connections import ConnectionRegistry

log = logging.getLogger(__name__)

# Rows per round trip. Large enough that the round trip stops mattering, small
# enough that a block is held in memory rather than the whole table.
BLOCK = 2_000
# Blocks between progress reports: every 100,000 rows.
PROGRESS_EVERY = 50



# The pack an extract reads, where its caller named one: the source
# accelerator loads a pack it has just built, before SDM is restarted on it.
# Per thread, so an extract running as a job never changes what anything else
# in the process reads.
_reading = threading.local()


def _pack() -> Any:
    from sdg import packs

    return getattr(_reading, "pack", None) or packs.active()


def definitions() -> Path:
    """Where the extract definitions are: the business pack's extract/ (the
    active pack's, unless the extract was handed one)."""
    return _pack().extract_folder

# A store holds one landscape: the customer's denorm tables carry no column
# naming the source a row came from, so an extract replaces the whole table.


class SourceQueryError(Exception):
    """A definition's SELECT failed on the source database."""


@dataclass
class TableResult:
    table: str
    rows: int
    seconds: float
    # Rows left behind because this tool generated them. Reported rather than
    # quietly dropped: an extract that is smaller than the source should say
    # why.
    skipped: int = 0
    # Why the table could not be filled, if it could not. Its old rows stay.
    error: str = ""


@dataclass
class ExtractResult:
    source: str
    tables: list[TableResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def rows(self) -> int:
        return sum(t.rows for t in self.tables)

    @property
    def skipped(self) -> int:
        return sum(t.skipped for t in self.tables)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "seconds": round(self.seconds, 2),
            "rows": self.rows,
            "tables": [
                {"table": t.table, "rows": t.rows, "skipped": t.skipped,
                 "seconds": round(t.seconds, 2), "error": t.error or None}
                for t in self.tables
            ],
        }


def sources() -> list[str]:
    """Every source with an extract definition on disk."""
    folder = definitions()
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.sql"))


def _statements(source: str) -> dict[str, str]:
    """The SELECT behind each denorm table, for one source.

    A definition file is a sequence of blocks, each introduced by a line naming
    the table it fills:

        -- table: member_denorm
        SELECT ...

    Plain SQL rather than anything generated, because an extract is exactly the
    join a person would write and reading it should not require this module.
    """
    path = definitions() / f"{source}.sql"
    if not path.exists():
        raise FileNotFoundError(
            f"no extract definition for {source!r}. Looked in {definitions()}; "
            f"found {', '.join(sources()) or 'nothing'}"
        )
    return parse_definitions(path.read_text(encoding="utf-8"), str(path))


def parse_definitions(text: str, where: str) -> dict[str, str]:
    """A definition file's blocks, by the table each fills (see _statements)."""
    blocks: dict[str, str] = {}
    table: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        marker = line.strip().lower()
        if marker.startswith("-- table:"):
            if table:
                blocks[table] = "\n".join(lines).strip()
            table = line.split(":", 1)[1].strip()
            lines = []
            continue
        lines.append(line)
    if table:
        blocks[table] = "\n".join(lines).strip()

    if not blocks:
        raise ValueError(f"{where} names no tables; expected a '-- table: <name>' line")
    return blocks


def _quoted(name: str) -> str:
    """A name bracket-quoted, any `]` in it doubled: a table named on a
    definition's `-- table:` line, or a column the SELECT names, is never
    read as SQL."""
    return "[" + str(name).replace("]", "]]") + "]"


# The key surrogate IDs are made with: secret, the same for every extract of
# one customer, so a household keeps one ID across tables and refreshes.
EXTRACT_KEY = "SDG_EXTRACT_KEY"
MIN_KEY_CHARS = 16
# How long a generated pack's extract waits on a lock in the source before
# its statement fails: a minute. Health's and banking's extracts set none.
GENERATED_LOCK_TIMEOUT_MS = 60_000


def surrogate_columns(source: str) -> set[str]:
    """Columns this source's extract writes as surrogates, not as the source
    holds them (pack.json extract.surrogates; health's FACETS: SBSB_ID, the
    household ID on a person's card)."""
    surrogates = (_pack().data.get("extract") or {}).get("surrogates") or {}
    return {c.upper() for c in surrogates.get(source, [])}


def withheld_columns(source: str) -> set[str]:
    """Columns this source's extract always writes as NULL, whatever its SELECT
    says (pack.json extract.withheld): a person's identifying values, kept out
    of the store on every row."""
    withheld = (_pack().data.get("extract") or {}).get("withheld") or {}
    return {c.upper() for c in withheld.get(source, [])}


def case_sensitive_columns(source: str) -> set[str]:
    """Surrogate columns whose source collation tells case apart (pack.json
    extract.case_sensitive, written by a build): their surrogates keep each
    value's case. Every other surrogate folds case, as a case-insensitive
    source compares its keys."""
    told = (_pack().data.get("extract") or {}).get("case_sensitive") or {}
    return {c.upper() for c in told.get(source, [])}


def kept_columns(source: str) -> set[str] | None:
    """The only columns this source's extract writes as read, besides its
    surrogates (pack.json extract.kept); every other column is written NULL,
    even one a hand edit adds to the SELECT. None where the pack names no kept
    columns for the source: then every column but the withheld is written."""
    kept = (_pack().data.get("extract") or {}).get("kept") or {}
    if not kept:
        return None
    if source not in kept:
        # A pack that keeps columns by source and does not name this one would
        # otherwise load every column as read: refused, never guessed.
        raise ValueError(
            f"The pack names the columns its extract keeps for "
            f"{', '.join(sorted(kept))}, but not for {source}. Name {source} in "
            "pack.json extract.kept, or read one of those sources. Nothing was extracted.")
    return {c.upper() for c in kept[source]}


def extract_key(source: str) -> bytes:
    """The secret surrogates are keyed with. Refused before anything is read
    when this source needs one and it is missing or short."""
    key = os.getenv(EXTRACT_KEY, "")
    if len(key) < MIN_KEY_CHARS:
        raise ValueError(
            f"The {source} extract replaces real IDs ({', '.join(sorted(surrogate_columns(source)))}) "
            f"with surrogates, and needs a secret key for that: set {EXTRACT_KEY} in .env to "
            f"at least {MIN_KEY_CHARS} characters, keep it private, and use the same one for "
            "every extract of this customer. Nothing was extracted.")
    return key.encode("utf-8")


# A surrogate's length: health's (FACETS SBSB_ID) stays 9, as its pattern data
# was made; a pack written by the source accelerator takes 16 (80 bits), so a
# key column of hundreds of millions of values has no two sharing one.
SURROGATE_CHARS = 9
GENERATED_SURROGATE_CHARS = 16


def _as_compared(value: Any, fold: bool) -> str:
    """A key as its source compares it: spaces at either end dropped, and
    case folded unless the source tells case apart."""
    text = str(value).strip()
    return text.upper() if fold else text


def surrogate(value: Any, key: bytes, chars: int = SURROGATE_CHARS, *,
              fold: bool = True) -> str | None:
    """A real ID's surrogate: `chars` letters and digits from an HMAC-SHA256 of it.
    The same ID always gets the same surrogate under one key, so households
    and the IDs made from it stay consistent; the real ID cannot be
    worked back from it without the key. `fold` False keeps the value's case
    (a source whose collation tells case apart)."""
    if value is None or not str(value).strip():
        return None
    digest = hmac.new(key, _as_compared(value, fold).encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(digest).decode("ascii")[:chars]


# Distinct values of one surrogate column a load tracks for collisions. At
# 80 bits two of even a hundred million keys sharing one is about one in a
# hundred million; past this many, the check stops (said in the log) rather
# than hold every key's digest in the server's memory.
COLLISION_TRACKED = 1_000_000


class SurrogateCollision(ValueError):
    """Two values of a column made one surrogate. Its words name the column
    and the table alone, so they are said as they stand."""


def _one_value_per_surrogate(seen: dict[int, int], made: str, value: Any, column: str,
                             table: str, *, fold: bool = True) -> None:
    """Refuse a surrogate already made from another value of the column: the
    two records would merge. Only a digest of each value is held, never the
    value, and the error names the column alone. Both are held as integers
    (the surrogate's 80 bits, 64 bits of the value's digest): one entry per
    distinct key, up to COLLISION_TRACKED, held for the table's load."""
    # Normalized as the surrogate is (spaces at either end; case unless the
    # source tells case apart), as the source compares keys: one key written
    # two ways is one record.
    digest = int.from_bytes(
        hashlib.sha256(_as_compared(value, fold).encode("utf-8")).digest()[:8], "big")
    found = int(made, 36)
    if found not in seen and len(seen) >= COLLISION_TRACKED:
        if not seen.get(-1):
            seen[-1] = 1
            log.info("%s in %s: more than %s distinct values, so later ones are not checked "
                     "for a shared surrogate (80 bits).", column, table, f"{COLLISION_TRACKED:,}")
        return
    if seen.setdefault(found, digest) != digest:
        raise SurrogateCollision(
            f"Two different values of {column} share one surrogate in {table}, so their "
            f"records would merge. Nothing was written to {table}. Extract again with "
            f"another SDG_EXTRACT_KEY (and keep that key for every later extract).")


def _blocks(cursor, size: int = BLOCK) -> Iterator[list[tuple]]:
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield rows


def extract(
    source: str,
    *,
    registry: ConnectionRegistry | None = None,
    schema: str | None = None,
    only: list[str] | None = None,
    exclude_keys: dict[str, list] | None = None,
    progress: Callable[..., None] | None = None,
    stop_on_error: bool = True,
    pack: Any = None,
    vet: Callable[[str], None] | None = None,
    stop: Callable[[], bool] | None = None,
    definitions: dict[str, str] | None = None,
    into: dict[str, str] | None = None,
    prepare: Callable[[Any], None] | None = None,
    finish: Callable[[Any, "ExtractResult"], None] | None = None,
) -> ExtractResult:
    """Read `source` into the analysis store, replacing what was there.

    `schema` is the store's, as configured unless given.

    Complete refresh, per table: the table's rows go, then the source is read
    again. A store holds one landscape, as the customer's denorm tables do.

    Each table is one transaction. A failure part way leaves that table as it
    was rather than half replaced -- an analysis over half a book is wrong in a
    way nothing downstream could detect.

    `pack` is the business pack whose definitions and privacy classes are read
    (the active one unless given). `vet` is shown every statement before
    anything is opened, and refuses by raising. `stop` is asked before each
    table: once it says stop, the tables left keep their rows and are reported
    as not run. One extract fills a store at a time (STORE_LOCK).
    `definitions` are the statements by table, read and checked by the
    caller, in place of the pack's file read again here.

    `into` names the store table each table's rows are written to, where it
    is not the table itself (a load staged beside the live table). `prepare`
    and `finish` run on the store session while the lock is held: before the
    first table, and after the last (with the result, which it may amend
    before it is logged).
    """
    before = getattr(_reading, "pack", None)
    _reading.pack = pack
    try:
        return _extract(source, registry=registry, schema=schema, only=only,
                        exclude_keys=exclude_keys, progress=progress,
                        stop_on_error=stop_on_error, vet=vet, stop=stop,
                        definitions=definitions, into=into or {}, prepare=prepare,
                        finish=finish)
    finally:
        _reading.pack = before


# Held by the store session an extract writes through, for the whole extract:
# a refresh from the screen and one from the command line (Tidal) never fill
# the same store at once. Released when that session closes.
STORE_LOCK = "sdm.extract"
STORE_BUSY = ("Another extract into this store is running (from the screen or the command "
              "line). Wait for it to finish, then extract again. Nothing was extracted.")
NOT_RUN = "Not run: the extract was stopped before this table. Its rows are as they were."
STOPPED_PART_WAY = ("Stopped part way through this table: nothing of it was written, so its "
                    "rows are as they were.")


class StoppedPartWay(Exception):
    """Stop was asked while a table was being read: its transaction is rolled
    back, so the table keeps the rows it had."""


class StoreBusy(Exception):
    """Another extract holds the store."""


def hold_store(write) -> None:
    """Take the store's extract lock on the write session, or refuse."""
    cursor = write.cursor()
    cursor.execute(
        "SET NOCOUNT ON; DECLARE @held INT; EXEC @held = sp_getapplock @Resource = ?, "
        "@LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0; SELECT @held",
        STORE_LOCK)
    row = cursor.fetchone()
    if row is None or row[0] is None or int(row[0]) < 0:
        raise StoreBusy(STORE_BUSY)


def release_store(write) -> None:
    """Give the store's extract lock back. Said, not raised, when it cannot be:
    the extract itself is done, and the lock still ends with the session.
    Released explicitly because a pooled connection's session outlives close()."""
    try:
        write.rollback()
        write.cursor().execute(
            "EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session'", STORE_LOCK)
    except Exception as exc:
        log.warning("The store's extract lock was not given back (%s); it ends when the "
                    "session does.", type(exc).__name__)


def _extract(source, *, registry, schema, only, exclude_keys, progress, stop_on_error,
             vet, stop, definitions, into, prepare, finish) -> ExtractResult:
    registry = registry or ConnectionRegistry(reads_production=True)
    schema = schema or registry.settings.denorm_schema
    statements = dict(definitions) if definitions is not None else _statements(source)
    if only:
        missing = [t for t in only if t not in statements]
        if missing:
            raise ValueError(
                f"{source} has no extract for {', '.join(missing)}; "
                f"it defines {', '.join(sorted(statements))}"
            )
        statements = {t: statements[t] for t in only}
    if vet is not None:
        for select in statements.values():
            vet(select)
    key = extract_key(source) if surrogate_columns(source) else b""
    kept_columns(source)  # refused here, before the source is read

    result = ExtractResult(source=source)
    started = time.perf_counter()

    with registry.connect("denorm") as write, registry.connect("source") as read:
        hold_store(write)
        try:
            if prepare is not None:
                prepare(write)
            try:
                _tables(read, write, source, schema, statements, exclude_keys, progress,
                        stop_on_error, stop, key, result, into)
            finally:
                if finish is not None:
                    finish(write, result)
        finally:
            release_store(write)

    result.seconds = time.perf_counter() - started
    _log(registry, schema, source, result)
    return result


def _tables(read, write, source, schema, statements, exclude_keys, progress, stop_on_error,
            stop, key, result, into) -> None:
    for table, select in statements.items():
        if stop is not None and stop():
            result.tables.append(TableResult(table=table, rows=0, seconds=0.0,
                                             error=NOT_RUN))
            if progress:
                progress(table, 0, True, error=NOT_RUN)
            continue
        try:
            result.tables.append(
                _one_table(
                    read, write, source, schema, table, select, exclude_keys or {},
                    progress, key=key, target=into.get(table), stop=stop,
                )
            )
        except StoppedPartWay:
            result.tables.append(TableResult(table=table, rows=0, seconds=0.0,
                                             error=STOPPED_PART_WAY))
            if progress:
                progress(table, 0, True, error=STOPPED_PART_WAY)
        except Exception as exc:
            if stop_on_error:
                raise
            # One table that cannot be read leaves its old rows and says
            # why; the rest of a long overnight refresh still happens.
            from sdg.discovery.errors import plain

            if kept_columns(source) is not None:
                # A generated pack's extract: said by kind and codes only,
                # since a driver's words can quote a value (a failed
                # conversion names it), and this lands in the log.
                from sdg.accelerator.source_read import error_said

                if isinstance(exc, SurrogateCollision):
                    error = str(exc)
                else:
                    where = " on the source" if isinstance(exc, SourceQueryError) else ""
                    error = (f"The load of {table} failed{where} "
                             f"({error_said(exc.__cause__ or exc)}).")
            else:
                error = str(exc) if isinstance(exc, SourceQueryError) else plain(exc)
            result.tables.append(TableResult(table=table, rows=0, seconds=0.0,
                                             error=error))
            if progress:
                # Said as a failure, never as "0 rows": an empty table and
                # one that could not be read are not the same thing.
                progress(table, 0, True, error=error)


def log_ddl(schema: str) -> str:
    """The extract log, created if missing; customer-tables creates it up front
    so an extract under a login that may not create tables still logs."""
    return (f"IF OBJECT_ID(N'{schema}.extract_log', N'U') IS NULL "
            f"CREATE TABLE [{schema}].[extract_log] (source NVARCHAR(100) NOT NULL, "
            f"table_name NVARCHAR(128) NOT NULL, row_count BIGINT NOT NULL, "
            f"error NVARCHAR(1000) NULL, finished_at DATETIME2 NOT NULL)")


def _log(registry, schema: str, source: str, result: ExtractResult) -> None:
    """When each table was last filled, and with how many rows.

    A refresh can leave a table with as many rows as before; the pattern
    analyses cached against it would then look current. This is the stamp
    that says they are not.
    """
    from datetime import datetime

    table = f"[{schema}].[extract_log]"
    try:
        with registry.connect("denorm") as write:
            cursor = write.cursor()
            cursor.execute(log_ddl(schema))
            now = datetime.now()
            for done in result.tables:
                cursor.execute(
                    f"INSERT INTO {table} (source, table_name, row_count, error, finished_at) "
                    f"VALUES (?, ?, ?, ?, ?)",
                    source, done.table, done.rows, done.error or None, now)
            write.commit()
    except Exception as exc:
        # A store this login cannot add a table to still gets its rows, but the
        # missing stamp is said: without it, analyses cached against these
        # tables can look current when they are not.
        log.warning(
            "The extract of %s finished, but its log entry could not be written "
            "to %s, so analyses cached against these tables may look current. "
            "Run the customer setup script to create the table, or grant this "
            "login INSERT on it. (%s)", source, table, type(exc).__name__)


def _one_table(
    read, write, source: str, schema: str, table: str, select: str,
    exclude_keys: dict[str, list],
    progress: Callable[[str, int, bool], None] | None = None,
    *,
    key: bytes = b"",
    target: str | None = None,
    stop: Callable[[], bool] | None = None,
) -> TableResult:
    started = time.perf_counter()
    qualified = f"{_quoted(schema)}.{_quoted(target or table)}"

    try:
        reader = read.cursor()
        if kept_columns(source) is not None:
            # A pack written by the source accelerator (it names its kept
            # columns; health and banking do not): a full read of a source
            # gives up on a lock held too long rather than block behind it.
            reader.execute(f"SET LOCK_TIMEOUT {int(GENERATED_LOCK_TIMEOUT_MS)}")
            # And it gives way in a deadlock: the source's own work comes first.
            reader.execute("SET DEADLOCK_PRIORITY LOW")
        cursor = reader.execute(select)
    except Exception as exc:
        if kept_columns(source) is not None:
            # A generated pack's: by kind and codes only, on every path (the
            # command line prints it): a driver's words can quote a value.
            from sdg.accelerator.source_read import error_said

            raise SourceQueryError(
                f"The {source} query for {table} failed on the source database "
                f"({error_said(exc)}), not on the SDM tables.") from exc
        # The query on the source failed: FACETS itself lacks what the
        # definition names. Nothing this tool's setup or upgrade would fix.
        raise SourceQueryError(
            f"The {source} query for {table} failed on the source database "
            f"({source}), not on the SDM tables: {exc}") from exc
    columns = [d[0] for d in cursor.description]

    placeholders = ", ".join(["?"] * len(columns))
    names = ", ".join(_quoted(c) for c in columns)
    insert = f"INSERT INTO {qualified} ({names}) VALUES ({placeholders})"

    # What this tool generated, and must not read back as though it were
    # measured. Keyed by the column the manifest records them under; a denorm
    # row carrying one of those keys came from a run, not from the source.
    skip_column, skip_values = _exclusions(columns, exclude_keys)
    # Real IDs this source's rows carry, written as their surrogates.
    surrogates = surrogate_columns(source)
    replaced = [i for i, c in enumerate(columns) if c.upper() in surrogates]
    told_apart = case_sensitive_columns(source)
    folded = {i: columns[i].upper() not in told_apart for i in replaced}
    # Columns written NULL on every row: withheld ones, and where the pack
    # names the kept columns, every column it does not name.
    withheld, kept = withheld_columns(source), kept_columns(source)
    nulled = [i for i, c in enumerate(columns)
              if c.upper() in withheld or (kept is not None and c.upper() not in kept | surrogates)]
    # A generated pack's surrogates stand for keys: each surrogate must stand
    # for one value, or two records would merge in the store. Health's and
    # banking's extracts are not checked.
    stood_for: dict[int, dict[int, int]] | None = (
        {i: {} for i in replaced} if kept is not None else None)
    chars = GENERATED_SURROGATE_CHARS if kept is not None else SURROGATE_CHARS

    writer = write.cursor()
    writer.fast_executemany = True
    rows = skipped = 0
    try:
        # Replace rather than merge, emptied and filled in one transaction, so
        # a failure part way leaves the old rows in place. TRUNCATE is far
        # faster on a large table than deleting row by row; a login allowed
        # to delete but not to truncate gets the delete.
        try:
            writer.execute(f"TRUNCATE TABLE {qualified}")
        except Exception:
            write.rollback()
            writer = write.cursor()
            writer.fast_executemany = True
            writer.execute(f"DELETE FROM {qualified}")
        position = columns.index(skip_column) if skip_column else -1
        for number, block in enumerate(_blocks(cursor), start=1):
            # Asked before every block after the first (before the first it
            # was asked with the table), not only between tables: a stopped
            # table is rolled back below and keeps its rows.
            if number > 1 and stop is not None and stop():
                try:
                    cursor.cancel()  # the source stops sending the rest
                except Exception as exc:  # the rollback below stands either way
                    log.info("The read of %s was not cancelled (%s); its connection "
                             "closes with the extract.", table, type(exc).__name__)
                raise StoppedPartWay(table)
            if position >= 0:
                keep = [r for r in block if r[position] not in skip_values]
                skipped += len(block) - len(keep)
            else:
                keep = block
            if keep:
                written = [list(r) for r in keep]
                for row in written:
                    for i in replaced:
                        value = row[i]
                        row[i] = surrogate(value, key, chars, fold=folded[i])
                        if stood_for is not None and row[i] is not None:
                            _one_value_per_surrogate(stood_for[i], row[i], value,
                                                     columns[i], table, fold=folded[i])
                    for i in nulled:
                        row[i] = None
                writer.executemany(insert, [tuple(r) for r in written])
            rows += len(keep)
            if progress and number % PROGRESS_EVERY == 0:
                progress(table, rows, False)
        write.commit()
        if progress:
            progress(table, rows, True)
    except Exception:
        write.rollback()
        raise

    return TableResult(
        table=table, rows=rows, skipped=skipped,
        seconds=time.perf_counter() - started,
    )


def _exclusions(columns: list[str], exclude_keys: dict[str, list]):
    """Which column to filter on, and which values to drop.

    `exclude_keys` is keyed by **column** -- see `keys_from_manifest`, which
    translates a run manifest into that shape. The manifest itself is keyed by
    table, and a denorm select has no tables in it, only the columns the join
    carried through. Translating at the boundary keeps the guessing in one
    place instead of here.

    Where none of the excluded columns is in this select, nothing is filtered:
    a denorm table that does not carry the key cannot be told which of its rows
    came from a run.
    """
    by_column = {c.upper(): c for c in columns}
    for column, values in (exclude_keys or {}).items():
        found = by_column.get(column.upper())
        if found and values:
            return found, set(values)
    return None, set()


def keys_from_manifest(manifest: dict | None) -> dict[str, set]:
    """The keys a run wrote, by the column that holds them.

    A manifest records `{table: [[key], [key], ...]}` and, separately, which
    columns those keys are. An extract needs the pairing: the column name is
    the only thing a denorm select and a source table have in common.

    Composite keys are reduced to their first column. A row of the denorm is
    one row of the child table, so the first key column identifies it; the
    second would narrow a set that is already right.
    """
    if not manifest:
        return {}
    columns = {
        record["table"]: (record.get("key_columns") or [])
        for record in manifest.get("tables") or []
    }
    out: dict[str, set] = {}
    for table, values in (manifest.get("keys") or {}).items():
        key_columns = columns.get(table) or []
        if not key_columns or not values:
            continue
        held = out.setdefault(key_columns[0], set())
        for value in values:
            held.add(value[0] if isinstance(value, (list, tuple)) else value)
    return out

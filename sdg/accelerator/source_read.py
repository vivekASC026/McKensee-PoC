"""Every statement the accelerator sends a source passes through here.

A source is a customer's database, often production, so the accelerator only
ever reads it. A statement is sent only when, with its comments taken out, it
starts with SELECT or WITH, is one statement (no `;` before its end), and
names nothing that writes: SQL Server runs `SELECT 1 DROP TABLE t` as two
statements with no `;` between them, and `SELECT ... INTO` makes a table, so
the start alone does not say a statement only reads. Quoted values and
bracketed names are set aside before the words are read, so a column named
[UPDATE] or a value 'DROP' is not mistaken for a command. SQL Server reads
@, # and $ as part of a name (EXEC@r runs EXEC on @r), so they are read as
spaces before the words are taken: no command hides glued to a variable.

SourceRegistry (sources.py) applies the same check to every statement
discovery and profiling send, so nothing reaches a source without it.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Table
from sdg.profiling import sample

# The one role a source is registered under. ConnectionRegistry.connect lets
# a registry that reads production open it only under this name.
SOURCE = "source"

# Words a reading statement never holds outside a quoted value or name.
WRITING_WORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER", "CREATE",
    "EXEC", "EXECUTE", "INTO", "GRANT", "REVOKE", "DENY", "BACKUP", "RESTORE",
    "DBCC", "BULK", "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "SHUTDOWN",
    "KILL", "RECONFIGURE", "WRITETEXT", "UPDATETEXT",
    # Waiting, queues and conversations, session state, transactions,
    # cursors, renaming and loading, messages: none belongs in a read.
    "WAITFOR", "DISABLE", "ENABLE", "RECEIVE", "SEND", "CONVERSATION", "SET", "USE",
    "DECLARE", "BEGIN", "COMMIT", "ROLLBACK", "SAVE", "SETUSER", "REVERT", "CHECKPOINT",
    "READTEXT", "LOAD", "OPEN", "RENAME", "COPY", "ADD", "PRINT", "RAISERROR", "THROW",
    # Control of flow: a batch that branches or loops is more than one read.
    "IF", "WHILE", "GOTO",
    # Lock hints that hold rows or tables against others: a read takes none.
    "UPDLOCK", "XLOCK", "TABLOCKX", "HOLDLOCK", "PAGLOCK",
})

# Drawing a sequence's next value changes the sequence.
_NEXT_VALUE = re.compile(r"\bNEXT\s+VALUE\s+FOR\b", re.IGNORECASE)

# Characters that end a line, and so a -- comment: SQL Server reads the next
# statement after any of them.
LINE_ENDS = "\r\n\x85\u2028\u2029"

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Characters SQL Server takes inside a name: read as spaces, so EXEC@r is EXEC.
_GLUE = re.compile(r"[@#$]")
_STARTS = re.compile(r"(SELECT|WITH)\b", re.IGNORECASE)


class NotARead(PermissionError):
    """A statement that might change a source was about to be sent to it."""


def _skeleton(sql: str) -> str:
    """The statement with comments removed and every quoted value or name
    replaced by a placeholder: what is left is the words SQL Server acts on.
    An unclosed quote or comment is refused, since what follows it cannot be
    read for certain."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        pair = sql[i:i + 2]
        if pair == "--":
            end = next((j for j in range(i + 2, n) if sql[j] in LINE_ENDS), n)
            i = end
            out.append(" ")
        elif pair == "/*":
            # SQL Server nests block comments.
            depth, i = 1, i + 2
            while i < n and depth:
                if sql[i:i + 2] == "/*":
                    depth, i = depth + 1, i + 2
                elif sql[i:i + 2] == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise NotARead("the statement has a comment that is never closed")
            out.append(" ")
        elif ch in "'[\"":
            close = {"'": "'", "[": "]", '"': '"'}[ch]
            i += 1
            while True:
                end = sql.find(close, i)
                if end < 0:
                    raise NotARead("the statement has a quote or a bracket that is never closed")
                # A doubled closing character stands for itself.
                if sql[end + 1:end + 2] == close:
                    i = end + 2
                    continue
                i = end + 1
                break
            out.append(" 0 " if ch == "'" else " x ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def refuse_unless_read(sql: str) -> None:
    """Raise NotARead unless `sql` is one statement that only reads."""
    words = _GLUE.sub(" ", _skeleton(sql or "")).strip()
    if not _STARTS.match(words):
        raise NotARead("only a statement starting with SELECT or WITH is sent to a source")
    if ";" in words.rstrip("; \t\r\n"):
        raise NotARead("only one statement at a time is sent to a source")
    found = sorted({w.upper() for w in _WORD.findall(words)} & WRITING_WORDS)
    if _NEXT_VALUE.search(words):
        found.append("NEXT VALUE FOR")
    if found:
        raise NotARead(f"a statement sent to a source may not hold {', '.join(found)}")


def query(registry: ConnectionRegistry, sql: str, params: Sequence[Any] = (), *,
          timeout: int = 60, lock_timeout_ms: int | None = None) -> list[dict[str, Any]]:
    """Run one reading statement against the source; refused otherwise.
    A statement still running after `timeout` seconds is stopped. With
    `lock_timeout_ms`, a statement waiting longer than that on another
    session's lock gives up rather than queue behind it: the registry sets it
    on the connection (SourceRegistry.lock_timeout), never in the statement."""
    refuse_unless_read(sql)
    holding = getattr(registry, "lock_timeout", None)
    if lock_timeout_ms is None or holding is None:
        return registry.query(SOURCE, sql, params, timeout=timeout)
    with holding(lock_timeout_ms):
        return registry.query(SOURCE, sql, params, timeout=timeout)


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


# Seconds one proposal check (a composite key, a nesting count) may run, and
# milliseconds it may wait on another session's lock: a check is a nicety,
# never worth holding up a production source.
CHECK_TIMEOUT = 20
CHECK_LOCK_TIMEOUT_MS = 5000

# The accelerator's own sample of a large table, for its checks: the profile's
# rows and seed (sdg/profiling/sample.py), and the share of pages the column
# profile asks for (columns.sample_sql), a little over the rows wanted, so the
# cap trims little of the table's end. profiling.sample.source asks for 2.5
# times the rows, so its TOP keeps only the first part of what was sampled;
# its other callers depend on it, so it is left as it is.
SAMPLE_FACTOR = 1.25


def is_sampled(table: Table) -> bool:
    """Whether a check reads this table through its sample."""
    return sample.is_sampled(table, sample.SAMPLE_ROWS)


def sample_source(table: Table, alias: str | None = None) -> str:
    """What to put after FROM in a check: the table, or a fixed sample of it."""
    name = sample.qualified(table)
    if not is_sampled(table):
        return f"{name} AS {alias}" if alias else name
    rows = sample.SAMPLE_ROWS
    percent = max(0.01, min(100.0, rows * SAMPLE_FACTOR / table.row_count * 100))
    sampled = (f"(SELECT TOP ({rows}) * FROM {name} "
               f"TABLESAMPLE SYSTEM ({percent:.4f} PERCENT) REPEATABLE ({sample.SAMPLE_SEED}))")
    return f"{sampled} AS {alias or _quote('sampled')}"


def count_source(table: Table, alias: str) -> str:
    """What to put after FROM in a count: the table, or its fixed sample of
    whole pages with nothing cut from it. A TOP over the sample would drop
    the last pages read, which on a table kept in date order are the newest
    rows: the very ones an in-force or window criterion keeps."""
    name = sample.qualified(table)
    if not is_sampled(table):
        return f"{name} AS {alias}"
    percent = max(0.01, min(100.0, sample.SAMPLE_ROWS / table.row_count * 100))
    return (f"{name} AS {alias} TABLESAMPLE SYSTEM ({percent:.4f} PERCENT) "
            f"REPEATABLE ({sample.SAMPLE_SEED})")


# A driver error's message number, as the driver writes it: "... (208)
# (SQLExecDirectW)".
_NUMBER = re.compile(r"\((\d{1,6})\)\s*\(SQL\w+\)")
_STATE = re.compile(r"[0-9A-Z]{5}")


def error_said(exc: BaseException) -> str:
    """A driver error by its kind, SQLSTATE and message number only. The
    driver's words can quote a value from the source (a failed conversion
    names it), so they are never shown or kept."""
    state = str(exc.args[0]) if getattr(exc, "args", None) else ""
    if not _STATE.fullmatch(state):
        state = ""
    number = _NUMBER.search(str(exc))
    parts = [type(exc).__name__, state, f"({number.group(1)})" if number else ""]
    return " ".join(p for p in parts if p)


def pair_counts(registry: ConnectionRegistry, table: Table, a: str,
                b: str) -> tuple[int, int, int]:
    """Over the rows holding both `a` and `b`: how many rows, how many values
    `b` holds, and how many (a, b) pairs -- the last two equal when each `b`
    falls under one `a` only. One pass, a pair keyed as the profile's
    dependency measure keys it (CONCAT with CHAR(1) between), through the
    table's fixed sample when it is large; the row count says whether each
    `b` was seen often enough on a sample for equal counts to mean anything."""
    for name in (a, b):
        if table.column(name) is None:
            raise ValueError(f"{table.name} has no column {name!r}")
    first, second = _quote(a), _quote(b)
    sql = (f"SELECT COUNT_BIG(*) AS rows_both, COUNT(DISTINCT {second}) AS distinct_b, "
           f"COUNT(DISTINCT CONCAT({first}, CHAR(1), {second})) AS distinct_pair "
           f"FROM {sample_source(table)} "
           f"WHERE {first} IS NOT NULL AND {second} IS NOT NULL")
    rows = query(registry, sql, timeout=CHECK_TIMEOUT, lock_timeout_ms=CHECK_LOCK_TIMEOUT_MS)
    if not rows:
        return 0, 0, 0
    row = rows[0]
    return (int(row.get("rows_both") or 0), int(row.get("distinct_b") or 0),
            int(row.get("distinct_pair") or 0))

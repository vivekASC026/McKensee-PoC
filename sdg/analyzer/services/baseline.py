"""Pattern baseline — the stable denominator behind every weight.

The problem this solves: if the denominator is derived from whatever the user
currently has filtered, a pattern's weight changes depending on how someone
navigated to it. Two people looking at the same pattern see different numbers,
and nobody can compare a weight recorded last week against one today.

So the full set of patterns is enumerated once, the totals are written to SQL
Server, and every weight afterwards divides by the stored figure. Restarts,
cache clears, and additional workers all read the same row.

Enumeration walks the top level of the hierarchy — one entry per group, plus a
book-wide entry — and records how many patterns exist there and how many
members or claims they cover. A pattern's weight is its count over the stored
total for its scope, so patterns within one group sum to 100% and any deeper
filter sums to less.

Recompute deliberately, not automatically: a denominator that moves on its own
is the thing this exists to prevent. POST /api/baseline/compute when the source
data has genuinely changed.
"""

import json
import logging
from datetime import datetime
from typing import Optional

import pyodbc

from sdg.analyzer.config import SQL_SCHEMA, get_connection_string

log = logging.getLogger(__name__)

TABLE = f"{SQL_SCHEMA}.pattern_baseline"

DDL = f"""
IF NOT EXISTS (
    SELECT 1 FROM sys.objects
    WHERE object_id = OBJECT_ID(N'{TABLE}') AND type = N'U'
)
CREATE TABLE {TABLE} (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    entity        NVARCHAR(20)  NOT NULL,   -- 'members' or 'claims'
    scope_key     NVARCHAR(200) NOT NULL,   -- 'ALL', or 'GRGR:10042'
    scope_label   NVARCHAR(300) NULL,
    n_patterns    INT           NOT NULL DEFAULT 0,
    total_count   BIGINT        NOT NULL DEFAULT 0,
    detail_json   NVARCHAR(MAX) NULL,
    computed_at   DATETIME2     NOT NULL DEFAULT GETDATE(),
    CONSTRAINT UQ_pattern_baseline UNIQUE (entity, scope_key)
);
"""


def _conn():
    return pyodbc.connect(get_connection_string())


def ensure_table() -> None:
    conn = _conn()
    try:
        conn.cursor().execute(DDL)
        conn.commit()
    finally:
        conn.close()


def upsert(entity: str, scope_key: str, scope_label: str,
           n_patterns: int, total_count: int, detail: Optional[dict] = None) -> None:
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            MERGE {TABLE} AS target
            USING (SELECT ? AS entity, ? AS scope_key) AS src
              ON target.entity = src.entity AND target.scope_key = src.scope_key
            WHEN MATCHED THEN UPDATE SET
                scope_label = ?, n_patterns = ?, total_count = ?,
                detail_json = ?, computed_at = ?
            WHEN NOT MATCHED THEN INSERT
                (entity, scope_key, scope_label, n_patterns, total_count,
                 detail_json, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            entity, scope_key,
            scope_label, n_patterns, total_count,
            json.dumps(detail or {}), datetime.now(),
            entity, scope_key, scope_label, n_patterns, total_count,
            json.dumps(detail or {}), datetime.now(),
        )
        conn.commit()
    finally:
        conn.close()


def _open():
    """A connection for reading the table.

    A failure here is raised, not read as "no baseline": an unreachable store
    or a refused permission would otherwise show as a baseline never computed,
    and weights and bands would quietly fall back to fixed shares. Reading
    never creates the table, so a login that may only read still can.
    """
    try:
        return _conn()
    except pyodbc.Error:
        log.exception("Could not reach %s", TABLE)
        raise


def _exists(cur) -> bool:
    """Whether the table is there: before the first baseline is computed it
    may not be, which is "never computed", not a failure."""
    cur.execute("SELECT OBJECT_ID(?, 'U')", TABLE)
    row = cur.fetchone()
    return bool(row) and row[0] is not None


def get(entity: str, scope_key: str) -> Optional[dict]:
    """One stored baseline, or None if it has never been computed."""
    conn = _open()
    try:
        cur = conn.cursor()
        if not _exists(cur):
            return None
        cur.execute(
            f"""SELECT scope_label, n_patterns, total_count, computed_at
                FROM {TABLE} WHERE entity = ? AND scope_key = ?""",
            entity, scope_key,
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "entity": entity,
            "scope_key": scope_key,
            "scope_label": row[0],
            "n_patterns": int(row[1] or 0),
            "total_count": int(row[2] or 0),
            "computed_at": row[3].isoformat() if row[3] else None,
        }
    finally:
        conn.close()


def get_all(entity: Optional[str] = None) -> list[dict]:
    conn = _open()
    try:
        cur = conn.cursor()
        if not _exists(cur):
            return []
        if entity:
            cur.execute(
                f"""SELECT entity, scope_key, scope_label, n_patterns,
                           total_count, computed_at
                    FROM {TABLE} WHERE entity = ? ORDER BY total_count DESC""",
                entity,
            )
        else:
            cur.execute(
                f"""SELECT entity, scope_key, scope_label, n_patterns,
                           total_count, computed_at
                    FROM {TABLE} ORDER BY entity, total_count DESC"""
            )
        return [
            {
                "entity": r[0], "scope_key": r[1], "scope_label": r[2],
                "n_patterns": int(r[3] or 0), "total_count": int(r[4] or 0),
                "computed_at": r[5].isoformat() if r[5] else None,
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def get_thresholds(entity: str | None = None) -> Optional[dict]:
    """Percentile cut-offs over every pattern size seen during enumeration.

    Absolute thresholds are a guess about data nobody has seen. Percentiles are
    self-calibrating: "very high" always means the top few percent of patterns
    that actually exist, whatever the scale of the book.
    """
    from sdg import packs

    entity = entity or packs.active().primary
    conn = _open()
    try:
        cur = conn.cursor()
        if not _exists(cur):
            return None
        cur.execute(
            f"SELECT detail_json FROM {TABLE} WHERE entity = ? AND scope_key = 'ALL'",
            entity,
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        try:
            detail = json.loads(row[0])
        except ValueError:
            # A damaged stored row: bands fall back to fixed shares, which the
            # analysis says ("no baseline"); recomputing the baseline repairs it.
            log.warning("The stored thresholds for %s are not valid JSON; "
                        "recompute the baseline to repair them", entity)
            return None
        th = detail.get("thresholds") if isinstance(detail, dict) else None
        return th if th and th.get("p95") else None
    finally:
        conn.close()

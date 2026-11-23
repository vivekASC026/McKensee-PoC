"""Generation requests: what was written, for whom, and how to write it again.

A request is one set of people a pattern generates. Its seed is the
installation's seed, the pattern and the moment the request was started, so a
new request is new people and the same request is the same people in every
file type written under it -- an 834 today, an 837 next month.

Nothing generated is kept. A request holds only what regenerates its files:
the seed, the pattern as it was, and for each file the export's parameters
and the rules, layout or sample config it was written through, each kept once
by its digest (`generation_snapshots`), however many files used it. A file's
fingerprint (SHA-256 of what was written) says whether writing it again gave
the same file, and the SDM build it was written by says why not when it did
not.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pyodbc

from sdg.analyzer.config import SQL_SCHEMA, get_connection_string

REQUESTS = f"{SQL_SCHEMA}.generation_requests"
FILES = f"{SQL_SCHEMA}.generation_request_files"
SNAPSHOTS = f"{SQL_SCHEMA}.generation_snapshots"

DDL = [
    f"""
IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'{REQUESTS}') AND type = N'U')
CREATE TABLE {REQUESTS} (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    pattern_key   NVARCHAR(100) NOT NULL,      -- 12 | live:<digest> | scenarios:<ids> | plan | load:<db>
    started       NVARCHAR(40)  NOT NULL,      -- the moment in the seed, as written
    pattern_id    INT           NULL,
    pattern_name  NVARCHAR(400) NULL,
    subject       NVARCHAR(20)  NULL,          -- members | claims | load: what it generated
    seed          NVARCHAR(400) NOT NULL,
    pattern_hash  CHAR(64)      NULL,          -- the pattern as it was, in generation_snapshots
    created_by    NVARCHAR(100) NULL,
    created_at    DATETIME2     NOT NULL DEFAULT GETDATE(),
    parent_id     INT           NULL,          -- claims for this request's members
    CONSTRAINT UQ_generation_requests UNIQUE (pattern_key, started)
);
""",
    f"""
IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'{FILES}') AND type = N'U')
CREATE TABLE {FILES} (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    request_id    INT           NOT NULL,
    file_name     NVARCHAR(400) NOT NULL,
    format        NVARCHAR(100) NOT NULL,      -- csv | fixed | edi:834 | sample:<config id> | database
    subject       NVARCHAR(20)  NOT NULL,      -- members | claims
    params_json   NVARCHAR(MAX) NOT NULL,      -- the export, as it was asked for
    snapshots     NVARCHAR(MAX) NOT NULL,      -- {{kind: digest}} of what it was written through
    sdm_version   NVARCHAR(40)  NULL,
    sdm_build     CHAR(16)      NULL,          -- digest of the code and profiles
    fingerprint   CHAR(64)      NOT NULL,      -- SHA-256 of the file
    bytes         BIGINT        NOT NULL,
    replay_of     INT           NULL,          -- the file this wrote again
    matched       BIT           NULL,          -- and whether it came out the same
    written_by    NVARCHAR(100) NULL,
    written_at    DATETIME2     NOT NULL DEFAULT GETDATE()
);
""",
    f"""
IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'{SNAPSHOTS}') AND type = N'U')
CREATE TABLE {SNAPSHOTS} (
    digest        CHAR(64)      NOT NULL PRIMARY KEY,
    kind          NVARCHAR(40)  NOT NULL,      -- rules | layout | config | pattern
    body          NVARCHAR(MAX) NOT NULL,
    created_at    DATETIME2     NOT NULL DEFAULT GETDATE()
);
""",
    # Tables made before claims could be written for a request's members.
    f"IF COL_LENGTH(N'{REQUESTS}', N'parent_id') IS NULL ALTER TABLE {REQUESTS} ADD parent_id INT NULL",
]

# A moment as the browser writes one: 2026-10-23T14:05:09.123Z.
_STARTED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z?$")

_ensured = False


def _conn():
    return pyodbc.connect(get_connection_string())


def ensure_tables() -> None:
    global _ensured
    if _ensured:
        return
    conn = _conn()
    try:
        for ddl in DDL:
            conn.execute(ddl)
        conn.commit()
        _ensured = True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# What a request is
# ---------------------------------------------------------------------------

def checked_started(started: str) -> str:
    started = (started or "").strip()
    if not _STARTED.match(started):
        raise ValueError(f"{started!r} is not a moment a request can start at.")
    return started


def seed_for(base: str, pattern_key: str, started: str) -> str:
    """The installation's seed, the pattern and the moment: new people per request."""
    return f"{base}:{pattern_key}:{started}"


def pattern_key(*, pattern_id: int | None = None, live: dict[str, Any] | None = None,
                scenarios: list[dict[str, Any]] | None = None) -> str:
    """What names the pattern in a request's seed and identity."""
    if pattern_id is not None:
        return str(int(pattern_id))
    if scenarios:
        return "scenarios:" + "-".join(str(int(p["pattern_id"])) for p in scenarios)
    if live is not None:
        # An analysis's pattern has no id until saved; what rebuilds it names it.
        address = {k: live.get(k) for k in ("domain", "filters", "k", "cluster_id", "claim_type")}
        return "live:" + digest(address)[:16]
    return "plan"


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Kept as the text it was, so a rule reads back the number it held.
        return {"__decimal__": str(value)}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not kept in a snapshot")


def _restored(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__decimal__"}:
            return Decimal(value["__decimal__"])
        return {k: _restored(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restored(v) for v in value]
    return value


def dumps(body: Any) -> str:
    return json.dumps(body, sort_keys=True, default=_plain, separators=(",", ":"))


def loads(text: str) -> Any:
    return _restored(json.loads(text))


def digest(body: Any) -> str:
    return hashlib.sha256(dumps(body).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------

def keep_snapshot(kind: str, body: Any) -> str:
    """Kept once by its digest; the same rules for a thousand files are one row."""
    ensure_tables()
    text = dumps(body)
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn = _conn()
    try:
        if conn.execute(f"SELECT 1 FROM {SNAPSHOTS} WHERE digest = ?", key).fetchone() is None:
            try:
                conn.execute(f"INSERT INTO {SNAPSHOTS} (digest, kind, body) VALUES (?, ?, ?)",
                             key, kind, text)
                conn.commit()
            except pyodbc.IntegrityError:
                conn.rollback()  # kept by another write a moment ago
        return key
    finally:
        conn.close()


def snapshot(key: str) -> Any:
    """A snapshot by its key. The key is the snapshot's identity -- what file
    records point at -- and is not checked against the body: a body rewritten
    in place (`rewrite_snapshot`) keeps the key its files name."""
    ensure_tables()
    conn = _conn()
    try:
        row = conn.execute(f"SELECT body FROM {SNAPSHOTS} WHERE digest = ?", key).fetchone()
        if row is None:
            raise LookupError(f"Snapshot {key[:12]} is missing.")
        return loads(row[0])
    finally:
        conn.close()


def unsaved_with_sample(only: set[str] | None = None) -> list[tuple[str, Any]]:
    """Snapshots of unsaved configurations that still hold their sample's
    text (kept before 2026-11-06), by key: (key, body). Only those keys when
    `only` names some."""
    ensure_tables()
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT digest, body FROM {SNAPSHOTS} WHERE kind = 'unsaved' "
            f"AND body LIKE '%\"sample\\_text\":%' ESCAPE '\\' ORDER BY created_at, digest"
        ).fetchall()
    finally:
        conn.close()
    found = []
    for key, text in rows:
        key = str(key).strip()
        if only is not None and key not in only:
            continue
        body = loads(text)
        if isinstance(body, dict) and "sample_text" in body:
            found.append((key, body))
    return found


def rewrite_snapshot(key: str, kind: str, body: Any) -> int:
    """One snapshot's body replaced in place, under the key it has (see
    `snapshot`): only the row of that key and kind. How many rows changed."""
    ensure_tables()
    conn = _conn()
    try:
        cur = conn.execute(f"UPDATE {SNAPSHOTS} SET body = ? WHERE digest = ? AND kind = ?",
                           dumps(body), key, kind)
        changed = cur.rowcount
        if changed > 1:
            conn.rollback()
            raise RuntimeError(f"Snapshot {key[:12]} is not one row; nothing was changed.")
        conn.commit()
        return changed
    finally:
        conn.close()


def open_request(*, pattern_key: str, started: str, seed: str, pattern_id: int | None,
                 pattern_name: str | None, subject: str | None, pattern_body: Any,
                 created_by: str | None, parent_id: int | None = None) -> dict[str, Any]:
    """The request for this pattern and moment: the one already open, or a new one."""
    ensure_tables()
    started = checked_started(started)
    found = find(pattern_key, started)
    if found is not None:
        return found
    pattern_hash = keep_snapshot("pattern", pattern_body) if pattern_body is not None else None
    conn = _conn()
    try:
        try:
            conn.execute(
                f"INSERT INTO {REQUESTS} (pattern_key, started, pattern_id, pattern_name, "
                f"subject, seed, pattern_hash, created_by, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                pattern_key, started, pattern_id, (pattern_name or "")[:400] or None,
                subject, seed, pattern_hash, created_by, parent_id,
            )
            conn.commit()
        except pyodbc.IntegrityError:
            conn.rollback()  # opened by the file written alongside this one
    finally:
        conn.close()
    found = find(pattern_key, started)
    assert found is not None
    return found


def find(pattern_key: str, started: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = _conn()
    try:
        cur = conn.execute(f"SELECT * FROM {REQUESTS} WHERE pattern_key = ? AND started = ?",
                           pattern_key, started)
        row = cur.fetchone()
        return _row(cur, row) if row else None
    finally:
        conn.close()


def get(request_id: int) -> dict[str, Any] | None:
    ensure_tables()
    conn = _conn()
    try:
        cur = conn.execute(f"SELECT * FROM {REQUESTS} WHERE id = ?", request_id)
        row = cur.fetchone()
        return _row(cur, row) if row else None
    finally:
        conn.close()


def record_file(*, request_id: int, file_name: str, format: str, subject: str,
                params: dict[str, Any], snapshots: dict[str, str], sdm_version: str,
                sdm_build: str, fingerprint: str, size: int, written_by: str | None,
                replay_of: int | None = None) -> dict[str, Any]:
    """A file written under a request. A file written again says whether it matched."""
    ensure_tables()
    matched = None
    if replay_of is not None:
        original = get_file(replay_of)
        matched = bool(original and original["fingerprint"] == fingerprint)
    conn = _conn()
    try:
        cur = conn.execute(
            f"INSERT INTO {FILES} (request_id, file_name, format, subject, params_json, "
            f"snapshots, sdm_version, sdm_build, fingerprint, bytes, replay_of, matched, "
            f"written_by) OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            request_id, file_name[:400], format, subject, dumps(params), dumps(snapshots),
            sdm_version, sdm_build, fingerprint, size, replay_of, matched, written_by,
        )
        new_id = int(cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    return {"id": new_id, "matched": matched}


def get_file(file_id: int) -> dict[str, Any] | None:
    ensure_tables()
    conn = _conn()
    try:
        cur = conn.execute(f"SELECT * FROM {FILES} WHERE id = ?", file_id)
        row = cur.fetchone()
        if row is None:
            return None
        item = _row(cur, row)
        item["params"] = loads(item.pop("params_json"))
        item["snapshots"] = loads(item["snapshots"])
        return item
    finally:
        conn.close()


def first_file(request_id: int) -> dict[str, Any] | None:
    """The first file written under a request (not a file written again): what
    its people were made with."""
    ensure_tables()
    conn = _conn()
    try:
        row = conn.execute(
            f"SELECT TOP (1) id FROM {FILES} WHERE request_id = ? AND replay_of IS NULL "
            f"ORDER BY written_at, id", request_id).fetchone()
    finally:
        conn.close()
    return get_file(int(row[0])) if row else None


def list_requests(*, day: str | None = None, pattern_id: int | None = None,
                  user: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Requests with at least one file written, newest first, each with its files."""
    ensure_tables()
    where, args = ["EXISTS (SELECT 1 FROM {files} f WHERE f.request_id = r.id)"], []
    if day:
        date.fromisoformat(day)  # refused unless it is a date
        where.append("CAST(r.created_at AS DATE) = ?")
        args.append(day)
    if pattern_id is not None:
        where.append("r.pattern_id = ?")
        args.append(pattern_id)
    if user:
        where.append("r.created_by = ?")
        args.append(user)
    conn = _conn()
    try:
        cur = conn.execute(
            f"SELECT TOP ({int(limit)}) r.* FROM {REQUESTS} r WHERE "
            + " AND ".join(where).format(files=FILES)
            + " ORDER BY r.created_at DESC, r.id DESC",
            *args,
        )
        requests = [_row(cur, row) for row in cur.fetchall()]
        if not requests:
            return []
        marks = ",".join("?" for _ in requests)
        cur = conn.execute(
            f"SELECT id, request_id, file_name, format, subject, sdm_version, sdm_build, "
            f"fingerprint, bytes, replay_of, matched, written_by, written_at, "
            f"TRY_CAST(JSON_VALUE(params_json, '$.members') AS INT) AS members, "
            # A database load: how many rows it wrote, and into which database.
            f"TRY_CAST(JSON_VALUE(params_json, '$.rows') AS INT) AS rows, "
            f"JSON_VALUE(params_json, '$.target.database') AS target, "
            # A file written again that differs: what the store holds differently.
            f"JSON_VALUE(snapshots, '$.changed') AS changed FROM {FILES} "
            f"WHERE request_id IN ({marks}) ORDER BY written_at, id",
            *[r["id"] for r in requests],
        )
        files: dict[int, list[dict[str, Any]]] = {}
        for row in cur.fetchall():
            item = _row(cur, row)
            if item.get("matched") is not None:
                item["matched"] = bool(item["matched"])
            item["changed"] = snapshot(item["changed"]) if item.get("changed") else []
            files.setdefault(item["request_id"], []).append(item)
        for request in requests:
            request["files"] = files.get(request["id"], [])
            originals = [f for f in request["files"] if f["replay_of"] is None]
            # Its people: the first file's members and subject.
            request["members"] = originals[0]["members"] if originals else None
            request["of"] = originals[0]["subject"] if originals else request.get("subject")
        return requests
    finally:
        conn.close()


def delete_request(request_id: int) -> None:
    """A request and its files, by id -- for a test to remove what it wrote --
    and the claims requests written for its members, which mean nothing
    without it. Snapshots stay: another request may share them."""
    ensure_tables()
    conn = _conn()
    try:
        children = [int(r[0]) for r in conn.execute(
            f"SELECT id FROM {REQUESTS} WHERE parent_id = ?", request_id).fetchall()]
    finally:
        conn.close()
    for child in children:
        delete_request(child)
    conn = _conn()
    try:
        conn.execute(f"DELETE FROM {FILES} WHERE request_id = ?", request_id)
        conn.execute(f"DELETE FROM {REQUESTS} WHERE id = ?", request_id)
        conn.commit()
    finally:
        conn.close()


def _row(cursor, row) -> dict[str, Any]:
    out = dict(zip([d[0] for d in cursor.description], row))
    for key in ("created_at", "written_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat(timespec="seconds")
    return out

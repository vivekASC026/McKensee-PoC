"""Generation configs for files modeled on a sample.

A config is the sample it was modeled on and the places that take new values
(for X12, `genSegments`). It is built from a master -- a masking config, a
standard file, or another generation config -- and saved under a name of its
own; the master is never altered. A saved config is the person's own: it can
be changed in place (`update`, its name too) or removed.
One table for every format: `format` says how the config and sample are read
(x12), `kind` what the sample is within it (834, 837).
"""

from __future__ import annotations

import json
from typing import Any

import pyodbc

from sdg.analyzer.config import SQL_SCHEMA, get_connection_string

TABLE = f"{SQL_SCHEMA}.sample_gen_configs"

DDL = f"""
IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'{TABLE}') AND type = N'U')
CREATE TABLE {TABLE} (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    name         NVARCHAR(200) NOT NULL,
    format       NVARCHAR(20)  NOT NULL,       -- x12
    kind         NVARCHAR(40)  NOT NULL,       -- 834 | 837
    version      NVARCHAR(40)  NULL,           -- 005010X222A1 ...
    based_on     NVARCHAR(400) NULL,           -- the master it was built from
    config_json  NVARCHAR(MAX) NOT NULL,       -- genSegments
    sample_name  NVARCHAR(400) NULL,
    sample_text  NVARCHAR(MAX) NOT NULL,       -- the file it is modeled on
    created_by   NVARCHAR(100) NULL,
    created_at   DATETIME2     NOT NULL DEFAULT GETDATE(),
    CONSTRAINT UQ_sample_gen_configs_name UNIQUE (name)
);
"""

SUMMARY = ("id", "name", "format", "kind", "version", "based_on", "sample_name",
           "created_by", "created_at")


def _conn():
    return pyodbc.connect(get_connection_string())


def ensure_table() -> None:
    conn = _conn()
    try:
        conn.execute(DDL)
        conn.commit()
    finally:
        conn.close()


def _row(cursor, row) -> dict[str, Any]:
    out = dict(zip([d[0] for d in cursor.description], row))
    if out.get("created_at") is not None:
        out["created_at"] = out["created_at"].isoformat(timespec="seconds")
    return out


def list_configs() -> list[dict[str, Any]]:
    from sdg.writers import samples

    ensure_table()
    conn = _conn()
    try:
        cur = conn.execute(
            f"SELECT {', '.join(SUMMARY)}, config_json FROM {TABLE} ORDER BY name")
        out = []
        for row in cur.fetchall():
            item = _row(cur, row)
            body = json.loads(item.pop("config_json") or "{}")
            if not samples.offered(item["format"]):
                continue  # a format the active business pack does not offer
            fmt = samples.FORMATS.get(item["format"])
            item["positions"] = fmt.positions(body) if fmt else 0
            item["subject"] = fmt.subject(item["kind"]) if fmt else None
            item["validation"] = fmt.VALIDATION if fmt else None
            item["extension"] = fmt.EXTENSION if fmt else ""
            out.append(item)
        return out
    finally:
        conn.close()


def get_config(config_id: int) -> dict[str, Any] | None:
    ensure_table()
    conn = _conn()
    try:
        cur = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", config_id)
        row = cur.fetchone()
        if row is None:
            return None
        item = _row(cur, row)
        item["config"] = json.loads(item.pop("config_json"))
        return item
    finally:
        conn.close()


def save_as(*, name: str, format: str, kind: str, version: str | None, based_on: str | None,
            config: dict[str, Any], sample_name: str | None, sample_text: str,
            created_by: str | None = "user") -> dict[str, Any]:
    """A new config. A name already taken is refused, never overwritten."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the config a name.")
    ensure_table()
    conn = _conn()
    try:
        taken = conn.execute(f"SELECT id FROM {TABLE} WHERE name = ?", name).fetchone()
        if taken:
            raise ValueError(f"A config named {name!r} already exists; save under another name.")
        cur = conn.execute(
            f"INSERT INTO {TABLE} (name, format, kind, version, based_on, config_json, "
            f"sample_name, sample_text, created_by) OUTPUT INSERTED.id "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            name, format, kind, version, based_on, json.dumps(config), sample_name,
            sample_text, created_by,
        )
        new_id = int(cur.fetchone()[0])
        conn.commit()
        return {"id": new_id, "name": name, "created": True}
    finally:
        conn.close()


def update(config_id: int, *, name: str, format: str, kind: str, version: str | None,
           config: dict[str, Any], sample_name: str | None, sample_text: str) -> dict[str, Any]:
    """A saved config changed in place: its places, its sample, its name. What it
    was built from stays as it was. A name another config has is refused."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the config a name.")
    ensure_table()
    conn = _conn()
    try:
        if conn.execute(f"SELECT id FROM {TABLE} WHERE id = ?", config_id).fetchone() is None:
            raise LookupError(f"No sample config {config_id}.")
        taken = conn.execute(f"SELECT id FROM {TABLE} WHERE name = ? AND id <> ?",
                             name, config_id).fetchone()
        if taken:
            raise ValueError(f"Another config is named {name!r}; choose another name.")
        conn.execute(
            f"UPDATE {TABLE} SET name = ?, format = ?, kind = ?, version = ?, config_json = ?, "
            f"sample_name = ?, sample_text = ? WHERE id = ?",
            name, format, kind, version, json.dumps(config), sample_name, sample_text, config_id,
        )
        conn.commit()
        return {"id": config_id, "name": name, "updated": True}
    finally:
        conn.close()


def delete_config(config_id: int) -> bool:
    ensure_table()
    conn = _conn()
    try:
        cur = conn.execute(f"DELETE FROM {TABLE} WHERE id = ?", config_id)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

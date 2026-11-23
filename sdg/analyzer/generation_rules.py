"""The reference lists rules draw from: names, streets, addresses.

Read from the store's sdm.lkp_* tables. Values are made by the engine's
generators (sdg.engine.rules); this module only reads the lists.
"""

import pandas as pd
import pyodbc

from .config import get_connection_string, SQL_SCHEMA
from sdg.analyzer.sql import read_sql

# SQL Server lookup table name mapping
_SQL_LOOKUP_MAP = {
    "first_names": "lkp_first_names",
    "last_names": "lkp_last_names",
    "street_names": "lkp_street_names",
    "zip_city_state": "lkp_zip_city_state",
}

_lookup_cache = {}


def _load_lookup(name: str) -> pd.DataFrame:
    """Load a lookup by name from SQL Server. Cached."""
    if name in _lookup_cache:
        return _lookup_cache[name]

    sql_table = _SQL_LOOKUP_MAP.get(name)
    if not sql_table:
        raise ValueError(f"Unknown lookup: '{name}'. Available: {list(_SQL_LOOKUP_MAP.keys())}")

    conn = pyodbc.connect(get_connection_string())
    try:
        # In the table's own order, said: a value is drawn by its place in the
        # list, so a list read in another order (after an index is rebuilt)
        # would give a file written again other names.
        df = _ordered(conn, sql_table)
    finally:
        conn.close()

    drop_cols = [c for c in ("id", "created_at") if c in df.columns]
    df = df.drop(columns=drop_cols)
    _lookup_cache[name] = df
    return df


def _ordered(conn, sql_table: str) -> pd.DataFrame:
    columns = [r[0] for r in conn.cursor().execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        (SQL_SCHEMA, sql_table)).fetchall()]
    if not columns:
        raise LookupError(f"Invalid object name {SQL_SCHEMA}.{sql_table}: the list is missing.")
    order = "[id]" if "id" in columns else ", ".join(
        f"[{c}]" for c in columns if c != "created_at")
    return read_sql(f"SELECT * FROM {SQL_SCHEMA}.{sql_table} ORDER BY {order}", conn)


def missing_list(error: Exception) -> bool:
    """Whether a list could not be read because the store has no such table."""
    text = str(error)
    return "Invalid object name" in text or "42S02" in text


def name_list(source: str, sex: str | None = None) -> list[str]:
    """First or last names, in the table's order; first names of one sex if asked."""
    frame = _load_lookup(source)
    if sex and "gender" in frame.columns:
        chosen = frame[frame["gender"].astype(str).str.strip().str.upper() == sex]
        if not chosen.empty:
            frame = chosen
    return [str(v).strip() for v in frame["name"].tolist() if str(v).strip()]


def street_list() -> list[tuple[str, str]]:
    """Street names with their kind: (Oak, Ave)."""
    frame = _load_lookup("street_names")
    kinds = frame["type"] if "type" in frame.columns else [""] * len(frame)
    return [(str(s).strip(), str(k or "").strip()) for s, k in zip(frame["street"], kinds)]


# A national ZIP list runs to tens of thousands; an even spread of this many
# keeps every rule small while still reaching across the list.
ADDRESS_LIMIT = 1000
_addresses: list[dict] | None = None


def address_records() -> list[dict]:
    """ZIP, city, state and county as whole records, ordered by ZIP.

    Read once: the list does not change while the tool runs. Every address
    field draws from this one list with one key, so a ZIP is never written
    apart from its own city and state.
    """
    global _addresses
    if _addresses is None:
        frame = _load_lookup("zip_city_state")
        rows = [{"ADDRESS_ZIP": str(r["zip"]).strip(), "ADDRESS_CITY": str(r["city"]).strip(),
                 "ADDRESS_STATE": str(r["state"]).strip(),
                 "ADDRESS_COUNTY": str(r.get("county") or "").strip()}
                for r in frame.to_dict("records")]
        rows.sort(key=lambda r: (r["ADDRESS_ZIP"], r["ADDRESS_CITY"], r["ADDRESS_STATE"]))
        if len(rows) > ADDRESS_LIMIT:
            # A spread chosen by each address's own digest, not by its place:
            # adding one ZIP to the list changes at most one address chosen,
            # rather than shifting every one after it.
            chosen = sorted(rows, key=_spread)[:ADDRESS_LIMIT]
            keep = {id(r) for r in chosen}
            rows = [r for r in rows if id(r) in keep]
        _addresses = rows
    return _addresses


def _spread(row: dict) -> str:
    import hashlib

    key = f"{row['ADDRESS_ZIP']}|{row['ADDRESS_CITY']}|{row['ADDRESS_STATE']}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_available_lookups() -> list[str]:
    """List available lookup names from SQL Server."""
    lookups = set()
    conn = pyodbc.connect(get_connection_string())
    cur = conn.cursor()
    cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME LIKE 'lkp_%'",
        (SQL_SCHEMA,)
    )
    for row in cur.fetchall():
        short_name = row[0].replace("lkp_", "", 1)
        lookups.add(short_name)
    conn.close()
    return sorted(lookups)

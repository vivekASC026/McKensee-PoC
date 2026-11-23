"""Load a business's records from the pattern store, filtered in SQL.

One loader for every entity the business pack names. The pack's features.json
says, under `loaders.<entity>`, where an entity's rows are and how they are
read: the table and the columns analysis reads, the order the rows are
clustered in, the newest row per key where a key has several, the filters a
scope or a condition becomes, and what is derived, joined and stripped once
the rows are in, and the scope levels a person narrows by, each one's
choices read from the same table. Nothing here names a business's columns.

SQL Server only. No full table loads: every query is filtered at the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd
import pyodbc

from sdg import packs
from sdg.analyzer.services.cache import ttl_cache
from sdg.analyzer.sql import literal, read_sql

from .config import DEFAULT_REFERENCE_DATE, SQL_SCHEMA, get_connection_string

# A year and a month in days, as elapsed time has always been measured here.
_DAYS = {"years": 365.25, "months": 30.44}

_PRESENT: dict[str, set[str]] = {}

# What a load returns, by version. Raise it whenever a change here returns
# other rows or other values for the same store: every cached analysis keys on
# it (disk_cache.data_fingerprint), so none answers from the loader before.
# 2: claim text trimmed (procedure codes were read padded), newest-row ties
# broken, the grid's equal counts in a fixed order.
READ_VERSION = 2


def no_rows(message: str) -> ValueError:
    """No rows match the filters. A plain ValueError still, so every caller
    that reads it as a refused request is unchanged; a walk tells it apart from
    a filter or configuration error with is_no_rows."""
    error = ValueError(message)
    error.no_rows = True  # type: ignore[attr-defined]
    return error


def is_no_rows(error: BaseException) -> bool:
    """True when the error is the loader's "no rows match" refusal."""
    return isinstance(error, ValueError) and getattr(error, "no_rows", False) is True


@dataclass
class Loaded:
    """An entity's rows as analysis reads them, and what they were filtered by."""
    rows: pd.DataFrame
    filters: dict[str, Any]
    labels: Optional[pd.DataFrame] = None
    family: Optional[pd.DataFrame] = None


def config(entity: str) -> dict[str, Any]:
    """How the active pack reads an entity (features.json, loaders.<entity>)."""
    loaders = packs.held("features:loaders")
    if entity.startswith("_") or entity not in loaders:
        raise ValueError(f"The {packs.active().name} pack reads no {entity!r}.")
    return loaders[entity]


def table(entity: str) -> str:
    return f"{SQL_SCHEMA}.{config(entity)['table']}"


def connect():
    return pyodbc.connect(get_connection_string())


def present_columns(name: str) -> set[str]:
    """The columns a store table has, read once per process."""
    if name not in _PRESENT:
        schema, bare = name.split(".", 1)
        conn = connect()
        try:
            rows = read_sql(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_SCHEMA = {literal(schema)} AND TABLE_NAME = {literal(bare)}", conn)
        finally:
            conn.close()
        found = set(rows["COLUMN_NAME"])
        if not found:
            # No table yet (or none this login can see): asked again next time,
            # not remembered as a table with no columns until a restart.
            return found
        _PRESENT[name] = found
    return _PRESENT[name]


def select_list(name: str, wanted: list[str]) -> str:
    """The wanted columns the table has, bracketed, in the order wanted."""
    have = present_columns(name)
    return ", ".join(f"[{c}]" for c in wanted if c in have) or "*"


def int_list(values) -> list[int]:
    """Numeric IN-list values, skipping anything that isn't one.

    These are inlined into raw SQL via int(), so a blank or non-numeric entry
    raised `invalid literal for int() with base 10: ''` from inside query
    construction, naming neither the filter nor the caller. Unusable values
    are dropped; if none survive, the caller omits the clause rather than
    emitting `IN ()`.
    """
    out = []
    for v in values or []:
        if v is None:
            continue
        text = str(v).strip()
        if not text:
            continue
        try:
            out.append(int(float(text)))
        except (TypeError, ValueError, OverflowError):  # "inf" is no id
            continue
    return out


def as_int(value, label):
    """A single numeric bound, or None when it isn't usable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a number, got {value!r}.")


def _given(entity: str, filters: dict[str, Any]) -> list[tuple[dict, Any]]:
    """Each of the entity's filters with its value, in the pack's order.

    A filter the entity does not have is refused, as a keyword the old
    per-entity loaders did not take was.
    """
    known = config(entity)["filters"]
    unknown = sorted(set(filters) - {f["key"] for f in known})
    if unknown:
        raise TypeError(f"{entity} are not filtered by {', '.join(unknown)}.")
    return [(f, filters.get(f["key"])) for f in known]


def _bound(kind: str) -> bool:
    return kind in ("at_least", "at_most")


def where(entity: str, filters: dict[str, Any], keep_blank: bool = False) -> str:
    """The WHERE clause for these filters, values inlined as literals.
    SQL Server ignores trailing spaces in CHAR comparisons.

    `keep_blank` sends a blank value as the value it is, where a filter would
    skip it: a scope choice can be blank, and choosing it narrows to the blank
    rows rather than to none of the narrowing."""
    parts = []
    for f, value in _given(entity, filters):
        column, kind = f["column"], f["kind"]
        if _bound(kind):
            bound = as_int(value, f["key"])
            if bound is not None:
                parts.append(f"{column} {'>=' if kind == 'at_least' else '<='} {bound}")
            continue
        if not value:
            continue
        # One value can arrive bare ("F" rather than ["F"], 12 rather than
        # [12]); a bare string would otherwise be read character by character.
        values = [value] if isinstance(value, (str, int, float)) else value
        if kind == "ids":
            ids = ",".join(str(x) for x in int_list(values))
            if ids:
                parts.append(f"{column} IN ({ids})")
            elif any(x is not None and str(x).strip() for x in values):
                # Ids were sent and none is a number ("abc", "1,2"): leaving
                # the clause out would read every row as if it were theirs.
                raise ValueError(f"{f['key']} takes numbers, not {value!r}.")
            continue
        if f.get("skip_blank") and not keep_blank:
            values = [x for x in values if str(x).strip()]
            if not values:
                continue
        parts.append(f"{column} IN ({','.join(literal(x) for x in values)})")
    return f"WHERE {' AND '.join(parts)}" if parts else ""


def trimmed(column: pd.Series) -> pd.Series:
    """Text with its padding trimmed; NULL stays NULL, other types as they are."""
    if column.dtype == object or pd.api.types.is_string_dtype(column):
        # As text (pandas' own text dtype), then NULL put back: astype(str)
        # alone made it "None" before pandas 3.
        return column.astype("str").str.strip().where(column.notna())
    return column


def _home_addresses(cfg: dict[str, Any], rows_table: str, where_sql: str, conn):
    """Each record's home address, named as the profile's geography reads it;
    None where the store has no address table."""
    spec = cfg.get("addresses")
    if not spec:
        return None
    name = f"{SQL_SCHEMA}.{spec['table']}"
    key, (role_column, role) = spec["key"], spec["role"]
    have = present_columns(name)
    if not {key, role_column, *spec["columns"]} <= have:
        return None
    named = ", ".join(f"{src} AS {as_}" for src, as_ in spec["columns"].items())
    homes = read_sql(f"""
        SELECT {key}, {named}
        FROM {name}
        WHERE {role_column} = {literal(role)} AND {key} IN (SELECT {key} FROM {rows_table} {where_sql})
        ORDER BY {key}, {', '.join(spec['columns'])}
    """, conn)
    for col in spec["columns"].values():
        homes[col] = homes[col].astype("string").str.strip()
    if spec.get("zip"):
        homes[spec["zip"]] = homes[spec["zip"]].str[:5]
    return homes.drop_duplicates(key)


def _derive(rows: pd.DataFrame, cfg: dict[str, Any], ref: pd.Timestamp) -> None:
    for d in cfg.get("derive", []):
        if d["op"] != "elapsed":
            raise ValueError(f"No derived measure {d['op']!r}.")
        if d["from"] in rows.columns:
            days = (ref - pd.to_datetime(rows[d["from"]], errors="coerce")).dt.days
            rows[d["column"]] = (days / _DAYS[d["unit"]]).clip(lower=0)
        else:
            rows[d["column"]] = 0


def load(entity: str, filters: dict[str, Any], reference_date=None) -> Loaded:
    """Only the entity's rows matching the filters, as analysis reads them.

    Ordered by the entity's key, because the rows become a matrix and the
    matrix is clustered: k-means is seeded, so the same matrix always gives
    the same answer, but "the same matrix" includes the order, and a SELECT
    without ORDER BY has none to rely on.
    """
    cfg = config(entity)
    given = _given(entity, filters)
    name = table(entity)
    columns = select_list(name, packs.held(f"features:{cfg['columns']}"))
    clause = where(entity, filters)
    order = ", ".join(cfg["order"])

    conn = connect()
    family = homes = None
    try:
        latest = cfg.get("latest")
        if latest:
            # Two rows equally newest: the same one every time. The pack's
            # columns first, then every other column read, so rows still tied
            # are the same in all that is read and either is the one.
            first = list(latest.get("then", []))
            rest = [c.strip("[]") for c in columns.split(", ") if c != "*"]
            then = "".join(f", {c}" for c in first + [c for c in rest
                                                      if c not in first + [latest["by"]]])
            rows = read_sql(f"""
                WITH ranked AS (
                    SELECT {columns},
                           ROW_NUMBER() OVER (PARTITION BY {latest['key']}
                                              ORDER BY {latest['by']} DESC{then}) AS _rn
                    FROM {name}
                    {clause}
                )
                SELECT * FROM ranked WHERE _rn = 1 ORDER BY {order}
            """, conn)
            if "_rn" in rows.columns:
                rows.drop(columns=["_rn"], inplace=True)
        else:
            rows = read_sql(f"SELECT {columns} FROM {name} {clause} ORDER BY {order}", conn)
        if cfg.get("family"):
            family = read_sql(f"SELECT {', '.join(cfg['family'])} FROM {name} {clause}", conn)
        homes = _home_addresses(cfg, name, clause, conn)
    finally:
        conn.close()
    if homes is not None and not rows.empty:
        rows = rows.merge(homes, on=cfg["addresses"]["key"], how="left")

    if rows.empty:
        asked = {f["key"]: v for f, v in given if v is not None}
        raise no_rows(cfg["empty"].format(filters=asked))

    for col in cfg.get("strip", []):
        # Text, as pandas 3 reads it (str) or as it read it before (object).
        # NULL stays NULL: as text it was "None", a value it never held.
        if col in rows.columns:
            rows[col] = trimmed(rows[col])

    reference_date = reference_date or DEFAULT_REFERENCE_DATE
    _derive(rows, cfg, pd.Timestamp(reference_date))

    if cfg.get("joined"):
        # What the tables beside the store add to a record (sdg.analyzer.provider_features).
        from sdg.analyzer import provider_features
        rows = provider_features.joined(cfg["joined"], rows, reference_date)

    used = {f["key"]: v for f, v in given
            if (v is not None if _bound(f["kind"]) else bool(v))}
    labels = None
    if cfg.get("labels"):
        labels = rows[[c for c in cfg["labels"] if c in rows.columns]].copy()
    return Loaded(rows=rows, filters=used, labels=labels, family=family)


def combination_sizes(entity: str, max_depth: int = 2, minimum: int = 0) -> pd.DataFrame:
    """Every scope combination with its count, one GROUP BY per depth.

    Discovery once built its combination list with a query per group, then
    loaded rows for every combination it found, including the many too small
    to produce anything worth ranking. This returns the whole grid with counts,
    so the walk can skip the small ones before loading anything and order the
    rest by size. The database aggregates; the application sees only distinct
    combinations.
    """
    cfg = config(entity)
    sizes = cfg["sizes"]
    levels = sizes["levels"][:max(1, min(max_depth, len(sizes["levels"])))]
    deepest = sizes["levels"][-1]
    first = set(sizes["levels"][0])
    ids = {f["column"] for f in cfg["filters"] if f["kind"] == "ids"}
    count = sizes["count"]
    name = table(entity)

    frames = []
    conn = connect()
    try:
        for depth, cols in enumerate(levels, start=1):
            select = ", ".join(f"RTRIM({c}) AS {c}" if sizes.get("trim") and c not in ids else c
                               for c in cols)
            group_by = ", ".join(cols)
            having = f"HAVING COUNT(*) >= {int(minimum)}" if minimum else ""
            df = read_sql(f"""
                SELECT {select}, COUNT(*) AS {count}
                FROM {name}
                GROUP BY {group_by}
                {having}
            """, conn)
            df["depth"] = depth
            for missing in (c for c in deepest if c not in first):
                if missing not in df.columns:
                    df[missing] = None
            frames.append(df)
    finally:
        conn.close()

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    # Largest first; equal counts in the order of their levels, so a walk
    # capped at N combinations takes the same N every time.
    by = [count, *[c for c in deepest if c in out.columns]]
    return out.sort_values(by, ascending=[False] + [True] * (len(by) - 1), kind="stable",
                           na_position="first").reset_index(drop=True)


def scope_levels(entity: str) -> list[dict[str, Any]]:
    """The entity's scope levels, broadest first, and the levels each narrows by."""
    return [{"level": s["level"], "takes": list(s["takes"])} for s in config(entity).get("scope", [])]


def _level(entity: str, level: str) -> dict[str, Any]:
    found = next((s for s in config(entity).get("scope", []) if s["level"] == level), None)
    if found is None:
        raise LookupError(f"{packs.active().term(entity, 'Many')} have no scope level {level!r}.")
    return found


def given(**filters) -> tuple:
    """Filters as a cache can key them: the ones set, each a tuple."""
    return tuple((k, tuple(v)) for k, v in filters.items() if v)


@ttl_cache(300)
def options(entity: str, level: str, chosen: tuple = ()) -> list[dict[str, str]]:
    """One scope level's choices, narrowed by the levels above it.

    `chosen` is given(...): the levels above and their values; any the level
    does not narrow by are left out. Each choice is {key, label}; a choice
    with no label of its own, or a blank or NULL one, is labeled by its key.
    A NULL key is no choice and is left out.
    """
    spec = _level(entity, level)
    trim = set(spec.get("trim", []))

    def col(c: str) -> str:
        return f"RTRIM({c}) AS {c}" if c in trim else c

    key = spec["key"]
    select = [col(key)]
    label = spec.get("label")
    named = spec.get("named")
    if named:
        bare = f"RTRIM({key})" if key in trim else key
        whens = " ".join(f"WHEN {literal(code)} THEN {literal(word)}"
                         for code, word in named["names"].items())
        select.append(f"CASE {bare} {whens} ELSE {bare} END AS {named['as']}")
        label = named["as"]
    elif label:
        select.append(col(label))
    clause = where(entity, {k: v for k, v in chosen if k in spec["takes"]}, keep_blank=True)
    conn = connect()
    try:
        df = read_sql(f"""
            SELECT DISTINCT {', '.join(select)}
            FROM {table(entity)}
            {clause}
            ORDER BY {spec['order']}
        """, conn)
    finally:
        conn.close()
    def said(value) -> str:
        # NULL is no text, not the words "None" or "nan"; a number column with
        # a NULL in it reads as floats, and 7.0 is the 7 it holds.
        if pd.isna(value):
            return ""
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value).strip()

    out = []
    for _, r in df.iterrows():
        if pd.isna(r[key]):
            continue  # a NULL key narrows to nothing: no choice to offer
        k = said(r[key])
        text = said(r[label]) if label else k
        out.append({"key": k, "label": text or k})
    return out


def overview(entity: str) -> dict[str, Any]:
    """The entity's headline counts, in one query. No rows are read."""
    counts = ",\n            ".join(
        f"COUNT(*) AS {as_}" if of == "*" else f"COUNT(DISTINCT {of}) AS {as_}"
        for as_, of in config(entity)["overview"])
    conn = connect()
    try:
        row = read_sql(f"""
        SELECT
            {counts}
        FROM {table(entity)}
    """, conn).iloc[0]
    finally:
        conn.close()
    return row.to_dict()


def highlights(entity: str) -> list[dict[str, Any]]:
    """What the screens list beside an entity's patterns as its most frequent
    values; none where the pack names none (features.json,
    loaders.<entity>.highlights)."""
    if entity.startswith("_") or entity not in packs.held("features:loaders"):
        return []  # an entity the pack reads no rows of has nothing to rank
    return [dict(h) for h in config(entity).get("highlights") or []]


@ttl_cache(300)
def highlight(entity: str, key: str, n: int = 30) -> list[dict[str, Any]]:
    """One highlight's n most frequent codes, with each one's description and
    how many rows have it: [{code, description, n}], most frequent first."""
    spec = next((h for h in highlights(entity) if h["key"] == key), None)
    if spec is None:
        raise ValueError(f"{packs.active().term(entity, 'Many')} have no highlight {key!r}.")
    code, description = spec["code"], spec["description"]
    conn = connect()
    try:
        df = read_sql(f"""
        SELECT TOP {int(n)} RTRIM({code}) AS {code},
               RTRIM({description}) AS {description},
               COUNT(*) AS cnt
        FROM {table(entity)}
        WHERE {code} IS NOT NULL AND {code} <> ''
        GROUP BY {code}, {description}
        ORDER BY cnt DESC
    """, conn)
    finally:
        conn.close()
    # A description is said as text, as the routes always said it.
    return [{"code": str(r[code]).strip(), "description": str(r[description]).strip(),
             "n": int(r["cnt"] or 0)} for _, r in df.iterrows()]

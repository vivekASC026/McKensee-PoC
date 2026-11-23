"""Load data from SQL Server with filter pushdown.

Hierarchy: Group (GRGR) → Plan Category (CSPD_CAT) → Plan (CSPI_ID/PLDS_DESC) → Product (PDPD_ID/PDDS_DESC)

SQL Server only. No full table loads — queries are filtered at the SQL level.
"""

import pandas as pd
import pyodbc

from sdg.analyzer.sql import literal, read_sql

from sdg.analyzer.config import (
    DEFAULT_REFERENCE_DATE,
    get_connection_string,
    SQL_SCHEMA,
)
from sdg.packs import held

_TABLE = f"{SQL_SCHEMA}.member_denorm"

# What analysis reads of a member, out of the fifty-odd columns the table has.
# Naming them, rather than SELECT *, halves what comes back over the wire on
# every load; the denorm tables exist to make these reads cheap.
MEMBER_COLUMNS = held("features:store.member_columns")

_PRESENT: dict[str, set[str]] = {}


def present_columns(table: str) -> set[str]:
    """The columns a store table has, read once per process."""
    if table not in _PRESENT:
        schema, name = table.split(".", 1)
        conn = _conn()
        try:
            rows = read_sql(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_SCHEMA = {literal(schema)} AND TABLE_NAME = {literal(name)}", conn)
        finally:
            conn.close()
        _PRESENT[table] = set(rows["COLUMN_NAME"])
    return _PRESENT[table]


def select_list(table: str, wanted: list[str]) -> str:
    """The wanted columns the table has, bracketed, in the order wanted."""
    have = present_columns(table)
    return ", ".join(f"[{c}]" for c in wanted if c in have) or "*"


def _conn():
    return pyodbc.connect(get_connection_string())


# ── Overview metrics (lightweight, no DataFrame) ─────────────────────────────

def get_overview_metrics():
    """Return dict of aggregate counts. No DataFrame created."""
    conn = _conn()
    row = read_sql(f"""
        SELECT
            COUNT(DISTINCT MEME_CK)   AS n_members,
            COUNT(DISTINCT GRGR_CK)   AS n_groups,
            COUNT(DISTINCT CSPD_CAT)  AS n_plan_categories,
            COUNT(DISTINCT CSPI_ID)   AS n_plans,
            COUNT(DISTINCT PDPD_ID)   AS n_products
        FROM {_TABLE}
    """, conn).iloc[0]
    conn.close()
    return row.to_dict()


# ── Cascading filter options (small queries, no full table) ──────────────────

def get_groups():
    """Return DataFrame of distinct GRGR_CK + GRGR_NAME."""
    conn = _conn()
    df = read_sql(f"""
        SELECT DISTINCT GRGR_CK, GRGR_NAME
        FROM {_TABLE}
        ORDER BY GRGR_NAME
    """, conn)
    conn.close()
    return df


def get_plan_categories(grgr_cks=None):
    """Return distinct CSPD_CAT + CSPD_CAT_DESC, filtered by group."""
    conn = _conn()
    where_parts = []
    if grgr_cks:
        ids = ",".join(str(x) for x in _int_list(grgr_cks))
        where_parts.append(f"GRGR_CK IN ({ids})")

    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    df = read_sql(f"""
        SELECT DISTINCT RTRIM(CSPD_CAT) AS CSPD_CAT, RTRIM(CSPD_CAT_DESC) AS CSPD_CAT_DESC
        FROM {_TABLE}
        {where}
        ORDER BY CSPD_CAT_DESC
    """, conn)
    conn.close()
    return df


def get_plans(grgr_cks=None, cspd_cats=None):
    """Return distinct CSPI_ID + PLDS_DESC, filtered by group/plan category."""
    conn = _conn()
    where_parts = []
    if grgr_cks:
        ids = ",".join(str(x) for x in _int_list(grgr_cks))
        where_parts.append(f"GRGR_CK IN ({ids})")
    if cspd_cats:
        vals = ",".join(literal(x) for x in cspd_cats)
        where_parts.append(f"CSPD_CAT IN ({vals})")

    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    df = read_sql(f"""
        SELECT DISTINCT RTRIM(CSPI_ID) AS CSPI_ID, RTRIM(PLDS_DESC) AS PLDS_DESC
        FROM {_TABLE}
        {where}
        ORDER BY PLDS_DESC
    """, conn)
    conn.close()
    return df


def get_products(grgr_cks=None, cspd_cats=None, cspi_ids=None):
    """Return distinct PDPD_ID + PDDS_DESC, filtered by hierarchy."""
    conn = _conn()
    where_parts = []
    if grgr_cks:
        ids = ",".join(str(x) for x in _int_list(grgr_cks))
        where_parts.append(f"GRGR_CK IN ({ids})")
    if cspd_cats:
        vals = ",".join(literal(x) for x in cspd_cats)
        where_parts.append(f"CSPD_CAT IN ({vals})")
    if cspi_ids:
        vals = ",".join(literal(x) for x in cspi_ids)
        where_parts.append(f"CSPI_ID IN ({vals})")

    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    df = read_sql(f"""
        SELECT DISTINCT RTRIM(PDPD_ID) AS PDPD_ID, RTRIM(PDDS_DESC) AS PDDS_DESC
        FROM {_TABLE}
        {where}
        ORDER BY PDDS_DESC
    """, conn)
    conn.close()
    return df


# ── Filtered member loading (only matching rows) ─────────────────────────────

def _int_list(values):
    """Numeric IN-list values, skipping anything that isn't one.

    These are inlined into raw SQL via int(), so a blank or non-numeric entry
    raised `invalid literal for int() with base 10: ''` from inside query
    construction — naming neither the filter nor the caller. Unusable values are
    dropped; if none survive, the caller omits the clause rather than emitting
    `IN ()`.
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
        except (TypeError, ValueError):
            continue
    return out


def _build_where(grgr_ck=None, cspd_cat=None, cspi_id=None, pdpd_id=None):
    """Build WHERE clause with inlined values (no parameter markers).
    SQL Server natively ignores trailing spaces in CHAR comparisons.
    """
    parts = []
    if grgr_ck:
        ids = ",".join(str(x) for x in _int_list(grgr_ck))
        if ids:
            parts.append(f"GRGR_CK IN ({ids})")
    if cspd_cat:
        vals = ",".join(literal(x) for x in cspd_cat)
        parts.append(f"CSPD_CAT IN ({vals})")
    if cspi_id:
        vals = ",".join(literal(x) for x in cspi_id)
        parts.append(f"CSPI_ID IN ({vals})")
    if pdpd_id:
        vals = ",".join(literal(x) for x in pdpd_id)
        parts.append(f"PDPD_ID IN ({vals})")
    where = f"WHERE {' AND '.join(parts)}" if parts else ""
    return where


_ADDRESS_TABLE = f"{SQL_SCHEMA}.member_address"


def _home_addresses(where: str, conn):
    """Each member's home ZIP, city and state (sdm.member_address, role HOME),
    named as the profile's geography reads them; None where the store has no
    address table (the 837 extract fills it)."""
    have = present_columns(_ADDRESS_TABLE)
    if not {"MEME_CK", "ADDR_ROLE", "SBAD_ZIP", "SBAD_CITY", "SBAD_STATE"} <= have:
        return None
    homes = read_sql(f"""
        SELECT MEME_CK, SBAD_ZIP AS MEME_ZIP, SBAD_CITY AS MEME_CITY, SBAD_STATE AS MEME_STATE
        FROM {_ADDRESS_TABLE}
        WHERE ADDR_ROLE = 'HOME' AND MEME_CK IN (SELECT MEME_CK FROM {_TABLE} {where})
    """, conn)
    for col in ("MEME_ZIP", "MEME_CITY", "MEME_STATE"):
        homes[col] = homes[col].astype("string").str.strip()
    homes["MEME_ZIP"] = homes["MEME_ZIP"].str[:5]
    return homes.drop_duplicates("MEME_CK")


def load_filtered_members(grgr_ck=None, cspd_cat=None, cspi_id=None, pdpd_id=None,
                           reference_date=None):
    """Load only members matching the given filters from SQL Server.

    Hierarchy: Group → Plan Category → Plan → Product.
    Returns (members_df, labels_df, family_data, filters_used).

    Two SQL queries:
    1. Members: one row per MEME_CK (most recent enrollment) — for clustering
    2. Family:  SBSB_CK + MEME_CK + MEME_REL only — for family stats
    """
    reference_date = reference_date or DEFAULT_REFERENCE_DATE
    ref = pd.Timestamp(reference_date)

    where = _build_where(grgr_ck, cspd_cat, cspi_id, pdpd_id)

    conn = _conn()

    # Query 1: One row per member (most recent enrollment).
    #
    # Ordered by the key because the rows become a matrix and the matrix is
    # clustered. k-means is seeded, so the same matrix always gives the same
    # answer -- but "the same matrix" includes the order, and a SELECT without
    # ORDER BY has none to rely on.
    members_sql = f"""
        WITH ranked AS (
            SELECT {select_list(_TABLE, MEMBER_COLUMNS)},
                   ROW_NUMBER() OVER (PARTITION BY MEME_CK ORDER BY MEME_ORIG_EFF_DT DESC) AS _rn
            FROM {_TABLE}
            {where}
        )
        SELECT * FROM ranked WHERE _rn = 1 ORDER BY MEME_CK
    """
    df = read_sql(members_sql, conn)
    if '_rn' in df.columns:
        df.drop(columns=['_rn'], inplace=True)

    # Query 2: Lightweight family data (3 columns, all rows)
    family_sql = f"""
        SELECT SBSB_CK, MEME_CK, MEME_REL
        FROM {_TABLE}
        {where}
    """
    family_data = read_sql(family_sql, conn)
    homes = _home_addresses(where, conn)
    conn.close()
    if homes is not None and not df.empty:
        df = df.merge(homes, on='MEME_CK', how='left')

    if df.empty:
        filters_used = {k: v for k, v in [
            ("grgr_ck", grgr_ck), ("cspd_cat", cspd_cat),
            ("cspi_id", cspi_id), ("pdpd_id", pdpd_id),
        ] if v is not None}
        raise ValueError(f"No members match the applied filters: {filters_used}")

    # Derive age and tenure
    if 'MEME_BIRTH_DT' in df.columns:
        df['_age'] = ((ref - pd.to_datetime(df['MEME_BIRTH_DT'], errors='coerce')).dt.days / 365.25).clip(lower=0)
    else:
        df['_age'] = 0

    if 'MEME_ORIG_EFF_DT' in df.columns:
        df['_tenure'] = ((ref - pd.to_datetime(df['MEME_ORIG_EFF_DT'], errors='coerce')).dt.days / 30.44).clip(lower=0)
    else:
        df['_tenure'] = 0

    # The member's primary care provider, from the tables beside the denorm.
    from sdg.analyzer import provider_features
    df = provider_features.members(df, str(reference_date))

    # Build filters_used
    filters_used = {}
    if grgr_ck:
        filters_used['grgr_ck'] = grgr_ck
    if cspd_cat:
        filters_used['cspd_cat'] = cspd_cat
    if cspi_id:
        filters_used['cspi_id'] = cspi_id
    if pdpd_id:
        filters_used['pdpd_id'] = pdpd_id

    # Build labels
    label_cols = ['MEME_CK', 'GRGR_CK', 'CSPD_CAT', 'CSPI_ID', 'PDPD_ID']
    labels_df = df[[c for c in label_cols if c in df.columns]].copy()

    return df, labels_df, family_data, filters_used


def get_combination_sizes(max_depth: int = 2, min_members: int = 0):
    """Every hierarchy combination with its member count, in one query.

    Discovery previously built its combination list with a query per group and
    another per group-category — hundreds of round trips before any analysis
    started — and then loaded member rows for every combination it found,
    including the many that hold a handful of members and produce nothing worth
    ranking.

    This returns the whole grid with counts in a single GROUP BY, so the walk
    can skip the small ones before loading anything and can order the rest by
    size. The database does the aggregation; the application only ever sees
    distinct combinations, not member rows.
    """
    levels = [
        ["GRGR_CK", "GRGR_NAME"],
        ["GRGR_CK", "GRGR_NAME", "CSPD_CAT", "CSPD_CAT_DESC"],
        ["GRGR_CK", "GRGR_NAME", "CSPD_CAT", "CSPD_CAT_DESC", "CSPI_ID"],
    ][:max(1, min(max_depth, 3))]

    frames = []
    conn = _conn()
    try:
        for depth, cols in enumerate(levels, start=1):
            select = ", ".join(cols)
            having = f"HAVING COUNT(*) >= {int(min_members)}" if min_members else ""
            df = read_sql(f"""
                SELECT {select}, COUNT(*) AS n_members
                FROM {_TABLE}
                GROUP BY {select}
                {having}
            """, conn)
            df["depth"] = depth
            for missing in ("CSPD_CAT", "CSPD_CAT_DESC", "CSPI_ID"):
                if missing not in df.columns:
                    df[missing] = None
            frames.append(df)
    finally:
        conn.close()

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    return out.sort_values("n_members", ascending=False).reset_index(drop=True)

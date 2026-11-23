"""Load claims data from SQL Server with filter pushdown.

Hierarchy: Group (GRGR) → Plan (CSPI_ID/PLDS_DESC) → Product (PDPD_ID) → Claim Type (CLCL_CL_TYPE)
Source: sdm.claims_denorm table in SQL Server.
"""

import pandas as pd
import pyodbc
from sdg.analyzer.services.cache import ttl_cache

from sdg.analyzer.config import get_connection_string, SQL_SCHEMA, DEFAULT_REFERENCE_DATE
from sdg.analyzer.sql import literal, read_sql
from sdg.packs import held

_TABLE = f"{SQL_SCHEMA}.claims_denorm"

# What analysis reads of a claim line, out of the sixty-odd columns the table
# has: the clustering features, what a pattern is described by, the
# combinations it carries, and what the rows view shows.
CLAIM_COLUMNS = held("features:store.claim_columns")


def _conn():
    return pyodbc.connect(get_connection_string())


def claims_available():
    """Check if claims_denorm table exists and has data."""
    try:
        conn = _conn()
        row = read_sql(f"SELECT COUNT(*) AS cnt FROM {_TABLE}", conn).iloc[0]
        conn.close()
        return int(row["cnt"]) > 0
    except Exception:
        return False


@ttl_cache(300)
def get_claims_overview():
    """Return dict of aggregate counts."""
    conn = _conn()
    row = read_sql(f"""
        SELECT
            COUNT(*)                          AS n_claim_lines,
            COUNT(DISTINCT CLCL_ID)           AS n_claims,
            COUNT(DISTINCT MEME_CK)           AS n_members,
            COUNT(DISTINCT GRGR_CK)           AS n_groups,
            COUNT(DISTINCT CSPI_ID)           AS n_plans,
            COUNT(DISTINCT PDPD_ID)           AS n_products,
            COUNT(DISTINCT IDCD_ID_PRIMARY)   AS n_diagnoses,
            COUNT(DISTINCT IPCD_ID)           AS n_procedures
        FROM {_TABLE}
    """, conn).iloc[0]
    conn.close()
    return row.to_dict()


# ── Cascading filter options ─────────────────────────────────────────────────

@ttl_cache(300)
def get_claims_groups():
    conn = _conn()
    df = read_sql(f"""
        SELECT DISTINCT GRGR_CK, RTRIM(GRGR_NAME) AS GRGR_NAME
        FROM {_TABLE}
        ORDER BY GRGR_NAME
    """, conn)
    conn.close()
    return df


@ttl_cache(300)
def get_claims_plans(_grgr_cks=None):
    conn = _conn()
    where = ""
    if _grgr_cks:
        ids = ",".join(str(int(x)) for x in _grgr_cks)
        where = f"WHERE GRGR_CK IN ({ids})"
    df = read_sql(f"""
        SELECT DISTINCT RTRIM(CSPI_ID) AS CSPI_ID, RTRIM(PLDS_DESC) AS PLDS_DESC
        FROM {_TABLE} {where}
        ORDER BY PLDS_DESC
    """, conn)
    conn.close()
    return df


@ttl_cache(300)
def get_claims_products(_grgr_cks=None, _cspi_ids=None):
    conn = _conn()
    parts = []
    if _grgr_cks:
        ids = ",".join(str(int(x)) for x in _grgr_cks)
        parts.append(f"GRGR_CK IN ({ids})")
    if _cspi_ids:
        vals = ",".join(literal(x) for x in _cspi_ids)
        parts.append(f"CSPI_ID IN ({vals})")
    where = f"WHERE {' AND '.join(parts)}" if parts else ""
    df = read_sql(f"""
        SELECT DISTINCT RTRIM(PDPD_ID) AS PDPD_ID
        FROM {_TABLE} {where}
        ORDER BY PDPD_ID
    """, conn)
    conn.close()
    return df


@ttl_cache(300)
def get_claims_types(_grgr_cks=None, _cspi_ids=None, _pdpd_ids=None):
    conn = _conn()
    parts = []
    if _grgr_cks:
        ids = ",".join(str(int(x)) for x in _grgr_cks)
        parts.append(f"GRGR_CK IN ({ids})")
    if _cspi_ids:
        vals = ",".join(literal(x) for x in _cspi_ids)
        parts.append(f"CSPI_ID IN ({vals})")
    if _pdpd_ids:
        vals = ",".join(literal(x) for x in _pdpd_ids)
        parts.append(f"PDPD_ID IN ({vals})")
    where = f"WHERE {' AND '.join(parts)}" if parts else ""
    df = read_sql(f"""
        SELECT DISTINCT RTRIM(CLCL_CL_TYPE) AS CLCL_CL_TYPE,
               CASE RTRIM(CLCL_CL_TYPE)
                   -- The 837 transaction types, which is what a claim carries.
                   WHEN 'P' THEN 'Professional'
                   WHEN 'I' THEN 'Institutional'
                   WHEN 'D' THEN 'Dental'
                   -- Kept for sources that label the line of business instead.
                   WHEN 'M' THEN 'Medical'
                   WHEN 'H' THEN 'Hospital'
                   ELSE RTRIM(CLCL_CL_TYPE)
               END AS Claim_Type
        FROM {_TABLE} {where}
        ORDER BY CLCL_CL_TYPE
    """, conn)
    conn.close()
    return df


# ── Demographic filter options ────────────────────────────────────────────────

@ttl_cache(300)
def get_top_diagnoses(n=30):
    conn = _conn()
    df = read_sql(f"""
        SELECT TOP {n} RTRIM(IDCD_ID_PRIMARY) AS IDCD_ID_PRIMARY,
               RTRIM(IDCD_DESC_PRIMARY) AS IDCD_DESC_PRIMARY,
               COUNT(*) AS cnt
        FROM {_TABLE}
        WHERE IDCD_ID_PRIMARY IS NOT NULL AND IDCD_ID_PRIMARY <> ''
        GROUP BY IDCD_ID_PRIMARY, IDCD_DESC_PRIMARY
        ORDER BY cnt DESC
    """, conn)
    conn.close()
    return df


@ttl_cache(300)
def get_top_procedures(n=30):
    conn = _conn()
    df = read_sql(f"""
        SELECT TOP {n} RTRIM(IPCD_ID) AS IPCD_ID,
               RTRIM(IPCD_DESC) AS IPCD_DESC,
               COUNT(*) AS cnt
        FROM {_TABLE}
        WHERE IPCD_ID IS NOT NULL AND IPCD_ID <> ''
        GROUP BY IPCD_ID, IPCD_DESC
        ORDER BY cnt DESC
    """, conn)
    conn.close()
    return df


# ── Filtered claims loading ──────────────────────────────────────────────────

def _int_list(values):
    """Numeric IN-list values, skipping anything that isn't one.

    Built with int() into raw SQL, so a blank or non-numeric entry raised
    `invalid literal for int() with base 10: ''` from inside query construction
    — a message that named neither the filter nor the caller. A value that
    cannot be an ID is dropped instead; if none survive, the filter is omitted
    rather than becoming `IN ()`.
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


def _as_int(value, label):
    """A single numeric bound, or None when it isn't usable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number, got {value!r}.")


def _build_claims_where(grgr_ck=None, cspi_id=None, pdpd_id=None, claim_type=None,
                        gender=None, age_min=None, age_max=None,
                        diagnosis=None, procedure=None):
    parts = []
    if grgr_ck:
        ids = ",".join(str(x) for x in _int_list(grgr_ck))
        if ids:
            parts.append(f"GRGR_CK IN ({ids})")
    if cspi_id:
        vals = ",".join(literal(x) for x in cspi_id if str(x).strip())
        if vals:
            parts.append(f"CSPI_ID IN ({vals})")
    if pdpd_id:
        vals = ",".join(literal(x) for x in pdpd_id if str(x).strip())
        if vals:
            parts.append(f"PDPD_ID IN ({vals})")
    if claim_type:
        vals = ",".join(literal(x) for x in claim_type if str(x).strip())
        if vals:
            parts.append(f"CLCL_CL_TYPE IN ({vals})")
    if gender:
        # A bare string would be iterated character by character, so one value
        # arriving as "F" rather than ["F"] has to be wrapped first.
        values = [gender] if isinstance(gender, str) else gender
        vals = ",".join(literal(x) for x in values if str(x).strip())
        if vals:
            parts.append(f"MEME_SEX IN ({vals})")
    age_min = _as_int(age_min, "age_min")
    age_max = _as_int(age_max, "age_max")
    if age_min is not None:
        parts.append(f"CLCL_ME_AGE >= {age_min}")
    if age_max is not None:
        parts.append(f"CLCL_ME_AGE <= {age_max}")
    if diagnosis:
        vals = ",".join(literal(x) for x in diagnosis)
        parts.append(f"IDCD_ID_PRIMARY IN ({vals})")
    if procedure:
        vals = ",".join(literal(x) for x in procedure)
        parts.append(f"IPCD_ID IN ({vals})")
    return f"WHERE {' AND '.join(parts)}" if parts else ""


def _columns() -> str:
    from data_loader_before_merge import select_list

    return select_list(_TABLE, CLAIM_COLUMNS)


def load_filtered_claims(grgr_ck=None, cspi_id=None, pdpd_id=None, claim_type=None,
                         gender=None, age_min=None, age_max=None,
                         diagnosis=None, procedure=None):
    """Load claims matching filters. Returns (claims_df, filters_used)."""
    where = _build_claims_where(grgr_ck, cspi_id, pdpd_id, claim_type,
                                 gender, age_min, age_max, diagnosis, procedure)
    conn = _conn()
    # Ordered for the same reason the member subset is: these rows become the
    # matrix that gets clustered, and an unordered SELECT makes the result
    # depend on how the server happened to return them. The claim and its line
    # number together are unique, so this is a total order.
    df = read_sql(
        f"SELECT {_columns()} FROM {_TABLE} {where} ORDER BY CLCL_ID, CDML_SEQ_NO", conn
    )
    conn.close()

    if df.empty:
        filters_used = {k: v for k, v in [
            ("grgr_ck", grgr_ck), ("cspi_id", cspi_id),
            ("pdpd_id", pdpd_id), ("claim_type", claim_type),
            ("gender", gender), ("age_min", age_min), ("age_max", age_max),
            ("diagnosis", diagnosis), ("procedure", procedure),
        ] if v is not None}
        raise ValueError(f"No claims match filters: {filters_used}")

    # Strip CHAR columns
    for col in ["CSPI_ID", "PDPD_ID", "CLCL_CL_TYPE", "CLCL_NTWK_IND",
                "CLCL_CUR_STS", "GRGR_NAME", "PLDS_DESC", "IDCD_ID_PRIMARY",
                "IPCD_ID", "PRPR_MCTR_SPEC", "MEME_SEX", "PSCD_ID"]:
        if col in df.columns and df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()

    filters_used = {}
    if grgr_ck: filters_used["grgr_ck"] = grgr_ck
    if cspi_id: filters_used["cspi_id"] = cspi_id
    if pdpd_id: filters_used["pdpd_id"] = pdpd_id
    if claim_type: filters_used["claim_type"] = claim_type
    if gender: filters_used["gender"] = gender
    if age_min is not None: filters_used["age_min"] = age_min
    if age_max is not None: filters_used["age_max"] = age_max
    if diagnosis: filters_used["diagnosis"] = diagnosis
    if procedure: filters_used["procedure"] = procedure

    # The provider's type and the network's name and type, from the tables
    # beside the denorm.
    from sdg.analyzer import provider_features
    df = provider_features.claims(df)
    return df, filters_used


# ── Claims profile builder ───────────────────────────────────────────────────

def profile_claims_cluster(cluster_df):
    """Build a profile dict for a claims cluster."""
    profile = {"size": len(cluster_df)}

    # Continuous stats
    for col, label in [("CDML_CHG_AMT", "charge_amount"), ("CDML_ALLOW", "allowed_amount"),
                        ("CDML_PAID_AMT", "paid_amount"), ("CDML_DED_AMT", "deductible"),
                        ("CDML_COPAY_AMT", "copay"), ("CDML_COINS_AMT", "coinsurance"),
                        ("CLCL_ME_AGE", "member_age")]:
        if col in cluster_df.columns:
            vals = pd.to_numeric(cluster_df[col], errors="coerce").dropna()
            if len(vals) > 0:
                from sdg.analyzer.profiler import deciles

                profile[label] = {
                    "mean": round(float(vals.mean()), 2),
                    "std": round(float(vals.std()), 2),
                    "min": round(float(vals.min()), 2),
                    "max": round(float(vals.max()), 2),
                    "deciles": deciles(vals),
                }

    # Categorical distributions
    for col, label in [("CLCL_CUR_STS", "claim_status"), ("CLCL_CL_TYPE", "claim_type"),
                        ("CLCL_NTWK_IND", "network"), ("MEME_SEX", "gender"),
                        ("PSCD_ID", "place_of_service"), ("PRPR_MCTR_SPEC", "provider_specialty"),
                        ("_network_name", "network_name"), ("_network_type", "network_type"),
                        ("_provider_entity", "provider_type")]:
        if col in cluster_df.columns:
            vc = cluster_df[col].value_counts(normalize=True)
            profile[label] = {str(k): round(float(v), 4) for k, v in vc.head(10).items()}

    # Top procedures
    if "IPCD_ID" in cluster_df.columns:
        top = cluster_df["IPCD_ID"].value_counts().head(10)
        profile["top_procedures"] = {str(k): int(v) for k, v in top.items()}

    # Top diagnoses
    if "IDCD_ID_PRIMARY" in cluster_df.columns:
        top = cluster_df["IDCD_ID_PRIMARY"].value_counts().head(10)
        profile["top_diagnoses"] = {str(k): int(v) for k, v in top.items()}

    # Procedure descriptions
    if "IPCD_ID" in cluster_df.columns and "IPCD_DESC" in cluster_df.columns:
        desc_map = cluster_df.drop_duplicates("IPCD_ID").set_index("IPCD_ID")["IPCD_DESC"].to_dict()
        # The top procedures' descriptions, not the first ten met in the rows.
        profile["procedure_descriptions"] = {
            str(k): str(desc_map[k]).strip()
            for k in cluster_df["IPCD_ID"].value_counts().head(10).index if k in desc_map}

    # Diagnosis descriptions
    if "IDCD_ID_PRIMARY" in cluster_df.columns and "IDCD_DESC_PRIMARY" in cluster_df.columns:
        desc_map = cluster_df.drop_duplicates("IDCD_ID_PRIMARY").set_index("IDCD_ID_PRIMARY")["IDCD_DESC_PRIMARY"].to_dict()
        profile["diagnosis_descriptions"] = {
            str(k): str(desc_map[k]).strip()
            for k in cluster_df["IDCD_ID_PRIMARY"].value_counts().head(10).index if k in desc_map}

    # Approval rate
    if "CLCL_CUR_STS" in cluster_df.columns:
        approved = cluster_df["CLCL_CUR_STS"].isin(["01", "AP"]).sum()
        profile["approval_rate"] = round(approved / len(cluster_df), 4) if len(cluster_df) > 0 else 0

    # In-network rate
    if "CLCL_NTWK_IND" in cluster_df.columns:
        in_net = (cluster_df["CLCL_NTWK_IND"] == "I").sum()
        profile["in_network_rate"] = round(in_net / len(cluster_df), 4) if len(cluster_df) > 0 else 0

    return profile


def get_claims_combination_sizes(max_depth: int = 2, min_claims: int = 0):
    """Every claims hierarchy combination with its claim count, in one query.

    The claims counterpart to the member pre-pass. Discovery previously built
    its list with a plans query per group — one round trip each, before any
    analysis — and then loaded claim rows for every combination including the
    many too small to yield a pattern.

    Hierarchy is Group → Plan → Product, so the levels differ from the member
    walk even though the shape of the fix is the same.
    """
    levels = [
        ["GRGR_CK", "GRGR_NAME"],
        ["GRGR_CK", "GRGR_NAME", "CSPI_ID", "PLDS_DESC"],
        ["GRGR_CK", "GRGR_NAME", "CSPI_ID", "PLDS_DESC", "PDPD_ID"],
    ][:max(1, min(max_depth, 3))]

    frames = []
    conn = _conn()
    try:
        for depth, cols in enumerate(levels, start=1):
            select = ", ".join(f"RTRIM({c}) AS {c}" if c != "GRGR_CK" else c
                               for c in cols)
            group_by = ", ".join(cols)
            having = f"HAVING COUNT(*) >= {int(min_claims)}" if min_claims else ""
            df = read_sql(f"""
                SELECT {select}, COUNT(*) AS n_claims
                FROM {_TABLE}
                GROUP BY {group_by}
                {having}
            """, conn)
            df["depth"] = depth
            for missing in ("CSPI_ID", "PLDS_DESC", "PDPD_ID"):
                if missing not in df.columns:
                    df[missing] = None
            frames.append(df)
    finally:
        conn.close()

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    return out.sort_values("n_claims", ascending=False).reset_index(drop=True)

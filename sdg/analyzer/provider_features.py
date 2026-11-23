"""Provider features, joined onto members and claim lines as they are loaded.

The customer's member_denorm and claims_denorm are theirs and are not
altered. What a pattern needs to know about providers comes from the tables
beside them (sdg/packs/health/sql/03_denorm_837_tables.sql): a member's primary
care provider from member_pcp, a provider's type from provider_denorm, a
network's name and type from provider_network. They are small next to the
denorms and read once, then joined in memory.

A store without those tables yet -- a customer who has not run 03 and 04 --
gets the features empty rather than an error.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from sdg.analyzer.config import SQL_SCHEMA
from sdg.analyzer.services.cache import ttl_cache
from sdg.analyzer.sql import read_sql
from sdg.packs import held

MEMBER_COLUMNS = tuple(held("features:joined.provider_member_columns"))
CLAIM_COLUMNS = tuple(held("features:joined.provider_claim_columns"))
ENTITY_WORDS = held("features:joined.provider_entity_words")

log = logging.getLogger(__name__)


def _read(sql: str, *params) -> Optional[pd.DataFrame]:
    from sdg.analyzer.entity_loader import connect as _conn

    conn = _conn()
    try:
        return read_sql(sql, conn, params=list(params) if params else None)
    except Exception as exc:
        from sdg.analyzer.claims_data_loader import _missing_table

        if _missing_table(exc):
            # A store without the tables yet: the features are empty, as above.
            log.info("A feature table is not in the store yet; its features are empty")
            return None
        # Anything else is raised, so it is neither shown as "none found"
        # nor kept by the cache for ten minutes.
        log.exception("Could not read the features from the store")
        raise
    finally:
        conn.close()


@ttl_cache(600)
def _providers() -> pd.DataFrame:
    frame = _read(f"SELECT LTRIM(RTRIM(PRPR_ID)) AS PRPR_ID, LTRIM(RTRIM(PRPR_ENTITY)) AS ENT, "
                  f"LTRIM(RTRIM(PRCF_MCTR_SPEC)) AS SPEC FROM {SQL_SCHEMA}.provider_denorm")
    return frame if frame is not None else pd.DataFrame(columns=["PRPR_ID", "ENT", "SPEC"])


@ttl_cache(600)
def _networks() -> pd.DataFrame:
    frame = _read(f"SELECT DISTINCT LTRIM(RTRIM(NWNW_ID)) AS NWNW_ID, "
                  f"LTRIM(RTRIM(NWNW_NAME)) AS NAME, LTRIM(RTRIM(NWNW_MCTR_TYPE)) AS KIND "
                  f"FROM {SQL_SCHEMA}.provider_network")
    if frame is None:
        return pd.DataFrame(columns=["NWNW_ID", "NAME", "KIND"])
    return frame.drop_duplicates("NWNW_ID")


@ttl_cache(600)
def _pcps(reference_date: str) -> pd.DataFrame:
    """Each member's primary care provider on the reference date."""
    frame = _read(f"SELECT MEME_CK, LTRIM(RTRIM(PRPR_ID)) AS PRPR_ID, MEPR_EFF_DT "
                  f"FROM {SQL_SCHEMA}.member_pcp "
                  f"WHERE MEPR_EFF_DT <= ? AND (MEPR_TERM_DT IS NULL OR MEPR_TERM_DT >= ?)",
                  reference_date, reference_date)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["MEME_CK", "PRPR_ID"])
    frame = frame.sort_values("MEPR_EFF_DT").drop_duplicates("MEME_CK", keep="last")
    return frame[["MEME_CK", "PRPR_ID"]]


def members(df: pd.DataFrame, reference_date: str) -> pd.DataFrame:
    """Members with their PCP, its specialty and type, and whether they have one."""
    if df is None or df.empty or "MEME_CK" not in df.columns:
        return df
    pcps = _pcps(str(reference_date)[:10]).merge(_providers(), on="PRPR_ID", how="left")
    found = pcps.set_index("MEME_CK")
    keys = df["MEME_CK"]
    df["_pcp_id"] = keys.map(found["PRPR_ID"]) if len(found) else None
    df["_pcp_specialty"] = keys.map(found["SPEC"]) if len(found) else None
    df["_pcp_entity"] = (keys.map(found["ENT"]).map(ENTITY_WORDS)
                         if len(found) else None)
    df["_has_pcp"] = df["_pcp_id"].notna().map({True: "Y", False: "N"})
    return df


def claims(df: pd.DataFrame) -> pd.DataFrame:
    """Claim lines with the provider's type and the network's name and type."""
    if df is None or df.empty:
        return df
    if "PRPR_ID" in df.columns:
        entity = _providers().set_index("PRPR_ID")["ENT"]
        df["_provider_entity"] = (df["PRPR_ID"].astype(str).str.strip().map(entity)
                                  .map(ENTITY_WORDS))
    else:
        df["_provider_entity"] = None
    if "NWNW_ID" in df.columns:
        nets = _networks().set_index("NWNW_ID")
        ids = df["NWNW_ID"].astype(str).str.strip()
        df["_network_name"] = ids.map(nets["NAME"]) if len(nets) else None
        df["_network_type"] = ids.map(nets["KIND"]) if len(nets) else None
    else:
        df["_network_name"] = df["_network_type"] = None
    # Out of network names no network, and says so.
    if "CLCL_NTWK_IND" in df.columns:
        out = df["CLCL_NTWK_IND"].astype(str).str.strip() == "O"
        df.loc[out, "_network_name"] = "Out of network"
        df.loc[out, "_network_type"] = "OUT"
    return df


def joined(name: str, df: pd.DataFrame, reference_date) -> pd.DataFrame:
    """What the pack's `loaders.<entity>.joined` names, added to loaded rows."""
    if name == "members":
        return members(df, str(reference_date))
    if name == "claims":
        return claims(df)
    raise ValueError(f"Nothing is joined as {name!r}.")


def clear() -> None:
    for fn in (_providers, _networks, _pcps):
        getattr(fn, "cache_clear", lambda: None)()

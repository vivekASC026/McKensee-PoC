"""What is read of claims beyond loading them.

Source: sdm.claims_denorm table in SQL Server.

Loading claims is sdg.analyzer.entity_loader's, as the business pack says.
The scope's choices are entity_loader.options, as the pack's
loaders.claims.scope says; the claim profile is the health pack's. The
most frequent codes are the pack's highlights (entity_loader.highlight).
"""

import logging

import pandas as pd
import pyodbc

from sdg.analyzer import entity_loader
from sdg.analyzer.entity_loader import connect as _conn
from sdg.analyzer.services.cache import ttl_cache
from sdg.analyzer.sql import read_sql

log = logging.getLogger(__name__)


def claims_available() -> bool:
    """Whether the child records' table exists and has rows.

    A missing table is "no", as there are none to read. Any other failure (the
    server down, no permission) raises, so an outage is not reported as "none
    held"."""
    from sdg import packs

    child = packs.active().child
    if not child:
        return False
    try:
        table = entity_loader.table(child)
    except ValueError:
        return False  # the pack reads no child records
    conn = _conn()
    try:
        row = read_sql(f"SELECT COUNT(*) AS cnt FROM {table}", conn).iloc[0]
    except Exception as exc:
        if _missing_table(exc):
            return False
        log.error("Could not count the rows in %s: %s", table, exc)
        raise
    finally:
        conn.close()
    return int(row["cnt"]) > 0


def _missing_table(exc: BaseException) -> bool:
    """Whether a read failed because the table does not exist (SQLSTATE 42S02).
    pandas may wrap the driver's error, so the cause is looked at too."""
    for err in (exc, exc.__cause__, exc.__context__):
        if isinstance(err, pyodbc.Error) and err.args and err.args[0] == "42S02":
            return True
    return False


@ttl_cache(300)
def get_claims_overview():
    """Return dict of aggregate counts."""
    from sdg import packs

    return entity_loader.overview(packs.active().child)


# ── Claims profile builder ───────────────────────────────────────────────────

def profile_claims_cluster(cluster_df):
    """A profile of one child-records cluster, as the pack's child_analysis
    lists it: each continuous column's spread, each categorical column's
    shares, the most frequent codes (and their descriptions), and the share
    of rows holding given values (health: approved, in network)."""
    from sdg.packs import held

    spec = held("features:child_analysis")
    profile = {"size": len(cluster_df)}

    # Continuous stats
    for col, label in spec["continuous"]:
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
    for col, label in spec["categorical"]:
        if col in cluster_df.columns:
            vc = cluster_df[col].value_counts(normalize=True)
            profile[label] = {str(k): round(float(v), 4) for k, v in vc.head(10).items()}

    # The most frequent codes
    for top in spec["top"]:
        if top["code"] in cluster_df.columns:
            counts = cluster_df[top["code"]].value_counts().head(10)
            profile[top["as"]] = {str(k): int(v) for k, v in counts.items()}

    # Their descriptions: the top codes', not the first ten met in the rows.
    for top in spec["top"]:
        code, description = top["code"], top.get("description")
        if description and code in cluster_df.columns and description in cluster_df.columns:
            desc_map = cluster_df.drop_duplicates(code).set_index(code)[description].to_dict()
            profile[top["descriptions"]] = {
                str(k): str(desc_map[k]).strip()
                for k in cluster_df[code].value_counts().head(10).index if k in desc_map}

    # The share of rows holding given values
    for rate in spec["rates"]:
        if rate["column"] in cluster_df.columns:
            held_ = cluster_df[rate["column"]].isin(rate["in"]).sum()
            profile[rate["as"]] = round(held_ / len(cluster_df), 4) if len(cluster_df) > 0 else 0

    return profile

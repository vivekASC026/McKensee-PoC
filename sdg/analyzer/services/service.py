"""Orchestration over the existing sdg.analyzer modules.

This file adds no analytics of its own. It reproduces the sequence that
_pages/1_profiler.py ran inline — load, cluster, profile, name — and returns
plain Python so FastAPI can serialize it.

The two-layer cache mirrors the profiler page's split: member data survives a
change to K, cluster assignments do not.
"""

import hashlib
import json
import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from sdg.analyzer.clustering import discover_clusters
from sdg.analyzer.config import DEFAULT_REFERENCE_DATE, DESCRIPTION_COLUMNS
from sdg.analyzer import entity_loader
from sdg.analyzer.naming import build_contextual_name
from sdg.analyzer.profiler import build_subset_summary, profile_cluster

from sdg import packs

from .cache import ttl_cache
from .disk_cache import two_tier

RARE_THRESHOLD = 0.03

# The denominator is the population at the TOPMOST level the user selected.
#
# Select three groups and nothing deeper: the patterns partition those groups,
# so their shares sum to 100%. Add a plan filter and they sum to less — the
# remainder of those groups belongs to patterns not currently in view, which is
# exactly what should be visible.
#
# The whole-book count is the wrong denominator here. Against a table with a
# billion claim lines every pattern rounds to zero, and a metric that always
# reads "<0.01%" ranks nothing.
# Fallback only, used before a baseline exists. Real banding comes from the
# percentile cut-offs computed during enumeration.
WEIGHT_BANDS = [
    (0.010,  "very high"),
    (0.004,  "high"),
    (0.0015, "medium"),
    (0.0004, "low"),
    (0.0,    "rare"),
]

# Which percentile of all observed pattern sizes each band starts at.
BAND_PERCENTILES = [
    ("p95", "very high"),
    ("p80", "high"),
    ("p50", "medium"),
    ("p20", "low"),
]


def weight_band(pct_of_scope: float, size: int = None,
                thresholds: dict = None) -> str:
    """Bucket a pattern.

    With stored thresholds, band by where the pattern's size falls among every
    pattern the enumeration saw — so "very high" means the top 5% of patterns
    that actually exist. Without them, fall back to fixed shares.
    """
    if thresholds and size is not None:
        for key, label in BAND_PERCENTILES:
            cut = thresholds.get(key)
            if cut is not None and size >= cut:
                return label
        return "rare"

    for floor, label in WEIGHT_BANDS:
        if pct_of_scope >= floor:
            return label
    return "rare"


def scope_denominator(filters: dict) -> tuple[int, str, bool]:
    """The stored baseline total these filters are measured against.

    Returns (count, label, from_baseline). Falls back to a live count only
    when no baseline has been computed yet, and says so, because a live count
    moves with the filter and a stored one does not.
    """
    # The denominator for a displayed percentage is the whole member book, so a
    # pattern covering 40% of members reads 40%. (The enumeration baseline is a
    # different quantity, used only to place patterns into weight bands.)
    book = book_size()
    every = f"all {packs.active().term(ENTITY, 'many')}"
    if book:
        return book, every, True
    # No records in the book at all: nothing to count live either.
    return 0, every, False


# ── JSON coercion ────────────────────────────────────────────────────────────

def to_py(obj: Any) -> Any:
    """Recursively convert numpy / pandas scalars into JSON-safe Python."""
    if obj is None:
        return None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, np.ndarray):
        return [to_py(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): to_py(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_py(x) for x in obj]
    return obj


def cache_key(filters: dict, k: Optional[int]) -> str:
    raw = json.dumps({"filters": filters, "k": k}, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


# ── Hierarchy options ────────────────────────────────────────────────────────

# The records this profiler analyzes: the pack's primary ones, and how
# (features.json primary_analysis).
ENTITY = packs.active().primary
PRIMARY = packs.held("features:primary_analysis")

@ttl_cache(300)
def overview() -> dict:
    return to_py(entity_loader.overview(ENTITY))


def book_size(view: Optional[dict] = None) -> int:
    """How many of the primary records the whole book holds: the count in
    the overview (this one's, or `view`) the pack names as its records
    (loaders.<entity>.counts)."""
    records = (entity_loader.config(ENTITY).get("counts") or {}).get("records", "")
    return int((overview() if view is None else view).get(records, 0) or 0)


# The member scope's choices, as the pack's loaders.members.scope reads them.

def groups() -> list[dict]:
    return entity_loader.options(ENTITY, "groups")


def plan_categories(grgr_cks: Optional[tuple] = None) -> list[dict]:
    return entity_loader.options(ENTITY, "plan-categories",
                                 entity_loader.given(grgr_ck=grgr_cks))


def plans(grgr_cks: Optional[tuple] = None, cspd_cats: Optional[tuple] = None) -> list[dict]:
    return entity_loader.options(ENTITY, "plans",
                                 entity_loader.given(grgr_ck=grgr_cks, cspd_cat=cspd_cats))


def products(grgr_cks: Optional[tuple] = None, cspd_cats: Optional[tuple] = None,
             cspi_ids: Optional[tuple] = None) -> list[dict]:
    return entity_loader.options(ENTITY, "products", entity_loader.given(
        grgr_ck=grgr_cks, cspd_cat=cspd_cats, cspi_id=cspi_ids))


# ── Analysis ─────────────────────────────────────────────────────────────────

@two_tier("members.load", ttl=3600)
def _load(filters: dict, reference_date: str):
    """Layer 1 — member data. Survives a change to K, and now a restart.

    A defined pattern's rules beyond the hierarchy come as `conditions`, and
    cut the loaded members down to the ones meeting them.
    """
    from . import defined_patterns

    scope, conditions = defined_patterns.split(filters)
    got = entity_loader.load(ENTITY, scope, reference_date=reference_date)
    loaded = (got.rows, got.labels, got.family, got.filters)
    return defined_patterns.narrow_members(loaded, conditions) if conditions else loaded


@two_tier("members.cluster", ttl=3600)
def _cluster(filters: dict, k: Optional[int], reference_date: str):
    """Layer 2 — assignments. Invalidated by a change to K.

    Cached separately from the data so adjusting K reuses the loaded frame
    instead of going back to SQL Server.
    """
    members, labels, family, filters_used = _load(filters, reference_date)
    from .defined_patterns import WHOLE

    if k == WHOLE:
        # The whole scope as one pattern: what a defined pattern is.
        return np.zeros(len(members), dtype=int), {"method": "whole_scope"}
    assignments, metrics = discover_clusters(
        members, labels, k=k, filters_used=filters_used,
    )
    return assignments, metrics


def scope_labels(summary: dict) -> dict[str, str]:
    """The name of each scope level the records sit in ({filter key: name}),
    from the level's label column (loaders.<entity>.scope); "All" where they
    span more than one."""
    from sdg.analyzer import scope_walk

    def first(col):
        vals = summary.get(col) or []
        return str(vals[0]) if len(vals) == 1 else "All"
    return {level["filter"]: first(level.get("label") or level["key"])
            for level in scope_walk._levels(ENTITY)}


def analyze(filters: dict, k: Optional[int] = None,
            reference_date: Optional[str] = None) -> dict:
    """Full profiler run. Mirrors _pages/1_profiler.py end to end."""
    ref = reference_date or DEFAULT_REFERENCE_DATE

    members, labels, family, filters_used = _load(filters, ref)
    if members is None or len(members) == 0:
        return {
            "total_members": book_size(),
            "subset_members": 0, "pct_of_total": 0.0, "n_patterns": 0,
            "method": "empty", "silhouette": None,
            "filters_used": to_py(filters_used or {}),
            "patterns": [], "cache_key": cache_key(filters, k),
        }

    assignments, metrics = _cluster(filters, k, ref)
    summary = build_subset_summary(members, filters_used)
    total_n = len(members)

    labels = scope_labels(summary)

    scope_n, scope_label, from_baseline = scope_denominator(filters)
    scope_n = scope_n or total_n

    from . import baseline as _bl
    thresholds = _bl.get_thresholds(ENTITY)

    # The shape behind each pattern's mean and range, on bins shared with the
    # whole scope so the two can be laid over each other.
    from sdg.analyzer.histograms import describe as _describe

    clusters = sorted({int(a) for a in assignments if a >= 0})
    distributions, shapes = _describe(
        members, PRIMARY["histograms"],
        {cid: np.asarray(assignments) == cid for cid in clusters},
    )

    patterns = []
    for cid in clusters:
        mask = np.asarray(assignments) == cid
        profile = profile_cluster(members, family, mask, cid, total_n)
        from sdg.plan.combinations import for_patterns, of_rows

        combinations = of_rows(members[np.asarray(mask)], entity_loader.config(ENTITY)["table"],
                               for_patterns())
        name = build_contextual_name(labels, cluster_id=cid, profile=profile)
        patterns.append({
            "cluster_id": cid,
            "name": name,
            "size": int(profile["size"]),
            "pct_of_subset": float(profile["pct_of_subset"]),
            # Share of the topmost selected level — the figure the band uses.
            "pct_of_total": round(int(profile["size"]) / scope_n, 6) if scope_n else 0.0,
            "rare": float(profile["pct_of_subset"]) < RARE_THRESHOLD,
            "weight_band": weight_band(
                int(profile["size"]) / scope_n if scope_n else 0.0,
                size=int(profile["size"]),
                thresholds=thresholds,
            ),
            "continuous": to_py(profile["continuous"]),
            "categorical": to_py(profile["categorical"]),
            "family": to_py(profile["family"]),
            "descriptions": to_py(profile["descriptions"]),
            "geography": to_py(profile.get("geography") or []),
            "enrollment": to_py(profile.get("enrollment") or []),
            "combinations": combinations,
            "histograms": shapes.get(cid, {}),
        })

    patterns.sort(key=lambda p: p["size"], reverse=True)

    book_members = book_size() or total_n

    return {
        # Context for the funnel header, not a denominator for weights.
        "total_members": book_members,
        "subset_members": total_n,
        "pct_of_total": round(total_n / book_members * 100, 3) if book_members else 0.0,
        "n_patterns": len(patterns),
        "scope_total": scope_n,
        "scope_label": scope_label,
        "scope_from_baseline": from_baseline,
        # Spelled out so the number on screen can always be traced back to the
        # arithmetic that produced it.
        "weight_explained": (
            f"pattern {packs.active().term(ENTITY, 'many')} / {scope_n:,} in {scope_label}"
            f" ({'stored baseline' if from_baseline else 'live count'})"
            + (f" · bands from percentiles of {thresholds.get('n_patterns', 0):,} patterns"
               if thresholds else " · bands from fixed shares (no baseline)")
        ),
        "band_thresholds": thresholds,
        # Share of the scope that deeper filters excluded. Zero when the only
        # selection is at the topmost level, which is when shares sum to 100%.
        "pct_outside_filter": round(1 - (total_n / scope_n), 6) if scope_n else 0.0,
        "method": str(metrics.get("method", "kmeans")),
        "silhouette": to_py(metrics.get("silhouette")),
        "filters_used": to_py(filters_used or {}),
        "patterns": patterns,
        "distributions": distributions,
        "cache_key": cache_key(filters, k),
    }


def pattern_context(filters: dict, k: Optional[int], cluster_id: int,
                    reference_date: Optional[str] = None):
    """Everything generation needs for one pattern: profile, source rows, name.

    Reuses the cached load and cluster, so this is cheap after an analyze().
    """
    ref = reference_date or DEFAULT_REFERENCE_DATE
    members, labels, family, filters_used = _load(filters, ref)
    if members is None or len(members) == 0:
        raise ValueError(f"No {packs.active().term(ENTITY, 'many')} match these filters.")

    assignments, _ = _cluster(filters, k, ref)
    mask = np.asarray(assignments) == cluster_id
    if not mask.any():
        raise ValueError(f"Pattern {cluster_id} is not present in this subset.")

    profile = profile_cluster(members, family, mask, cluster_id, len(members))
    summary = build_subset_summary(members, filters_used)
    name = build_contextual_name(scope_labels(summary), cluster_id=cluster_id, profile=profile)
    return profile, filters_used, members.iloc[mask], name


def df_preview(df: pd.DataFrame, limit: int = 100) -> tuple[list[str], list[dict]]:
    """Column order plus the first `limit` rows, JSON-safe."""
    if df is None or df.empty:
        return [], []
    head = df.head(limit)
    cols = [str(c) for c in head.columns]
    rows = [
        {str(c): to_py(v) for c, v in rec.items()}
        for rec in head.to_dict(orient="records")
    ]
    return cols, rows


# ── Funnel counts ────────────────────────────────────────────────────────────

@two_tier("funnel", ttl=1800)
def funnel_counts(*chosen: tuple) -> dict:
    """The primary records' count as each scope level is applied in turn.

    `chosen` is each level's values, broadest first (health: groups, plan
    categories, plans, products). One cheap COUNT(DISTINCT) per level rather
    than a full load, so the funnel shows how the filters bite at each step.
    Each count is named as the pack's loader names it (loaders.<entity>.funnel;
    health: group, cat, plan, product), after "total".
    """
    import pyodbc

    from sdg.analyzer import scope_walk
    from sdg.analyzer.config import get_connection_string

    levels = scope_walk._levels(ENTITY)
    names = entity_loader.config(ENTITY).get("funnel") or [lv["level"] for lv in levels]
    key = packs.plain_name(packs.held("features:store.member_key"), "store.member_key")
    steps: list[tuple[str, dict]] = [("total", {})]
    narrowed: dict = {}
    for level, name, values in zip(levels, names, list(chosen) + [()] * len(levels)):
        narrowed = {**narrowed, level["filter"]: list(values) or None}
        steps.append((name, dict(narrowed)))

    out: dict[str, Optional[int]] = {}
    conn = pyodbc.connect(get_connection_string())
    try:
        cur = conn.cursor()
        for name, filters in steps:
            where = entity_loader.where(ENTITY, filters)
            cur.execute(f"SELECT COUNT(DISTINCT [{key}]) FROM {entity_loader.table(ENTITY)} {where}")
            row = cur.fetchone()
            out[name] = int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()
    return out

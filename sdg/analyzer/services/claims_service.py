"""Claims pattern analysis.

Same shape as the member profiler: cascade the hierarchy, cluster the filtered
subset, profile each cluster. Claims carry more signal than members — age and
gender plus diagnosis, procedure, place of service, provider specialty, and the
financial columns — so the profile is richer, but the workflow is identical.

Clusters are found on the features chosen on the Config screen
(discovery_features): numbers standardized and weighted 2x, lists one-hot.
"""

from typing import Optional

import numpy as np
import pandas as pd

from sdg.analyzer import claims_data_loader as cdl
from sdg.analyzer import entity_loader, pattern_scope
from sdg.analyzer.claims_data_loader import profile_claims_cluster

from sdg.analyzer.clustering import silhouette

from .disk_cache import two_tier
from .service import cache_key, to_py, weight_band
from sdg import packs
from sdg.packs import held, is_primary

# The records this service analyzes: the pack's child ones, and how their
# rows are read (features.json child_analysis).
ENTITY = packs.active().child
CHILD = held("features:child_analysis")
RECORD_KEY, PRIMARY_KEY, TYPE_COLUMN = (CHILD["record_key"], CHILD["primary_key"],
                                        CHILD["type_column"])

CONTINUOUS_WEIGHT = 2.0
RARE_THRESHOLD = 0.03
SILHOUETTE_SAMPLE = 5000

# Exclusive ceiling on the claims k sweep, kept wider than the member one.
#
# Members never select above k=4, so clustering.MEMBER_K_RANGE stops at 6. Claims
# do reach the top: across 63 combinations the sweep chose k=6 five times, k=7
# twice and k=10 twice. Capping at 6 would have cost those four combinations
# clusters — one dropped from 10 to 6 — for a silhouette loss of up to 0.0176 on
# a 0.40 score. That is a 1.7x walk against slightly coarser patterns on 6% of
# combinations, and the patterns are the product.
CLAIMS_K_MAX = 11
# Rows the cluster count is chosen on; the final fit uses all of them.
K_SEARCH_SAMPLE = 50_000


# The claim scope's choices, as the pack's loaders.claims.scope reads them.

def groups() -> list[dict]:
    return entity_loader.options(ENTITY, "groups")


def plans(grgr: Optional[tuple] = None) -> list[dict]:
    return entity_loader.options(ENTITY, "plans", entity_loader.given(grgr_ck=grgr))


def products(grgr: Optional[tuple] = None, plan: Optional[tuple] = None) -> list[dict]:
    return entity_loader.options(ENTITY, "products",
                                 entity_loader.given(grgr_ck=grgr, cspi_id=plan))


def claim_types(grgr: Optional[tuple] = None, plan: Optional[tuple] = None,
                product: Optional[tuple] = None) -> list[dict]:
    return entity_loader.options(ENTITY, "types", entity_loader.given(
        grgr_ck=grgr, cspi_id=plan, pdpd_id=product))


def _features(df: pd.DataFrame):
    """Numeric matrix from the features chosen on the Config screen."""
    from .discovery_features import matrix

    return matrix(df, ENTITY, CONTINUOUS_WEIGHT)


@two_tier("claims.booktotal", ttl=3600)
def book_totals() -> dict:
    """Whole-book claim and line counts. Context, not the denominator."""
    raw = cdl.get_claims_overview() or {}
    counts = entity_loader.config(ENTITY).get("counts") or {}
    return {
        "claims": int(raw.get(counts.get("records", ""), 0) or 0),
        "lines": int(raw.get(counts.get("lines", ""), 0) or 0),
    }


def scope_denominator(filters: dict) -> tuple:
    """The stored baseline total for these filters.

    Falls back to a live count only when nothing has been computed yet.
    """
    total = book_totals()["claims"]
    return total, f"all {packs.active().term(ENTITY, 'many')}", True


@two_tier("claims.load", ttl=3600)
def _load(filters: dict):
    from . import defined_patterns

    scope, conditions = defined_patterns.split(filters)
    got = entity_loader.load(ENTITY, scope)
    loaded = (got.rows, got.filters)
    return defined_patterns.narrow_claims(loaded, conditions) if conditions else loaded


# The 837 transaction types, which is what a claim carries, and the names the
# claim type list gives them.
CLAIM_TYPE_NAMES = held("features:store.claim_type_names")


@two_tier("claims.cluster.by_type.2", ttl=3600)
def _cluster(filters: dict, k: Optional[int]):

    df, _ = _load(filters)
    n = len(df)
    from .defined_patterns import WHOLE

    types = (df[TYPE_COLUMN].astype(str).str.strip().to_numpy()
             if TYPE_COLUMN in df.columns else None)
    if k == WHOLE:
        # The whole scope as one pattern: what a defined pattern is. Of each
        # claim type, one: a defined pattern holding professional and
        # institutional claims is two patterns, for the reason below.
        if types is None or len(set(types)) < 2:
            return np.zeros(n, dtype=int), {"method": "whole_scope"}
        order = {t: i for i, t in enumerate(sorted(set(types)))}
        return (np.array([order[t] for t in types], dtype=int),
                {"method": "whole_scope", "split_by": "claim_type",
                 "n_clusters": len(order)})
    if types is None or len(set(types)) < 2:
        return _cluster_rows(df, k)

    # Each claim type is patterned on its own. A professional claim, an
    # institutional one and a dental one follow different rules -- charges,
    # codes, which fields may be blank -- so no pattern mixes them. Every line
    # of a claim has the claim's type, so no claim is split by this.
    assignments = np.zeros(n, dtype=int)
    next_id = 0
    by_type = {}
    for claim_type in sorted(set(types)):
        mask = types == claim_type
        labels, found = _cluster_rows(df.iloc[mask], k)
        assignments[mask] = np.asarray(labels) + next_id
        next_id += int(np.max(labels)) + 1
        by_type[claim_type] = to_py(found)
    score = None
    X = _features(df)
    if X is not None and next_id >= 2:
        score = silhouette(X, assignments, SILHOUETTE_SAMPLE)
    return assignments, {"method": "kmeans", "split_by": "claim_type",
                         "n_clusters": next_id, "silhouette": score,
                         "by_claim_type": by_type}


def _cluster_rows(df: pd.DataFrame, k: Optional[int]):
    """The lines of one claim type, clustered on the chosen features."""
    from sklearn.cluster import KMeans

    n = len(df)
    if n < 8:
        return np.zeros(n, dtype=int), {"method": "single_cluster",
                                        "reason": f"n={n} < 8, too few to cluster"}

    X = _features(df)
    if X is None:
        return np.zeros(n, dtype=int), {"method": "single_cluster",
                                        "reason": "no usable feature columns"}

    def sil(mat, labels):
        # None when a sample cannot hold two groups; see clustering.silhouette.
        return silhouette(mat, labels, SILHOUETTE_SAMPLE)

    if k is None:
        # The number of clusters is chosen on a fixed sample and only the final
        # fit sees every line. Nine trial fits over the whole book were most of
        # a cold analysis; the silhouette was only ever read from 5,000 rows,
        # so a 50,000-row sample loses nothing it was measuring.
        trial = X
        if n > K_SEARCH_SAMPLE:
            pick = np.sort(np.random.default_rng(42).choice(n, K_SEARCH_SAMPLE, replace=False))
            trial = X[pick]
        best_k, best = 2, -1.0
        for cand in range(2, min(CLAIMS_K_MAX, n)):
            labels = KMeans(n_clusters=cand, n_init=3, random_state=42).fit_predict(trial)
            if len(set(labels)) < 2:
                continue
            s = sil(trial, labels)
            if s is not None and s > best:
                best, best_k = s, cand
        k = best_k

    assignments = KMeans(n_clusters=k, n_init=10, random_state=42).fit_predict(X)
    score = sil(X, assignments) or 0.0
    return assignments, {"method": "kmeans", "n_clusters": int(k), "silhouette": score}


def _name(profile: dict, cid: int) -> str:
    """Readable label from the records' own vocabulary (child_analysis.name):
    the one type they share, the age band, a sex most of them share, the most
    frequent code."""
    words = CHILD["name"]
    bits = []

    types = profile.get(words["type"]) or {} if words.get("type") else {}
    if len(types) == 1:
        code = next(iter(types))
        bits.append(CLAIM_TYPE_NAMES.get(str(code).strip(), str(code).strip()))

    age = profile.get(words["age"]) or {} if words.get("age") else {}
    mean = age.get("mean")
    if mean is not None:
        m = float(mean)
        bits.append(next(word for below, word in words["ages"] if below is None or m < below))

    sex = profile.get(words["sex"]) or {} if words.get("sex") else {}
    if sex:
        top, share = max(sex.items(), key=lambda kv: kv[1])
        if share >= 0.65:
            bits.append("female" if top.upper().startswith("F") else "male")

    codes = profile.get(words["top"]) or {} if words.get("top") else {}
    if codes:
        code = next(iter(codes))
        desc = (profile.get(words.get("top_descriptions") or "") or {}).get(code)
        bits.append(f"{desc}" if desc else f"{words.get('top_prefix') or ''} {code}".strip())

    return " · ".join(bits) if bits else f"{words['fallback']} {cid}"


def _tell_apart(patterns: list[dict]) -> None:
    """Patterns the vocabulary names alike are told apart by their average
    charge, which is most often what split them; a tie left after that is
    numbered."""
    alike: dict[str, list[dict]] = {}
    for pattern in patterns:
        alike.setdefault(pattern["name"], []).append(pattern)
    for name, same in alike.items():
        if len(same) < 2:
            continue
        for pattern in same:
            amount = CHILD["name"]["amount"]
            mean = ((pattern.get("continuous") or {}).get(amount) or {}).get("mean")
            if mean is not None:
                pattern["name"] = f"{name} · {CHILD['name']['amount_words']} ${float(mean):,.0f}"
        seen: dict[str, int] = {}
        for pattern in same:
            seen[pattern["name"]] = seen.get(pattern["name"], 0) + 1
            if seen[pattern["name"]] > 1:
                pattern["name"] = f"{pattern['name']} #{seen[pattern['name']]}"


# The clinical facts of a claim line, and the element each one is. Kept as
# whole combinations, the way an address is kept: a diagnosis and a procedure
# drawn apart can make a pair no claim has ever carried.
CLINICAL_COLUMNS = held("features:store.clinical_columns")
# The most common combinations a pattern keeps, with how often each occurs.
MAX_COMBINATIONS = 300


def _combinations(subset: pd.DataFrame, store: str) -> list[dict]:
    from sdg.plan.combinations import for_patterns, of_rows

    return of_rows(subset, store, for_patterns())


def describe(subset: pd.DataFrame, cid: int, *, total_claims: int,
             total_lines: int, scope_n: int, thresholds: dict | None = None) -> dict:
    """One claims pattern as the screen and a saved pattern both read it.

    Shared by the analysis and by saving a pattern made of chosen records, so
    the two can never describe the same lines differently.
    """
    has_claim_id = RECORD_KEY in subset.columns
    profile = profile_claims_cluster(subset)
    size = int(profile.get("size", len(subset)))
    n_claims = int(subset[RECORD_KEY].nunique()) if has_claim_id else size

    # Weight is a share of CLAIMS, not claim lines. Lines-per-claim varies
    # between patterns, so a line-based share overstates patterns whose
    # claims happen to carry more detail lines.
    pct = (n_claims / total_claims) if total_claims else 0.0
    pct_lines = (size / total_lines) if total_lines else 0.0
    # Share of the topmost selected level, not the whole table.
    pct_total = (n_claims / scope_n) if scope_n else 0.0

    return {
        "cluster_id": cid,
        "name": _name(profile, cid),
        "size": size,
        "pct_of_subset": round(pct, 4),
        "pct_of_lines": round(pct_lines, 4),
        "pct_of_total": round(pct_total, 6),
        "rare": pct < RARE_THRESHOLD,
        "weight_band": weight_band(pct_total, size=n_claims, thresholds=thresholds),
        "n_claims": n_claims,
        "n_members": int(subset[PRIMARY_KEY].nunique()) if PRIMARY_KEY in subset.columns else 0,
        # Its own combinations of every group of dependent features -- the
        # diagnosis with the procedure, the claim type with both -- drawn whole.
        "combinations": _combinations(subset, entity_loader.config(ENTITY)["table"]),
        "approval_rate": to_py(profile.get("approval_rate")),
        "in_network_rate": to_py(profile.get("in_network_rate")),
        "continuous": {
            label: to_py(profile[label]) for label in CHILD["shown_continuous"] if label in profile
        },
        "categorical": {
            label: to_py(profile[label]) for label in CHILD["shown_categorical"] if label in profile
        },
        # The most frequent codes and their descriptions (child_analysis
        # .shown_top: health's diagnoses and procedures, a bank's merchant codes).
        **{key: to_py(profile.get(key) or {}) for key in CHILD["shown_top"]},
    }


def analyze(filters: dict, k: Optional[int] = None) -> dict:
    df, filters_used = _load(filters)
    total_lines = len(df)
    has_claim_id = RECORD_KEY in df.columns
    total_claims = int(df[RECORD_KEY].nunique()) if has_claim_id else total_lines
    book = book_totals()
    book_claims = book["claims"] or total_claims
    scope_n, scope_label, from_baseline = scope_denominator(filters)
    scope_n = scope_n or total_claims

    from . import baseline as _bl
    thresholds = _bl.get_thresholds(ENTITY)
    assignments, metrics = _cluster(filters, k)

    from sdg.analyzer.histograms import describe as _describe

    clusters = sorted({int(a) for a in assignments if a >= 0})
    distributions, shapes = _describe(
        df, CHILD["histograms"],
        {cid: np.asarray(assignments) == cid for cid in clusters},
    )

    patterns = []
    claims_seen = 0
    for cid in clusters:
        mask = np.asarray(assignments) == cid
        subset = df.iloc[mask]
        pattern = describe(subset, cid, total_claims=total_claims,
                           total_lines=total_lines, scope_n=scope_n,
                           thresholds=thresholds)
        claims_seen += pattern["n_claims"]
        pattern["histograms"] = shapes.get(cid, {})
        patterns.append(pattern)

    patterns.sort(key=lambda p: p["n_claims"], reverse=True)
    _tell_apart(patterns)

    # Clustering runs at line grain, so a single claim's lines can be split
    # across patterns. When that happens the per-pattern claim counts sum to
    # more than the subset total and the percentages exceed 100. Report it
    # rather than quietly rescaling — it tells the user something true about
    # how well the chosen features separate whole claims.
    split_claims = max(0, claims_seen - total_claims)

    return {
        "subset_claim_lines": total_lines,
        "subset_claims": total_claims,
        "denominator": ENTITY,
        "book_claims": book_claims,
        "book_claim_lines": book["lines"],
        "scope_total": scope_n,
        "scope_label": scope_label,
        "scope_from_baseline": from_baseline,
        "weight_explained": (
            f"pattern {packs.active().term(ENTITY, 'many')} / {scope_n:,} in {scope_label}"
            f" ({'stored baseline' if from_baseline else 'live count'})"
            + (f" · bands from percentiles of {thresholds.get('n_patterns', 0):,} patterns"
               if thresholds else " · bands from fixed shares (no baseline)")
        ),
        "band_thresholds": thresholds,
        "pct_outside_filter": round(1 - (total_claims / scope_n), 6) if scope_n else 0.0,
        "split_claims": split_claims,
        "avg_lines_per_claim": round(total_lines / total_claims, 2) if total_claims else 0.0,
        "subset_members": int(df[PRIMARY_KEY].nunique()) if PRIMARY_KEY in df.columns else 0,
        "n_patterns": len(patterns),
        "method": str(metrics.get("method", "kmeans")),
        "silhouette": to_py(metrics.get("silhouette")),
        "filters_used": to_py(filters_used or {}),
        "patterns": patterns,
        "distributions": distributions,
        "cache_key": cache_key(filters, k),
    }


def pattern_rows(filters: dict, k: Optional[int], cluster_id: int) -> pd.DataFrame:
    """The real claim rows behind one pattern — used for generation."""
    df, _ = _load(filters)
    assignments, _ = _cluster(filters, k)
    mask = np.asarray(assignments) == cluster_id
    if not mask.any():
        raise ValueError(f"Pattern {cluster_id} is not present in this subset.")
    return df.iloc[mask]


# ── Generation ───────────────────────────────────────────────────────────────

def _is_member_pattern(p: dict) -> bool:
    """Whether a stored pattern belongs to the member domain.

    Delegates to pattern_service.entity_of so the dropdown and the clone path
    cannot disagree about a row's domain. They previously used separate tests:
    a clone of a legacy claims pattern was tagged a member by one and a claims
    pattern by the other, so it was offered here and then failed to generate.
    """
    from . import pattern_service as ps

    return is_primary(ps.entity_of(p))


def saved_member_patterns() -> list[dict]:
    """Member patterns available to attach generated claims to.

    Claims patterns are excluded. Mapping claims onto a claims pattern is not a
    coherent request — the generator would be handed a claims profile where it
    expects member demographics — and it was the source of the int('') failure
    rather than a readable message.
    """
    from sdg.analyzer import db

    out = []
    for p in db.get_all_patterns(include_archived=False) or []:
        if not _is_member_pattern(p):
            continue
        out.append({
            "pattern_id": int(p["id"]),
            "name": str(p.get("contextual_name") or f"Pattern {p['id']}"),
            "member_count": int(p.get("member_count") or 0),
            **pattern_scope.named_labels(pattern_scope.labels_of(p)),
            "saved": bool(p.get("saved_at")),
        })
    out.sort(key=lambda x: (not x["saved"], -x["member_count"]))
    return out


def _require_member_pattern(member_pattern_id: int) -> None:
    """Reject a mapping onto anything that isn't a member pattern.

    The dropdown no longer offers claims patterns, but a stale browser tab or a
    direct API call still can — and the failure was a raw int() error from deep
    inside the loaders rather than anything explaining what went wrong.
    """
    from sdg.analyzer import db

    if not member_pattern_id:
        raise ValueError(
            "Choose a member pattern first — generated claims attach to "
            "subscribers drawn from it."
        )

    pattern = db.get_pattern(member_pattern_id)
    if not pattern:
        raise ValueError(
            f"Member pattern {member_pattern_id} no longer exists. "
            "Pick another from the list."
        )
    if not _is_member_pattern(pattern):
        name = pattern.get("contextual_name") or f"Pattern {member_pattern_id}"
        raise ValueError(
            f"“{name}” is a claims pattern, not a member pattern. Claims attach "
            "to subscribers, so the mapping has to be a member pattern."
        )


def generate(*_args, **_kwargs):
    """Gone with the analyzer's generator; the plan engine generates now."""
    raise NotImplementedError(
        "Generation moved to the plan engine. Build a plan, then use /api/export."
    )


def _sanitise_pool(df):
    """Coerce every key column to int, leaving everything else alone.

    Defensive on purpose: it does not matter which rule is missing or which
    downstream line would have cast it — after this, none of them can fail.
    """
    if df is None or df.empty:
        return df

    from sdg.analyzer import coerce

    for col in df.columns:
        if coerce.is_numeric_column(col):
            df[col] = coerce.to_int(df[col])
    return df

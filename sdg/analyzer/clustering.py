"""K-means clustering with auto-k selection and label-based mode."""

import numpy as np

from sdg import packs

from .config import CONTINUOUS_WEIGHT


# Silhouette computes every pairwise distance, so it costs O(n^2). Above a
# few thousand points that dominates everything else in this module. Scoring a
# fixed-size sample estimates the same value closely enough to choose k, and
# caps the cost regardless of subset size. random_state keeps it deterministic.
SILHOUETTE_SAMPLE = 5000

# n_init controls how many random restarts K-Means tries. The sweep below only
# needs a good-enough fit to compare k values against each other, so it uses
# fewer restarts; the final fit that produces the clusters people actually see
# still uses the full count.
SWEEP_N_INIT = 3
FINAL_N_INIT = 10


def _silhouette(X, labels):
    """Silhouette score, sampled so the cost stays flat as n grows."""
    score = silhouette(X, labels)
    if score is None:
        raise ValueError("fewer than two groups to compare")
    return score


def silhouette(X, labels, sample: int = SILHOUETTE_SAMPLE):
    """How well the groups separate, or None when there are not two to compare.

    Scored on a sample of rows. A group of a few rows among many -- a handful
    of claims with extreme charges -- can be missed by the sample entirely,
    leaving one group, which the score cannot be taken on. Then the sample is
    taken again with one row of every group in it. Where the first sample
    holds two groups, the score is exactly what it always was.
    """
    from sklearn.metrics import silhouette_score

    labels = np.asarray(labels)
    n = labels.shape[0]
    groups = np.unique(labels)
    if len(groups) < 2 or len(groups) >= n:
        return None
    try:
        return float(silhouette_score(
            X, labels, sample_size=min(sample, n), random_state=42))
    except ValueError:
        pass
    pick = np.random.default_rng(42).choice(n, min(sample, n), replace=False)
    every = np.array([np.flatnonzero(labels == g)[0] for g in groups])
    pick = np.union1d(pick, every)
    if len(pick) <= len(groups):
        return None
    return float(silhouette_score(X[pick], labels[pick]))


def build_features(data, continuous_cols=None):
    """The clustering's input: the features chosen on the Config screen.

    Numbers standardized and weighted (CONTINUOUS_WEIGHT), lists one-hot.
    `continuous_cols` is kept for callers that pass it; the choice decides.
    """
    from .services.discovery_features import matrix

    X = matrix(data, packs.active().primary, CONTINUOUS_WEIGHT)
    if X is None:
        X = np.zeros((len(data), 1))
    return X, None


# The k values the member sweep considers.
#
# Measured over all 42 member combinations in SDM_Platform: the sweep chose k=3
# eighteen times and k=4 twenty-four times, and never once chose anything above
# 4. Values 7-10 were 27 KMeans fits per combination that could not win, and the
# sweep is ~85% of a discovery walk. Cutting to 2-6 keeps two values of headroom
# above the highest ever selected and measured 1.76x faster with an identical
# silhouette to four decimal places, the same k on every combination, and the
# same cluster count.
#
# Claims keeps the wider sweep and sets its own range — see CLAIMS_K_MAX in
# api/claims_service.py. There the ceiling is real: four combinations select
# k=7 or k=10, so the same narrowing would cost them clusters.
#
# This is a property of the data, not of the algorithm. A book whose members
# genuinely split into eight groups would want the range back, and the symptom
# would be the sweep always landing on the top of the range.
MEMBER_K_RANGE = range(2, 7)


def find_optimal_k(X, k_range=MEMBER_K_RANGE):
    """Silhouette sweep over k_range, return best k."""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    best_k = 2
    best_score = -1
    for k in k_range:
        if k >= X.shape[0]:
            break
        km = KMeans(n_clusters=k, n_init=SWEEP_N_INIT, random_state=42)
        labels = km.fit_predict(X)
        if len(set(labels)) < 2:
            continue
        try:
            score = _silhouette(X, labels)
        except ValueError:
            continue
        if score > best_score:
            best_score = score
            best_k = k
    return best_k


def discover_clusters(data, labels_df, k=None, filters_used=None):
    """Run clustering and return (assignments, metrics). K-means over the
    chosen features; `labels_df` and `filters_used` are the load's, kept in
    the signature its callers share."""
    from sklearn.cluster import KMeans
    n = len(data)
    metrics = {}

    # Guard: too few members for meaningful clustering
    if n < 8:
        assignments = np.zeros(n, dtype=int)
        metrics['method'] = 'single_cluster'
        metrics['reason'] = f'n={n} < 8, too few for clustering'
        return assignments, metrics

    X, _ = build_features(data)

    # K-means mode
    if k is None:
        k = find_optimal_k(X)
        metrics['k_selection'] = 'silhouette'
    else:
        metrics['k_selection'] = 'user_specified'

    km = KMeans(n_clusters=k, n_init=FINAL_N_INIT, random_state=42)
    assignments = km.fit_predict(X)

    try:
        sil = _silhouette(X, assignments) if len(set(assignments)) >= 2 else 0.0
    except ValueError:
        sil = 0.0
    metrics['method'] = 'kmeans'
    metrics['n_clusters'] = k
    metrics['silhouette'] = round(sil, 4)

    return assignments, metrics

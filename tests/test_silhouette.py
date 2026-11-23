"""How well groups separate is scored on a sample, and a sample that misses a
small group is taken again rather than failing the analysis.

At a customer, a handful of claims with extreme charges became a group of their
own; a 5,000-row sample of the millions held none of them, the score was asked
for one group, and the analysis stopped with "Number of labels is 1".
"""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import silhouette_score

from sdg.analyzer.clustering import silhouette


def _outliers(n: int = 20_000, few: int = 2):
    """Rows around one point, and `few` far away that the scoring sample skips."""
    rng = np.random.default_rng(7)
    X = rng.normal(0, 1, size=(n, 2))
    labels = np.zeros(n, dtype=int)
    # The rows the score's own sample reads, so the far ones are put elsewhere.
    sampled = set(np.random.RandomState(42).permutation(n)[:5000].tolist())
    far = [i for i in range(n - 1, -1, -1) if i not in sampled][:few]
    X[far] += 1_000
    labels[far] = 1
    return X, labels


def test_the_sample_that_failed_at_the_customer_now_scores():
    X, labels = _outliers()
    with pytest.raises(ValueError, match="Number of labels is 1"):
        silhouette_score(X, labels, sample_size=5000, random_state=42)
    score = silhouette(X, labels)
    assert score is not None and -1.0 <= score <= 1.0


def test_where_the_sample_holds_every_group_the_score_is_unchanged():
    rng = np.random.default_rng(3)
    X = np.vstack([rng.normal(0, 1, (6000, 2)), rng.normal(8, 1, (6000, 2))])
    labels = np.repeat([0, 1], 6000)
    assert silhouette(X, labels) == float(
        silhouette_score(X, labels, sample_size=5000, random_state=42))


def test_one_group_or_every_row_its_own_has_no_score():
    X = np.arange(20, dtype=float).reshape(10, 2)
    assert silhouette(X, np.zeros(10, dtype=int)) is None
    assert silhouette(X, np.arange(10)) is None


def test_claims_with_a_few_extreme_charges_are_still_grouped(monkeypatch):
    """The claims grouping, on lines where a few charges are far from the rest."""
    import pandas as pd

    from sdg.analyzer.services import claims_service as cs

    X, _ = _outliers(30_000, 3)
    monkeypatch.setattr(cs, "_features", lambda df: X[df.index.to_numpy()])
    frame = pd.DataFrame(index=np.arange(len(X)))
    assignments, found = cs._cluster_rows(frame, None)
    assert found["method"] == "kmeans" and len(set(assignments)) >= 2
    assignments, found = cs._cluster_rows(frame, 2)
    assert found["silhouette"] is not None

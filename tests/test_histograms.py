"""The distributions behind a pattern's charts.

Every pattern is counted into bins drawn over its whole scope, so the bars of
one pattern can be laid against the scope's and against each other's. If the
bins were not shared, or a value were dropped off either end, the charts would
draw two different things as if they were one.
"""

import numpy as np
import pandas as pd
import pytest

from sdg.analyzer.histograms import counts_for, describe, edges_for


def test_bins_fall_on_round_numbers_and_cover_every_value():
    values = np.random.default_rng(1).normal(40, 12, 5000)
    bins = edges_for(values)
    edges = bins["edges"]
    step = edges[1] - edges[0]
    assert step in (1, 2, 2.5, 5, 10)
    assert edges[0] <= values.min()
    assert sum(counts_for(values, bins)) == len(values)


def test_a_long_tail_is_folded_into_an_open_last_bin():
    """One charge of 25,000 among thousands under 700 must not flatten the rest."""
    values = np.concatenate([np.random.default_rng(2).lognormal(4.5, 0.6, 5000), [25000.0]])
    bins = edges_for(values)
    assert bins["open_top"] is True
    assert bins["edges"][-1] < 2000
    counts = counts_for(values, bins)
    assert sum(counts) == len(values)
    assert counts[-1] >= 1


def test_every_pattern_is_counted_on_the_scopes_bins():
    rng = np.random.default_rng(3)
    frame = pd.DataFrame({"age": np.concatenate([rng.normal(25, 4, 600), rng.normal(60, 6, 400)])})
    labels = np.array([0] * 600 + [1] * 400)
    scope, shapes = describe(frame, {"_age": "age"}, {0: labels == 0, 1: labels == 1})

    assert sum(scope["_age"]["counts"]) == 1000
    assert sum(shapes[0]["_age"]) == 600 and sum(shapes[1]["_age"]) == 400
    # Shared bins: the patterns' counts add up to the scope's, bin by bin.
    assert [a + b for a, b in zip(shapes[0]["_age"], shapes[1]["_age"])] == scope["_age"]["counts"]


def test_a_feature_that_is_absent_is_skipped_rather_than_invented():
    frame = pd.DataFrame({"age": [1, 2, 3]})
    scope, shapes = describe(frame, {"_tenure": "tenure"}, {0: np.array([True, True, True])})
    assert scope == {} and shapes == {0: {}}


@pytest.mark.db
def test_the_analysis_sends_each_patterns_distribution():
    """Through the API, whose response model drops what it does not declare."""
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    body = TestClient(app).post(
        "/api/patterns/analyze", json={"filters": {"grgr_ck": [1]}, "k": 2},
    ).json()
    assert set(body["distributions"]) >= {"_age", "_tenure"}
    for pattern in body["patterns"]:
        assert sum(pattern["histograms"]["_age"]) == pattern["size"]
    scope = body["distributions"]["_age"]
    assert len(scope["counts"]) == len(scope["edges"]) - 1

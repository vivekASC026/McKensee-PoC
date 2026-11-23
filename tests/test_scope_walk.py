"""The combinations a walk visits come from the business pack's scope levels.

Discovery and the baseline once walked health's group, plan category, plan
and product in loops written per record kind. scope_walk walks whatever
levels the pack's loader lists, stops at the levels a pattern's scope is made
of, and gives each value as its filter takes it. (On this machine's store the
new walks were compared with the old ones at every depth, limit and cap
before the old ones were removed: all twenty the same.)
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from sdg.analyzer import scope_walk
from sdg.analyzer.services import analysis


def test_the_levels_walked_are_the_pattern_scope_levels():
    assert [lv["filter"] for lv in scope_walk._levels("members")] == [
        "grgr_ck", "cspd_cat", "cspi_id", "pdpd_id"]
    # Health's claims narrow by type too, but no pattern is found in a type.
    assert [lv["filter"] for lv in scope_walk._levels("claims")] == ["grgr_ck", "cspi_id", "pdpd_id"]
    assert scope_walk.first_filter("claims") == "grgr_ck"
    assert scope_walk.depth_of({"grgr_ck": [1], "cspi_id": ["P"], "claim_type": ["I"]}, "claims") == 2


GRID = pd.DataFrame([
    {"GRGR_CK": 10, "GRGR_NAME": "Big", "CSPI_ID": None, "PLDS_DESC": None, "PDPD_ID": None,
     "n_claims": 900, "depth": 1},
    {"GRGR_CK": "x", "GRGR_NAME": "Bad id", "CSPI_ID": None, "PLDS_DESC": None, "PDPD_ID": None,
     "n_claims": 800, "depth": 1},
    {"GRGR_CK": 20, "GRGR_NAME": "Mid", "CSPI_ID": "A", "PLDS_DESC": "Plan A", "PDPD_ID": None,
     "n_claims": 400, "depth": 2},
    {"GRGR_CK": 30, "GRGR_NAME": "Deep", "CSPI_ID": "B", "PLDS_DESC": "Plan B", "PDPD_ID": "X",
     "n_claims": 50, "depth": 3},
    {"GRGR_CK": 40, "GRGR_NAME": "Other", "CSPI_ID": None, "PLDS_DESC": None, "PDPD_ID": None,
     "n_claims": 10, "depth": 1},
])


def test_by_size_reads_each_row_to_its_depth_as_the_filters_take_it():
    with patch("sdg.analyzer.entity_loader.combination_sizes", return_value=GRID) as sizes:
        combos = scope_walk.by_size("claims", 3, None, cap=500, minimum=100)
    sizes.assert_called_once_with("claims", max_depth=3, minimum=100)
    # An id that is no number is no combination.
    assert combos == [{"grgr_ck": [10]}, {"grgr_ck": [20], "cspi_id": ["A"]},
                      {"grgr_ck": [30], "cspi_id": ["B"], "pdpd_id": ["X"]}, {"grgr_ck": [40]}]
    with patch("sdg.analyzer.entity_loader.combination_sizes", return_value=GRID):
        assert scope_walk.by_size("claims", 3, 2, cap=500, minimum=0) == combos[:2]
        assert scope_walk.by_size("claims", 3, None, cap=1, minimum=0) == combos[:1]


def test_by_size_gives_way_where_the_aggregate_cannot_be_read():
    with patch("sdg.analyzer.entity_loader.combination_sizes", side_effect=RuntimeError("no")):
        assert scope_walk.by_size("members", 2, None, 500, 30) is None
    with patch("sdg.analyzer.entity_loader.combination_sizes", return_value=pd.DataFrame()):
        assert scope_walk.by_size("members", 2, None, 500, 30) is None


CHOICES = {
    ("groups", ()): [{"key": "1", "label": "One"}, {"key": "2", "label": "Two"}],
    ("plan-categories", (("grgr_ck", (1,)),)): [{"key": "M", "label": "Medical"}],
    ("plan-categories", (("grgr_ck", (2,)),)): [],
    ("plans", (("grgr_ck", (1,)), ("cspd_cat", ("M",)))): [{"key": "P1", "label": "P1"}],
    ("products", (("grgr_ck", (1,)), ("cspd_cat", ("M",)), ("cspi_id", ("P1",)))): [
        {"key": "D", "label": "D"}],
}


def _options(entity, level, chosen=()):
    return CHOICES.get((level, tuple(chosen)), [])


def test_every_walks_each_value_then_what_it_narrows_to():
    with patch("sdg.analyzer.entity_loader.options", side_effect=_options):
        assert scope_walk.every("members", 4, None, 100) == [
            {"grgr_ck": [1]},
            {"grgr_ck": [1], "cspd_cat": ["M"]},
            {"grgr_ck": [1], "cspd_cat": ["M"], "cspi_id": ["P1"]},
            {"grgr_ck": [1], "cspd_cat": ["M"], "cspi_id": ["P1"], "pdpd_id": ["D"]},
            {"grgr_ck": [2]},
        ]
        assert scope_walk.every("members", 4, None, 100, deepest=2) == [
            {"grgr_ck": [1]}, {"grgr_ck": [1], "cspd_cat": ["M"]}, {"grgr_ck": [2]}]
        assert scope_walk.every("members", 1, 1, 100) == [{"grgr_ck": [1]}]
        assert len(scope_walk.every("members", 4, None, 3)) == 3


def test_the_baseline_walks_the_unfiltered_book_first():
    from sdg.api.routers.baseline import _combinations

    with patch("sdg.analyzer.entity_loader.options", side_effect=_options):
        combos = _combinations("members", 2, None, 10_000)
        assert combos == [{}, {"grgr_ck": [1]}, {"grgr_ck": [1], "cspd_cat": ["M"]},
                          {"grgr_ck": [2]}]
        assert len(_combinations("members", 4, None, 3)) == 3


def test_each_record_kind_is_analyzed_by_its_role():
    primary, child = analysis.for_entity("members"), analysis.for_entity("claims")
    assert (primary.role, primary.subset_key, primary.size_key) == ("primary", "subset_members", "size")
    assert (child.role, child.subset_key, child.size_key) == ("child", "subset_claims", "n_claims")
    assert child.size({"n_claims": "7"}) == 7 and child.subset({}) == 0
    with pytest.raises(LookupError, match="entity must be"):
        analysis.for_entity("subscribers")

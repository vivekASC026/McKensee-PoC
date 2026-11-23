"""Changing a rule the tool worked out.

Everything the tool decides is a proposal, so every generator has to be
editable and every edit has to be checked before it reaches a plan. These tests
cover both halves: the description a form is built from, and the validation that
stands between a typed-in value and the engine.
"""

from __future__ import annotations

import json

import pytest

from sdg.config import REPO_ROOT
from sdg.plan.editing import (
    GENERATORS,
    STRUCTURAL_WARNING,
    check,
    check_fits,
    check_ratio,
    coerce,
    coerce_ratio,
    generator_catalog,
    ratio_shapes,
)


# ---------------------------------------------------------------------------
# Every generator is editable
# ---------------------------------------------------------------------------

def test_every_generator_a_plan_may_contain_can_be_edited():
    """The gap this guards against: adding a generator nobody can change."""
    schema = json.loads((REPO_ROOT / "sdg" / "plan" / "schema.json").read_text(encoding="utf-8"))
    allowed = set(schema["$defs"]["generator"]["enum"])
    assert allowed == set(GENERATORS), (
        "a plan can contain a generator the interface cannot edit, or the other way round"
    )


def test_the_catalog_describes_each_generator_well_enough_to_build_a_form():
    for spec in generator_catalog():
        assert spec["label"] and spec["summary"], spec["name"]
        for field in spec["fields"]:
            assert field["name"] and field["label"], spec["name"]
            assert field["type"] in {
                "text", "number", "integer", "boolean", "select", "lines", "numbers",
            }, field
            # A dropdown that offers nothing cannot be filled in.
            if field["type"] == "select":
                assert field.get("options"), field


def test_overriding_a_structural_rule_is_allowed_but_says_what_it_costs():
    assert "fails the checks" in STRUCTURAL_WARNING


# ---------------------------------------------------------------------------
# Form values become engine values
# ---------------------------------------------------------------------------

def test_numbers_typed_into_a_form_arrive_as_numbers():
    params = coerce("number", {
        "distribution": "normal", "min": "0", "max": "500.5",
        "mean": "12", "stddev": "3.5", "decimals": "2",
    })
    assert params["min"] == 0.0 and isinstance(params["min"], float)
    assert params["max"] == 500.5
    assert params["decimals"] == 2 and isinstance(params["decimals"], int)


def test_a_field_left_empty_is_absent_rather_than_blank():
    """An empty string in a plan reads as a real value, which it is not."""
    params = coerce("lookup", {"values": ["A", "B"], "record_key": "", "field": None})
    assert params == {"values": ["A", "B"]}


def test_blank_lines_in_a_list_are_dropped():
    params = coerce("lookup", {"values": ["A", "", "B", "   "], "weights": ["1", "", "2"]})
    assert params["values"] == ["A", "B"]
    assert params["weights"] == [1.0, 2.0]


def test_a_parameter_the_generator_does_not_describe_is_left_alone():
    """Rules carry parameters the catalog set; editing one must not drop them."""
    params = coerce("lookup", {"values": ["A"], "reuse_domain": {"table": "dbo.X"}})
    assert params["reuse_domain"] == {"table": "dbo.X"}


def test_a_word_where_a_number_belongs_is_refused_by_name():
    with pytest.raises(ValueError, match="Decimal places"):
        coerce("number", {"distribution": "uniform", "decimals": "two"})


# ---------------------------------------------------------------------------
# What a person is told before saving
# ---------------------------------------------------------------------------

def test_a_rule_with_everything_it_needs_has_nothing_to_report():
    assert check("pattern", {"regex": r"[A-Z]{3}\d{8}", "unique": True}) == []
    assert check("skip", {}) == []


def test_a_missing_requirement_names_the_field_and_the_rule():
    problems = check("pattern", {})
    assert problems == ["Format is needed for a pattern rule."]


def test_a_format_that_cannot_be_compiled_says_why():
    problems = check("pattern", {"regex": "[Z-A]"})
    assert len(problems) == 1
    assert "cannot be used" in problems[0] and "Z-A" in problems[0]


def test_an_expression_that_cannot_be_compiled_says_why():
    problems = check("expression", {"expression": "1 +"})
    assert len(problems) == 1
    assert "cannot be used" in problems[0]


def test_an_expression_reaching_for_internals_is_refused():
    problems = check("expression", {"expression": "A.__class__"})
    assert problems and "cannot be used" in problems[0]


def test_weights_that_do_not_line_up_with_values_are_counted_back():
    problems = check("lookup", {"values": ["A", "B"], "weights": [1.0]})
    assert problems == [
        "There are 2 values but 1 weights; they have to match, or leave the weights empty."
    ]


def test_evenly_weighted_values_need_no_weights():
    assert check("lookup", {"values": ["A", "B"]}) == []


def test_a_range_that_runs_backwards_is_caught():
    problems = check("number", {"distribution": "uniform", "min": 10, "max": 1})
    assert problems == ["The lowest value is above the highest."]


def test_a_shaped_distribution_needs_its_shape():
    problems = check("number", {"distribution": "lognormal", "min": 0})
    assert len(problems) == 1
    assert "mean and a standard deviation" in problems[0]
    assert check("number", {"distribution": "uniform", "min": 0}) == []


def test_a_date_window_that_runs_backwards_is_caught():
    problems = check("date_relative", {"anchor": "CLCL_RECD_DT", "min_days": 30, "max_days": 5})
    assert problems == ["The earliest gap is larger than the latest."]


def test_a_generator_that_does_not_exist_is_named():
    assert check("magic", {}) == ["There is no generator called magic."]


def test_problems_are_reported_together_rather_than_one_at_a_time():
    problems = check("number", {"distribution": "normal", "min": 10, "max": 1})
    assert len(problems) == 2


# ---------------------------------------------------------------------------
# The two halves work as one
# ---------------------------------------------------------------------------

def test_a_form_full_of_strings_survives_the_round_trip():
    typed = {"distribution": "normal", "min": "1", "max": "100", "mean": "50", "stddev": "10"}
    params = coerce("number", typed)
    assert check("number", params) == []


def test_coercion_is_what_makes_the_check_pass():
    """Strings would compare wrongly, so the order of the two calls matters."""
    typed = {"distribution": "uniform", "min": "9", "max": "100"}
    # "9" > "100" as text; 9 < 100 as numbers.
    assert check("number", coerce("number", typed)) == []


# ---------------------------------------------------------------------------
# How many children each parent gets
# ---------------------------------------------------------------------------

def test_the_measured_shape_is_offered_but_not_asked_to_be_retyped():
    """A histogram is the honest answer; nobody should type one in."""
    shapes = {s["name"]: s for s in ratio_shapes()}
    assert set(shapes) == {"measured", "fixed", "range"}
    assert shapes["measured"]["fields"] == []


def test_both_replacement_shapes_offer_a_childless_share():
    """A flat ratio with no childless share is what makes fake data look fake."""
    for name in ("fixed", "range"):
        fields = [f["name"] for f in next(
            s for s in ratio_shapes() if s["name"] == name
        )["fields"]]
        assert fields[-1] == "zero_share"


def test_a_stated_ratio_survives_the_round_trip():
    params = coerce_ratio("fixed", {"value": "5", "zero_share": "0.3"})
    assert params == {"value": 5.0, "zero_share": 0.3}
    assert check_ratio("fixed", params) == []


def test_a_range_needs_both_ends():
    problems = check_ratio("range", coerce_ratio("range", {"min": "2"}))
    assert problems == ["Most is needed for between two numbers."]


def test_a_range_that_runs_backwards_is_caught():
    problems = check_ratio("range", coerce_ratio("range", {"min": "9", "max": "2"}))
    assert problems == ["The fewest is above the most."]


def test_a_childless_share_outside_its_bounds_is_caught():
    for share in ("1.4", "1", "-0.2"):
        problems = check_ratio("fixed", coerce_ratio("fixed", {"value": "5", "zero_share": share}))
        assert any("between 0 and 1" in p for p in problems), share


def test_every_parent_childless_is_refused_because_it_makes_no_rows():
    assert check_ratio("fixed", {"value": 5.0, "zero_share": 1.0})


def test_a_word_where_a_count_belongs_is_refused_by_name():
    with pytest.raises(ValueError, match="Children per parent needs a number"):
        coerce_ratio("fixed", {"value": "lots"})


def test_a_ratio_shape_that_does_not_exist_is_named():
    assert check_ratio("vibes", {}) == ["There is no ratio shape called vibes."]


# ---------------------------------------------------------------------------
# The childless share has to actually do something
# ---------------------------------------------------------------------------

def test_a_childless_share_lowers_the_average_it_is_attached_to():
    """Effective counts are computed from the mean, so it has to include this."""
    from sdg.plan.model import Ratio

    flat = Ratio(parent="dbo.P", kind="fixed", value=5)
    with_none = Ratio(parent="dbo.P", kind="fixed", value=5, zero_share=0.3)
    assert flat.mean_children() == 5.0
    assert with_none.mean_children() == pytest.approx(3.5)

    spread = Ratio(parent="dbo.P", kind="range", min=2, max=8, zero_share=0.5)
    assert spread.mean_children() == pytest.approx(2.5)


def test_a_histogram_is_not_docked_twice_for_its_childless_parents():
    """The zero bin already counts them; the share is descriptive there."""
    from sdg.plan.model import HistogramBin, Ratio

    ratio = Ratio(
        parent="dbo.P",
        kind="distribution",
        zero_share=0.5,
        histogram=[HistogramBin(children=0, weight=0.5), HistogramBin(children=4, weight=0.5)],
    )
    assert ratio.mean_children() == pytest.approx(2.0)


def test_the_engine_draws_the_childless_share_it_was_given():
    from sdg.engine.execute import children_for_parent
    from sdg.plan.model import Ratio

    ratio = Ratio(parent="dbo.P", kind="fixed", value=5, zero_share=0.3)
    counts = [
        children_for_parent(ratio, "a-seed", "default", "dbo.C", i)
        for i in range(4000)
    ]
    childless = sum(1 for c in counts if c == 0) / len(counts)
    assert 0.27 < childless < 0.33
    # Everyone who gets children gets exactly the stated number.
    assert set(counts) == {0, 5}


def test_the_same_seed_draws_the_same_counts():
    from sdg.engine.execute import children_for_parent
    from sdg.plan.model import Ratio

    ratio = Ratio(parent="dbo.P", kind="fixed", value=5, zero_share=0.3)
    once = [children_for_parent(ratio, "s", "default", "dbo.C", i) for i in range(200)]
    again = [children_for_parent(ratio, "s", "default", "dbo.C", i) for i in range(200)]
    assert once == again


def test_a_stated_ratio_replaces_the_measured_one_and_says_so():
    from sdg.plan.build import ratio_for

    ratio, mean = ratio_for(
        None, "dbo.CMC_CLCL_CLAIM", "dbo.CMC_MEME_MEMBER",
        {"kind": "fixed", "value": 5.0, "zero_share": 0.3},
    )
    assert ratio is not None
    assert ratio.kind == "fixed" and ratio.value == 5.0
    assert ratio.provenance == "user"
    assert mean == pytest.approx(3.5)
    # The plan records where it came from, like every other rule.
    assert "set by hand" in ratio.evidence[0].detail
    assert "30% with none" in ratio.evidence[0].detail


def test_without_an_override_and_without_a_profile_there_is_no_ratio():
    from sdg.plan.build import ratio_for

    assert ratio_for(None, "dbo.C", "dbo.P", None) == (None, None)
    assert ratio_for(None, "dbo.C", "dbo.P", {"kind": "measured"}) == (None, None)


# ---------------------------------------------------------------------------
# An edit that would stop a load partway
# ---------------------------------------------------------------------------

class _Column:
    """Just enough of a physical column for the width check."""

    def __init__(self, name, length, character=True):
        self.name = name
        self.is_character = character
        self._length = length

    def character_length(self):
        return self._length


def test_a_pattern_reports_the_longest_value_it_can_make():
    from sdg.engine.pattern import compile_pattern

    assert compile_pattern(r"[A-Z]{3}\d{8}").widest() == 11
    assert compile_pattern(r"[A-Z]{4}\d{9}").widest() == 13
    assert compile_pattern(r"PRV\d{9}").widest() == 12
    # A variable repeat is measured at its longest, not its shortest.
    assert compile_pattern(r"[A-Z]{2,4}\d{1,3}").widest() == 7
    # An alternation is measured by its widest branch.
    assert compile_pattern(r"(AB|CDEF)\d{2}").widest() == 6


def test_a_pattern_too_wide_for_its_column_is_refused():
    """The failure this prevents is a truncation error mid-load, not a bad plan."""
    problems = check_fits(
        "pattern", {"regex": r"[A-Z]{4}\d{9}"}, _Column("MEME_ID", 11),
    )
    assert len(problems) == 1
    assert "holds 11 characters" in problems[0]
    assert "can produce 13" in problems[0]


def test_a_pattern_that_fits_is_allowed():
    assert check_fits("pattern", {"regex": r"[A-Z]{3}\d{8}"}, _Column("MEME_ID", 11)) == []


def test_a_lookup_value_too_wide_for_its_column_is_refused():
    problems = check_fits(
        "lookup", {"values": ["01", "PENDING REVIEW"]}, _Column("CLCL_CUR_STS", 2),
    )
    assert problems and "can produce 14" in problems[0]


def test_a_constant_too_wide_for_its_column_is_refused():
    problems = check_fits("constant", {"value": "REJECTED"}, _Column("STS", 2))
    assert problems and "can produce 8" in problems[0]


def test_a_non_character_column_has_no_width_to_exceed():
    assert check_fits("constant", {"value": "12345"}, _Column("N", 4, character=False)) == []


def test_an_unmeasurable_column_is_not_guessed_at():
    """No model yet, or a varchar(max), means there is nothing to check against."""
    assert check_fits("pattern", {"regex": r"\d{99}"}, None) == []
    assert check_fits("pattern", {"regex": r"\d{99}"}, _Column("C", -1)) == []


def test_a_format_that_will_not_compile_is_left_to_the_other_check():
    """Two messages about one typo would be noise."""
    assert check_fits("pattern", {"regex": "[Z-A]"}, _Column("C", 2)) == []

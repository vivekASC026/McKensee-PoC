"""Turning a discovered pattern into generation rules.

The analyzer describes a cluster in statistics and the generator wants rules.
These tests pin the three translations that would otherwise lose information
quietly: a band must not collapse to its mean, a distribution must not collapse
to its majority value, and a prevalence must not be invented.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sdg.analyzer.bridge import MINIMUM_SHARE, describe, ratio_overrides, to_pattern
from sdg.api.app import app

client = TestClient(app)

MEMBER = "dbo.CMC_MEME_MEMBER"


# ---------------------------------------------------------------------------
# Continuous features cross as bands
# ---------------------------------------------------------------------------

def test_a_measured_range_crosses_as_a_band_not_as_its_mean():
    """A cluster spanning 18 to 32 is not a population of 25-year-olds."""
    pattern = to_pattern({
        "cluster_id": 0,
        "continuous": {"_age": {"mean": 25.1, "std": 4.0, "min": 18, "max": 32}},
    })
    age = next(p for p in pattern.parameters if p.label == "Age")
    assert (age.min, age.max) == (18, 32)
    assert age.value is None
    assert age.unit == "years"


def test_a_range_that_was_not_measured_falls_back_to_one_deviation():
    """Mean and deviation are a narrower claim than nothing, and a truer one
    than the mean on its own."""
    pattern = to_pattern({"continuous": {"_age": {"mean": 40.0, "std": 6.0}}})
    age = next(p for p in pattern.parameters if p.label == "Age")
    assert (age.min, age.max) == (34, 46)


def test_a_feature_with_nothing_measurable_is_left_out_rather_than_guessed():
    pattern = to_pattern({"continuous": {"_age": {}}})
    assert pattern.parameters == []


def test_tenure_is_carried_in_the_months_the_analyzer_measured():
    pattern = to_pattern({"continuous": {"_tenure": {"min": 12, "max": 60}}})
    tenure = next(p for p in pattern.parameters if p.label == "Tenure")
    assert (tenure.min, tenure.max, tenure.unit) == (12, 60, "months")


# ---------------------------------------------------------------------------
# Categorical features cross as distributions
# ---------------------------------------------------------------------------

def test_a_split_crosses_as_weights_rather_than_as_its_majority():
    pattern = to_pattern({
        "categorical": {"MEME_SEX": {"pct": {"F": 0.54, "M": 0.46}}},
    })
    sex = next(p for p in pattern.parameters if p.label == "Sex")
    assert sex.values == ["F", "M"]
    assert sex.weights == [0.54, 0.46]
    assert sex.value is None


def test_percentages_given_out_of_a_hundred_still_read_as_proportions():
    pattern = to_pattern({
        "categorical": {"MEME_SEX": {"pct": {"F": 54.0, "M": 46.0}}},
    })
    sex = next(p for p in pattern.parameters if p.label == "Sex")
    assert sum(sex.weights) == pytest.approx(1.0)
    assert sex.weights[0] == pytest.approx(0.54, abs=0.001)


def test_counts_are_used_when_no_percentages_were_given():
    pattern = to_pattern({
        "categorical": {"MEME_SEX": {"counts": {"F": 27, "M": 23}}},
    })
    sex = next(p for p in pattern.parameters if p.label == "Sex")
    assert sex.weights[0] == pytest.approx(0.54, abs=0.001)


def test_a_share_too_small_to_be_part_of_the_pattern_is_dropped():
    """Carrying noise through would widen the generated population past the
    one that was actually found."""
    pattern = to_pattern({
        "categorical": {
            "MEME_MARITAL_STATUS": {
                "pct": {"M": 0.70, "S": 0.29, "W": MINIMUM_SHARE / 2},
            }
        },
    })
    marital = next(p for p in pattern.parameters if p.label == "Marital Status")
    assert "W" not in marital.values
    assert sum(marital.weights) == pytest.approx(1.0)


def test_a_category_with_one_value_left_crosses_as_that_value():
    pattern = to_pattern({"categorical": {"MEME_SEX": {"pct": {"F": 1.0}}}})
    sex = next(p for p in pattern.parameters if p.label == "Sex")
    assert sex.value == "F" and sex.values is None


# ---------------------------------------------------------------------------
# Prevalence and family shape
# ---------------------------------------------------------------------------

def test_the_share_of_the_population_is_carried_through_as_stated():
    assert to_pattern({"pct_of_total": 0.032}).support == pytest.approx(0.032)
    assert to_pattern({"pct_of_total": 3.2}).support == pytest.approx(0.032)
    assert to_pattern({"cluster_id": 1}).support is None


def test_dependents_per_subscriber_become_members_per_subscriber():
    """The subscriber is a member too, which is the off-by-one worth naming."""
    override = ratio_overrides(
        {"family": {"avg_dependents": 1.8}},
        child=MEMBER, parent="dbo.CMC_SBSB_SUBSC",
    )
    assert override[MEMBER]["kind"] == "fixed"
    assert override[MEMBER]["value"] == pytest.approx(2.8)


def test_a_pattern_with_no_family_statistics_states_no_ratio():
    assert ratio_overrides({}, child=MEMBER, parent="dbo.CMC_SBSB_SUBSC") == {}


def test_the_pattern_keeps_the_name_the_analyzer_gave_it():
    pattern = to_pattern({"cluster_id": 2, "name": "Riverside-YoungAdult-Single-P0"})
    assert pattern.label == "Riverside-YoungAdult-Single-P0"
    assert pattern.id == "cluster-2"
    assert "nothing to state" in describe(pattern)


# ---------------------------------------------------------------------------
# Against the analyzer and the plan, for real
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_a_discovered_pattern_resolves_onto_the_columns_it_governs():
    analysis = client.post(
        "/api/patterns/analyze", json={"filters": {"grgr_ck": [1]}, "k": 3}
    )
    assert analysis.status_code == 200, analysis.text

    response = client.post("/api/patterns/to-plan", json={
        "pattern": analysis.json()["patterns"][0],
        "kind": "member",
        "as_of": "2026-06-30",
    })
    assert response.status_code == 200, response.text
    body = response.json()

    targets = {t["column"]: t for t in body["resolution"]["targets"]}
    # Age and tenure are not columns; they resolve onto the dates that are.
    assert targets["MEME_BIRTH_DT"]["generator"] == "date_relative"
    assert targets["MEME_ORIG_EFF_DT"]["generator"] == "date_relative"
    # The demographics the analyzer clustered on come back as weighted lists.
    assert targets["MEME_SEX"]["generator"] == "lookup"
    assert targets["MEME_SEX"]["params"]["weights"]
    # The member's own columns, or the subscriber above it where the family's
    # plan and group are held.
    assert all(t["table"] == MEMBER
               or (t["table"] == "dbo.CMC_SBSB_SUBSC" and t["relation"] == "ancestor")
               for t in body["resolution"]["targets"]), [
        (t["table"], t["column"], t["relation"]) for t in body["resolution"]["targets"]
        if t["table"] != MEMBER]
    # Nothing was written: resolving is a read.
    assert body["applied"] == []


@pytest.mark.db
def test_the_birth_date_window_produces_the_ages_the_pattern_describes():
    from datetime import date, timedelta

    analysis = client.post(
        "/api/patterns/analyze", json={"filters": {"grgr_ck": [8]}, "k": 3}
    ).json()
    raw = analysis["patterns"][0]
    body = client.post("/api/patterns/to-plan", json={
        "pattern": raw, "kind": "member", "as_of": "2026-06-30",
    }).json()

    birth = next(
        t for t in body["resolution"]["targets"] if t["column"] == "MEME_BIRTH_DT"
    )
    anchor = date.fromisoformat(birth["params"]["anchor"])
    measured = raw["continuous"]["_age"]
    for offset in (birth["params"]["min_days"], birth["params"]["max_days"]):
        born = anchor + timedelta(days=offset)
        age = anchor.year - born.year - (
            (anchor.month, anchor.day) < (born.month, born.day)
        )
        assert round(measured["min"]) - 1 <= age <= round(measured["max"]) + 1


# ---------------------------------------------------------------------------
# Claims patterns
# ---------------------------------------------------------------------------

def test_the_claims_vocabulary_reaches_the_same_elements():
    """The two analyzers name the same ideas differently: one measures over
    members, the other over claim lines."""
    pattern = to_pattern({
        "continuous": {
            "member_age": {"min": 24, "max": 53},
            "charge_amount": {"min": 5, "max": 354},
        },
        "categorical": {
            "claim_status": {"02": 0.7, "04": 0.3},
            "network": {"I": 0.86, "O": 0.14},
        },
    }, kind="claim")
    labels = {p.label for p in pattern.parameters}
    assert labels == {"Age", "Charge Amount", "Claim Status", "Network Indicator"}


def test_a_flat_mapping_of_value_to_number_is_read_as_a_distribution():
    """The claims analyzer gives shares directly rather than under a `pct` key."""
    pattern = to_pattern({"categorical": {"claim_status": {"02": 0.7, "04": 0.3}}})
    status = next(p for p in pattern.parameters if p.label == "Claim Status")
    assert status.values == ["02", "04"]
    assert status.weights == [0.7, 0.3]


def test_ranked_code_lists_cross_as_weighted_values():
    """top_diagnoses is a distribution in everything but name: a mapping of
    code to how often it appeared."""
    pattern = to_pattern({
        "top_diagnoses": {"E119000": 6418, "I10X000": 6041, "K029000": 5199},
        "top_procedures": {"L7224": 205, "R3213": 201},
    }, kind="claim")
    diagnoses = next(p for p in pattern.parameters if p.label == "Diagnosis Code")
    assert diagnoses.values[0] == "E119000", "the commonest code should lead"
    assert sum(diagnoses.weights) == pytest.approx(1.0)
    assert any(p.label == "Procedure Code" for p in pattern.parameters)


def test_a_claims_pattern_takes_its_support_from_the_share_of_lines():
    assert to_pattern({"pct_of_lines": 0.108}, kind="claim").support == pytest.approx(0.108)


@pytest.mark.db
def test_a_claims_pattern_binds_what_a_claim_can_carry_and_explains_the_rest():
    analysis = client.post("/api/claims/analyze", json={"grgr_ck": [3], "k": 3})
    assert analysis.status_code == 200, analysis.text

    response = client.post("/api/patterns/to-plan", json={
        "pattern": analysis.json()["patterns"][0],
        "kind": "claim",
        "as_of": "2026-06-30",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolution"]["subject"] == "dbo.CMC_CLCL_CLAIM"

    targets = {t["column"]: t for t in body["resolution"]["targets"]}
    assert targets["CLCL_CUR_STS"]["generator"] == "lookup"
    assert targets["CLCL_CL_TYPE"]["generator"] == "lookup"
    # A diagnosis and a procedure are drawn as the pairs the pattern's claims
    # carry, never apart: the claim's diagnosis from those combinations, the
    # line's procedure from the ones holding that diagnosis. The diagnosis is
    # still only ever a code the diagnosis table holds.
    diagnosis = targets["CLCL_IDCD_ID"]
    assert diagnosis["generator"] == "lookup"
    key = diagnosis["params"]["record_key"]
    assert key.startswith("pattern_combo:") and "DIAGNOSIS_CODE" in key
    assert diagnosis["params"]["within"] == "dbo.CMC_IDCD_DIAG_CODE"
    procedure = targets["CDML_SVC_CD"]
    assert procedure["params"]["record_key"] == key
    assert procedure["params"]["given"]["DIAGNOSIS_CODE"] == "dbo.CMC_CLCL_CLAIM.CLCL_IDCD_ID"
    # The charge was measured over lines, and that is where it lands.
    assert targets["CDML_CHG_AMT"]["generator"] == "number"

    # The members' ages and sex are said on the member, which is generated
    # before its claims, and the share is drawn over members, so a claim of
    # the pattern is made by a member the pattern describes.
    pattern_ages = analysis.json()["patterns"][0]["continuous"]["member_age"]
    birth = targets["MEME_BIRTH_DT"]
    assert birth["relation"] == "ancestor"
    assert birth["params"]["max_days"] <= -int(pattern_ages["min"]) * 365
    assert birth["params"]["min_days"] >= -(int(pattern_ages["max"]) + 2) * 366
    assert targets["MEME_SEX"]["relation"] == "ancestor"
    assert body["resolution"]["share_of"] == "dbo.CMC_MEME_MEMBER"

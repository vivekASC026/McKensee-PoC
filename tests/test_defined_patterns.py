"""Patterns a person defines by a rule on each feature they choose.

A defined pattern is a scope: group, plan category, plan and product as the
filters every analysis takes, everything else as conditions on the loaded
rows. Measured (k=1) it is the defined pattern itself; analyzed it splits into
the patterns inside it. Saved, it is a pattern like any found one -- listed,
marked as defined, and written to a file from its own members' values.

Every pattern saved here is deleted by the id the save returned, and only if
the save created it.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from sdg.analyzer.services import defined_patterns
from sdg.api.app import app
from tests.saved_cleanup import unrestore

client = TestClient(app)

WOMEN_30_45 = {"conditions": {"gender": {"in": ["F"]}, "age": {"min": 30, "max": 45}}}


# ---------------------------------------------------------------------------
# Rules, without a database
# ---------------------------------------------------------------------------

def test_a_rule_on_a_feature_the_pattern_has_not_got_is_refused():
    with pytest.raises(ValueError, match="not a feature"):
        defined_patterns.check("members", {"diagnosis": {"in": ["E119000"]}})


def test_a_range_whose_ends_are_crossed_is_refused():
    with pytest.raises(ValueError, match="low end"):
        defined_patterns.check("members", {"age": {"min": 50, "max": 20}})


def test_an_empty_rule_is_dropped_rather_than_matching_nothing():
    assert defined_patterns.check("members", {
        "gender": {"in": []}, "age": {"min": "", "max": None},
        "marital_status": {"in": [" M ", "M", "S"]},
    }) == {"marital_status": {"in": ["M", "S"]}}


def test_the_hierarchy_is_scope_and_not_a_condition():
    with pytest.raises(ValueError, match="scope"):
        defined_patterns.check("members", {"grgr_ck": {"in": ["1"]}})


def test_conditions_keep_only_the_rows_meeting_all_of_them():
    import pandas as pd

    frame = pd.DataFrame({"MEME_SEX": ["F", "M", "F", "F"], "_age": [31, 35, 50, 44.9]})
    keep = defined_patterns.mask(frame, "members", WOMEN_30_45["conditions"] | {
        "age": {"min": 30.0, "max": 45.0}, "gender": {"in": ["F"]}})
    assert keep.tolist() == [True, False, False, True]


# ---------------------------------------------------------------------------
# Against the pattern store
# ---------------------------------------------------------------------------

def _forget(pattern_ids: list[int]) -> None:
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as connection:
        cursor = connection.cursor()
        for pattern_id in pattern_ids:
            cursor.execute(f"DELETE FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,))
        connection.commit()


@pytest.mark.db
def test_every_feature_says_what_it_can_hold():
    members = {f["key"]: f for f in client.get("/api/patterns/features?entity=members")
               .json()["features"]}
    assert {"grgr_ck", "cspd_cat", "cspi_id", "pdpd_id", "age", "tenure", "gender",
            "marital_status"} <= set(members)
    assert "state" not in members, "the store holds no addresses"
    assert members["grgr_ck"]["scope"] and members["grgr_ck"]["options"]
    assert members["grgr_ck"]["options"][0]["label"], "a group is offered by its name"
    assert members["age"]["kind"] == "range"
    claims = {f["key"]: f for f in client.get("/api/patterns/features?entity=claims")
              .json()["features"]}
    assert {"claim_type", "diagnosis", "procedure", "network", "charge",
            "member_age"} <= set(claims)
    assert claims["charge"]["max"] > claims["charge"]["min"]
    assert client.get("/api/patterns/features?entity=plans").status_code == 422


@pytest.mark.db
def test_a_defined_member_pattern_is_measured_from_the_members_meeting_its_rules():
    measured = client.post("/api/patterns/analyze", json={"filters": WOMEN_30_45, "k": 1})
    assert measured.status_code == 200, measured.text
    body = measured.json()
    assert body["method"] == "whole_scope" and body["n_patterns"] == 1
    pattern = body["patterns"][0]
    assert pattern["size"] == body["subset_members"] > 0
    assert set(pattern["categorical"]["MEME_SEX"]["counts"]) == {"F"}
    ages = pattern["continuous"]["_age"]
    assert 30 <= ages["min"] and ages["max"] <= 45

    rows = client.post("/api/patterns/rows", json={
        "filters": WOMEN_30_45, "k": 1, "cluster_id": 0, "limit": 500,
    }).json()
    assert rows["total"] == pattern["size"]
    assert all(r["MEME_SEX"] == "F" and 30 <= r["_age"] <= 45 for r in rows["rows"])

    inside = client.post("/api/patterns/analyze", json={"filters": WOMEN_30_45, "k": None})
    assert inside.status_code == 200, inside.text
    found = inside.json()["patterns"]
    assert len(found) >= 2, "nothing was found inside the defined pattern"
    assert sum(p["size"] for p in found) == pattern["size"]


@pytest.mark.db
def test_a_defined_claims_pattern_is_measured_and_looked_inside():
    rules = {"conditions": {"claim_type": {"in": ["P"]}, "diagnosis": {"in": ["E119000"]},
                            "charge": {"min": 500}}}
    measured = client.post("/api/claims/analyze", json={**rules, "k": 1})
    assert measured.status_code == 200, measured.text
    body = measured.json()
    assert body["method"] == "whole_scope" and body["subset_claim_lines"] > 0

    rows = client.post("/api/claims/rows", json={**rules, "k": 1, "cluster_id": 0,
                                                  "limit": 500}).json()
    for r in rows["rows"]:
        assert r["CLCL_CL_TYPE"] == "P" and r["IDCD_ID_PRIMARY"] == "E119000"
        assert float(r["CDML_CHG_AMT"]) >= 500

    inside = client.post("/api/claims/analyze", json={**rules, "k": None}).json()
    assert sum(p["size"] for p in inside["patterns"]) == body["subset_claim_lines"]


@pytest.mark.db
def test_a_saved_defined_pattern_is_listed_as_defined_and_writes_from_its_rules():
    group = client.get("/api/patterns/features?entity=members").json()["features"][0]
    group_key = int(group["options"][0]["value"])
    scope = {"grgr_ck": [group_key], **WOMEN_30_45}
    name = "SDMT defined: women 30-45 in one group"
    made: list[int] = []
    saves: list[dict] = []
    try:
        kept = client.post("/api/patterns/keep", json={
            "domain": "members", "filters": scope, "k": 1, "cluster_id": 0,
            "name": name, "origin": "user_defined",
        })
        assert kept.status_code == 200, kept.text
        body = kept.json()
        saves.append(body)
        if body["created"]:
            made.append(body["pattern_id"])
        assert body["name"] == name and body["member_count"] > 0

        listed = {p["pattern_id"]: p for p in client.get("/api/discovery/saved").json()["patterns"]}
        assert listed[body["pattern_id"]]["origin"] == "user_defined"
        for_files = {p["pattern_id"]: p for p in
                     client.get("/api/patterns/for-files").json()["patterns"]}
        assert for_files[body["pattern_id"]]["origin"] == "user_defined"

        written = client.post("/api/export", json={
            "format": "csv", "subject": "members", "pattern_id": body["pattern_id"],
            "members": 40, "rows": 40,
        })
        assert written.status_code == 200, written.text
        rows = list(csv.DictReader(io.StringIO(written.text)))
        assert len(rows) == 40
        assert {r["SEX"] for r in rows} == {"F"}, "the gender rule did not reach the file"
    finally:
        _forget(made)
        unrestore(saves)


@pytest.mark.db
def test_a_defined_claims_pattern_of_several_claim_types_is_one_pattern_of_each():
    rules = {"conditions": {"charge": {"min": 500}}}
    measured = client.post("/api/claims/analyze", json={**rules, "k": 1})
    assert measured.status_code == 200, measured.text
    body = measured.json()
    assert body["method"] == "whole_scope"
    patterns = body["patterns"]
    assert len(patterns) >= 2, "the rules held one claim type; choose rules holding several"
    types = [set(p["categorical"]["claim_type"]) for p in patterns]
    assert all(len(t) == 1 for t in types), types
    assert len(set.union(*types)) == len(patterns), "two patterns of one claim type"
    assert sum(p["size"] for p in patterns) == body["subset_claim_lines"]
    assert sum(p["n_claims"] for p in patterns) == body["subset_claims"]

    made: list[int] = []
    saves: list[dict] = []
    try:
        for pattern, (claim_type,) in zip(patterns, types):
            kept = client.post("/api/patterns/keep", json={
                "domain": "claims", "filters": rules, "k": 1,
                "cluster_id": pattern["cluster_id"],
                "name": f"SDMT defined: charge over 500 · {claim_type}",
                "origin": "user_defined",
            })
            assert kept.status_code == 200, kept.text
            saves.append(kept.json())
            if kept.json()["created"]:
                made.append(kept.json()["pattern_id"])
            rows = client.get(f"/api/patterns/{kept.json()['pattern_id']}/rows?limit=500")
            assert rows.status_code == 200, rows.text
            assert {r["CLCL_CL_TYPE"].strip() for r in rows.json()["rows"]} == {claim_type}

            written = client.post("/api/export", json={
                "format": "csv", "subject": "claims",
                "pattern_id": kept.json()["pattern_id"], "rows": 40,
            })
            assert written.status_code == 200, written.text
            lines = list(csv.DictReader(io.StringIO(written.text)))
            # A claims file has no claim type, so the type is checked by its
            # codes: every pair written is one this type's claims carry.
            own = {(r["DIAGNOSIS_CODE"], r["PROCEDURE_CODE"])
                   for c in pattern["combinations"]
                   if {"DIAGNOSIS_CODE", "PROCEDURE_CODE"} <= set(c["elements"])
                   for r in c["records"]}
            pairs = {(r["DIAGNOSIS_CODE"].strip(), r["PROCEDURE_CODE"].strip()) for r in lines}
            assert lines and pairs <= own, (
                f"a file from the {claim_type} pattern wrote pairs its claims never carry: "
                f"{sorted(pairs - own)[:5]}")
    finally:
        _forget(made)
        unrestore(saves)

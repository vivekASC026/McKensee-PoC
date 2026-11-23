"""A diagnosis and a procedure, generated only as pairs real claims carry.

Raised in the demo: a format-correct 837 is no use if its lines pair a
diagnosis with a procedure that never occur together. They did -- the claim's
diagnosis and the line's procedure were drawn apart, and in a plain load the
procedure was invented from its format. Now a claims pattern keeps its real
combinations, a plan draws both from the pairs the claims history holds, a
line's procedure is drawn given its claim's diagnosis, and a run's validation
fails on any pair the history has never seen.
"""

from __future__ import annotations

import csv
import io

import pytest

from sdg.engine.execute import run_plan
from sdg.plan.model import Plan, PlanField, PlanTable, Ratio, new_meta

PAIRS = [
    {"DIAGNOSIS_CODE": "E119000", "PROCEDURE_CODE": "P1"},
    {"DIAGNOSIS_CODE": "E119000", "PROCEDURE_CODE": "P2"},
    {"DIAGNOSIS_CODE": "J069000", "PROCEDURE_CODE": "P3"},
    {"DIAGNOSIS_CODE": "I10X000", "PROCEDURE_CODE": "P4"},
    {"DIAGNOSIS_CODE": "I10X000", "PROCEDURE_CODE": "P5"},
]
REAL = {(p["DIAGNOSIS_CODE"], p["PROCEDURE_CODE"]) for p in PAIRS}


def _claims(linked: bool, within: str | None = None) -> Plan:
    """A claim with a diagnosis, and lines with a procedure, from the pairs."""
    procedure = {"values": PAIRS, "record_key": "pattern_clinical", "field": "PROCEDURE_CODE"}
    if linked:
        procedure["given"] = {"DIAGNOSIS_CODE": "dbo.CLAIM.DX"}
    diagnosis = {"values": PAIRS, "record_key": "pattern_clinical", "field": "DIAGNOSIS_CODE"}
    if within:
        diagnosis["within"] = within
    return Plan(
        meta=new_meta("pairs", "seed-one"),
        tables=[
            PlanTable(name="dbo.CLAIM", mode="generate", count=300, level=0, fields=[
                PlanField(name="ID", bucket="business_key", generator="sequence",
                          params={"start": 1}, provenance="structural"),
                PlanField(name="DX", bucket="code", generator="lookup",
                          params=diagnosis, provenance="learned"),
            ]),
            PlanTable(name="dbo.LINE", mode="generate", level=1, driving_parent="dbo.CLAIM",
                      ratio=Ratio(parent="dbo.CLAIM", kind="fixed", value=4), fields=[
                PlanField(name="CLAIM_ID", bucket="reference", generator="reference",
                          params={"parent_table": "dbo.CLAIM", "parent_column": "ID",
                                  "pick": "driving"}, provenance="structural"),
                PlanField(name="PROC", bucket="code", generator="lookup",
                          params=procedure, provenance="learned"),
            ]),
        ],
        generation_order=["dbo.CLAIM", "dbo.LINE"],
    )


def _pairs(result) -> list[tuple[str, str]]:
    claims = {r["ID"]: r for r in result.rows("dbo.CLAIM")}
    return [(claims[l["CLAIM_ID"]]["DX"], l["PROC"]) for l in result.rows("dbo.LINE")]


def test_a_lines_procedure_is_drawn_given_its_claims_diagnosis():
    linked = _pairs(run_plan(_claims(linked=True)))
    assert len(linked) == 1200
    assert all(pair in REAL for pair in linked), "a line paired codes no claim carries"

    # Drawn apart, as they were, the same pairs come out wrong: this is what
    # the test exists to catch.
    apart = _pairs(run_plan(_claims(linked=False)))
    assert any(pair not in REAL for pair in apart)


def test_a_diagnosis_is_only_ever_a_code_the_target_holds():
    from sdg.engine.execute import ReferenceSets

    sets = ReferenceSets()
    sets.add("dbo.DIAG", ["E119000", "I10X000"])
    result = run_plan(_claims(linked=True, within="dbo.DIAG"), reference_sets=sets)
    pairs = _pairs(result)
    assert {d for d, _ in pairs} <= {"E119000", "I10X000"}
    assert all(pair in REAL for pair in pairs)


@pytest.mark.db
def test_a_claim_file_from_a_pattern_carries_only_real_pairs():
    from fastapi.testclient import TestClient

    from sdg.api.app import app, service

    client = TestClient(app)
    pattern = client.post("/api/claims/analyze", json={"grgr_ck": [1], "k": 3}).json()["patterns"][0]
    group = next(g for g in pattern["combinations"] if "PROCEDURE_CODE" in g["elements"])
    assert group["records"], "the pattern kept no combinations"
    history = {(r["d"], r["p"]) for r in service.registry.query(
        "denorm", "SELECT DISTINCT LTRIM(RTRIM(IDCD_ID_PRIMARY)) AS d, "
                  "LTRIM(RTRIM(IPCD_ID)) AS p FROM sdm.claims_denorm")}

    def written(profile: dict) -> list[tuple[str, str]]:
        response = client.post("/api/export", json={
            "format": "csv", "subject": "claims", "rows": 300,
            "pattern": {"profile": profile, "domain": "claims", "filters": {"grgr_ck": [1]},
                        "k": 3, "cluster_id": profile["cluster_id"]},
        })
        assert response.status_code == 200, response.text
        return [(r["DIAGNOSIS_CODE"], r["PROCEDURE_CODE"])
                for r in csv.DictReader(io.StringIO(response.text))]

    lines = written(pattern)
    assert lines and all(pair in history for pair in lines)
    # And they are the pattern's own: every pair is one of its combinations.
    own = {(r["DIAGNOSIS_CODE"], r["PROCEDURE_CODE"]) for r in group["records"]}
    assert set(lines) <= own


@pytest.mark.db
def test_a_plain_plan_draws_the_claim_diagnosis_and_line_procedure_as_pairs():
    from sdg.api.app import service

    plan = service.plan if service.plan and not service.plan_stale() else service.build()[0]
    fields = {(t.name, f.name): f for t in plan.tables for f in t.fields}
    diagnosis = fields[("dbo.CMC_CLCL_CLAIM", "CLCL_IDCD_ID")]
    procedure = fields[("dbo.CMC_CDML_CL_LINE", "CDML_SVC_CD")]
    assert diagnosis.generator == procedure.generator == "lookup"
    key = diagnosis.params["record_key"]
    assert key.startswith("combo:") and procedure.params["record_key"] == key
    assert {"DIAGNOSIS_CODE", "PROCEDURE_CODE"} <= set(key[len("combo:"):].split("+"))
    assert diagnosis.params["within"] == "dbo.CMC_IDCD_DIAG_CODE"
    assert procedure.params["given"]["DIAGNOSIS_CODE"] == "dbo.CMC_CLCL_CLAIM.CLCL_IDCD_ID"

    result = run_plan(plan, limit=200, reference_sets=service.reference_sets_for_run())
    claims = {r["CLCL_ID"]: r for r in result.rows("dbo.CMC_CLCL_CLAIM")}
    history = {(r["d"], r["p"]) for r in service.registry.query(
        "denorm", "SELECT DISTINCT LTRIM(RTRIM(IDCD_ID_PRIMARY)) AS d, "
                  "LTRIM(RTRIM(IPCD_ID)) AS p FROM sdm.claims_denorm")}
    pairs = [(claims[l["CLCL_ID"]]["CLCL_IDCD_ID"], l["CDML_SVC_CD"])
             for l in result.rows("dbo.CMC_CDML_CL_LINE")]
    assert pairs and all(pair in history for pair in pairs)

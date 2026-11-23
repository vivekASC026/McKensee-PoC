"""A requirement analyzed, not only matched; a pattern split into its scenarios.

From the demo feedback. A requirement is read into the rules of a pattern and
analyzed like any user defined one -- measured against the book, and looked
inside. A pattern is split into the scenarios its own rows hold, and the ones
chosen are generated together: one load with each as a cohort, or one file
with each scenario's records tagged with it.

Patterns kept here are deleted by the ids the keep returned, only when it
created them.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app
from tests.saved_cleanup import unrestore

client = TestClient(app)
pytestmark = pytest.mark.db


def _filters(entity: str, rules: dict) -> dict:
    """The scope a reading defines, as the screen builds it."""
    scope = {"grgr_ck", "cspd_cat", "cspi_id", "pdpd_id"}
    out: dict = {}
    conditions = {}
    for key, rule in rules.items():
        if key in scope:
            out[key] = [int(v) for v in rule["in"]] if key == "grgr_ck" else rule["in"]
        else:
            conditions[key] = rule
    if conditions:
        out["conditions"] = conditions
    return out


def _forget(pattern_ids: list[int]) -> None:
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as connection:
        cursor = connection.cursor()
        for pattern_id in pattern_ids:
            cursor.execute(f"DELETE FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,))
        connection.commit()


# ---------------------------------------------------------------------------
# A requirement, read and analyzed
# ---------------------------------------------------------------------------

def test_a_requirement_is_read_into_the_rules_it_states():
    read = client.post("/api/story/read", json={
        "story": "Generate 500 professional claims for female members aged 40 to 60 "
                 "with type 2 diabetes on a PPO plan",
    }).json()
    assert read["entity"] == "claims" and read["count"] == 500
    rules = read["rules"]
    assert rules["claim_type"] == {"in": ["P"]}
    assert rules["gender"] == {"in": ["F"]}
    assert rules["member_age"] == {"min": 40, "max": 60}
    assert rules["diagnosis"] == {"in": ["E119000"]}
    assert rules["cspi_id"]["in"], "a PPO plan was not read"
    assert all(f["from"] for f in read["found"]), "a rule was read from no words"

    members = client.post("/api/story/read", json={
        "story": "1000 married seniors in Texas",
    }).json()
    assert members["entity"] == "members" and members["count"] == 1000
    assert members["rules"]["marital_status"] == {"in": ["M"]}
    # The store holds no addresses, so a place is not a rule.
    assert "state" not in members["rules"]
    assert members["rules"]["age"] == {"min": 65}
    # An age word is never read as part of a name.
    assert "grgr_ck" not in members["rules"]

    assert client.post("/api/story/read", json={"story": "  "}).status_code == 422


def test_a_requirement_is_analyzed_as_a_pattern_of_its_own():
    read = client.post("/api/story/read", json={
        "story": "claims for type 2 diabetes from female members over 40",
    }).json()
    filters = _filters(read["entity"], read["rules"])
    body = {**{k: v for k, v in filters.items() if k != "conditions"},
            "conditions": filters.get("conditions"), "k": 1}
    measured = client.post("/api/claims/analyze", json=body)
    assert measured.status_code == 200, measured.text
    whole = measured.json()
    assert whole["method"] == "whole_scope" and whole["subset_claim_lines"] > 0

    rows = client.post("/api/claims/rows", json={**body, "cluster_id": 0, "limit": 200}).json()
    for row in rows["rows"]:
        assert row["IDCD_ID_PRIMARY"] == "E119000" and row["MEME_SEX"] == "F"
        assert float(row["CLCL_ME_AGE"]) >= 40

    inside = client.post("/api/claims/analyze", json={**body, "k": None}).json()
    assert len(inside["patterns"]) >= 2, "nothing was found inside the requirement"


# ---------------------------------------------------------------------------
# Scenarios of a pattern, generated together
# ---------------------------------------------------------------------------

def _first_member_pattern() -> dict:
    analysis = client.post("/api/patterns/analyze",
                           json={"filters": {"grgr_ck": [1]}, "k": 3}).json()
    return analysis["patterns"][0]


def test_a_pattern_lists_the_scenarios_its_rows_hold():
    dims = client.get("/api/patterns/scenario-dimensions?entity=members").json()["dimensions"]
    assert {"gender", "age_band", "plan"} <= {d["key"] for d in dims}

    pattern = _first_member_pattern()
    listed = client.post("/api/patterns/scenarios", json={
        "domain": "members", "filters": {"grgr_ck": [1]}, "k": 3,
        "cluster_id": pattern["cluster_id"], "dimensions": ["gender", "age_band"],
    })
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["rows"] == pattern["size"]
    assert sum(s["rows"] for s in body["scenarios"]) == body["rows"]
    assert {tuple(s["values"]) for s in body["scenarios"]} == {("gender", "age_band")}

    refused = client.post("/api/patterns/scenarios", json={
        "domain": "members", "filters": {"grgr_ck": [1]}, "k": 3,
        "cluster_id": pattern["cluster_id"], "dimensions": [],
    })
    assert refused.status_code == 422


def test_chosen_scenarios_are_kept_and_written_into_one_file():
    pattern = _first_member_pattern()
    address = {"domain": "members", "filters": {"grgr_ck": [1]}, "k": 3,
               "cluster_id": pattern["cluster_id"], "dimensions": ["gender"]}
    listed = client.post("/api/patterns/scenarios", json=address).json()["scenarios"]
    assert {s["values"]["gender"] for s in listed} == {"F", "M"}

    made: list[int] = []
    restored: list[dict] = []
    try:
        kept = client.post("/api/patterns/scenarios/keep", json={
            **address, "name": "SDMT scenario test",
            "scenarios": [s["values"] for s in listed],
        })
        assert kept.status_code == 200, kept.text
        kept = kept.json()["kept"]
        made = [k["pattern_id"] for k in kept if k["created"]]
        restored = kept
        assert len(kept) == 2
        by_gender = {k["scenario"]["gender"]: k for k in kept}
        rows = {s["values"]["gender"]: s["rows"] for s in listed}
        for gender, entry in by_gender.items():
            assert entry["member_count"] == rows[gender]
            assert entry["name"].startswith("SDMT scenario test · Gender")

        written = client.post("/api/export", json={
            "format": "csv", "subject": "members",
            "scenarios": [{"pattern_id": by_gender[g]["pattern_id"], "label": g, "count": 25}
                          for g in ("F", "M")],
        })
        assert written.status_code == 200, written.text
        out = list(csv.DictReader(io.StringIO(written.text)))
        assert len(out) == 50
        assert {r["SCENARIO"] for r in out} == {"F", "M"}
        for row in out:
            assert row["SEX"] == row["SCENARIO"], "a scenario's records carry another's values"
        assert len({r["MEMBER_ID"] for r in out}) == 50, "two scenarios made the same person"
        families = [{r["SUBSCRIBER_ID"] for r in out if r["SCENARIO"] == g} for g in ("F", "M")]
        assert not families[0] & families[1], "two scenarios made the same family"

        # Into the database: one load, each scenario a cohort at its share.
        planned = client.post("/api/story/to-plan", json={
            "pattern_ids": [k["pattern_id"] for k in kept], "total_volume": 1000,
            "as_of": "2026-06-30",
        })
        assert planned.status_code == 200, planned.text
        assert sum(planned.json()["allocation"].values()) == 1000
    finally:
        _forget(made)
        unrestore(restored)


def test_scenarios_loaded_together_each_keep_their_own_plan(monkeypatch):
    """Split by plan, two scenarios into one database load: each segment's
    families are on that scenario's plan, not on every plan of the pattern.

    Two things used to lose it. A member's plan is held on the subscriber, above
    the member, where a member segment could not reach; and with several
    segments in one load a column kept only the last segment's rule."""
    import json

    from sdg.analyzer import db
    from sdg.analyzer.bridge import to_pattern
    from sdg.api.app import service
    from sdg.engine.execute import run_plan
    from sdg.plan.model import Plan
    from sdg.plan.patterns import apply_to_decisions, resolve_pattern

    analysis = client.post("/api/patterns/analyze",
                           json={"filters": {"grgr_ck": [1]}, "k": 3}).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    address = {"domain": "members", "filters": {"grgr_ck": [1]}, "k": 3,
               "cluster_id": pattern["cluster_id"], "claim_type": None}
    listed = client.post("/api/patterns/scenarios",
                         json={**address, "dimensions": ["plan"]}).json()["scenarios"][:2]
    kept = client.post("/api/patterns/scenarios/keep", json={
        **address, "dimensions": ["plan"], "name": "plan probe",
        "scenarios": [s["values"] for s in listed]}).json()["kept"]
    try:
        resolutions, wanted = [], {}
        for part in kept:
            profile = json.loads(db.get_pattern(part["pattern_id"])["profile_json"])
            profile["pct_of_total"] = 0.3
            name = f"probe-{part['pattern_id']}"
            resolution = resolve_pattern(
                to_pattern(profile, kind="member", as_of="2026-06-30", pattern_id=name),
                catalog=service.catalog, mappings=service.require_mappings(),
                plan=service.require_plan(), model=service.model)
            assert resolution.share_of == "dbo.CMC_SBSB_SUBSC", resolution.rejections
            resolutions.append(resolution)
            wanted[name] = part["scenario"]["plan"]

        document, _ = apply_to_decisions(resolutions, service.read_decisions(), as_cohort=True)
        monkeypatch.setattr(service, "read_decisions", lambda: document)
        monkeypatch.setattr(Plan, "save", lambda self, path: None)
        plan, _graph, _info = service.build()
        result = run_plan(plan, limit=2000, reference_sets=service.reference_sets_for_run())

        bits = {c.name: 1 << i for i, c in enumerate(plan.cohorts)}
        subscribers = result.tables["dbo.CMC_SBSB_SUBSC"]
        assert not any(tag & (tag - 1) for tag in subscribers.tags),             "a family is in one scenario at most"
        for name, plan_id in wanted.items():
            inside = [row for row, tag in zip(subscribers.rows, subscribers.tags)
                      if tag & bits[name]]
            assert inside, f"{name} got no families"
            assert {row["CSPI_ID"] for row in inside} == {plan_id}, name
    finally:
        _forget([p["pattern_id"] for p in kept if p.get("created")])
        unrestore(kept)

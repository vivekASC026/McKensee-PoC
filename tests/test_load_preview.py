"""Load to database's preview never builds a plan.

Opening the dialog asks /api/patterns/to-plan with apply false: it reads the
columns from the plan there is -- even one out of date -- and with no plan
says they are worked out when the pattern is loaded. Only Load (apply true)
builds a missing or out-of-date plan. Nothing here writes decisions: the
applying step is replaced by one that records it was asked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app, service

client = TestClient(app)

PATTERN = {"cluster_id": 0, "categorical": {"_sex": {"pct": {"F": 0.6, "M": 0.4}}}}

pytestmark = pytest.mark.db


@pytest.fixture
def builds(monkeypatch):
    """Every plan build asked for, none made: the plan on disk stays as it is."""
    asked: list[int] = []
    plan = service.plan
    assert plan is not None, "the suite's working copy has a plan"

    def build():
        asked.append(1)
        return plan, None, {}

    monkeypatch.setattr(service, "build", build)
    return asked


def _created() -> str | None:
    payload = client.get("/api/plan").json()
    return payload.get("meta", {}).get("created") if payload.get("built") else None


def _ask(apply: bool):
    return client.post("/api/patterns/to-plan", json={
        "pattern": PATTERN, "kind": "member", "as_of": "2026-06-30", "apply": apply})


def test_a_preview_on_a_stale_plan_reads_it_and_builds_nothing(builds, monkeypatch):
    monkeypatch.setattr(type(service), "plan_stale", lambda self: True)
    before = _created()
    preview = _ask(False)
    assert preview.status_code == 200, preview.text
    assert preview.json()["resolution"] is not None, "the plan there is was not read"
    assert preview.json()["applied"] == []
    assert builds == [], "a preview built the plan"
    assert _created() == before


def test_a_preview_with_no_plan_builds_none_and_says_so(builds, monkeypatch):
    monkeypatch.setattr(type(service), "plan", property(lambda self: None))
    preview = _ask(False)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["resolution"] is None and body["plan"] == "none" and body["applied"] == []
    assert builds == [], "a preview built the plan"


def test_load_builds_a_stale_or_missing_plan(builds, monkeypatch):
    import sdg.plan.patterns as patterns

    applied: list[int] = []
    monkeypatch.setattr(patterns, "apply_to_decisions",
                        lambda resolutions, document, as_cohort=False: (document, ["X"]))
    monkeypatch.setattr(service, "write_decisions", lambda document: applied.append(1))

    with monkeypatch.context() as stale:
        stale.setattr(type(service), "plan_stale", lambda self: True)
        assert _ask(True).status_code == 200
    assert builds == [1], "Load did not rebuild an out-of-date plan"

    with monkeypatch.context() as missing:
        missing.setattr(type(service), "plan", property(lambda self: None))
        assert _ask(True).status_code == 200
    assert builds == [1, 1], "Load did not build a missing plan"


def test_the_chosen_records_profile_is_measured_without_saving():
    """What a load of chosen records sets comes from their own values, as
    saving them would store -- read with nothing saved."""
    from sdg.analyzer import db

    filters = {"grgr_ck": [1]}
    found = client.post("/api/patterns/analyze", json={"filters": filters, "k": 3}).json()
    pattern = max(found["patterns"], key=lambda p: p["size"])
    rows = client.post("/api/patterns/rows", json={
        "filters": filters, "k": 3, "cluster_id": pattern["cluster_id"], "limit": 5}).json()
    chosen = rows["keys"][:5]
    asked = {"domain": "members", "filters": filters, "k": 3,
             "cluster_id": pattern["cluster_id"], "record_keys": chosen}
    saved_before = {p["id"] for p in db.get_saved_patterns()}

    measured = client.post("/api/patterns/profile", json=asked)
    assert measured.status_code == 200, measured.text
    profile = measured.json()["profile"]
    assert {p["id"] for p in db.get_saved_patterns()} == saved_before, "the preview saved"
    assert profile["size"] == len(chosen), profile.get("size")
    assert profile != pattern, "the whole pattern's profile, not the chosen records'"
    preview = client.post("/api/patterns/to-plan", json={
        "pattern": profile, "kind": "member", "as_of": "2026-06-30", "apply": False})
    assert preview.status_code == 200, preview.text
    assert preview.json()["applied"] == []

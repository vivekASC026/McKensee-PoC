"""From discovery to a database load without going back for the plan.

Two things made the way to a load a round trip. A pattern could not be sent
to the database until a plan existed, so the person went on to build one and
came back. And every rule changed after a plan was built left that plan on
disk saying what it said before, with nothing to tell the screen it was out of
date. These cover both: the pattern route builds the plan it needs, and the
plan says when it is stale so the screen rebuilds it on arrival.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app
from sdg.api.service import ARTIFACTS, Service

client = TestClient(app)


def test_a_plan_is_stale_once_anything_it_was_built_from_changes(tmp_path: Path):
    service = Service(work_dir=tmp_path)
    assert service.plan_stale() is False, "no plan is not a stale plan"

    from sdg.plan.build import BUILDER_REVISION
    from sdg.plan.model import Plan, new_meta

    plan = tmp_path / ARTIFACTS["plan"]
    decisions = tmp_path / ARTIFACTS["decisions"]
    decisions.write_text("{}")
    Plan(meta=new_meta("t", "s", builder=BUILDER_REVISION - 1), tables=[],
         generation_order=[]).save(plan)
    assert service.plan_stale() is True, "a plan from an earlier builder was not rebuilt"
    Plan(meta=new_meta("t", "s", builder=BUILDER_REVISION), tables=[],
         generation_order=[]).save(plan)
    # Two saves within one clock tick share a timestamp, and the service's
    # cache is keyed on it; the second is a later file, so say so.
    now = time.time() + 1
    os.utime(plan, (now, now))
    earlier = time.time() - 60
    os.utime(decisions, (earlier, earlier))
    assert service.plan_stale() is False

    later = time.time() + 60
    os.utime(decisions, (later, later))
    assert service.plan_stale() is True, "a decision made after the plan went unnoticed"


@pytest.mark.db
def test_a_pattern_goes_to_the_database_with_no_plan_built_first():
    """Loading a pattern builds the plan it needs rather than refusing. (A
    preview builds nothing: tests/test_load_preview.py.)"""
    from sdg.api.app import service

    plan = service.path("plan")
    aside = plan.with_suffix(".aside")
    decisions = service.path("decisions")
    kept = decisions.read_bytes() if decisions.exists() else None
    stamp = decisions.stat() if decisions.exists() else None

    def put_back() -> None:
        # The applied pattern is this test's alone: later loads count on a
        # plan without it. Its time goes back too, so the plan is judged
        # against the decisions it was built from.
        if kept is None:
            decisions.unlink(missing_ok=True)
        else:
            decisions.write_bytes(kept)
            os.utime(decisions, (stamp.st_atime, stamp.st_mtime))

    # The plan there was goes aside with its time, and comes back in place
    # of the one this test builds.
    plan_stamp = plan.stat() if plan.exists() else None
    if plan.exists():
        shutil.move(plan, aside)
    try:
        analysis = client.post("/api/patterns/analyze", json={"filters": {}, "k": 1}).json()
        pattern = analysis["patterns"][0]
        resolved = client.post("/api/patterns/to-plan", json={
            "pattern": pattern, "kind": "member", "apply": True,
        })
        assert resolved.status_code == 200, resolved.text
        assert plan.exists(), "no plan was built for the pattern to reach"
        loaded = resolved.json()
        assert loaded["resolution"] is not None, "the pattern was not resolved onto the plan"
        assert loaded["applied"], "Load applied nothing of the pattern"

        # The plan was built first and the pattern then written to the
        # decisions, so the plan says it is out of date: the screen rebuilds
        # it before any row goes in.
        assert decisions.read_bytes() != kept, "Load wrote nothing to the decisions"
        assert decisions.stat().st_mtime > plan.stat().st_mtime, (
            "the decisions were not written after the plan was built")
        current = client.get("/api/plan").json()
        assert current["built"] and current["stale"] is True
    finally:
        put_back()
        plan.unlink(missing_ok=True)
        if aside.exists():
            shutil.move(aside, plan)
            os.utime(plan, (plan_stamp.st_atime, plan_stamp.st_mtime))


@pytest.mark.db
def test_the_plan_says_it_is_stale_after_a_rule_changes():
    from sdg.api.app import service

    client.post("/api/plan")
    assert client.get("/api/plan").json()["stale"] is False

    settings = service.path("settings")
    before = settings.stat() if settings.exists() else None
    try:
        settings.touch()
        later = time.time() + 5
        os.utime(settings, (later, later))
        assert client.get("/api/plan").json()["stale"] is True
    finally:
        if before is not None:
            os.utime(settings, (before.st_atime, before.st_mtime))

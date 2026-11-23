"""Today's files, byte for byte, kept before SDM becomes domain neutral.

Every file format is written here through /api/export, as the screens write
it, from inputs kept in tests/fixtures/pack-baselines/: the rules and the
saved patterns, read once from the database and kept. The database itself is
shut off while these run, so what is compared is the code alone. Moving
health insurance into a business pack must leave every file the same.

To record (only when the files are meant to change, or a case is added):
    set SDM_RECORD_BASELINES=1 && pytest tests/test_pack_baselines.py
Recording reads the rules and patterns live and keeps them beside the files.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sdg.analyzer.services import generation_requests as gr
from sdg.api import app as app_module
from sdg.api.app import app

client = TestClient(app)

ROOT = Path(__file__).parents[1]
KEPT = Path(__file__).parent / "fixtures" / "pack-baselines"
INPUTS = KEPT / "inputs.json"
RECORDING = os.getenv("SDM_RECORD_BASELINES") == "1"

# Stand-in ids for the two patterns kept: a request's seed names its pattern,
# so a fixed id keeps the seed fixed.
MEMBERS_PATTERN, CLAIMS_PATTERN = 990001, 990002
FIXED_LAYOUT, DELIMITED_LAYOUT = 990003, 990004
# A run of members made earlier: the 834 of MEMBERS, as its first file.
EARLIER_RUN = 990010
STARTED = "2026-10-01T09:30:00.000Z"
SEED = "baseline-seed"

# What is read from the database, and kept: everything else (the plan, the
# store's description, the standard samples) is read from files as it is.
FROM_DATABASE = ("rules", "rule names", "pattern:", "layout:", "config:")
WORK = KEPT / "work"

MEMBERS = {"subject": "members", "pattern_id": MEMBERS_PATTERN, "members": 6,
           "request_started": STARTED, "record": False}
CLAIMS = {"subject": "claims", "pattern_id": CLAIMS_PATTERN, "members": 4, "rows": 40,
          "claims_min": 1, "claims_max": 2, "request_started": STARTED, "record": False}

CASES: dict[str, dict[str, Any]] = {
    "members.csv": {**MEMBERS, "format": "csv"},
    "members.834": {**MEMBERS, "format": "edi:834"},
    "members.fhir": {**MEMBERS, "format": "standard:fhir-members"},
    "members.hl7-adt": {**MEMBERS, "format": "standard:hl7-adt"},
    "members.hl7v3": {**MEMBERS, "format": "standard:hl7v3-prpa-add"},
    "claims.csv": {**CLAIMS, "format": "csv"},
    "claims.837P": {**CLAIMS, "format": "edi:837P"},
    "claims.835": {**CLAIMS, "format": "edi:835"},
    "claims.fhir": {**CLAIMS, "format": "standard:fhir-claims"},
    "claims.hl7-dft": {**CLAIMS, "format": "standard:hl7-dft"},
    "claims.share": {**CLAIMS, "format": "edi:837P", "claimed_share": 50, "members": 10, "rows": 60},
    "claims.mix": {**CLAIMS, "format": "csv", "claims_mix": [[1, 50], [3, 50]],
                   "lines_mix": [[1, 70], [2, 30]], "members": 8, "rows": 60},
    "members.834.split": {**MEMBERS, "format": "edi:834", "per_transaction": 2},
    "members.cda": {**MEMBERS, "format": "standard:cda-ccd", "members": 3, "rows": 3},
    "members.x12-834": {**MEMBERS, "format": "standard:x12-834"},
    "claims.x12-837": {**CLAIMS, "format": "standard:x12-837"},
    "members.fixed": {**MEMBERS, "format": "fixed", "layout_id": FIXED_LAYOUT},
    "members.delimited": {**MEMBERS, "format": "csv", "layout_id": DELIMITED_LAYOUT},
    "members.scenarios": {**MEMBERS, "format": "csv", "pattern_id": None, "scenarios": [
        {"pattern_id": MEMBERS_PATTERN, "label": "First", "count": 3},
        {"pattern_id": MEMBERS_PATTERN, "label": "Second", "count": 2}]},
    "claims.for-run": {**CLAIMS, "format": "edi:837P", "of_request": EARLIER_RUN,
                       "request_started": "2026-10-25T10:00:00.000Z"},
}

# Layouts as a person defines them: elements by name, one bound to a rule.
LAYOUT_FIELDS = [
    {"name": "MEMBER_ID", "length": 12}, {"name": "LAST_NAME", "length": 16},
    {"name": "FIRST_NAME", "length": 12}, {"name": "DOB", "length": 10},
    {"name": "SEX", "length": 1}, {"name": "FILLER", "length": 3, "blank": True},
    {"name": "ADDRESS_ZIP", "length": 5},
]
LAYOUTS = {
    f"layout:{FIXED_LAYOUT}": {"id": FIXED_LAYOUT, "name": "Baseline fixed", "kind": "fixed",
                                "status": "approved", "fields": LAYOUT_FIELDS},
    f"layout:{DELIMITED_LAYOUT}": {"id": DELIMITED_LAYOUT, "name": "Baseline delimited",
                                    "kind": "delimited", "delimiter": "|",
                                    "status": "approved", "fields": LAYOUT_FIELDS},
}


def _kept_inputs() -> dict[str, Any]:
    return json.loads(INPUTS.read_text(encoding="utf-8")) if INPUTS.exists() else {}


def _patterns_live() -> tuple[dict[str, Any], dict[str, int]]:
    """The first stored members pattern and claims pattern, under the stand-in ids."""
    from sdg.analyzer import db

    stored = [p for p in db.get_all_patterns() if p.get("profile_json")]
    members = next(p for p in stored if '"claims_pattern": true' not in p["profile_json"])
    claims = next(p for p in stored if '"claims_pattern": true' in p["profile_json"])
    return ({f"pattern:{MEMBERS_PATTERN}": db.get_pattern(members["id"]),
             f"pattern:{CLAIMS_PATTERN}": db.get_pattern(claims["id"])},
            {"members": members["id"], "claims": claims["id"]})


@pytest.fixture
def kept(monkeypatch, tmp_path):
    """The database shut off, and what the export reads from it served kept."""
    inputs = _kept_inputs()
    live_ids: dict[str, int] = {}
    reached: list[str] = []
    if RECORDING:
        live_patterns, live_ids = _patterns_live()
    real_source = app_module._source

    def source(kind: str, live):
        if not kind.startswith(FROM_DATABASE):
            return real_source(kind, live)
        if kind in LAYOUTS:
            return gr.loads(json.dumps(LAYOUTS[kind]))
        if RECORDING and kind not in inputs:
            value = live_patterns[kind] if kind.startswith("pattern:") else live()
            inputs[kind] = json.loads(gr.dumps(value))
        if kind not in inputs:
            raise AssertionError(f"{kind} is read from the database but not kept; "
                                 "record with SDM_RECORD_BASELINES=1")
        return gr.loads(json.dumps(inputs[kind]))

    monkeypatch.setattr(app_module, "_source", source)

    # The work folder -- the store's description, the plan, the settings --
    # as it was when the baselines were recorded, not as the machine has it.
    import shutil

    from sdg.api.service import Cached

    work = tmp_path / "work"
    shutil.copytree(WORK, work)
    service = app_module.service
    monkeypatch.setattr(service, "work", work)
    monkeypatch.setattr(service, "_cache", Cached())
    for cached in ("_export_run", "_coverage_plans", "_combinations"):
        monkeypatch.setattr(service, cached, None, raising=False)
    monkeypatch.setattr(service, "_code_sets_key", ("unset",), raising=False)

    # An earlier run of members, as the generation requests table holds it.
    started = gr.checked_started(STARTED)
    earlier = {"id": EARLIER_RUN, "seed": gr.seed_for(SEED, str(MEMBERS_PATTERN), started),
               "subject": "members", "pattern_key": str(MEMBERS_PATTERN),
               "pattern_id": MEMBERS_PATTERN, "parent_id": None}
    first = {"subject": "members", "params": {**MEMBERS, "format": "edi:834"},
             "snapshots": {"rules": "kept:rules", "algorithms": "kept:algorithms",
                           f"pattern:{MEMBERS_PATTERN}": f"kept:pattern:{MEMBERS_PATTERN}"}}
    real_get, real_first, real_snapshot = gr.get, gr.first_file, gr.snapshot

    def snapshot(key):
        if str(key).startswith("kept:"):
            kind = str(key)[len("kept:"):]
            if kind == "algorithms":
                from sdg.engine.generators import algorithm_versions

                return kept_read("algorithms", algorithm_versions)
            return gr.loads(json.dumps(inputs[kind]))
        return real_snapshot(key)

    monkeypatch.setattr(gr, "get", lambda i: dict(earlier) if i == EARLIER_RUN else real_get(i))
    monkeypatch.setattr(gr, "first_file",
                        lambda i: json.loads(json.dumps(first)) if i == EARLIER_RUN
                        else real_first(i))
    monkeypatch.setattr(gr, "snapshot", snapshot)

    # The store's members and a pattern's rows are read live even writing a
    # file again (the store is what they are of): kept the same way here.
    from sdg.analyzer.services import pattern_data
    from sdg.writers import elements

    def kept_read(kind: str, live):
        if RECORDING and kind not in inputs:
            inputs[kind] = json.loads(gr.dumps(live()))
        if kind not in inputs:
            raise AssertionError(f"{kind} is not kept; record with SDM_RECORD_BASELINES=1")
        return gr.loads(json.dumps(inputs[kind]))

    real_rows, real_members = pattern_data.rows_for, elements.store_members

    def rows_for(pattern_id):
        import pandas as pd

        real_id = {MEMBERS_PATTERN: "members", CLAIMS_PATTERN: "claims"}[pattern_id]
        keys = kept_read(f"pattern rows:{pattern_id}", lambda: [
            k for k in real_rows(live_ids[real_id])["df"]["MEME_CK"].tolist()])
        return {"df": pd.DataFrame({"MEME_CK": keys})}

    def store_members(keys, shape, registry):
        listed = [k for k in keys]
        return kept_read(f"store members:{gr.digest(listed)[:16]}",
                         lambda: real_members(listed, shape, registry))

    # The lists names, streets and addresses are drawn from: read afresh, and kept.
    from sdg.analyzer import generation_rules as lists

    real_lookup = lists._load_lookup

    def load_lookup(name):
        import pandas as pd

        rows = kept_read(f"list:{name}", lambda: real_lookup(name).to_dict("records"))
        return pd.DataFrame(rows)

    # The store's combinations (a diagnosis with its procedure, a provider
    # in the member's network), measured live and kept.
    from dataclasses import asdict

    from sdg.engine import rules as engine_rules
    from sdg.plan.combinations import Group

    real_combinations = app_module.service.combinations

    measured: list[Group] = []

    def combinations(refresh: bool = False):
        # Made once and handed out again, as the service hands out the groups
        # it measured: a cache keyed by a group's id relies on that.
        if not measured:
            kept_groups = kept_read("combinations", lambda: [
                asdict(g) for g in real_combinations(refresh=True)])
            measured.extend(
                Group(store=g["store"], elements=list(g["elements"]),
                      columns=list(g["columns"]),
                      combinations=[(tuple(v), n) for v, n in g["combinations"]],
                      evidence=[tuple(e) for e in g["evidence"]]) for g in kept_groups)
        return measured

    monkeypatch.setattr(app_module.service, "combinations", combinations)

    # A standard file's config is proposed from the rules, read directly.
    from sdg.analyzer.services import layout_service

    real_by_element, real_get_rules = layout_service.rules_by_element, layout_service.get_rules
    monkeypatch.setattr(layout_service, "rules_by_element",
                        lambda *a, **k: kept_read("rules", lambda: real_by_element(*a, **k)))
    monkeypatch.setattr(layout_service, "get_rules",
                        lambda *a, **k: kept_read("rule list", lambda: real_get_rules(*a, **k)))
    monkeypatch.setattr(engine_rules, "_COMBINATIONS", {})
    monkeypatch.setattr(lists, "_lookup_cache", {})
    monkeypatch.setattr(lists, "_addresses", None)
    monkeypatch.setattr(lists, "_load_lookup", load_lookup)
    monkeypatch.setattr(pattern_data, "rows_for", rows_for)
    monkeypatch.setattr(elements, "store_members", store_members)
    monkeypatch.setattr(app_module, "_base_seed", lambda: SEED)
    monkeypatch.delenv("SDG_X12_USAGE", raising=False)
    if not RECORDING:
        import pyodbc

        def refused(*args, **kwargs):
            # Noted as well as raised: some reads fall back quietly on an error.
            import traceback

            reached.append("".join(traceback.format_stack(limit=8)))
            raise AssertionError("a baseline reached the database")

        monkeypatch.setattr(pyodbc, "connect", refused)
    yield inputs
    assert not reached, "a baseline reached the database:\n" + reached[0]
    if RECORDING:
        KEPT.mkdir(parents=True, exist_ok=True)
        INPUTS.write_text(json.dumps(inputs, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _unzipped(written: bytes) -> bytes:
    """A zip as its entries, names and contents: a zip entry carries the time
    it was packed, which is not what the file says."""
    import io
    import zipfile

    if not written.startswith(b"PK\x03\x04"):
        return written
    with zipfile.ZipFile(io.BytesIO(written)) as packed:
        return b"".join(b"=== " + n.encode("utf-8") + b" ===\n" + packed.read(n) + b"\n"
                        for n in packed.namelist())


def _compare(name: str, written: bytes) -> None:
    written = _unzipped(written)
    path = KEPT / f"{name}.txt"
    if RECORDING:
        KEPT.mkdir(parents=True, exist_ok=True)
        path.write_bytes(written)
        return
    assert path.exists(), f"no baseline for {name}; record with SDM_RECORD_BASELINES=1"
    expected = path.read_bytes()
    if written != expected:
        diff = "\n".join(list(difflib.unified_diff(
            expected.decode("utf-8", "replace").splitlines(),
            written.decode("utf-8", "replace").splitlines(),
            "kept", "written", lineterm=""))[:40])
        pytest.fail(f"{name} differs from its baseline "
                    f"({hashlib.sha256(written).hexdigest()[:12]} vs "
                    f"{hashlib.sha256(expected).hexdigest()[:12]}):\n{diff}")


@pytest.mark.parametrize("name", list(CASES))
def test_a_file_is_written_as_before(kept, name):
    response = client.post("/api/export", json=CASES[name])
    assert response.status_code == 200, response.text
    assert response.content, name
    _compare(name, response.content)


def test_files_from_a_plan_that_generates_providers_are_refused(monkeypatch):
    """A plan may hold Generate for providers; files from it are refused, in
    words (user decision 2026-11-10, option 1)."""
    from sdg.plan.model import Plan

    plan = Plan.load(ROOT / "examples" / "plan-sample.json")
    monkeypatch.setattr(app_module.service, "require_plan", lambda: plan)
    response = client.post("/api/export", json={"format": "csv", "subject": "members",
                                                "root_limit": 12})
    assert response.status_code == 409, response.text
    assert "never generated" in response.json()["detail"]


def test_a_file_from_the_plan_is_written_as_before(kept, monkeypatch):
    """The plan's own run, no pattern: the engine, the elements a file names,
    the delimited and fixed-width writers -- through /api/export and called
    directly. The plan is examples/plan-sample.json, every table generated."""
    from sdg.engine.execute import run_plan
    from sdg.plan.model import Plan
    from sdg.writers import elements, export

    plan = Plan.load(ROOT / "examples" / "plan-sample.json")
    monkeypatch.setattr(app_module.service, "require_plan", lambda: plan)
    # The example plan generates providers so every table is exercised; a plan
    # the pipeline builds is refused that (tests/test_ui_api.py,
    # test_providers_are_never_generated), which is not what this compares.
    import sdg.engine.execute as execute
    monkeypatch.setattr(execute, "refuse_never_generated", lambda plan: None)
    response = client.post("/api/export", json={"format": "csv", "subject": "members",
                                                "root_limit": 12})
    assert response.status_code == 200, response.text
    _compare("plan.export.csv", response.content)

    result = run_plan(plan, limit=12)
    records = list(elements.run_members(result, plan))
    assert records
    fields = sorted({k for r in records for k in r})
    delimited = "".join(export.stream_delimited(iter(records), fields))
    fixed = "".join(export.stream_fixed_width(
        iter(records), [{"name": f, "length": 14} for f in fields]))
    _compare("plan.members.csv", delimited.encode("utf-8"))
    _compare("plan.members.fixed", fixed.encode("utf-8"))


@pytest.mark.parametrize("route", ["/api/patterns/scenario-dimensions", "/api/samples/defaults"])
def test_what_the_screens_are_told_is_as_before(kept, route):
    """Lists the screens are given that health shapes today."""
    response = client.get(route)
    assert response.status_code == 200, response.text
    name = "api" + route.replace("/api", "").replace("/", ".")
    _compare(name, json.dumps(response.json(), indent=1, sort_keys=True).encode("utf-8"))

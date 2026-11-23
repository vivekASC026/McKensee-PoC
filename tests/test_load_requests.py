"""A database load kept as a generation request, for audit.

Nothing loaded is kept: the record is the plan as it was, where its keys
started, the algorithm versions and fingerprints -- of each table's rows, and
of the keys it pointed at, never the keys themselves. Regenerating makes the
rows again, as CSV, without writing to any database. Rows a test opens are
removed by their ids.
"""

from __future__ import annotations

import io
import json
import zipfile

from fastapi.testclient import TestClient

from sdg.analyzer.services import generation_requests as gr
from sdg.api import load_requests
from sdg.api.app import app
from sdg.engine.execute import ReferenceSets, run_plan
from sdg.plan.model import Plan, PlanField, PlanTable, ReuseSource, new_meta
from sdg.plan.refsets import ReferenceSet
from sdg.writers.sqlserver import TableWrite, WriteReport

client = TestClient(app)

TARGET = {"server": "localhost", "database": "SDM_TEST_TARGET"}


def _plan(start: int = 1) -> Plan:
    """Members pointing at plans the target already holds."""
    return Plan(
        meta=new_meta("load-audit-test", "seed-load"),
        tables=[
            PlanTable(name="dbo.PLAN", mode="reuse", fields=[],
                      source=ReuseSource(connection="source", table="PLAN",
                                         key_columns=["PLAN_ID"])),
            PlanTable(
                name="dbo.MEMBER", mode="generate", count=25, level=0,
                fields=[
                    PlanField(name="MEME_CK", bucket="business_key", generator="sequence",
                              params={"start": start}, provenance="structural"),
                    PlanField(name="AGE", bucket="free", generator="number",
                              params={"distribution": "uniform", "min": 0, "max": 90},
                              provenance="learned"),
                    PlanField(name="PLAN_ID", bucket="reference", generator="reference",
                              params={"parent_table": "dbo.PLAN", "parent_column": "PLAN_ID"},
                              provenance="structural"),
                    PlanField(name="NOTE", bucket="free", generator="skip",
                              provenance="structural"),
                ],
            ),
        ],
        generation_order=["dbo.MEMBER"],
    )


def _plans(keys) -> tuple[ReferenceSets, dict]:
    reference = ReferenceSet(table="dbo.PLAN", connection="source", source_table="dbo.PLAN",
                             key_column="PLAN_ID", keys=list(keys), available=len(keys))
    sets = ReferenceSets()
    sets.add("dbo.PLAN", reference.keys, reference.attributes)
    return sets, {"dbo.PLAN": reference}


def _load(plan: Plan, ran: Plan, keys=("P1", "P2", "P3")):
    sets, references = _plans(keys)
    result = run_plan(ran, reference_sets=sets)
    report = WriteReport(tables=[TableWrite(table="dbo.MEMBER",
                                            rows_written=len(result.rows("dbo.MEMBER")))])
    kept = load_requests.record(plan=plan, ran=ran, result=result, report=report,
                                references=references, target=TARGET, partial=False,
                                who="test", version="0", build="test-build")
    return kept, result


def test_a_load_keeps_its_record_and_none_of_its_rows():
    plan = _plan()
    ran = load_requests.with_starts(plan, {"dbo.MEMBER.MEME_CK": 5001})
    kept, result = _load(plan, ran)
    try:
        record = gr.get_file(kept["file"])
        params = record["params"]
        assert record["format"] == "database" and record["subject"] == "load"
        # Where the keys started, read from the target before writing.
        assert params["starts"] == {"dbo.MEMBER.MEME_CK": 5001}
        assert result.rows("dbo.MEMBER")[0]["MEME_CK"] == 5001
        assert params["tables"][0]["rows"] == 25 and params["rows"] == 25
        held = params["reference_sets"]["dbo.PLAN"]
        assert held["size"] == 3 and len(held["fingerprint"]) == 64
        # No rows, no keys: neither a member's values nor the plans it points at.
        text = json.dumps(params)
        assert "P1" not in text and "5001," not in text
        assert gr.snapshot(record["snapshots"]["plan"])["meta"]["seed"] == "seed-load"
        assert set(gr.snapshot(record["snapshots"]["algorithms"])) >= {"number", "sequence"}
        listed = next(r for r in gr.list_requests() if r["id"] == kept["request"])
        assert listed["subject"] == "load" and listed["files"][0]["rows"] == 25
        assert listed["files"][0]["target"] == "SDM_TEST_TARGET"
    finally:
        gr.delete_request(kept["request"])


def test_a_load_regenerated_is_the_same_rows_without_writing_anywhere():
    plan = _plan()
    ran = load_requests.with_starts(plan, {"dbo.MEMBER.MEME_CK": 5001})
    kept, result = _load(plan, ran)
    try:
        original = gr.get_file(kept["file"])
        body, name, evidence = load_requests.regenerate(
            original, load_references=lambda p: _plans(("P1", "P2", "P3")),
            who="test", version="0", build="test-build")
        assert evidence["matched"], evidence
        assert evidence["explanation"] == "Every table came out as it was written."
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            assert set(archive.namelist()) == {"evidence.json", "dbo.MEMBER.csv"}
            lines = archive.read("dbo.MEMBER.csv").decode().splitlines()
        # The columns written, not the one skipped; the keys where the load's started.
        assert lines[0] == "MEME_CK,AGE,PLAN_ID"
        assert lines[1].startswith("5001,") and len(lines) == 26
        first = result.rows("dbo.MEMBER")[0]
        assert lines[1] == f"{first['MEME_CK']},{first['AGE']},{first['PLAN_ID']}"
        replay = next(f for f in gr.list_requests()[0]["files"] if f["replay_of"] == kept["file"])
        assert replay["matched"] is True and replay["file_name"] == name
    finally:
        gr.delete_request(kept["request"])


def test_a_source_that_changed_since_is_said_and_not_hidden():
    plan = _plan()
    kept, _ = _load(plan, plan)
    try:
        original = gr.get_file(kept["file"])
        _, _, evidence = load_requests.regenerate(
            original, load_references=lambda p: _plans(("P1", "P2", "P9", "P10")),
            who="test", version="0", build="test-build")
        assert not evidence["matched"]
        assert evidence["reference_sets"] == [{
            "table": "dbo.PLAN", "source": "source:dbo.PLAN.PLAN_ID",
            "keys_then": 3, "keys_now": 4, "matched": False}]
        assert "the source no longer holds the same dbo.PLAN" in evidence["explanation"]
    finally:
        gr.delete_request(kept["request"])


def test_a_load_is_regenerated_from_the_requests_screen():
    # A plan with nothing to reuse: the screen's regenerate reads no database.
    plan = _plan()
    plan.tables = plan.tables[1:]
    plan.tables[0].fields = plan.tables[0].fields[:2]
    kept, _ = _load(plan, load_requests.with_starts(plan, {"dbo.MEMBER.MEME_CK": 70}))
    try:
        response = client.post(f"/api/generation-requests/files/{kept['file']}/again")
        assert response.status_code == 200, response.text
        assert response.headers["x-sdm-matched"] == "1"
        assert response.headers["x-sdm-request"] == str(kept["request"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            evidence = json.loads(archive.read("evidence.json"))
            assert archive.read("dbo.MEMBER.csv").decode().splitlines()[1].startswith("70,")
        assert evidence["request"] == kept["request"] and evidence["seed"] == "seed-load"
        assert evidence["loaded"]["into"] == TARGET
    finally:
        gr.delete_request(kept["request"])


def test_the_build_is_the_code_not_what_it_writes_while_running(tmp_path, monkeypatch):
    """A cache written while running, or compiled bytecode, is not a new build:
    a file written again would otherwise say SDM changed when it had not."""
    from sdg.api import app as api

    code = tmp_path / "sdg"
    (code / "analyzer" / "cache" / "api").mkdir(parents=True)
    (code / "__pycache__").mkdir()
    (code / "engine.py").write_text("x = 1\n", encoding="utf-8")
    (code / "profiles.json").write_text("{}\n", encoding="utf-8")
    before = [p.name for p in api._build_files(code)]
    (code / "analyzer" / "cache" / "api" / "members.pkl").write_bytes(b"frames")
    (code / "analyzer" / "cache" / "api" / "members.meta").write_text("m", encoding="utf-8")
    (code / "__pycache__" / "engine.cpython-313.pyc").write_bytes(b"\0")
    assert [p.name for p in api._build_files(code)] == before == ["engine.py", "profiles.json"]
    (code / "rules.py").write_text("y = 2\n", encoding="utf-8")
    assert "rules.py" in [p.name for p in api._build_files(code)]

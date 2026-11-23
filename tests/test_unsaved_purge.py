"""The one-time purge of samples kept by runs of unsaved configurations.

Runs written before 2026-11-06 kept an unsaved configuration's sample text in
their snapshot. `sdg requests --purge-unsaved-samples` (a dry run unless
`--apply`) rewrites those snapshots -- and nothing else -- to what a run keeps
now: the sample's digest and facts, and where SDM still holds it. The key the
files name stays, so the runs still resolve, and then follow the usual Rerun
rules. Every test here purges only the snapshots it kept itself (by key), and
removes only the runs it opened (by id).
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from sdg.analyzer.services import generation_requests as gr
from sdg.api.app import app
from tests.test_generation_requests import _members_pattern, _unsaved_asked, _unsaved_from

client = TestClient(app)

pytestmark = pytest.mark.db


@pytest.fixture
def opened():
    """Runs a test opened, removed by their ids afterwards."""
    ids: list[int] = []
    yield ids
    for request_id in ids:
        gr.delete_request(request_id)


def _old_run(opened, monkeypatch, asked):
    """A run written as SDM wrote one before 2026-11-06: its snapshot holds
    the configuration with its sample's text."""
    import sdg.api.app as app_module

    with monkeypatch.context() as before:
        before.setattr(app_module, "_unsaved_kept", lambda body: body)
        written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    file = gr.get_file(gr.first_file(request_id)["id"])
    key = file["snapshots"][file["format"]]
    held = gr.snapshot(key)
    assert "sample_text" in held, "the run did not keep its sample as runs used to"
    return written, file, key, held


def _purge(apply: bool, key: str):
    from sdg.api.app import purge_unsaved_samples

    return purge_unsaved_samples(apply=apply, only={key})


def test_an_uploaded_sample_is_purged_from_its_run_and_rerun_then_says_so(opened, monkeypatch):
    from sdg.api.app import UNSAVED_PURGE_LOG, UNSAVED_RERUN, service

    pattern = _members_pattern()
    sample, asked = _unsaved_asked(pattern, "2026-11-07T11:00:00.000Z",
                                   sample_name=f"mine-{uuid.uuid4().hex[:12]}.hl7")
    written, file, key, held = _old_run(opened, monkeypatch, asked)
    # Before the purge it reruns from the sample it kept.
    replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert replay.status_code == 200 and replay.content == written.content

    # A dry run changes nothing.
    dry = _purge(False, key)
    assert [e["digest"] for e in dry] == [key] and "rewritten" not in dry[0]
    assert gr.snapshot(key) == held

    log = service.work / UNSAVED_PURGE_LOG
    logged_before = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    done = _purge(True, key)
    assert [(e["digest"], e["rewritten"]) for e in done] == [(key, 1)]
    assert done[0]["source"] is None, "an uploaded sample has no source in SDM"

    # Rewritten under the same key: the sample's text is gone, its facts kept.
    purged = gr.snapshot(key)
    assert "sample_text" not in purged and "ROE^JOAN" not in gr.dumps(purged)
    assert purged["config"] == held["config"] and purged["kind"] == held["kind"]
    facts = purged["sample"]
    assert facts["digest"] == hashlib.sha256(sample.encode("utf-8")).hexdigest()
    assert facts["size"] == len(sample.encode("utf-8"))
    assert facts["format"] == "hl7v2" and facts["counts"]["segments"] > 0
    assert facts["name"] == held["sample_name"] and facts["source"] is None
    # The file still names it.
    assert gr.get_file(file["id"])["snapshots"][file["format"]] == key

    # Logged before it was made.
    logged = log.read_text(encoding="utf-8").splitlines()
    assert len(logged) == len(logged_before) + 1
    entry = json.loads(logged[-1])
    assert entry["digest"] == key and entry["who"] and entry["at"]

    # Run again, there is nothing left to do.
    assert _purge(True, key) == []
    assert gr.snapshot(key) == purged

    # The run now follows the Rerun rules for an uploaded sample.
    refused = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == UNSAVED_RERUN


def test_a_standard_files_sample_is_purged_and_still_reruns_the_same(opened, monkeypatch):
    """A sample SDM still holds -- here a standard file -- is found by its
    text: the purged run points at it and reruns byte-identical."""
    pattern = _members_pattern()
    asked = _unsaved_from(pattern, "2026-11-07T11:30:00.000Z", default_id="hl7-adt")
    # A sample name of its own, so the snapshot is this test's row.
    asked["sample_config"]["sampleName"] = f"adt-{uuid.uuid4().hex[:12]}.hl7"
    written, file, key, _held = _old_run(opened, monkeypatch, asked)

    done = _purge(True, key)
    assert [(e["digest"], e["rewritten"], e["source"]) for e in done] == [
        (key, 1, {"standard": "hl7-adt"})]
    assert "sample_text" not in gr.snapshot(key)

    replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert replay.status_code == 200, replay.text
    assert replay.content == written.content


def test_the_purge_touches_no_other_snapshot(opened, monkeypatch):
    """Only kind 'unsaved' rows holding a sample: a run kept the new way and
    a snapshot of another kind that merely mentions sample_text are left as
    they are -- even when named."""
    from sdg.api.app import purge_unsaved_samples

    pattern = _members_pattern()
    _sample, asked = _unsaved_asked(pattern, "2026-11-07T12:00:00.000Z",
                                    sample_name=f"mine-{uuid.uuid4().hex[:12]}.hl7")
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    opened.append(int(written.headers["X-SDM-Request"]))
    file = gr.get_file(gr.first_file(opened[-1])["id"])
    kept_new = file["snapshots"][file["format"]]
    other = gr.keep_snapshot("rules", {"sample_text": f"not a sample {uuid.uuid4().hex}"})
    before = {kept_new: gr.snapshot(kept_new), other: gr.snapshot(other)}

    assert purge_unsaved_samples(apply=True, only={kept_new, other}) == []
    assert {k: gr.snapshot(k) for k in before} == before


def test_the_command_is_a_dry_run_unless_applied(opened, monkeypatch):
    """`sdg requests --purge-unsaved-samples` lists and changes nothing;
    --apply alone is refused."""
    from typer.testing import CliRunner

    from sdg.cli.main import app as cli

    pattern = _members_pattern()
    _sample, asked = _unsaved_asked(pattern, "2026-11-07T12:30:00.000Z",
                                    sample_name=f"mine-{uuid.uuid4().hex[:12]}.hl7")
    _written, _file, key, held = _old_run(opened, monkeypatch, asked)

    runner = CliRunner()
    listed = runner.invoke(cli, ["requests", "--purge-unsaved-samples"])
    assert listed.exit_code == 0, listed.output
    said = " ".join(listed.output.split())
    assert "would be rewritten" in said and "Nothing was changed" in said, said
    assert gr.snapshot(key) == held

    alone = runner.invoke(cli, ["requests", "--apply"])
    assert alone.exit_code != 0
    assert gr.snapshot(key) == held

    # This test's own row, purged so it leaves no sample behind.
    assert [e["rewritten"] for e in _purge(True, key)] == [1]

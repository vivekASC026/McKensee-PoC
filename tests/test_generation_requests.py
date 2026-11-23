"""Generation requests: new people per request, the same people within one,
and every file written again from the request's record alone.

Nothing generated is kept: a request holds its seed, the pattern and what
each file was written through. Rows a test opens are removed by their ids.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sdg.analyzer.services import generation_requests as gr
from sdg.api.app import app

client = TestClient(app)


def test_a_request_seed_is_the_installation_the_pattern_and_the_moment():
    assert gr.seed_for("demo-seed-001", "12", "2026-10-23T14:05:09.123Z") == \
        "demo-seed-001:12:2026-10-23T14:05:09.123Z"
    assert gr.pattern_key(pattern_id=12) == "12"
    assert gr.pattern_key(scenarios=[{"pattern_id": 3}, {"pattern_id": 9}]) == "scenarios:3-9"
    assert gr.pattern_key() == "plan"
    live = {"domain": "members", "filters": {"gender": ["F"]}, "k": 4, "cluster_id": 2,
            "claim_type": None, "profile": {"name": "anything"}}
    # An analysis's pattern is named by what rebuilds it, not by its numbers.
    assert gr.pattern_key(live=live) == gr.pattern_key(live={**live, "profile": {}})
    assert gr.pattern_key(live=live) != gr.pattern_key(live={**live, "cluster_id": 3})


def test_a_moment_is_refused_unless_it_is_one():
    assert gr.checked_started("2026-10-23T14:05:09Z")
    for bad in ("", "yesterday", "2026-10-23", "2026-10-23T14:05:09Z; DROP"):
        with pytest.raises(ValueError):
            gr.checked_started(bad)


def test_a_snapshot_reads_back_the_numbers_it_held():
    body = {"min_value": Decimal("10.50"), "values": ["F", "M"], "n": 3}
    assert gr.loads(gr.dumps(body)) == body
    assert gr.digest(body) == gr.digest(dict(reversed(list(body.items()))))


def _members_pattern():
    from sdg.analyzer import db

    pattern = next((p for p in db.get_all_patterns()
                    if p.get("profile_json") and "claims_pattern" not in (p["profile_json"] or "")),
                   None)
    if pattern is None:
        pytest.skip("no stored members pattern")
    return pattern


def _subscribers(edi_text: str) -> set[str]:
    return {s.split("*")[2].rstrip("~") for s in edi_text.splitlines() if s.startswith("REF*0F")}


@pytest.fixture
def opened():
    """Requests a test opened, removed by their ids afterwards."""
    ids: list[int] = []
    yield ids
    for request_id in ids:
        gr.delete_request(request_id)


@pytest.mark.db
def test_a_new_request_is_new_people_and_one_request_the_same_people(opened):
    pattern = _members_pattern()
    base = {"subject": "members", "pattern_id": pattern["id"], "members": 5}
    first = {**base, "request_started": "2026-10-23T09:00:00.000Z"}
    second = {**base, "request_started": "2026-11-17T09:00:00.000Z"}

    day_one = client.post("/api/export", json={"format": "edi:834", **first})
    assert day_one.status_code == 200, day_one.text
    again = client.post("/api/export", json={"format": "edi:834", **first}).text
    later = client.post("/api/export", json={"format": "edi:834", **second}).text

    assert _subscribers(day_one.text) and _subscribers(day_one.text) == _subscribers(again)
    assert not _subscribers(day_one.text) & _subscribers(later), "a new request repeated people"

    # The CSV of the same request names the same people as its 834.
    csv = client.post("/api/export", json={"format": "csv", **first}).text.strip().split("\r\n")
    column = csv[0].split(",").index("SUBSCRIBER_ID")
    assert {row.split(",")[column] for row in csv[1:]} == _subscribers(day_one.text)

    # Nothing was downloaded, so nothing was kept.
    assert gr.find(str(pattern["id"]), first["request_started"]) is None


@pytest.mark.db
def test_a_downloaded_file_is_kept_and_written_again_the_same(opened):
    pattern = _members_pattern()
    asked = {"format": "edi:834", "subject": "members", "pattern_id": pattern["id"],
             "members": 4, "request_started": "2026-10-23T10:15:00.000Z", "record": True}

    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    # What the checks and the preview read is the file that was kept.
    unkept = client.post("/api/export", json={**asked, "record": False})
    assert unkept.content == written.content and "X-SDM-Request" not in unkept.headers

    request = gr.get(request_id)
    assert request["pattern_id"] == pattern["id"]
    assert request["seed"].endswith(f":{pattern['id']}:2026-10-23T10:15:00.000Z")

    listed = [r for r in gr.list_requests(pattern_id=pattern["id"]) if r["id"] == request_id]
    assert len(listed) == 1 and len(listed[0]["files"]) == 1
    kept = gr.get_file(listed[0]["files"][0]["id"])
    assert kept["fingerprint"] == hashlib.sha256(written.content).hexdigest()
    assert kept["bytes"] == len(written.content)
    assert {"rules", f"pattern:{pattern['id']}"} <= set(kept["snapshots"])
    # Only what makes the file again: no rows, no members.
    assert "members" not in kept["params"] or kept["params"]["members"] == 4

    # A second file type under the same request opens no second request.
    csv = client.post("/api/export", json={**asked, "format": "csv"})
    assert int(csv.headers["X-SDM-Request"]) == request_id

    replay = client.post(f"/api/generation-requests/files/{kept['id']}/again")
    assert replay.status_code == 200, replay.text
    assert replay.content == written.content
    files = [r for r in gr.list_requests(pattern_id=pattern["id"])
             if r["id"] == request_id][0]["files"]
    again = [f for f in files if f["replay_of"] == kept["id"]]
    assert len(again) == 1 and again[0]["matched"] is True


@pytest.mark.db
def test_written_again_uses_the_rules_as_they_were(opened, monkeypatch):
    """A rule changed since is not what the file was written with."""
    from sdg.analyzer.services import layout_service

    pattern = _members_pattern()
    asked = {"format": "edi:834", "subject": "members", "pattern_id": pattern["id"],
             "members": 3, "request_started": "2026-10-23T11:30:00.000Z", "record": True}
    written = client.post("/api/export", json=asked)
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    file_id = gr.list_requests(pattern_id=pattern["id"])[0]["files"][0]["id"]

    rules = layout_service.rules_by_element()
    changed = {**rules, "LAST_NAME": {**rules.get("LAST_NAME", {}), "element": "LAST_NAME",
                                      "gen_method": "lookup", "values": ["CHANGED"]}}
    monkeypatch.setattr(layout_service, "rules_by_element", lambda: changed)
    assert "CHANGED" in client.post("/api/export", json={**asked, "record": False}).text

    replay = client.post(f"/api/generation-requests/files/{file_id}/again")
    assert replay.content == written.content


@pytest.mark.db
def test_a_file_modeled_on_a_sample_is_written_again_from_its_config_as_it_was(opened):
    from sdg.analyzer.services import sample_gen_configs

    read = client.post("/api/samples/read", json={"defaultId": "fhir-members"}).json()
    config = client.post("/api/samples/configs", json={
        "name": "SDMT request replay FHIR", "rows": read["rows"], "basedOn": read["basedOn"],
        "fromDefaultId": "fhir-members",
        "vary": [{"at": "Patient telecom[*]", "present": 70, "min": 1, "max": 3}],
    }).json()
    assert "id" in config, config
    try:
        _replayed_from_config(opened, config)
    finally:
        sample_gen_configs.delete_config(config["id"])


@pytest.mark.db
@pytest.mark.parametrize("default_id, subject, check", [
    ("fhir-members", "members", lambda text: '"resourceType": "Bundle"' in text
     or '"resourceType":"Bundle"' in text),
    ("hl7-adt", "members", lambda text: text.startswith("MSH|")),
])
def test_a_standard_file_is_written_with_no_config_saved_and_again_the_same(
        opened, default_id, subject, check):
    """Generate file on a pattern writes FHIR or HL7 v2 through the standard
    file as its tab proposes it, kept under a run and written again the same."""
    pattern = _members_pattern()
    asked = {"format": f"standard:{default_id}", "subject": subject,
             "pattern_id": pattern["id"], "members": 3,
             "request_started": "2026-11-06T12:00:00.000Z", "record": True,
             "file_name": f"{default_id}.txt"}
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    assert check(written.text), written.text[:300]
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    kept = [r for r in gr.list_requests(pattern_id=pattern["id"]) if r["id"] == request_id][0]
    file = gr.get_file(kept["files"][0]["id"])
    assert f"standard:{default_id}" in file["snapshots"]
    assert file["format"] == f"standard:{default_id}"

    replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert replay.status_code == 200, replay.text
    assert replay.content == written.content


@pytest.mark.db
def test_an_unknown_standard_file_is_refused():
    response = client.post("/api/export", json={"format": "standard:nothing", "subject": "members",
                                                "pattern_id": 1})
    assert response.status_code == 404


def _unsaved_asked(pattern, started, sample_name="mine.hl7"):
    """An HL7 sample uploaded, read against its standard and its suggestions
    accepted: the export of that configuration, not saved."""
    from sdg.writers.samples import standard

    sample = standard.files("hl7-adt")["sample"].replace("DOE^JANE", "ROE^JOAN")
    read = client.post("/api/samples/read", json={
        "sample": sample, "sampleName": sample_name, "auto": True}).json()
    assert read["defaultId"] == "hl7-adt"
    rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
    accepted = {"sample": sample, "sampleName": sample_name, "basedOn": read["basedOn"],
                "rows": rows, "vary": read["vary"]}
    return sample, {"format": "unsaved", "sample_config": accepted, "subject": "members",
                    "pattern_id": pattern["id"], "members": 3,
                    "request_started": started, "record": True, "file_name": "adt.hl7"}


@pytest.mark.db
def test_an_unsaved_configuration_is_written_kept_without_its_sample_and_rerun_says_so(opened):
    """A sample uploaded, read against its format's standard and its
    suggestions accepted, generates with no configuration saved. The run keeps
    the configuration and facts about the sample (digest, size, counts), never
    the sample: Rerun needs it, so Rerun says so in one sentence (user
    decision 2026-11-06: keep only what Rerun needs; if Rerun is not possible,
    say it)."""
    import hashlib

    from sdg.analyzer.services import sample_gen_configs
    from sdg.api.app import UNSAVED_RERUN

    pattern = _members_pattern()
    sample, asked = _unsaved_asked(pattern, "2026-11-06T13:00:00.000Z")
    configs_before = {c["id"] for c in sample_gen_configs.list_configs()}

    preview = client.post("/api/export", json={**asked, "record": False})
    assert preview.status_code == 200, preview.text
    assert preview.text.startswith("MSH|") and "ROE^JOAN" not in preview.text
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    assert written.content == preview.content
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    assert {c["id"] for c in sample_gen_configs.list_configs()} == configs_before, \
        "a configuration was saved"

    kept = [r for r in gr.list_requests(pattern_id=pattern["id"]) if r["id"] == request_id][0]
    file = gr.get_file(kept["files"][0]["id"])
    assert file["format"].startswith("unsaved:")
    assert file["format"] in file["snapshots"], file["snapshots"]
    assert "sample_config" not in file["params"], "the sample is repeated in the parameters"

    kept_config = gr.snapshot(file["snapshots"][file["format"]])
    assert "sample_text" not in kept_config, "the run kept the uploaded sample"
    assert "ROE^JOAN" not in gr.dumps(kept_config), "the sample's content is in the run"
    assert "MSH|" not in gr.dumps(kept_config), "the sample's content is in the run"
    facts = kept_config["sample"]
    assert facts["digest"] == hashlib.sha256(sample.encode("utf-8")).hexdigest()
    assert facts["size"] == len(sample.encode("utf-8"))
    assert facts["format"] == "hl7v2" and facts["kind"] == kept_config["kind"]
    assert facts["counts"]["segments"] > 0, facts
    assert kept_config["config"]["genSegments"], "the configuration was not kept"

    assert facts["source"] is None, "an uploaded sample has no source in SDM"

    # History says before Rerun is pressed that it can't, and why.
    listed = client.get("/api/generation-requests", params={"pattern_id": pattern["id"]}).json()
    row = next(f for r in listed["requests"] if r["id"] == request_id for f in r["files"])
    assert row["rerunnable"] is False and row["rerun_refusal"] == UNSAVED_RERUN

    replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"] == UNSAVED_RERUN

    # Asked for by its name alone, outside its run, it is not there to write.
    gone = client.post("/api/export", json={**asked, "format": file["format"],
                                            "sample_config": None, "record": False})
    assert gone.status_code == 409


@pytest.mark.db
def test_a_run_that_kept_its_unsaved_sample_before_reruns_the_same(opened, monkeypatch):
    """Runs written before the sample stopped being kept hold it in their
    snapshot: they still rerun byte-identical."""
    import sdg.api.app as app_module

    import uuid

    pattern = _members_pattern()
    # A sample name of its own: the snapshot is this run's row, never one an
    # earlier run kept and `sdg requests --purge-unsaved-samples` rewrote.
    _sample, asked = _unsaved_asked(pattern, "2026-11-06T13:30:00.000Z",
                                    sample_name=f"mine-{uuid.uuid4().hex[:12]}.hl7")
    with monkeypatch.context() as before:
        # As SDM kept it until 2026-11-06: the configuration with its sample.
        before.setattr(app_module, "_unsaved_kept", lambda body: body)
        written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    kept = [r for r in gr.list_requests(pattern_id=pattern["id"]) if r["id"] == request_id][0]
    file = gr.get_file(kept["files"][0]["id"])
    assert "sample_text" in gr.snapshot(file["snapshots"][file["format"]])

    replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert replay.status_code == 200, replay.text
    assert replay.content == written.content
    # Its own snapshot purged afterwards: the test leaves no sample behind.
    from sdg.api.app import purge_unsaved_samples

    purge_unsaved_samples(apply=True, only={file["snapshots"][file["format"]]})


def _unsaved_from(pattern, started, *, default_id=None, config_id=None):
    """A configuration read from a standard file or a saved configuration, no
    sample uploaded, its suggestions accepted: the export of it, not saved."""
    read = client.post("/api/samples/read", json=(
        {"defaultId": default_id} if default_id else {"configId": config_id})).json()
    rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
    accepted = {"sample": None, "sampleName": read["sampleName"], "basedOn": read["basedOn"],
                "rows": rows, "vary": read["vary"], "fromDefaultId": default_id,
                "fromConfigId": config_id}
    return {"format": "unsaved", "sample_config": accepted, "subject": "members",
            "pattern_id": pattern["id"], "members": 3, "request_started": started,
            "record": True, "file_name": "adt.hl7"}


def _kept_unsaved(opened, pattern, asked):
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    kept = [r for r in gr.list_requests(pattern_id=pattern["id"]) if r["id"] == request_id][0]
    file = gr.get_file(kept["files"][0]["id"])
    return written, file, gr.snapshot(file["snapshots"][file["format"]])


def _listed_file(pattern, file):
    listed = client.get("/api/generation-requests", params={"pattern_id": pattern["id"]}).json()
    return next(f for r in listed["requests"] if r["id"] == file["request_id"]
                for f in r["files"] if f["id"] == file["id"])


@pytest.mark.db
def test_an_unsaved_configuration_of_a_standard_file_reruns_the_same(opened):
    """Read from a standard file with nothing uploaded, the sample is SDM's
    own: the run keeps where it came from beside its digest, never its text,
    and Rerun writes the identical file from the standard file."""
    from sdg.writers.samples import standard

    pattern = _members_pattern()
    asked = _unsaved_from(pattern, "2026-11-07T09:00:00.000Z", default_id="hl7-adt")
    written, file, kept = _kept_unsaved(opened, pattern, asked)
    sample = standard.files("hl7-adt")["sample"]
    assert "sample_text" not in kept
    assert "MSH|" not in gr.dumps(kept), "the sample's content is in the run"
    assert kept["sample"]["source"] == {"standard": "hl7-adt"}
    assert kept["sample"]["digest"] == hashlib.sha256(sample.encode("utf-8")).hexdigest()

    row = _listed_file(pattern, file)
    assert row["rerunnable"] is True and row["rerun_refusal"] is None

    replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert replay.status_code == 200, replay.text
    assert replay.content == written.content


@pytest.mark.db
def test_an_unsaved_configuration_of_a_saved_one_reruns_until_its_sample_changes(opened):
    """Read from a saved configuration with nothing uploaded: Rerun writes the
    identical file from that configuration's sample -- and once that sample
    is changed, says so in one sentence and writes nothing."""
    import time

    from sdg.analyzer.services import sample_gen_configs
    from sdg.api.app import UNSAVED_CHANGED
    from sdg.writers.samples import standard

    read = client.post("/api/samples/read", json={"defaultId": "hl7-adt"}).json()
    config = client.post("/api/samples/configs", json={
        "name": f"SDMT unsaved source {time.time_ns()}", "rows": read["rows"],
        "basedOn": read["basedOn"], "fromDefaultId": "hl7-adt"}).json()
    assert "id" in config, config
    try:
        pattern = _members_pattern()
        asked = _unsaved_from(pattern, "2026-11-07T09:30:00.000Z", config_id=config["id"])
        written, file, kept = _kept_unsaved(opened, pattern, asked)
        assert "sample_text" not in kept
        assert kept["sample"]["source"] == {"config": config["id"]}

        replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
        assert replay.status_code == 200, replay.text
        assert replay.content == written.content

        # The saved configuration's sample changed: the run's sample is gone.
        other = standard.files("hl7-adt")["sample"].replace("DOE^JANE", "POE^JUNE")
        changed = client.put(f"/api/samples/configs/{config['id']}", json={
            "name": config["name"], "sample": other, "rows": read["rows"]})
        assert changed.status_code == 200, changed.text

        def files_of_run() -> int:
            return len(next(r for r in gr.list_requests(pattern_id=pattern["id"])
                            if r["id"] == file["request_id"])["files"])

        files_before = files_of_run()
        row = _listed_file(pattern, file)
        assert row["rerunnable"] is False and row["rerun_refusal"] == UNSAVED_CHANGED
        refused = client.post(f"/api/generation-requests/files/{file['id']}/again")
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == UNSAVED_CHANGED
        assert files_of_run() == files_before, "a refused Rerun recorded a file"
    finally:
        sample_gen_configs.delete_config(config["id"])


@pytest.mark.db
def test_a_refused_rerun_makes_no_members(opened, monkeypatch):
    """The refusal is decided before any member is generated."""
    import sdg.api.app as app_module

    pattern = _members_pattern()
    _sample, asked = _unsaved_asked(pattern, "2026-11-07T10:00:00.000Z")
    _written, file, _kept = _kept_unsaved(opened, pattern, asked)

    def made(*args, **kwargs):
        raise AssertionError("members were generated for a refused Rerun")

    monkeypatch.setattr(app_module, "_export_records", made)
    refused = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert refused.status_code == 409, refused.text


def _replayed_from_config(opened, config):
    pattern = _members_pattern()
    asked = {"format": f"sample:{config['id']}", "subject": "members",
             "pattern_id": pattern["id"], "members": 3,
             "request_started": "2026-10-23T12:00:00.000Z", "record": True}
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    kept = [r for r in gr.list_requests(pattern_id=pattern["id"]) if r["id"] == request_id][0]
    file = gr.get_file(kept["files"][0]["id"])
    assert f"config:{config['id']}" in file["snapshots"]

    replay = client.post(f"/api/generation-requests/files/{file['id']}/again")
    assert replay.status_code == 200, replay.text
    assert replay.content == written.content


@pytest.mark.db
def test_a_claims_file_for_a_pattern_is_written_again_the_same(opened):
    from sdg.analyzer import db

    pattern = next((p for p in db.get_all_patterns()
                    if p.get("profile_json") and '"claims_pattern": true' in p["profile_json"]),
                   None)
    if pattern is None:
        pytest.skip("no stored claims pattern")
    asked = {"format": "edi:837P", "subject": "claims", "pattern_id": pattern["id"],
             "rows": 20, "request_started": "2026-10-23T12:30:00.000Z", "record": True}
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    file_id = [r for r in gr.list_requests(pattern_id=pattern["id"])
               if r["id"] == request_id][0]["files"][0]["id"]
    replay = client.post(f"/api/generation-requests/files/{file_id}/again")
    assert replay.content == written.content


@pytest.mark.db
def test_a_claims_file_written_again_says_the_store_members_changed(opened, monkeypatch):
    """A claims file's members are read from the store each time it is written;
    when the store holds them differently, the file written again says so."""
    from sdg.analyzer import db
    from sdg.writers import elements

    pattern = next((p for p in db.get_all_patterns()
                    if p.get("profile_json") and '"claims_pattern": true' in p["profile_json"]),
                   None)
    if pattern is None:
        pytest.skip("no stored claims pattern")
    asked = {"format": "edi:837P", "subject": "claims", "pattern_id": pattern["id"],
             "rows": 20, "request_started": "2026-10-28T08:00:00.000Z", "record": True}
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    first = gr.first_file(request_id)
    assert "store members" in first["snapshots"]

    held = elements.store_members

    def refreshed(*args, **kwargs):
        members = held(*args, **kwargs)
        members[0] = {**members[0], "MEME_LAST_NAME": "Refreshed"}
        return members

    monkeypatch.setattr(elements, "store_members", refreshed)
    client.post(f"/api/generation-requests/files/{first['id']}/again")
    files = [r for r in gr.list_requests(pattern_id=pattern["id"])
             if r["id"] == request_id][0]["files"]
    again = [f for f in files if f["replay_of"] == first["id"]][-1]
    assert again["changed"] == ["the store's members"]


@pytest.mark.db
def test_a_later_file_type_for_an_earlier_request_has_its_people(opened, monkeypatch):
    """Day 1, an 834; day 25, after a rule changed, a CSV and a FHIR file for
    request 1 name the 834's people, as they were named then."""
    from sdg.analyzer.services import layout_service, sample_gen_configs

    pattern = _members_pattern()
    day_one = client.post("/api/export", json={
        "format": "edi:834", "subject": "members", "pattern_id": pattern["id"], "members": 4,
        "request_started": "2026-10-01T09:00:00.000Z", "record": True})
    request_id = int(day_one.headers["X-SDM-Request"])
    opened.append(request_id)
    names = {s.split("*")[3] for s in day_one.text.splitlines() if s.startswith("NM1*IL")}

    rules = layout_service.rules_by_element()
    changed = {**rules, "LAST_NAME": {**rules.get("LAST_NAME", {}), "element": "LAST_NAME",
                                      "gen_method": "lookup", "values": ["CHANGED"]}}
    monkeypatch.setattr(layout_service, "rules_by_element", lambda: changed)

    later = {"subject": "members", "members": 4, "request_id": request_id}
    csv = client.post("/api/export", json={"format": "csv", **later, "record": True})
    assert csv.status_code == 200, csv.text
    assert int(csv.headers["X-SDM-Request"]) == request_id
    rows = csv.text.strip().split("\r\n")
    header = rows[0].split(",")
    assert {r.split(",")[header.index("SUBSCRIBER_ID")] for r in rows[1:]} == \
        _subscribers(day_one.text)
    assert {r.split(",")[header.index("LAST_NAME")] for r in rows[1:]} == names

    read = client.post("/api/samples/read", json={"defaultId": "fhir-members"}).json()
    config = client.post("/api/samples/configs", json={
        "name": "SDMT later file type FHIR", "rows": read["rows"], "basedOn": read["basedOn"],
        "fromDefaultId": "fhir-members"}).json()
    try:
        fhir = client.post("/api/export", json={"format": f"sample:{config['id']}", **later})
        assert fhir.status_code == 200, fhir.text
        assert names <= {n.get("family") for e in fhir.json()["entry"]
                         if e["resource"]["resourceType"] == "Patient"
                         for n in e["resource"].get("name", [])}
    finally:
        sample_gen_configs.delete_config(config["id"])

    files = [r for r in gr.list_requests(pattern_id=pattern["id"]) if r["id"] == request_id][0]
    assert [f["format"] for f in files["files"]] == ["edi:834", "csv"]
    assert files["members"] == 4 and files["of"] == "members"

    claims = client.post("/api/export", json={"format": "edi:837P", **later,
                                              "subject": "claims"})
    assert claims.status_code == 409 and "members" in claims.json()["detail"]


def _claims_pattern():
    from sdg.analyzer import db

    pattern = next((p for p in db.get_all_patterns()
                    if p.get("profile_json") and '"claims_pattern": true' in p["profile_json"]),
                   None)
    if pattern is None:
        pytest.skip("no stored claims pattern")
    return pattern


def _people(edi_text: str) -> list[tuple[str, ...]]:
    """Each member, or patient, in an X12 file: name, then birth date and sex.
    A dependent's claim names the subscriber (no DMG), then the patient."""
    out, name = [], None
    for segment in edi_text.splitlines():
        parts = segment.rstrip("~").split("*")
        if parts[0] == "NM1" and parts[1] in ("IL", "QC"):
            name = (parts[3], parts[4])
        elif parts[0] == "DMG" and name is not None:
            out.append((*name, parts[2], parts[3]))
            name = None
    return out


@pytest.mark.db
def test_claims_for_an_earlier_request_are_for_its_members_as_written(opened):
    """Day 1, an 834 of new members; day 25, an 837 of a claims pattern for
    them: every claim names a member of the 834, with the 834's birth date and
    sex, whatever the claims pattern says of members. The claims are a request
    of their own, linked to the members', and are written again the same."""
    MEMBERS = 40
    members_pattern, claims_pattern = _members_pattern(), _claims_pattern()
    day_one = client.post("/api/export", json={
        "format": "edi:834", "subject": "members", "pattern_id": members_pattern["id"],
        "members": MEMBERS, "request_started": "2026-10-01T09:30:00.000Z", "record": True})
    parent = int(day_one.headers["X-SDM-Request"])
    opened.append(parent)
    enrolled = set(_people(day_one.text))
    assert len(enrolled) == MEMBERS

    asked = {"format": "edi:837P", "subject": "claims", "of_request": parent,
             "request_id": parent, "pattern_id": claims_pattern["id"],
             "request_started": "2026-10-25T10:00:00.000Z", "claimed_share": 100,
             "claims_min": 1, "claims_max": 2, "record": True}
    day_25 = client.post("/api/export", json=asked)
    assert day_25.status_code == 200, day_25.text
    child = int(day_25.headers["X-SDM-Request"])
    opened.append(child)
    assert child != parent and gr.get(child)["parent_id"] == parent
    billed = set(_people(day_25.text))
    assert billed == enrolled, billed ^ enrolled

    # A share of them, as the request asks. Enough members that a 40% share
    # is never all or none of them by chance (with 8, none came up about 1 run in 25).
    some = client.post("/api/export", json={**asked, "claimed_share": 40, "record": False})
    assert some.status_code == 200, some.text
    assert 0 < len(set(_people(some.text))) < MEMBERS

    file_id = gr.first_file(child)["id"]
    replay = client.post(f"/api/generation-requests/files/{file_id}/again")
    assert replay.content == day_25.content

    # The claims request chosen later: the same claims in another format, and
    # its members' file is the members request's.
    claims_csv = client.post("/api/export", json={"format": "csv", "subject": "claims",
                                                  "request_id": child}).text.strip().split("\r\n")
    column = claims_csv[0].split(",").index("CLAIM_ID")
    claim_ids = {s.split("*")[1] for s in day_25.text.splitlines() if s.startswith("CLM*")}
    assert {r.split(",")[column] for r in claims_csv[1:]} == claim_ids
    members_csv = client.post("/api/export", json={"format": "csv", "subject": "members",
                                                   "members": MEMBERS, "request_id": child})
    assert members_csv.status_code == 200, members_csv.text
    rows = members_csv.text.strip().split("\r\n")
    header = rows[0].split(",")
    assert {r.split(",")[header.index("SUBSCRIBER_ID")] for r in rows[1:]} == \
        _subscribers(day_one.text)

    check = client.post(f"/api/generation-requests/{parent}/claims-check",
                        json={"pattern_id": claims_pattern["id"]}).json()
    assert check["members"] == MEMBERS
    assert {f["element"] for f in check["features"]} >= {"SEX"}

    refused = client.post("/api/export", json={**asked, "pattern_id": members_pattern["id"],
                                               "record": False})
    assert refused.status_code == 422 and "not a claims pattern" in refused.json()["detail"]
    unnamed = client.post("/api/export", json={**asked, "pattern_id": None, "record": False})
    assert unnamed.status_code == 422


@pytest.mark.db
def test_written_again_with_the_algorithm_versions_it_was_made_with(opened):
    """An algorithm changed after a file was written makes new files differently;
    the kept file is written again by the version that made it."""
    from sdg.engine.generators import base

    pattern = _members_pattern()
    asked = {"format": "edi:834", "subject": "members", "pattern_id": pattern["id"],
             "members": 3, "request_started": "2026-10-23T13:45:00.000Z", "record": True}
    written = client.post("/api/export", json=asked)
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    file_id = [r for r in gr.list_requests(pattern_id=pattern["id"])
               if r["id"] == request_id][0]["files"][0]["id"]
    assert gr.snapshot(gr.get_file(file_id)["snapshots"]["algorithms"])["pattern"] == 1

    first = base._VERSIONS["pattern"][1]
    base.register_algorithm("pattern", 2, lambda ctx: "V2" + str(first(ctx))[2:])
    try:
        assert client.post("/api/export", json={**asked, "record": False}).content \
            != written.content
        replay = client.post(f"/api/generation-requests/files/{file_id}/again")
        assert replay.content == written.content
    finally:
        base._VERSIONS["pattern"].pop(2)


@pytest.mark.db
def test_more_of_a_claims_request_is_made_by_what_its_first_file_was(opened, monkeypatch):
    """A claims request chosen later writes its claims with the rules, the
    claims pattern and the versions its first file had -- not today's."""
    from sdg.analyzer.services import layout_service

    members_pattern, claims_pattern = _members_pattern(), _claims_pattern()
    day_one = client.post("/api/export", json={
        "format": "edi:834", "subject": "members", "pattern_id": members_pattern["id"],
        "members": 4, "request_started": "2026-10-02T09:30:00.000Z", "record": True})
    parent = int(day_one.headers["X-SDM-Request"])
    opened.append(parent)
    claims = client.post("/api/export", json={
        "format": "csv", "subject": "claims", "of_request": parent, "request_id": parent,
        "pattern_id": claims_pattern["id"], "request_started": "2026-10-26T10:00:00.000Z",
        "claims_mix": [[1, 50], [2, 50]], "lines_mix": [[1, 60], [3, 40]], "record": True})
    assert claims.status_code == 200, claims.text
    child = int(claims.headers["X-SDM-Request"])
    opened.append(child)

    rules = layout_service.rules_by_element()
    changed = {element: {**rule, "gen_method": "lookup", "values": ["CHANGED"]}
               for element, rule in rules.items()}
    monkeypatch.setattr(layout_service, "rules_by_element", lambda: changed)
    again = client.post("/api/export", json={"format": "csv", "subject": "claims",
                                             "request_id": child})
    assert again.status_code == 200, again.text
    assert "CHANGED" not in again.text, "today's rules made an earlier request's claims"
    assert again.content == claims.content, "the mixes asked for were not kept"


@pytest.mark.db
def test_a_later_file_records_the_versions_it_was_made_with(opened):
    """A file for an earlier request is made by the versions its people were:
    those are what it records, so it is written again by them too."""
    from sdg.engine.generators import base

    pattern = _members_pattern()
    day_one = client.post("/api/export", json={
        "format": "edi:834", "subject": "members", "pattern_id": pattern["id"],
        "members": 3, "request_started": "2026-10-03T08:00:00.000Z", "record": True})
    request_id = int(day_one.headers["X-SDM-Request"])
    opened.append(request_id)
    first = base._VERSIONS["pattern"][1]
    base.register_algorithm("pattern", 2, lambda ctx: "V2" + str(first(ctx))[2:])
    try:
        later = client.post("/api/export", json={
            "format": "csv", "subject": "members", "members": 3, "request_id": request_id,
            "record": True})
        assert later.status_code == 200, later.text
        kept = [f for f in gr.list_requests() if f["id"] == request_id][0]["files"]
        csv_file = gr.get_file(kept[-1]["id"])
        assert gr.snapshot(csv_file["snapshots"]["algorithms"])["pattern"] == 1
        assert set(gr.snapshot(csv_file["snapshots"]["lists"])) == {
            "first names", "last names", "streets", "addresses"}
        replay = client.post(f"/api/generation-requests/files/{csv_file['id']}/again")
        assert replay.content == later.content
    finally:
        base._VERSIONS["pattern"].pop(2)


@pytest.mark.db
def test_a_later_file_records_an_algorithm_newer_than_its_request_as_version_1(opened):
    """An algorithm added after a request was written made that request's next
    file at version 1 -- the request's versions do not name it -- so version 1
    is what the file records, not the algorithm's latest."""
    from sdg.engine.generators import base

    pattern = _members_pattern()
    day_one = client.post("/api/export", json={
        "format": "edi:834", "subject": "members", "pattern_id": pattern["id"],
        "members": 3, "request_started": "2026-10-04T08:00:00.000Z", "record": True})
    request_id = int(day_one.headers["X-SDM-Request"])
    opened.append(request_id)
    base.register_algorithm("added later", 1, lambda: "v1")
    base.register_algorithm("added later", 2, lambda: "v2")
    try:
        later = client.post("/api/export", json={
            "format": "csv", "subject": "members", "members": 3, "request_id": request_id,
            "record": True})
        assert later.status_code == 200, later.text
        kept = [f for f in gr.list_requests() if f["id"] == request_id][0]["files"]
        made_by = gr.snapshot(gr.get_file(kept[-1]["id"])["snapshots"]["algorithms"])
        assert made_by["added later"] == 1, made_by
    finally:
        base._VERSIONS.pop("added later")


def test_an_address_list_too_long_keeps_its_spread_when_one_is_added(monkeypatch):
    """A long ZIP list is cut to an even spread chosen by each address's own
    digest: one ZIP added changes at most one address of the spread."""
    import pandas as pd

    from sdg.analyzer import generation_rules as lists

    def frame(n):
        return pd.DataFrame([{"zip": f"{z:05d}", "city": f"C{z}", "state": "OH", "county": ""}
                             for z in range(n)])

    monkeypatch.setattr(lists, "ADDRESS_LIMIT", 50)
    monkeypatch.setattr(lists, "_addresses", None)
    monkeypatch.setattr(lists, "_load_lookup", lambda name: frame(400))
    before = lists.address_records()
    monkeypatch.setattr(lists, "_addresses", None)
    monkeypatch.setattr(lists, "_load_lookup", lambda name: pd.concat(
        [frame(400), pd.DataFrame([{"zip": "00150", "city": "NEW", "state": "OH", "county": ""}])]))
    after = lists.address_records()
    assert len(before) == len(after) == 50
    assert before == sorted(before, key=lambda r: r["ADDRESS_ZIP"])
    assert len([r for r in after if r not in before]) <= 1


def test_an_address_list_that_cannot_be_read_is_said(monkeypatch):
    from sdg.analyzer import generation_rules as lists
    from sdg.api import app as api

    def fails(error):
        def read():
            raise error
        return read

    monkeypatch.setattr(lists, "address_records",
                        fails(RuntimeError("[42S02] Invalid object name 'sdm.lkp_zip_city_state'")))
    assert api._addresses() == []
    monkeypatch.setattr(lists, "address_records", fails(RuntimeError("TCP Provider: timeout")))
    with pytest.raises(RuntimeError, match="timeout"):
        api._addresses()


@pytest.mark.db
def test_an_834_is_families_and_their_claims_are_the_dependents_under_the_subscriber(opened):
    """New members come in families: a subscriber (INS*Y), then dependents
    (INS*N) with the subscriber's ID -- or, where no subscriber in the file
    fits, one made and not written, as an 834 adding a dependent names a
    subscriber enrolled before. Claims for them name the subscriber and then
    the patient, with the patient's own birth date and sex from the 834."""
    members_pattern, claims_pattern = _members_pattern(), _claims_pattern()
    enrolled = client.post("/api/export", json={
        "format": "edi:834", "subject": "members", "pattern_id": members_pattern["id"],
        "members": 30, "request_started": "2026-10-02T09:30:00.000Z", "record": True})
    parent = int(enrolled.headers["X-SDM-Request"])
    opened.append(parent)
    loops = [s.rstrip("~").split("*") for s in enrolled.text.splitlines()
             if s.startswith(("INS*", "REF*0F"))]
    pairs = list(zip(loops[0::2], loops[1::2]))
    heads = {ref[2] for ins, ref in pairs if ins[1] == "Y"}
    dependents = [ref[2] for ins, ref in pairs if ins[1] == "N"]
    assert dependents, "thirty members and not one dependent"
    assert set(dependents) & heads, "no dependent joined a subscriber in the file"
    first_of = {}
    for at, (ins, ref) in enumerate(pairs):
        first_of.setdefault(ref[2], at)
    assert all(pairs[first_of[s]][0][1] == "Y" for s in heads), "a family opens on its subscriber"

    claims = client.post("/api/export", json={
        "format": "edi:837P", "subject": "claims", "of_request": parent,
        "pattern_id": claims_pattern["id"], "claimed_share": 100, "claims_min": 1,
        "claims_max": 1, "request_started": "2026-10-26T10:00:00.000Z", "record": True})
    assert claims.status_code == 200, claims.text
    opened.append(int(claims.headers["X-SDM-Request"]))
    assert claims.text.count("PAT*") == len(dependents)
    assert claims.text.count("CLM*") == len(pairs)
    # Each patient's HL sits under a subscriber's; siblings in a row share it.
    segments = [s.rstrip("~").split("*") for s in claims.text.splitlines()]
    levels = {s[1]: s[3] for s in segments if s[0] == "HL"}
    assert all(levels[s[2]] == "22" for s in segments if s[0] == "HL" and s[3] == "23")
    assert claims.text.count("HL*") - 1 <= len(pairs) + len(dependents)
    assert sorted(_people(claims.text)) == sorted(_people(enrolled.text))


@pytest.mark.db
def test_the_command_line_writes_a_file_for_a_pattern_and_writes_it_again(opened, tmp_path):
    """`sdg write-file` keeps the file under a request as the screens do, and
    `sdg write-again` makes the same file from the record alone."""
    import re

    from typer.testing import CliRunner

    from sdg.cli.main import app as cli

    pattern = _members_pattern()
    runner = CliRunner()
    written = runner.invoke(cli, ["write-file", "--pattern", str(pattern["id"]), "--format",
                                  "edi:834", "--members", "4", "--out", str(tmp_path)])
    assert written.exit_code == 0, written.output
    request_id = int(re.search(r"kept under request (\d+)", written.output).group(1))
    opened.append(request_id)
    first = gr.first_file(request_id)
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and f"request-{request_id}" in files[0].name

    again = runner.invoke(cli, ["write-again", str(first["id"]), "--out", str(tmp_path)])
    assert again.exit_code == 0, again.output
    assert "the same file" in " ".join(again.output.split())
    copy = next(f for f in tmp_path.iterdir() if "-again" in f.name)
    assert copy.read_bytes() == files[0].read_bytes()

    listed = runner.invoke(cli, ["requests", "--pattern", str(pattern["id"])])
    assert listed.exit_code == 0 and str(request_id) in listed.output


@pytest.mark.db
def test_a_file_from_the_plan_is_written_again_from_the_plan_it_was_written_from(
        opened, monkeypatch):
    """A file generated from the plan keeps the plan: replanned or reseeded
    since, the file written again is the same file."""
    from sdg.api.app import service

    asked = {"format": "csv", "subject": "members", "rows": 20, "root_limit": 20,
             "request_started": "2026-11-06T09:00:00.000Z", "record": True}
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    first = gr.first_file(request_id)
    assert {"plan", "reused keys"} <= set(first["snapshots"])

    reseeded = service.require_plan().model_copy(deep=True)
    reseeded.meta.seed = "a-plan-built-since"
    monkeypatch.setattr(type(service), "require_plan", lambda self: reseeded)
    replay = client.post(f"/api/generation-requests/files/{first['id']}/again")
    assert replay.status_code == 200, replay.text
    assert replay.content == written.content


@pytest.mark.db
def test_a_file_written_again_says_the_combinations_changed(opened, monkeypatch):
    """The store's combinations are read live; when they differ from when the
    file was written -- or cannot be read -- the file written again says so."""
    from sdg.plan import combinations

    pattern = _members_pattern()
    asked = {"format": "csv", "subject": "members", "pattern_id": pattern["id"],
             "members": 4, "request_started": "2026-11-06T10:00:00.000Z", "record": True}
    written = client.post("/api/export", json=asked)
    assert written.status_code == 200, written.text
    request_id = int(written.headers["X-SDM-Request"])
    opened.append(request_id)
    first = gr.first_file(request_id)
    assert "combinations" in first["snapshots"]

    monkeypatch.setattr(combinations, "signature",
                        lambda: {"groups": [], "unreadable": {"discover": "OperationalError"}})
    client.post(f"/api/generation-requests/files/{first['id']}/again")
    files = [r for r in gr.list_requests(pattern_id=pattern["id"])
             if r["id"] == request_id][0]["files"]
    again = [f for f in files if f["replay_of"] == first["id"]][-1]
    assert "the combinations measured in the store" in again["changed"]


def test_a_run_kept_with_an_empty_sample_says_it_cannot_be_rerun():
    """A snapshot kept with its sample, but an empty one, has nothing to write
    from: it is refused plainly, not offered and then failed."""
    from sdg.api.app import UNSAVED_RERUN, unsaved_rerun_refusal

    for empty in ("", None):
        assert unsaved_rerun_refusal({"sample_text": empty}) == UNSAVED_RERUN, empty
    assert unsaved_rerun_refusal({"sample_text": "ISA*00~"}) is None

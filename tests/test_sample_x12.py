"""X12 EDI modeled on a sample: everything copied, except what the config names.

The sample files are the masking tool's demo samples (an 837P, an 837P with a
dependent patient) and an 834; the master config is a masking config
(`maskSegments`, masking algorithm names), which is read, proposed from, and
never changed: what is saved is a new `genSegments` config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdg.writers import samples
from sdg.writers.samples import x12 as es
from sdg.writers.snip import validate

pytestmark = pytest.mark.db

HERE = Path(__file__).resolve().parent / "fixtures" / "edi"


def _text(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules():
    return samples.algorithm_rules()


def _rows(sample_name: str, rules, master: str | None = "master-837-masking.json"):
    sample = es.read_sample(_text(sample_name))
    entries = es.read_config(_text(master)) if master else []
    return sample, es.propose(sample, entries, rules)


def _by_label(rows) -> dict[str, dict]:
    return {r["label"]: r for r in rows}


def _segs(text: str, tag: str) -> list[list[str]]:
    return [s.strip().split("*") for s in text.split("~") if s.strip().startswith(tag + "*")]


def _accept_all(rows):
    """What a person does who takes every proposal and every suggestion."""
    for r in rows:
        r["algorithm"] = r["algorithm"] or r["suggested"]
    return es.read_config(es.write_config(rows))


def _new_errors(text: str, sample: str, up_to: int) -> list:
    """SNIP errors the written file has that the sample it copies does not."""
    def rules_failing(t):
        return {(lv["level"], f["rule"]) for lv in validate(t)["levels"]
                if lv["level"] <= up_to for f in lv["findings"] if f["severity"] == "error"}
    return sorted(rules_failing(text) - rules_failing(sample))


def _claims(members: int = 3) -> list[dict]:
    """Claim lines keyed by element: member m has two claims of m+1 lines."""
    out = []
    for m in range(members):
        for c in range(2):
            for line in range(m + 1):
                out.append({"MEMBER_ID": f"MBR{m:04d}", "LAST_NAME": f"LAST{m}",
                            "CLAIM_ID": f"CLM{m}{c}", "CHARGE_AMOUNT": 10.25 + line,
                            "SERVICE_DATE": f"2026-03-0{line + 1}"})
    return out


# ---------------------------------------------------------------------------
# Reading and proposing
# ---------------------------------------------------------------------------

def test_a_masking_config_is_read_as_a_master(rules):
    entries = es.read_config(_text("master-837-masking.json"))
    assert entries and all(e.fields for e in entries)
    dtp = next(e for e in entries if e.segment == "DTP" and (1, "472") in e.qualifiers)
    assert (2, "D8") in dtp.qualifiers, "DTP's second qualifier is the qualifier field"
    assert dtp.fields[0][0] == 3, "position 4 counts the segment id: DTP03"
    scoped = next(e for e in entries if e.segment == "N3" and e.loop)
    assert scoped.loop[0] == "NM1"


def test_every_position_of_the_sample_is_proposed_from_the_master(rules):
    _sample, rows = _rows("claim.837", rules)
    by = _by_label(rows)
    # The config's masking names, read as the element their place holds.
    assert by["NM1*IL NM103"]["algorithm"] == "LAST_NAME"
    assert by["NM1*IL NM103"]["config_name"] == "P2_LASTNAME2_AL"
    assert by["NM1*IL NM104"]["algorithm"] == "FIRST_NAME"
    assert by["NM1*IL NM109"]["algorithm"] == "MEMBER_ID"
    assert by["DMG (in NM1*IL) DMG02"]["algorithm"] == "DOB"
    assert by["N3 (in NM1*IL) N301"]["algorithm"] == "STREET_1"
    assert by["DTP*472*D8 DTP03"]["algorithm"] == "SERVICE_DATE"
    # A provider's tax ID is not a member's SSN, whatever the masking name says.
    assert by["REF*EI REF02"]["config_name"] == "P5_TAX_SSN_AL"
    assert by["REF*EI REF02"]["algorithm"] is None
    # Not in the config: copied, with what it could be suggested.
    assert by["SV1 SV101-2"]["algorithm"] is None
    assert by["SV1 SV101-2"]["suggested"] == "PROCEDURE_CODE"
    # A provider's address is the provider's: no member element suggested.
    assert by["N4 (in NM1*85) N403"]["suggested"] is None
    # Numbers the structure carries are not positions anybody chooses.
    assert not any(r["label"].startswith(("HL ", "LX ", "ST ", "SE ")) for r in rows)
    assert "CLM CLM02" not in by


def test_no_master_still_lists_every_position_with_suggestions(rules):
    _sample, rows = _rows("members.834", rules, master=None)
    assert rows and all(r["algorithm"] is None for r in rows)
    by = _by_label(rows)
    assert by["NM1*IL NM103"]["suggested"] == "LAST_NAME"
    assert by["NM1*IL NM109"]["suggested"] == "SSN", "an 834 names the member by SSN (NM108 34)"
    assert by["DMG (in NM1*IL) DMG02"]["suggested"] == "DOB"


def test_a_saved_config_is_gen_segments_and_reads_back_the_same(rules):
    _sample, rows = _rows("claim.837", rules)
    config = es.write_config(rows, transaction="837", based_on="master-837-masking.json")
    assert "genSegments" in config and "maskSegments" not in config
    assert config["based_on"] == "master-837-masking.json"
    again = es.read_config(json.dumps(config))
    sample = es.read_sample(_text("claim.837"))
    assert [r["algorithm"] for r in es.propose(sample, again, rules)] == \
           [r["algorithm"] for r in rows]


def test_samples_that_cannot_be_used_say_why():
    with pytest.raises(es.SampleError, match="not an X12"):
        es.read_sample("hello")
    with pytest.raises(es.SampleError, match="genSegments"):
        es.read_config({"segments": []})


def test_the_format_is_read_off_the_sample():
    assert samples.detect(_text("claim.837")) is es
    assert samples.detect("﻿" + _text("members.834")) is es
    with pytest.raises(samples.SampleError, match="not a format a file can be modeled on"):
        samples.detect("hello")
    with pytest.raises(samples.SampleError, match="No sample format"):
        samples.module("nope")


# ---------------------------------------------------------------------------
# What repeats and what follows is the profile's, not the code's
# ---------------------------------------------------------------------------

@pytest.fixture()
def profile_dir(tmp_path, monkeypatch):
    """A copy of the shipped profiles to edit; the shipped files are not touched."""
    import shutil

    from sdg.writers.samples import profile

    for p in profile.DIR.glob("*.json"):
        shutil.copy(p, tmp_path / p.name)
    monkeypatch.setattr(profile, "DIR", tmp_path)
    return tmp_path


def _edit(directory: Path, name: str, change) -> None:
    path = directory / f"{name}.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    change(body)
    path.write_text(json.dumps(body), encoding="utf-8")


def test_a_derived_position_is_the_profiles_to_say(rules, profile_dir):
    sample_text = _text("claim.837")
    sample_totals = {s[2] for s in _segs(sample_text, "CLM")}
    sample, rows = _rows("claim.837", rules)
    # CLM itself left to the sample: CLM02 is then the sum rule's or the sample's.
    entries = [e for e in _accept_all(rows) if e.segment != "CLM"]

    summed ="".join(es.stream(sample, entries, _claims(), rules, seed="t"))
    assert not {s[2] for s in _segs(summed, "CLM")} <= sample_totals, "CLM02 is summed"

    _edit(profile_dir, "x12-837",
          lambda b: b.update(derive=[d for d in b["derive"] if d["op"] != "sum"]))
    sample = es.read_sample(sample_text)
    kept = "".join(es.stream(sample, entries, _claims(), rules, seed="t"))
    assert {s[2] for s in _segs(kept, "CLM")} <= sample_totals, "no sum rule: the sample's CLM02"


def test_a_new_kind_of_file_is_a_profile(rules, profile_dir):
    other = _text("members.834").replace("ST*834*", "ST*999*")
    with pytest.raises(es.SampleError, match="modeled on an 834, an 835 or an 837"):
        es.read_sample(other)

    (profile_dir / "x12-999.json").write_text(json.dumps({
        "extends": "x12", "kind": "999", "subject": "members",
        "counts": {"people": "INS"}, "summary": "{people} people",
        "units": [{"name": "person", "starts": "INS", "row": "count"}],
    }), encoding="utf-8")
    sample = es.read_sample(other)
    assert es.facts(sample)[1].endswith("people") and es.subject("999") == "members"
    rows = es.propose(sample, [], rules)
    text = "".join(es.stream(sample, _accept_all(rows), [{"LAST_NAME": "ONE"}, {}], rules))
    assert len(_segs(text, "INS")) == 2


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_an_837_keeps_the_sample_and_changes_only_the_configured_positions(rules):
    sample, rows = _rows("claim.837", rules)
    entries = es.read_config(es.write_config(rows))       # the master's proposals only
    text = "".join(es.stream(sample, entries, _claims(), rules, seed="t"))
    original = _text("claim.837")

    # The envelope, header and billing provider are the sample's, as written --
    # but for the interchange's own numbers: this file's control number (ISA13,
    # GS06, GE02, IEA02) and ISA15 T, test data, never the sample's P.
    from sdg.writers.edi_formats.registry import interchange_number

    number = interchange_number("t")
    for line in original.split("~")[:12]:
        line = line.strip()
        if line.startswith("ISA"):
            isa = line.split("*")
            isa[13], isa[15] = f"{number:09d}", "T"
            line = "*".join(isa)
        elif line.startswith("GS"):
            gs = line.split("*")
            gs[6] = str(number)
            line = "*".join(gs)
        assert line in text, f"{line} not copied"
    assert f"GE*1*{number}~" in text and f"IEA*1*{number:09d}~" in text
    subs = _segs(text, "NM1")
    members = [s for s in subs if s[1] == "IL"]
    assert [m[9] for m in members] == ["MBR0000", "MBR0001", "MBR0002"]
    assert [m[3] for m in members] == ["LAST0", "LAST1", "LAST2"]
    # A configured date takes the record's, written as the sample writes it.
    assert {s[3] for s in _segs(text, "DTP") if s[1] == "472"} == {"20260301", "20260302", "20260303"}
    # Positions the config does not name are the sample's: procedure codes.
    assert {s[1] for s in _segs(text, "SV1")} <= {s[1] for s in _segs(original, "SV1")}

    # Structure: HL numbered in order with parents, LX per claim, CLM02 its
    # lines, SE counting ST..SE, and SNIP levels 1 to 4 clean.
    hls = _segs(text, "HL")
    assert [h[1] for h in hls] == [str(i) for i in range(1, len(hls) + 1)]
    assert all(h[2] == "1" for h in hls if h[3] == "22")
    claims = text.split("CLM*")[1:]
    for chunk in claims:
        lx = [s[1] for s in _segs("CLM*" + chunk.split("HL*")[0], "LX")]
        assert lx == [str(i) for i in range(1, len(lx) + 1)]
    se = _segs(text, "SE")[0]
    st_to_se = text.split("ST*", 1)[1].split("SE*", 1)[0].count("~") + 1
    assert int(se[1]) == st_to_se
    report = validate(text)
    failing = [(lv["level"], lv["findings"][:2]) for lv in report["levels"]
               if lv["level"] <= 4 and lv["errors"]]
    assert not failing, failing


def test_claim_totals_add_up_their_lines_once_lines_are_generated(rules):
    sample, rows = _rows("claim.837", rules)
    entries = _accept_all(rows)
    text = "".join(es.stream(sample, entries, _claims(), rules, seed="t"))
    body = text.split("CLM*")[1:]
    for chunk in body:
        claim = "CLM*" + chunk.split("HL*")[0]
        total = sum(float(s[2]) for s in _segs(claim, "SV1"))
        assert float(_segs(claim, "CLM")[0][2]) == pytest.approx(total)
    assert not [lv for lv in validate(text)["levels"] if lv["level"] == 3 and lv["errors"]]


def test_a_dependent_patient_loop_keeps_its_parent(rules):
    sample, rows = _rows("claim-dependent.837", rules)
    text = "".join(es.stream(sample, _accept_all(rows), _claims(2), rules, seed="t"))
    hls = _segs(text, "HL")
    ids = {h[1]: h for h in hls}
    for h in hls:
        if h[3] == "23":
            assert ids[h[2]][3] == "22", "a patient's parent is its subscriber"
    assert sum(1 for h in hls if h[3] == "23") == 2
    # The sample's own DTP*435*DT is copied as it is; nothing new fails.
    assert not _new_errors(text, sample.text, 4)


def test_an_834_writes_one_member_loop_per_member(rules):
    sample, rows = _rows("members.834", rules, master=None)
    entries = _accept_all(rows)
    records = [{"LAST_NAME": f"Person{i}", "SSN": f"900-00-{i:04d}"} for i in range(7)]
    text = "".join(es.stream(sample, entries, records, rules, seed="t"))
    ins = _segs(text, "INS")
    assert len(ins) == 7
    names = [s for s in _segs(text, "NM1") if s[1] == "IL"]
    assert [n[3] for n in names] == [f"Person{i}" for i in range(7)]
    assert [n[9] for n in names] == [f"90000{i:04d}" for i in range(7)], "SSN as the sample: digits"
    se = _segs(text, "SE")[0]
    assert int(se[1]) == text.split("ST*", 1)[1].split("SE*", 1)[0].count("~") + 1
    # Relationship codes are X12's (18, 19, 01), and INS01 follows them.
    for seg in ins:
        assert seg[2] in {"18", "01", "19", "53"}, seg
        assert (seg[1] == "Y") == (seg[2] == "18"), seg
    assert not _new_errors(text, sample.text, 4)


def test_the_same_records_and_seed_write_the_same_file(rules):
    sample, rows = _rows("claim.837", rules)
    entries = _accept_all(rows)
    one = "".join(es.stream(sample, entries, _claims(), rules, seed="t"))
    two = "".join(es.stream(sample, entries, _claims(), rules, seed="t"))
    assert one == two


# ---------------------------------------------------------------------------
# Through the API: read, save as, write for a pattern
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    return TestClient(app)


@pytest.fixture()
def made(client):
    """Configs this test saved, removed afterwards by those ids only."""
    ids: list[int] = []
    yield ids
    for config_id in ids:
        client.delete(f"/api/samples/configs/{config_id}")


def _ok(response):
    assert response.status_code == 200, response.text[:2000]
    return response


def test_read_save_as_and_write_an_837_for_a_pattern(client, made):
    from tests.test_pattern_routes import FILTERS, _member_ids

    master = _text("master-837-masking.json")
    read = _ok(client.post("/api/samples/read", json={
        "sample": _text("claim.837"), "sampleName": "claim.837",
        "master": master, "masterName": "master-837-masking.json"})).json()
    assert read["kind"] == "837" and read["counts"]["claims"] == 2
    assert read["format"] == "x12" and read["subject"] == "claims"
    assert read["validation"] == "snip" and "2 claims" in read["summary"]
    assert "LAST_NAME" in read["algorithms"] and "STREET_1" in read["algorithms"]
    rows = read["rows"]
    for r in rows:
        r["algorithm"] = r["algorithm"] or r["suggested"]

    name = "test edi sample 837 (made by tests)"
    saved = _ok(client.post("/api/samples/configs", json={
        "name": name, "sample": _text("claim.837"), "sampleName": "claim.837",
        "basedOn": "master-837-masking.json", "rows": rows})).json()
    made.append(saved["id"])
    again = client.post("/api/samples/configs", json={
        "name": name, "sample": _text("claim.837"), "rows": rows})
    assert again.status_code == 409, "a name already taken is refused, never overwritten"
    listed = {c["id"]: c for c in _ok(client.get("/api/samples/configs")).json()["configs"]}
    assert listed[saved["id"]]["based_on"] == "master-837-masking.json"
    assert listed[saved["id"]]["positions"] > 0
    assert (listed[saved["id"]]["format"], listed[saved["id"]]["kind"],
            listed[saved["id"]]["subject"]) == ("x12", "837", "claims")
    assert _text("master-837-masking.json") == master, "the master is never changed"

    # Read the saved config again: its sample comes with it.
    reread = _ok(client.post("/api/samples/read", json={"configId": saved["id"]})).json()
    assert [r["algorithm"] for r in reread["rows"]] == [r["algorithm"] for r in rows]

    analysis = _ok(client.post("/api/claims/analyze", json=FILTERS)).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    body = {"format": f"sample:{saved['id']}", "rows": 40,
            "pattern": {"profile": pattern, "domain": "claims", "filters": FILTERS, "k": None,
                        "cluster_id": pattern["cluster_id"]}}
    text = _ok(client.post("/api/export", json=body)).text
    members = {s[9] for s in _segs(text, "NM1") if s[1] == "IL"}
    held = _member_ids(pattern)
    assert members and (held is None or members <= held), "claims for people outside the pattern"
    assert "NM1*85*2*RIVERSIDE FAMILY CLINIC" in text, "the sample's billing provider is kept"
    check = _ok(client.post("/api/edi/snip/check", json={"export": body})).json()
    assert not [lv for lv in check["levels"] if lv["level"] <= 4 and lv["errors"]]


def test_an_834_from_a_pattern_is_new_members_with_its_values(client, made):
    from tests.test_pattern_routes import FILTERS, _sexes

    read = _ok(client.post("/api/samples/read", json={
        "sample": _text("members.834"), "sampleName": "members.834"})).json()
    assert read["kind"] == "834"
    rows = [{**r, "algorithm": r["suggested"]} for r in read["rows"]]
    saved = _ok(client.post("/api/samples/configs", json={
        "name": "test edi sample 834 (made by tests)", "sample": _text("members.834"),
        "rows": rows})).json()
    made.append(saved["id"])

    analysis = _ok(client.post("/api/patterns/analyze", json={"filters": FILTERS, "k": 3})).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    body = {"format": f"sample:{saved['id']}", "members": 12,
            "pattern": {"profile": pattern, "domain": "members", "filters": FILTERS, "k": 3,
                        "cluster_id": pattern["cluster_id"]}}
    text = _ok(client.post("/api/export", json=body)).text
    assert len(_segs(text, "INS")) == 12
    sexes = {s[3] for s in _segs(text, "DMG")}
    if _sexes(pattern):
        assert sexes <= _sexes(pattern)

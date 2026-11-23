"""FHIR modeled on a sample: the same engine as X12, a JSON reader and FHIR profiles.

Samples are built from the masking tool's published FHIR examples
(edi-mask samples/fhir), linked into Bundles; the master is its FHIR masking
config, comments and all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sdg.writers import samples
from sdg.writers.samples import fhir as fs

pytestmark = pytest.mark.db

HERE = Path(__file__).resolve().parent / "fixtures" / "fhir"


def _text(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules():
    return samples.algorithm_rules()


def _accept_all(fmt, rows):
    for r in rows:
        r["algorithm"] = r["algorithm"] or r["suggested"]
    return fmt.read_config(fmt.write_config(rows))


def _claims(members: int = 2) -> list[dict]:
    """Claim lines: member m has two claims of m+1 lines."""
    out = []
    for m in range(members):
        for c in range(2):
            for line in range(m + 1):
                out.append({"MEMBER_ID": f"MBR{m:04d}", "LAST_NAME": f"LAST{m}", "SEX": "F",
                            "CLAIM_ID": f"CLM{m}{c}", "CHARGE_AMOUNT": 10.25 + line,
                            "SERVICE_DATE": f"2026-03-0{line + 1}", "PROCEDURE_CODE": "99213"})
    return out


def _resources(text: str, kind: str | None = None) -> list[dict]:
    doc = json.loads(text)
    assert doc["resourceType"] == "Bundle"
    return [e["resource"] for e in doc["entry"]
            if kind is None or e["resource"]["resourceType"] == kind]


# ---------------------------------------------------------------------------
# Reading and proposing
# ---------------------------------------------------------------------------

def test_the_format_and_kind_are_read_off_the_sample():
    for name, kind in (("claims-bundle.json", "claims"), ("members-bundle.json", "members"),
                       ("patient.json", "members")):
        fmt = samples.detect(_text(name))
        assert fmt is fs
        assert fs.read_sample(_text(name)).kind == kind, name
    assert fs.subject("claims") == "claims" and fs.subject("members") == "members"
    with pytest.raises(samples.SampleError, match="fit no FHIR profile"):
        fs.read_sample(json.dumps({"resourceType": "Medication", "id": "x"}))
    with pytest.raises(samples.SampleError, match="not JSON"):
        fs.read_sample("{ nope")


def test_the_masking_config_with_its_comments_is_read_as_a_master():
    entries = fs.read_config(_text("master-fhir-masking.json"))
    assert len(entries) > 100
    phone = [e for e in entries if e.type == "Patient" and e.path == "telecom[*].value"]
    assert {dict(e.where)["system"] for e in phone} == {"phone", "fax", "email"}
    with pytest.raises(samples.SampleError, match="genResources"):
        fs.read_config({"maskSegments": []})


def test_every_place_is_proposed_from_the_master_and_where_it_sits(rules):
    sample = fs.read_sample(_text("claims-bundle.json"))
    rows = fs.propose(sample, fs.read_config(_text("master-fhir-masking.json")), rules)
    by = {r["label"]: r for r in rows}
    assert by["Patient name[*].family"]["algorithm"] == "LAST_NAME"
    assert by["Patient name[*].family"]["config_name"] == "lastName"
    assert by["Patient birthDate"]["algorithm"] == "DOB"
    assert by["Patient gender"]["suggested"] == "SEX"
    assert by["Claim item[*].productOrService.coding[*].code"]["suggested"] == "PROCEDURE_CODE"
    # Where a sibling says what a value is, it is part of the place.
    assert by["Patient telecom[*].value (system=phone)"]["config_name"] == "phone"
    # Not the member's: an organization's name is suggested nothing, whatever the master says.
    org = by["Organization name"]
    assert org["config_name"] == "orgName" and org["algorithm"] is None and org["suggested"] is None
    # What the structure carries is nobody's choice: ids, references, item numbers.
    assert not [r for r in rows if r["path"].endswith(("id", "reference", ".sequence"))
                or r["path"].startswith("text.")]


def test_a_saved_config_is_gen_resources_and_reads_back_the_same(rules):
    sample = fs.read_sample(_text("claims-bundle.json"))
    rows = fs.propose(sample, fs.read_config(_text("master-fhir-masking.json")), rules)
    config = fs.write_config(rows, based_on="master")
    assert config["genResources"] and fs.positions(config) == sum(1 for r in rows if r["algorithm"])
    again = fs.propose(sample, fs.read_config(json.dumps(config)), rules)
    assert [r["algorithm"] for r in again] == [r["algorithm"] for r in rows]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_claims_keep_the_sample_and_link_every_resource_to_its_member(rules):
    sample = fs.read_sample(_text("claims-bundle.json"))
    entries = _accept_all(fs, fs.propose(sample, [], rules))
    text = "".join(fs.stream(sample, entries, _claims(3), rules, seed="t"))
    resources = _resources(text)

    ids = [f"{r['resourceType']}/{r['id']}" for r in resources]
    assert len(ids) == len(set(ids)), "every resource written has its own id"
    assert _resources(text, "Organization")[0]["id"] == "hl7", "the head is the sample's"
    patients = _resources(text, "Patient")
    coverages = _resources(text, "Coverage")
    claims = _resources(text, "Claim")
    assert len(patients) == 3 and len(coverages) == 3 and len(claims) == 6

    # References follow the new ids: a member's coverage and claims point at that member.
    by_member = {p["identifier"][0]["value"]: f"Patient/{p['id']}" for p in patients}
    assert set(by_member) == {"MBR0000", "MBR0001", "MBR0002"}
    for c in coverages:
        assert c["beneficiary"]["reference"] in by_member.values()
    coverage_of = {c["beneficiary"]["reference"]: f"Coverage/{c['id']}" for c in coverages}
    for claim in claims:
        member = claim["identifier"][0]["value"][3]          # CLM<m><c>
        assert claim["patient"]["reference"] == by_member[f"MBR000{member}"]
        assert claim["insurance"][0]["coverage"]["reference"] == coverage_of[claim["patient"]["reference"]]
        # Items numbered from 1, the total their net added up, the sample's codes replaced.
        assert [i["sequence"] for i in claim["item"]] == list(range(1, int(member) + 2))
        assert claim["total"]["value"] == pytest.approx(sum(i["net"]["value"] for i in claim["item"]))
        assert {i["productOrService"]["coding"][0]["code"] for i in claim["item"]} == {"99213"}
        assert isinstance(claim["item"][0]["net"]["value"], float), "a number stays a number"
    for p in patients:
        assert p["gender"] == "female", "SEX F written as FHIR codes it"
        assert "text" not in p, "the sample's narrative is not the member's"
        assert all(a["text"].startswith(" ".join(a["line"]) + ", " + a["city"])
                   for a in p["address"]), "an address's text says the address written"
    # Full URLs follow the ids.
    doc = json.loads(text)
    for e in doc["entry"]:
        r = e["resource"]
        assert e["fullUrl"].endswith(f"{r['resourceType']}/{r['id']}")


def test_a_single_resource_sample_writes_a_bundle_of_members(rules):
    sample = fs.read_sample(_text("patient.json"))
    entries = _accept_all(fs, fs.propose(sample, [], rules))
    records = [{"LAST_NAME": f"Person{i}", "MEMBER_ID": f"M{i}"} for i in range(5)]
    text = "".join(fs.stream(sample, entries, records, rules, seed="t"))
    patients = _resources(text, "Patient")
    assert [p["name"][0]["family"] for p in patients] == [f"Person{i}" for i in range(5)]
    assert json.loads(text)["type"] == "collection"


def test_a_name_or_address_text_says_the_parts_written(rules):
    """Where the sample has a HumanName or Address text, it is said again from
    the parts written; where it has none, none is made. Never proposed."""
    sample = fs.read_sample(_text("members-bundle.json"))
    rows = fs.propose(sample, fs.read_config(_text("master-fhir-masking.json")), rules)
    assert not any(r["label"].endswith(".text") for r in rows)
    entries = _accept_all(fs, rows)
    records = [{"LAST_NAME": "SMITH", "FIRST_NAME": "ANA", "MEMBER_ID": "M1"}]
    patient = _resources("".join(fs.stream(sample, entries, records, rules, seed="t")),
                         "Patient")[0]
    address = patient["address"][0]
    assert address["text"] == (f"{' '.join(address['line'])}, {address['city']}, "
                               f"{address['district']}, {address['state']} "
                               f"{address['postalCode']}")
    assert "Erewhon" not in address["text"]
    assert all("text" not in name for name in patient["name"]), "the sample has none"
    assert fs._said({"given": ["Ana", "B"], "family": "Smith"},
                    "{prefix} {given} {family} {suffix}") == "Ana B Smith"
    assert fs._said({"line": ["1 Main St"], "state": "GA"},
                    "{line}, {city}, {district}, {state} {postalCode}") == "1 Main St, GA"


def test_a_large_file_is_one_valid_bundle_across_chunks(rules):
    sample = fs.read_sample(_text("members-bundle.json"))
    entries = _accept_all(fs, fs.propose(sample, [], rules))
    records = [{"MEMBER_ID": f"M{i:05d}"} for i in range(700)]
    pieces = list(fs.stream(sample, entries, records, rules, seed="t"))
    assert len(pieces) > 3, "written in pieces, not held whole"
    resources = _resources("".join(pieces))
    assert len(resources) == 1 + 700 * 2


def test_the_same_records_and_seed_write_the_same_file(rules):
    sample = fs.read_sample(_text("claims-bundle.json"))
    entries = _accept_all(fs, fs.propose(sample, [], rules))
    one = "".join(fs.stream(sample, entries, _claims(), rules, seed="t"))
    assert one == "".join(fs.stream(sample, entries, _claims(), rules, seed="t"))


def test_x12_fhir_hl7_and_cda_run_on_one_engine_and_differ_by_profile():
    """The requirement: one framework; formats differ in config, readers in syntax."""
    import inspect

    from sdg.writers.samples import engine, profile, segments

    for reader in (fs, samples.x12, samples.hl7v2, samples.cda, segments):
        source = inspect.getsource(reader)
        assert "engine.stream(" in source or reader is segments
        for op in ("sequence", "index", "sum", "rename"):
            assert f'"{op}"' not in source and f"'{op}'" not in source, (reader.__name__, op)
    kinds = {p.stem for p in profile.DIR.glob("*.json")}
    assert {"x12-834", "x12-837", "fhir-members", "fhir-claims", "hl7v2-adt",
            "hl7v2-dft", "hl7v2-oru", "cda-document", "cda-ccd"} <= kinds
    assert all(hasattr(engine, n) for n in ("stream", "decide", "fit"))


# ---------------------------------------------------------------------------
# Through the API, for a pattern's members
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


def test_read_save_as_and_write_fhir_claims_for_a_pattern(client, made):
    from tests.test_pattern_routes import FILTERS, _member_ids

    read = _ok(client.post("/api/samples/read", json={
        "sample": _text("claims-bundle.json"), "sampleName": "claims-bundle.json",
        "master": _text("master-fhir-masking.json"),
        "masterName": "master-fhir-masking.json"})).json()
    assert (read["format"], read["kind"], read["subject"]) == ("fhir", "claims", "claims")
    assert read["validation"] == "fhir" and "1 claims" in read["summary"]
    rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
    saved = _ok(client.post("/api/samples/configs", json={
        "name": "test fhir sample claims (made by tests)", "sample": _text("claims-bundle.json"),
        "sampleName": "claims-bundle.json", "basedOn": "master-fhir-masking.json",
        "rows": rows})).json()
    made.append(saved["id"])
    listed = {c["id"]: c for c in _ok(client.get("/api/samples/configs")).json()["configs"]}
    assert (listed[saved["id"]]["format"], listed[saved["id"]]["extension"]) == ("fhir", ".json")

    analysis = _ok(client.post("/api/claims/analyze", json=FILTERS)).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    body = {"format": f"sample:{saved['id']}", "rows": 40,
            "pattern": {"profile": pattern, "domain": "claims", "filters": FILTERS, "k": None,
                        "cluster_id": pattern["cluster_id"]}}
    response = _ok(client.post("/api/export", json=body))
    assert response.headers["content-type"].startswith("application/fhir+json")
    resources = _resources(response.text)
    members = {r["identifier"][0]["value"] for r in resources if r["resourceType"] == "Patient"}
    held = _member_ids(pattern)
    assert members and (held is None or members <= held), "claims for people outside the pattern"
    refused = client.post("/api/edi/snip/check", json={"export": body})
    assert refused.status_code == 422, "SNIP is for X12 only"
    report = _ok(client.post("/api/samples/check", json={"export": body})).json()
    assert report["passed"], [f for lv in report["levels"] for f in lv["findings"]][:5]
    assert [lv["level"] for lv in report["levels"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Checking a written file
# ---------------------------------------------------------------------------

def test_a_written_file_passes_the_fhir_checks(rules):
    from sdg.writers.samples import profile

    sample = fs.read_sample(_text("claims-bundle.json"))
    entries = _accept_all(fs, fs.propose(sample, [], rules))
    text = "".join(fs.stream(sample, entries, _claims(3), rules, seed="t"))
    report = fs.check(text, profile.for_kind("fhir", "claims"))
    assert report["passed"], [f for lv in report["levels"] for f in lv["findings"]][:5]
    assert not any(lv["errors"] for lv in report["levels"])
    # Warnings only for references the sample itself makes outside its Bundle.
    sample_targets = {"Endpoint/example", "Organization/1", "Practitioner/example"}
    for f in report["levels"][1]["findings"]:
        assert any(t in f["message"] for t in sample_targets), f["message"]


def test_the_checks_find_what_is_wrong():
    from sdg.writers.samples import profile

    doc = json.loads(_text("claims-bundle.json"))
    patient = doc["entry"][1]["resource"]
    patient["gender"] = "F"                                   # not FHIR's code
    patient["birthDate"] = "1974-13-45"                       # not a date
    doc["entry"][3]["resource"]["patient"]["reference"] = "Patient/nobody"
    doc["entry"].append(copy.deepcopy(doc["entry"][2]))       # a Coverage twice
    report = fs.check(json.dumps(doc), profile.for_kind("fhir", "claims"))
    assert not report["passed"]
    rules_failing = {f["rule"] for lv in report["levels"] for f in lv["findings"]}
    assert {"code:gender", "reference-resolves", "id-unique"} <= rules_failing
    assert report["levels"][0]["errors"] >= 1, "the bad date is a structure error"


# ---------------------------------------------------------------------------
# Optional and repeating places (a config's vary)
# ---------------------------------------------------------------------------

def test_a_place_is_in_some_patients_and_not_others_and_repeats(rules):
    from sdg.writers.samples import engine, profile

    sample = fs.read_sample(_text("members-bundle.json"))
    places = {p["at"]: p for p in fs.structure(sample)}
    assert places["Patient telecom[*]"]["repeats"] and places["Patient address[*]"]["count"] >= 1
    entries = _accept_all(fs, fs.propose(sample, [], rules))
    vary = engine.read_vary({"vary": [
        {"at": "Patient telecom[*]", "present": 60, "min": 1, "max": 3},
        {"at": "Patient address[*]", "present": 50}]})
    records = [{"MEMBER_ID": f"M{i}", "LAST_NAME": f"L{i}"} for i in range(60)]
    text = "".join(fs.stream(sample, entries, records, rules, seed="t", vary=vary))
    patients = _resources(text, "Patient")
    phones = [len(p.get("telecom", [])) for p in patients]
    assert set(phones) == {0, 1, 2, 3}, "left out, once, or up to three times"
    assert 20 <= phones.count(0) <= 36
    addressed = sum("address" in p for p in patients)
    assert 18 <= addressed <= 42
    # A copy past the sample's is a value of its own, not the sample's again.
    three = next(p for p in patients if len(p.get("telecom", [])) == 3)
    values = [t.get("value") for t in three["telecom"] if t.get("value")]
    assert len(values) == len(set(values))
    assert text == "".join(fs.stream(sample, entries, records, rules, seed="t", vary=vary))
    report = fs.check(text, profile.for_kind("fhir", sample.kind))
    assert not any(lv["errors"] for lv in report["levels"]), report


def test_what_the_records_repeat_is_not_offered_to_vary():
    sample = fs.read_sample(_text("claims-bundle.json"))
    places = {p["at"] for p in fs.structure(sample)}
    assert "Claim item[*]" not in places, "a claim's lines are as many as the records have"
    assert "Claim diagnosis[*]" in places or "Claim insurance[*]" in places


def test_a_vary_list_is_checked():
    from sdg.writers.samples import engine
    from sdg.writers.samples.base import SampleError

    assert engine.read_vary({"vary": [{"at": "Patient telecom[*]", "min": 2}]})[0].high == 2
    for bad in ({"at": ""}, {"at": "Patient name[*]", "present": 140},
                {"at": "Patient name[*]", "min": 3, "max": 1}):
        with pytest.raises(SampleError):
            engine.read_vary({"vary": [bad]})


def test_vary_is_saved_with_the_config_and_read_back(client, made):
    read = _ok(client.post("/api/samples/read", json={
        "sample": _text("members-bundle.json"), "sampleName": "members-bundle.json"})).json()
    assert any(p["at"] == "Patient telecom[*]" for p in read["structure"])
    rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
    vary = [{"at": "Patient telecom[*]", "present": 70, "min": 1, "max": 3}]
    body = {"name": "test fhir vary (made by tests)", "sample": _text("members-bundle.json"),
            "sampleName": "members-bundle.json", "rows": rows, "vary": vary}
    saved = _ok(client.post("/api/samples/configs", json=body)).json()
    made.append(saved["id"])
    again = _ok(client.post("/api/samples/read", json={"configId": saved["id"]})).json()
    assert again["vary"] == [{"at": "Patient telecom[*]", "present": 70.0, "min": 1, "max": 3}]
    bad = client.post("/api/samples/configs", json={
        **body, "name": "test fhir vary bad (made by tests)",
        "vary": [{"at": "Patient telecom[*]", "present": 150}]})
    assert bad.status_code == 422

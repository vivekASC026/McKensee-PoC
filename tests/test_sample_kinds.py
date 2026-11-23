"""Every kind of sample the masking tool ships, read and written by profiles alone.

The masking tool's samples folder has, besides the 834/837 and the FHIR,
HL7 and CDA files the first profiles were written for, an 835 remittance,
FHIR resources on their own (a Claim, a Coverage, an Encounter...) and HL7
ACK, MDM, ORM, SIU and VXU messages. Each is a profile in
`sdg/writers/samples/profiles/`; nothing here is a format's own code. Also:
the PHONE algorithm (a sample's phone places, written the way the sample
writes a phone) and a saved config changed in place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sdg.writers import samples
from sdg.writers.samples import engine
from sdg.writers.samples import fhir as fs
from sdg.writers.samples import hl7v2 as hs
from sdg.writers.samples import x12 as es
from sdg.writers.snip import validate

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _text(path: str) -> str:
    return (FIXTURES / path).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules():
    return samples.algorithm_rules()


def _written(fmt, sample, rules, records, master: str | None = None) -> str:
    """The file a person writes who takes every proposal and suggestion."""
    rows = fmt.propose(sample, fmt.read_config(master) if master else [], rules)
    for r in rows:
        r["algorithm"] = r["algorithm"] or r["suggested"]
    config = fmt.read_config(fmt.write_config(rows, transaction=sample.kind, version=sample.version))
    return "".join(fmt.stream(sample, config, records, rules, seed="kinds"))


def _claim_lines() -> list[dict]:
    """Two members: the first with one claim of two lines, the second one claim of one."""
    return [
        {"MEMBER_ID": "M1", "CLAIM_ID": "C1", "CHARGE_AMOUNT": 100.0, "SERVICE_DATE": "2026-03-01",
         "PROVIDER_ID": "P1"},
        {"MEMBER_ID": "M1", "CLAIM_ID": "C1", "CHARGE_AMOUNT": 50.0, "SERVICE_DATE": "2026-03-02",
         "PROVIDER_ID": "P1"},
        {"MEMBER_ID": "M2", "CLAIM_ID": "C2", "CHARGE_AMOUNT": 80.0, "SERVICE_DATE": "2026-03-05",
         "PROVIDER_ID": "P2"},
    ]


def _members(n: int = 3) -> list[dict]:
    return [{"MEMBER_ID": f"M{i}", "LAST_NAME": f"LAST{i}"} for i in range(n)]


# ---------------------------------------------------------------------------
# Every sample reads, as the kind its profile says
# ---------------------------------------------------------------------------

KINDS = {
    "edi/remit.835": ("x12", "835", "claims"),
    "edi/remit-dependent.835": ("x12", "835", "claims"),
    "fhir/one/claim-example.json": ("fhir", "claim", "claims"),
    "fhir/one/condition-example.json": ("fhir", "condition", "claims"),
    "fhir/one/coverage-example.json": ("fhir", "coverage", "members"),
    "fhir/one/encounter-example.json": ("fhir", "encounter", "claims"),
    "fhir/one/observation-example.json": ("fhir", "observation", "claims"),
    "fhir/one/organization-example.json": ("fhir", "organization", "claims"),
    "fhir/one/practitioner-example.json": ("fhir", "practitioner", "claims"),
    "fhir/one/relatedperson-example.json": ("fhir", "relatedperson", "members"),
    "hl7/ack_error.hl7": ("hl7v2", "ACK", "members"),
    "hl7/mdm_t02_document.hl7": ("hl7v2", "MDM", "members"),
    "hl7/orm_o01_order.hl7": ("hl7v2", "ORM", "claims"),
    "hl7/siu_s12_appt.hl7": ("hl7v2", "SIU", "claims"),
    "hl7/vxu_v04_immun.hl7": ("hl7v2", "VXU", "members"),
    # The ones read before, still read as they were.
    "fhir/members-bundle.json": ("fhir", "members", "members"),
    "fhir/claims-bundle.json": ("fhir", "claims", "claims"),
    "fhir/patient.json": ("fhir", "members", "members"),
}


@pytest.mark.parametrize("path", sorted(KINDS))
def test_every_sample_reads_as_its_kind_and_writes(path, rules):
    text = _text(path)
    fmt = samples.detect(text)
    sample = fmt.read_sample(text)
    assert (fmt.FORMAT, sample.kind, fmt.subject(sample.kind)) == KINDS[path]
    records = _members() if fmt.subject(sample.kind) == "members" else _claim_lines()
    written = _written(fmt, sample, rules, records)
    assert written.strip()


def test_a_bundle_still_wins_over_a_resource_on_its_own():
    """A Patient with its Coverage is members, not a Coverage file; a Coverage
    on its own is a Coverage file."""
    both = json.loads(_text("fhir/members-bundle.json"))
    assert fs.read_sample(json.dumps(both)).kind == "members"
    coverage = next(e["resource"] for e in both["entry"] if e["resource"]["resourceType"] == "Coverage")
    assert fs.read_sample(json.dumps(coverage)).kind == "coverage"


# ---------------------------------------------------------------------------
# The 835: amounts follow the lines generated
# ---------------------------------------------------------------------------

def _segs(text: str, tag: str) -> list[list[str]]:
    return [s.strip().split("*") for s in text.split("~") if s.strip().startswith(tag + "*")]


@pytest.mark.parametrize("name", ["remit.835", "remit-dependent.835"])
def test_an_835_pays_each_claim_its_lines_and_the_payment_adds_them_up(name, rules):
    sample_text = _text(f"edi/{name}")
    sample = es.read_sample(sample_text)
    text = _written(es, sample, rules, _claim_lines())

    claims = _segs(text, "CLP")
    assert [c[1] for c in claims] == ["C1", "C2"], "one claim payment per claim"
    lines = _segs(text, "SVC")
    assert [float(s[2]) for s in lines] == [100.0, 50.0, 80.0], "a line's charge is generated"
    first = _segs(sample_text, "SVC")[0]
    ratio = float(first[3]) / float(first[2])
    assert [float(s[3]) for s in lines] == pytest.approx([100 * ratio, 50 * ratio, 80 * ratio])
    assert float(claims[0][3]) == 150.0 and float(claims[1][3]) == 80.0, "CLP03 is the lines' charge"
    assert float(claims[0][4]) == pytest.approx(150 * ratio), "CLP04 is the lines' payment"
    bpr = _segs(text, "BPR")[0]
    assert float(bpr[2]) == pytest.approx(sum(float(c[4]) for c in claims)), "BPR02 is every payment"

    se = _segs(text, "SE")[0]
    body = [s for s in text.split("~") if s.strip()]
    st = next(i for i, s in enumerate(body) if s.strip().startswith("ST*"))
    end = next(i for i, s in enumerate(body) if s.strip().startswith("SE*"))
    assert int(se[1]) == end - st + 1, "SE01 counts ST through SE"

    def errors(t):
        return {(lv["level"], f["rule"]) for lv in validate(t)["levels"]
                for f in lv["findings"] if f["severity"] == "error"}
    assert errors(text) <= errors(sample_text), "no SNIP error the sample does not have"


# ---------------------------------------------------------------------------
# HL7: each message its own
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["ack_error", "mdm_t02_document", "vxu_v04_immun",
                                  "orm_o01_order", "siu_s12_appt"])
def test_each_new_hl7_message_type_writes_one_message_per_record(name, rules):
    sample = hs.read_sample(_text(f"hl7/{name}.hl7"))
    members = hs.subject(sample.kind) == "members"
    records = _members() if members else _claim_lines()
    text = _written(hs, sample, rules, records)
    headers = [line for line in re.split(r"[\r\n]+", text) if line.startswith("MSH")]
    assert len(headers) == (3 if members else 2), "a message per member, or per claim"
    controls = [h.split("|")[9] for h in headers]
    assert len(set(controls)) == len(controls), "every message has its own control id"


# ---------------------------------------------------------------------------
# PHONE
# ---------------------------------------------------------------------------

def test_phone_is_an_algorithm_and_a_masking_phone_reads_as_it(rules):
    assert "PHONE" in rules
    assert engine.hint("phone") == "PHONE" and engine.hint("P4_PHONE_AL") == "PHONE"
    assert engine.hint("P4_PHONE_EXT_AL") is None, "an extension is not a phone number"


@pytest.mark.parametrize("example, written", [
    ("", "(678) 555-0123"),
    ("6785550100", "6785550123"),
    ("678", "678"),
    ("5550100", "5550123"),
    ("(03) 5555 6473", "(67) 8555 0123"),
    ("(+1) 734-677-7777", "(+1) 678-555-0123"),
    ("+33 (237) 998327", "+36 (785) 550123"),
    ("555.555.0100", "678.555.0123"),
])
def test_a_phone_is_written_the_way_the_sample_writes_one(example, written):
    assert engine.fit("(678) 555-0123", example) == written


def test_a_phone_place_is_suggested_and_takes_the_samples_shape(rules):
    sample = fs.read_sample(_text("fhir/patient.json"))
    rows = fs.propose(sample, [], rules)
    phones = [r for r in rows if r["suggested"] == "PHONE"]
    assert phones and all("telecom" in r["label"] for r in phones)
    text = _written(fs, sample, rules, _members(2))
    values = re.findall(r'"system": "phone",\s*"value": "([^"]+)"', text)
    shape = re.sub(r"\d", "9", phones[0]["example"])
    assert values and re.sub(r"\d", "9", values[0]) == shape

    adt = hs.read_sample(_text("hl7/adt_a01.hl7"))
    labels = {r["label"]: r["suggested"] for r in hs.propose(adt, [], rules)}
    assert labels.get("PID-13.6") == "PHONE" and labels.get("PID-13.7") == "PHONE"


# ---------------------------------------------------------------------------
# A saved config changed in place, then removed
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    return TestClient(app)


def test_a_saved_config_is_changed_in_place_and_removed(client):
    import time

    sample = _text("edi/remit.835")
    read = client.post("/api/samples/read", json={"sample": sample, "sampleName": "remit.835"})
    assert read.status_code == 200, read.text
    rows = read.json()["rows"]
    for r in rows:
        r["algorithm"] = r["algorithm"] or r["suggested"]
    name = f"test kinds 835 {int(time.time())}"
    saved = client.post("/api/samples/configs", json={
        "name": name, "sample": sample, "sampleName": "remit.835", "basedOn": None,
        "rows": rows, "fromConfigId": None})
    assert saved.status_code == 200, saved.text
    config_id = saved.json()["id"]
    try:
        def summary():
            return next(c for c in client.get("/api/samples/configs").json()["configs"]
                        if c["id"] == config_id)
        before = summary()["positions"]
        # Read back from itself, one place set back to the sample's, renamed, saved.
        again = client.post("/api/samples/read", json={"configId": config_id}).json()["rows"]
        dropped = next(r for r in again if r["algorithm"])
        dropped["algorithm"] = None
        changed = client.put(f"/api/samples/configs/{config_id}", json={
            "name": name + " v2", "sample": None, "sampleName": None, "rows": again})
        assert changed.status_code == 200, changed.text
        after = summary()
        assert after["name"] == name + " v2" and after["positions"] == before - 1
        assert after["based_on"] is None, "what it was built from is kept, not itself"
        assert after["sample_name"] == "remit.835", "its sample is kept"
        clash = client.put(f"/api/samples/configs/{config_id}", json={
            "name": "", "sample": None, "sampleName": None, "rows": again})
        assert clash.status_code == 409
    finally:
        assert client.delete(f"/api/samples/configs/{config_id}").status_code == 200
    assert client.delete(f"/api/samples/configs/{config_id}").status_code == 404

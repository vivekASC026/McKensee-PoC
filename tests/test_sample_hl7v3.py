"""HL7 v3 messages modeled on a sample: the same XML reader as CDA, a profile per
interaction. Samples are the Open eHealth IPF example messages (Apache 2.0),
github.com/oehf/ipf tutorials/iheclient/src/main/resources/example-messages.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from sdg.writers import samples
from sdg.writers.samples import cda, engine, standard

pytestmark = pytest.mark.db

HERE = Path(__file__).resolve().parent / "fixtures" / "hl7v3"
V3 = "{urn:hl7-org:v3}"
PERSON = f"{V3}controlActProcess/{V3}subject/{V3}registrationEvent/{V3}subject1/{V3}patient"


def _text(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules():
    return samples.algorithm_rules()


def _standard(rules):
    found = standard.files("hl7v3-prpa-add")
    sample = cda.read_sample(found["sample"])
    rows = cda.propose(sample, cda.read_config(found["master"]), rules)
    return sample, rows


def _members(n: int) -> list[dict]:
    return [{"MEMBER_ID": f"M{i}", "LAST_NAME": f"PERSON{i}", "SEX": "MF"[i % 2],
             "DOB": f"1980-05-0{i + 1}"} for i in range(n)]


def test_patient_registry_messages_are_read_and_others_are_named():
    add = cda.read_sample(_text("PIXv3_FEED_REG_Maximal_Request.xml"))
    revise = cda.read_sample(_text("PIXv3_FEED_REV_Maximal_Request.xml"))
    assert samples.detect(_text("PIXv3_FEED_REG_Maximal_Request.xml")) is cda
    assert (add.kind, revise.kind) == ("PRPA_IN201301UV02", "PRPA_IN201302UV02")
    assert cda.subject(add.kind) == "members"
    assert cda.facts(add)[1] == "1 message · 1 patient · 3 ids · 2 names · 2 addresses"
    with pytest.raises(samples.SampleError, match="PRPA_IN201305UV02 fits no HL7 v3 profile"):
        cda.read_sample(_text("PDQv3.xml"))
    # A C-CDA is still a document, not a message.
    ccd = cda.read_sample(standard.files("cda-ccd")["sample"])
    assert ccd.kind == "CCD"


def test_the_standard_message_is_answered_everywhere(rules):
    sample, rows = _standard(rules)
    # As the screen counts: a place the config names with no algorithm, or a
    # suggestion not yet taken.
    open_rows = [r["label"] for r in rows if r["config_name"] and not r["algorithm"]
                 and not r["kept"]]
    assert not open_rows, open_rows
    assert not [r["label"] for r in rows if r["suggested"] and not r["algorithm"]]
    chosen = {r["label"].split(" (")[0].rsplit("/", 2)[-2:][-1]: r["algorithm"] for r in rows
              if r["algorithm"]}
    assert chosen["given"] == "FIRST_NAME" and chosen["family"] == "LAST_NAME"
    # The mother's name is somebody else's: copied, never proposed as the member's.
    mother = next(r for r in rows if "personalRelationship" in r["path"]
                  and r["path"].endswith("family"))
    assert mother["algorithm"] is None and mother["suggested"] is None


def test_a_message_per_member_each_its_own_and_the_checks_pass(rules):
    sample, rows = _standard(rules)
    entries = cda.read_config(cda.write_config(rows))
    text = "".join(cda.stream(sample, entries, _members(6), rules, seed="t"))
    docs = cda.documents(text)
    assert len(docs) == 6
    roots = [etree.fromstring(d.encode("utf-8")) for d in docs]
    assert {r.tag for r in roots} == {f"{V3}PRPA_IN201301UV02"}
    assert len({r.find(f"{V3}id").get("extension") for r in roots}) == 6, "a message id each"
    assert {r.find(f"{V3}creationTime").get("value") for r in roots} == {"20260630120000"}
    families = [r.find(f"{PERSON}/{V3}patientPerson/{V3}name/{V3}family").text for r in roots]
    assert families == [f"PERSON{i}" for i in range(6)]
    genders = [r.find(f"{PERSON}/{V3}patientPerson/{V3}administrativeGenderCode").get("code")
               for r in roots]
    assert genders == ["M", "F"] * 3
    ssn = roots[0].find(f"{PERSON}/{V3}patientPerson/{V3}asOtherIDs/{V3}id").get("extension")
    assert ssn != "100-09-1234", "the SSN is generated"
    # What routes the message is the sample's.
    assert roots[0].find(f"{V3}interactionId").get("extension") == "PRPA_IN201301UV02"
    report = cda.check(text, sample.profile)
    assert report["passed"], report
    assert report["summary"] == "6 messages; no errors."
    packed = b"".join(cda.package(iter([text]), sample.kind))
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert archive.namelist()[0] == "PRPA_IN201301UV02-00001.xml"


def test_the_checks_find_a_broken_message(rules):
    sample, rows = _standard(rules)
    entries = cda.read_config(cda.write_config(rows))
    doc = cda.documents("".join(cda.stream(sample, entries, _members(1), rules, seed="t")))[0]
    broken = doc.replace("<acceptAckCode", "<other", 1).replace(
        'administrativeGenderCode code="M"', 'administrativeGenderCode code="X"', 1)
    report = cda.check(broken + doc, sample.profile)
    found = {f["rule"] for level in report["levels"] for f in level["findings"]}
    assert not report["passed"]
    assert "required" in found and "id-unique" in found
    assert any(r.startswith("code:") for r in found)


def test_an_element_of_a_message_is_optional_and_repeats(rules):
    sample, rows = _standard(rules)
    entries = cda.read_config(cda.write_config(rows))
    person = "controlActProcess/subject/registrationEvent/subject1/patient/patientPerson"
    vary = engine.read_vary({"vary": [
        {"at": f"{person}/telecom", "present": 70, "min": 1, "max": 4},
        {"at": f"{person}/maritalStatusCode", "present": 50}]})
    places = {p["at"] for p in cda.structure(sample)}
    assert f"{person}/telecom" in places
    text = "".join(cda.stream(sample, entries, _members(40), rules, seed="t", vary=vary))
    roots = [etree.fromstring(d.encode("utf-8")) for d in cda.documents(text)]
    phones = [len(r.findall(f"{PERSON}/{V3}patientPerson/{V3}telecom")) for r in roots]
    assert {0, 1, 4} <= set(phones) and max(phones) == 4
    married = [r.find(f"{PERSON}/{V3}patientPerson/{V3}maritalStatusCode") is not None
               for r in roots]
    assert any(married) and not all(married)
    assert cda.check(text, sample.profile)["passed"]


def test_the_hl7v3_tab_offers_the_message_through_the_api():
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    client = TestClient(app)
    listed = client.get("/api/samples/defaults").json()["defaults"]
    assert {"hl7v3-prpa-add", "cda-ccd"} <= {d["id"] for d in listed if d["format"] == "cda"}
    read = client.post("/api/samples/read", json={"defaultId": "hl7v3-prpa-add"})
    assert read.status_code == 200, read.text
    body = read.json()
    assert (body["format"], body["kind"], body["subject"]) == ("cda", "PRPA_IN201301UV02",
                                                               "members")
    assert body["structure"]

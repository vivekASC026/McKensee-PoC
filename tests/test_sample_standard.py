"""The standard file of each format: FHIR, HL7 v2 and CDA (and X12) can be
written before the customer gives a sample, as the EDI tab's transaction sets
can. Each standard is a made-up sample and the master config whose places it
proposes; a person's changes are saved as a new config from it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from sdg.api.app import app
from sdg.writers import samples
from sdg.writers.samples import standard

client = TestClient(app)


def test_every_standard_file_reads_as_its_format_and_proposes_places():
    listed = standard.listing()
    assert {d["format"] for d in listed} == {"fhir", "hl7v2", "cda", "x12"}
    for entry in listed:
        found = standard.files(entry["id"])
        fmt = samples.detect(found["sample"])
        assert fmt.FORMAT == entry["format"], entry["id"]
        sample = fmt.read_sample(found["sample"])
        rows = fmt.propose(sample, fmt.read_config(found["master"]), samples.algorithm_rules())
        assert any(r.get("algorithm") for r in rows), f"{entry['id']}: nothing generated"
    assert standard.files("no-such") is None


def test_the_api_lists_them_and_reads_one_without_a_sample():
    listed = client.get("/api/samples/defaults").json()["defaults"]
    kinds = {d["id"]: d["format"] for d in listed}
    assert kinds["hl7-oru"] == "hl7v2" and kinds["cda-ccd"] == "cda"
    assert kinds["fhir-members"] == "fhir"

    read = client.post("/api/samples/read", json={"defaultId": "hl7-oru"}).json()
    assert (read["format"], read["kind"]) == ("hl7v2", "ORU")
    assert read["sampleName"] == "hl7-oru-r01.hl7" and read["basedOn"].startswith("standard")
    assert any(r["algorithm"] for r in read["rows"])

    missing = client.post("/api/samples/read", json={"defaultId": "no-such"})
    assert missing.status_code == 404


def test_a_sample_of_ones_own_replaces_the_standard_sample_but_keeps_its_master():
    own = standard.files("hl7-adt")["sample"].replace("DOE^JANE", "ROE^JOAN")
    read = client.post("/api/samples/read", json={
        "defaultId": "hl7-dft", "sample": own, "sampleName": "mine.hl7"}).json()
    assert read["kind"] == "ADT" and read["sampleName"] == "mine.hl7"
    assert read["basedOn"].startswith("standard HL7V2")


def _read(default_id: str) -> dict:
    read = client.post("/api/samples/read", json={"defaultId": default_id}).json()
    return {r["label"]: r for r in read["rows"]}


def test_a_standard_layout_generates_what_a_pattern_names_though_its_master_masks_it_not():
    """The standard masters are masking configs, which leave sex, state and the
    clinical codes alone. Written for a pattern those are the pattern's: a
    standard layout generates every place SDM knows, so a women's pattern is
    not written as the sample's man."""
    for default_id in [d["id"] for d in standard.listing()]:
        rows = _read(default_id)
        need = [label for label, r in rows.items()
                if not r["algorithm"] and r["config_name"] and not r["kept"]]
        assert not need, f"{default_id}: {need} need an algorithm"
        copied = [label for label, r in rows.items() if not r["algorithm"] and r["suggested"]]
        assert not copied, f"{default_id}: {copied} copied though SDM knows them"
    assert _read("fhir-members")["Patient gender"]["algorithm"] == "SEX"
    adt = _read("hl7-adt")
    assert adt["PID-8"]["algorithm"] == "SEX"
    assert adt["PID-8"]["reason"] == "the standard layout generates it"
    dft = _read("hl7-dft")
    for label, element in [("DG1-3.1", "DIAGNOSIS_CODE"), ("FT1-7.1", "PROCEDURE_CODE"),
                           ("FT1-4", "SERVICE_DATE"), ("FT1-11", "CHARGE_AMOUNT"),
                           ("PID-8", "SEX")]:
        assert dft[label]["algorithm"] == element, label
    assert "DG1-3.2" not in dft, "a derived component is no place"
    assert _read("hl7-oru")["OBR-4.1"]["algorithm"] == "PROCEDURE_CODE"


def test_a_saved_config_of_ones_own_still_copies_what_its_master_leaves():
    """Only SDM's own standard layouts take the suggestions unasked: a master of
    one's own is read as it says, with the suggestion offered."""
    found = standard.files("fhir-members")
    read = client.post("/api/samples/read", json={
        "sample": found["sample"], "master": found["master"], "masterName": "mine.json"}).json()
    gender = next(r for r in read["rows"] if r["label"] == "Patient gender")
    assert gender["algorithm"] is None and gender["suggested"] == "SEX"


def test_the_standard_ccd_carries_its_claims_diagnosis_in_the_problem_list():
    rows = _read("cda-ccd")
    problem = next(r for label, r in rows.items()
                   if label.endswith("observation/value/@code (section=11450-4)"))
    assert problem["algorithm"] == "DIAGNOSIS_CODE"


# ---------------------------------------------------------------------------
# A sample uploaded with no setup: its format found, its standard applied
# ---------------------------------------------------------------------------

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _detect(text: str) -> dict:
    answer = client.post("/api/samples/detect", json={"sample": text})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_a_samples_format_and_standard_configuration_are_found_from_its_content():
    edi = FIXTURES / "edi"
    found = {name: _detect((edi / name).read_text(encoding="utf-8"))
             for name in ("members.834", "claim.837", "remit.835")}
    assert {n: (f["format"], f["kind"]) for n, f in found.items()} == {
        "members.834": ("x12", "834"), "claim.837": ("x12", "837"),
        "remit.835": ("x12", "835")}
    assert found["members.834"]["defaultId"] == "x12-834"
    assert found["claim.837"]["defaultId"] == "x12-837"
    # No standard 835 sample: the X12 master, through the format's first standard.
    assert found["remit.835"]["defaultId"].startswith("x12-")
    assert found["members.834"]["subject"] == "members"

    hl7 = _detect((FIXTURES / "hl7" / "oru_r01_result.hl7").read_text(encoding="utf-8"))
    assert (hl7["format"], hl7["kind"], hl7["defaultId"]) == ("hl7v2", "ORU", "hl7-oru")
    fhir = _detect((FIXTURES / "fhir" / "claims-bundle.json").read_text(encoding="utf-8"))
    assert (fhir["format"], fhir["defaultId"]) == ("fhir", "fhir-claims")
    cda = _detect((FIXTURES / "cda" / "ccd.xml").read_text(encoding="utf-8"))
    assert (cda["format"], cda["defaultId"]) == ("cda", "cda-ccd")

    # A delimited or fixed-width file is a flat file: its layout is read instead.
    assert _detect("MEMBER_ID|FIRST_NAME\nA1|Dana\n") == {"format": "flat"}


def test_a_sample_read_with_no_master_is_read_against_its_formats_standard():
    sample = (FIXTURES / "edi" / "claim.837").read_text(encoding="utf-8")
    read = client.post("/api/samples/read", json={
        "sample": sample, "sampleName": "claim.837", "auto": True}).json()
    assert read["kind"] == "837" and read["defaultId"] == "x12-837"
    assert read["basedOn"].startswith("standard X12")
    assert any(r["algorithm"] for r in read["rows"]), "the master names nothing"
    # The suggestions are offered, not taken: Accept takes them.
    assert any(r["suggested"] and not r["algorithm"] for r in read["rows"])

    # A master named is used, not the standard; nothing named and no auto,
    # suggestions only, as before.
    plain_read = client.post("/api/samples/read", json={"sample": sample}).json()
    assert plain_read["basedOn"] is None and plain_read["defaultId"] is None

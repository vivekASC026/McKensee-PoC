"""CDA modeled on a sample: the same engine as X12, FHIR and HL7 v2, an XML
reader and CDA profiles. Sample and master are the masking tool's (edi-mask
samples/cda/ccd-example.xml, configs/CDAR2Config.json), as they are.
"""

from __future__ import annotations

import difflib
import io
import re
import zipfile
from pathlib import Path

import pytest

from sdg.writers import samples
from sdg.writers.samples import cda

pytestmark = pytest.mark.db

HERE = Path(__file__).resolve().parent / "fixtures" / "cda"


def _text(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules():
    return samples.algorithm_rules()


@pytest.fixture(scope="module")
def sample():
    return cda.read_sample(_text("ccd.xml"))


def _accept_all(rows):
    for r in rows:
        r["algorithm"] = r["algorithm"] or r["suggested"]
    return cda.read_config(cda.write_config(rows))


def _members(n: int) -> list[dict]:
    return [{"LAST_NAME": f"PERSON{i}", "MEMBER_ID": f"M{i}", "SEX": "M" if i % 2 else "F",
             "DOB": f"1980-05-0{i + 1}"} for i in range(n)]


def _claims(members: int = 3, days=lambda m, c: [1]) -> list[dict]:
    """Claim lines: member m has m+1 claims; claim c has two lines on each of
    its `days(m, c)` (days of the month, in the order the lines come). No SSN
    or first name, so those are the rules' to make."""
    out = []
    for m in range(members):
        for c in range(m + 1):
            for day in days(m, c):
                for line in range(2):
                    out.append({"MEMBER_ID": f"MBR{m}", "LAST_NAME": f"LAST{m}", "SEX": "F",
                                "CLAIM_ID": f"CLM{m}{c}",
                                "SERVICE_DATE": f"2026-0{c + 1}-{day:02d}",
                                "PROCEDURE_CODE": "99213"})
    return out


def _value(doc: str, before: str) -> str:
    return doc.split(before, 1)[1].split('"', 1)[0]


def test_the_format_and_document_type_are_read_off_the_sample(sample):
    assert samples.detect(_text("ccd.xml")) is cda
    assert (sample.kind, sample.version) == ("CCD", "R2")
    assert cda.facts(sample)[1] == "1 document · 7 sections · 7 entries"
    assert cda.subject("CCD") == "claims"
    # Any other ClinicalDocument is read as a CDA document: written the same way.
    other = _text("ccd.xml").replace('code="34133-9"', 'code="18842-5"', 1)
    assert cda.read_sample(other).kind == "CDA"
    with pytest.raises(samples.SampleError, match="not HL7 v3 XML"):
        cda.read_sample('<?xml version="1.0"?><ClinicalDocument/>')
    with pytest.raises(samples.SampleError, match="DOCTYPE"):
        cda.read_sample('<!DOCTYPE x [<!ENTITY a "b">]><ClinicalDocument xmlns="urn:hl7-org:v3"/>')


def test_the_masking_config_is_read_as_a_master_and_places_are_proposed(sample, rules):
    entries = cda.read_config(_text("master-cda-masking.json"))
    assert len(entries) > 30
    rows = {r["label"]: r for r in cda.propose(sample, entries, rules)}
    patient = "recordTarget/patientRole"
    assert rows[f"{patient}/patient/name/family"]["algorithm"] == "LAST_NAME"
    assert rows[f"{patient}/patient/name/family"]["config_name"] == "lastName"
    assert rows[f"{patient}/patient/birthTime/@value"]["algorithm"] == "DOB"
    # Two ids side by side, told apart by their assigning authority.
    assert rows[f"{patient}/id/@extension (root=1.3.6.1.4.1.16517.1)"]["algorithm"] == "MEMBER_ID"
    assert rows[f"{patient}/id/@extension (root=2.16.840.1.113883.4.1)"]["algorithm"] == "SSN"
    # The scheme says what a contact is; //telecom/@value reaches every one.
    assert rows[f"{patient}/telecom/@value (mailto:)"]["config_name"] == "email"
    assert rows[f"{patient}/telecom/@value (tel:)"]["config_name"] == "phone"
    # Somebody else's name, whatever the masking name says, is not the member's.
    doctor = rows["author/assignedAuthor/assignedPerson/name/family"]
    assert doctor["config_name"] == "lastName" and doctor["algorithm"] is None
    assert rows[f"{patient}/patient/guardian/guardianPerson/name/family"]["algorithm"] is None
    # The document's own id is the structure's (rename); templates and the narrative are nobody's.
    assert "id/@extension (root=be84a8e4-a22e-4210-a4a6-b3c48273e84c)" not in rows
    assert not any("templateId" in label or "/text/" in label for label in rows)


def test_a_document_differs_from_the_sample_only_where_the_config_says(sample, rules):
    entries = _accept_all(cda.propose(sample, cda.read_config(_text("master-cda-masking.json")),
                                      rules))
    text = "".join(cda.stream(sample, entries, _members(3), rules, seed="t"))
    docs = cda.documents(text)
    assert len(docs) == 3 and all(d.startswith('<?xml version="1.0"') for d in docs)
    changed = [line for line in difflib.unified_diff(
        _text("ccd.xml").splitlines(), docs[1].splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++")]
    tags = {line.strip().lstrip("+").strip().split()[0].split(">")[0].lstrip("<")
            for line in changed}
    # The sample's stylesheet, namespaces and clinical content are copied; its
    # comments are not (a sample's own notes can name a real patient), which
    # shifts a closing tag beside one (/name); the procedures table (td) is said
    # from its entries; the master names every telecom "phone", which is now an
    # algorithm (PHONE).
    assert tags <= {"id", "setId", "streetAddressLine", "city", "state", "postalCode", "telecom",
                    "given", "family", "/name", "administrativeGenderCode", "birthTime",
                    "maritalStatusCode", "effectiveTime", "low", "high", "time",
                    "code", "/code", "td"}, tags
    assert "<!--" not in docs[1]
    assert '<?xml-stylesheet type="text/xsl" href="CDA.xsl"?>' in docs[1]
    assert 'xmlns:sdtc="urn:hl7-org:sdtc"' in docs[1] and "<sdtc:raceCode" in docs[1]
    assert '<family qualifier="SP">PERSON1</family>' in docs[1]
    assert '<id extension="M1" root="1.3.6.1.4.1.16517.1"/>' in docs[1]
    # A code written with its display; a birth time as precise as the sample's.
    assert 'code="M" displayName="Male"' in docs[1] and '<birthTime value="19800502"/>' in docs[1]
    ids = {d.split('<id extension="', 1)[1].split('"', 1)[0] for d in docs}
    assert len(ids) == 3, "every document has its own id"


def _person(doc: str) -> str:
    """Who a document is about: the patient block's ids and name."""
    role = doc.split("<patientRole>", 1)[1].split("<providerOrganization>", 1)[0]
    return "".join(re.findall(r'extension="[^"]*"|<given>[^<]*|<family[^>]*>[^<]*', role))


def _by_member(docs: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d in docs:
        out.setdefault(d.split('<family qualifier="SP">', 1)[1][:5], []).append(d)
    return out


def test_a_claim_gets_a_document_per_day_of_service(sample, rules):
    """How many documents is the claim's: one per day its lines are on -- an
    office visit one, a five-day stay five -- every one the sample's document
    under the same config, the member's places the member's in all of them,
    the date that day's, the id its own. Nothing in the profile says how many."""
    entries = _accept_all(cda.propose(sample, cda.read_config(_text("master-cda-masking.json")),
                                      rules))
    stays = {(1, 0): [3, 4, 5, 6, 7], (2, 2): [9, 8, 9]}   # a stay; lines out of date order
    records = _claims(3, lambda m, c: stays.get((m, c), [1]))
    docs = cda.documents("".join(cda.stream(sample, entries, records, rules, seed="t")))
    by_member = _by_member(docs)
    assert sorted(by_member) == ["LAST0", "LAST1", "LAST2"]
    expected = {0: [["01"]], 1: [["03", "04", "05", "06", "07"], ["01"]],
                2: [["01"], ["01"], ["09", "08"]]}
    for m, own in enumerate(by_member[f"LAST{m}"] for m in range(3)):
        assert len({_person(d) for d in own}) == 1, "one person in all of a member's documents"
        days = [_value(d, '<effectiveTime value="')[:8] for d in own]
        assert days == [f"20260{c + 1}{day}" for c, claim in enumerate(expected[m])
                        for day in claim]
    assert len(docs) == 1 + 6 + 4
    assert len({_value(d, '<id extension="') for d in docs}) == len(docs), "every id its own"
    assert len({_person(own[0]) for own in by_member.values()}) == 3, "three people"
    assert cda.check("".join(docs), sample.profile)["passed"]


def test_claims_without_dates_get_one_and_members_one_each(sample, rules):
    entries = _accept_all(cda.propose(sample, [], rules))
    undated = [{k: v for k, v in r.items() if k != "SERVICE_DATE"} for r in _claims(3)]
    docs = cda.documents("".join(cda.stream(sample, entries, undated, rules, seed="t")))
    assert [len(own) for own in _by_member(docs).values()] == [1, 2, 3]
    assert len(cda.documents("".join(cda.stream(sample, entries, _members(4), rules,
                                                seed="t")))) == 4


def _procedures(doc: str) -> list[str]:
    section = doc.split("<title>PROCEDURES</title>", 1)[1].split("</section>", 1)[0]
    return section.split("<entry ")[1:]


def test_the_procedures_are_the_claim_lines_of_the_day(sample, rules):
    """The procedures section has an entry per claim line of the document's day:
    the line's code (CPT, or HCPCS when it starts with a letter) and date, a new
    id; the sample's translations of its own procedure are gone. Entries are
    the sample's procedure, observation and act entries, taken in turn."""
    entries = _accept_all(cda.propose(sample, cda.read_config(_text("master-cda-masking.json")),
                                      rules))
    lines = [("2026-03-05", "99213"), ("2026-03-05", "93000"), ("2026-03-05", "J1100"),
             ("2026-03-05", "99214"), ("2026-03-06", "99232")]
    records = [{"MEMBER_ID": "M1", "LAST_NAME": "SMITH", "CLAIM_ID": "C1",
                "SERVICE_DATE": day, "PROCEDURE_CODE": code} for day, code in lines]
    text = "".join(cda.stream(sample, entries, records, rules, seed="t"))
    first, second = cda.documents(text)
    found = [(re.search(r'<code [^>]*code="([^"]+)" codeSystem="([^"]+)" codeSystemName="(\w+)"',
                        e).groups()) for e in _procedures(first)]
    assert found == [("99213", "2.16.840.1.113883.6.12", "CPT"),
                     ("93000", "2.16.840.1.113883.6.12", "CPT"),
                     ("J1100", "2.16.840.1.113883.6.285", "HCPCS"),
                     ("99214", "2.16.840.1.113883.6.12", "CPT")]
    kinds = [re.search(r"<(procedure|observation|act) ", e).group(1) for e in _procedures(first)]
    assert kinds == ["procedure", "observation", "act", "procedure"], "taken in turn"
    assert all("<translation" not in e and "Laparoscopic" not in e.split("</code>")[0]
               for e in _procedures(first))
    assert all("20260305" in e and 'value="2014' not in e.split("<performer>")[0]
               for e in _procedures(first)), "the line's date"
    ids = [re.search(r'<id root="([^"]+)"', e).group(1) for d in (first, second)
           for e in _procedures(d)]
    assert len(set(ids)) == 5 and "64af26d5-88ef-4169-ba16-c6ef16a1824f" not in ids
    assert [re.search(r'code="(\w+)"', e).group(1) for e in _procedures(second)] == ["99232"]
    # How the sample's procedure was done, where and what it found are not the line's.
    assert not any("<methodCode" in e or "<targetSiteCode" in e for e in _procedures(first))
    observation = _procedures(first)[1]
    assert '<value xsi:type="CD" nullFlavor="UNK"/>' in observation
    assert "Borderline" not in first.split("<title>PROCEDURES</title>", 1)[1].split("</section>")[0]
    assert first.count("<targetSiteCode") == _text("ccd.xml").count("<targetSiteCode") - 2, \
        "the other sections keep theirs"
    # Every other section is the sample's, its dates included; only a telecom,
    # which the master names "phone", is generated wherever it is.
    allergies = first.split("<title>ALLERGIES AND ADVERSE REACTIONS</title>", 1)[1]

    def phoneless(t):
        # Comments are not copied: the sample is compared without its own.
        t = re.sub(r"\n[ \t]*<!--.*?-->", "", t, flags=re.DOTALL)
        return re.sub(r'<telecom[^>]*/>', "<telecom/>", t)
    assert phoneless(allergies.split("</section>", 1)[0]) in phoneless(_text("ccd.xml"))
    assert cda.check(text, sample.profile)["passed"]


def test_the_procedures_table_is_written_from_the_entries(sample, rules):
    """The section's narrative has a row per entry, said from it (code and system,
    date), each row's ids numbered and the entry's text pointing at its row; the
    sample's procedures are gone from it."""
    entries = _accept_all(cda.propose(sample, cda.read_config(_text("master-cda-masking.json")),
                                      rules))
    lines = [("2026-03-05", "99213"), ("2026-03-05", "93000"), ("2026-03-05", "J1100"),
             ("2026-03-05", "99214"), ("2026-03-06", "99232")]
    records = [{"MEMBER_ID": "M1", "LAST_NAME": "SMITH", "CLAIM_ID": "C1",
                "SERVICE_DATE": day, "PROCEDURE_CODE": code} for day, code in lines]
    text = "".join(cda.stream(sample, entries, records, rules, seed="t"))
    first, second = cda.documents(text)
    table = first.split("<title>PROCEDURES</title>", 1)[1].split("</table>", 1)[0]
    rows = re.findall(r'<tr ID="(\w+)">\s*<td ID="(\w+)">([^<]*)</td>\s*<td>([^<]*)</td>', table)
    assert [r[:3] for r in rows] == [("Procedure1", "ProcedureDesc1", "99213 (CPT)"),
                                     ("Procedure2", "ProcedureDesc2", "93000 (CPT)"),
                                     ("Procedure3", "ProcedureDesc3", "J1100 (HCPCS)"),
                                     ("Procedure4", "ProcedureDesc4", "99214 (CPT)")]
    assert all(r[3].startswith("05 Mar 2026") for r in rows)
    assert "Laparoscopic" not in table and "2014" not in table
    refs = [re.search(r'<text>\s*<reference value="#(\w+)"', e).group(1)
            for e in _procedures(first)]
    assert refs == ["Procedure1", "Procedure2", "Procedure3", "Procedure4"]
    assert "99232 (CPT)" in second and "Procedure2" not in second.split("RESULTS")[0]
    assert cda.check(text, sample.profile)["passed"]
    assert not any(lv["warnings"] for lv in cda.check(text, sample.profile)["levels"])


def test_a_section_place_is_told_apart_from_the_same_path_elsewhere(sample, rules):
    rows = {r["label"]: r for r in cda.propose(
        sample, cda.read_config(_text("master-cda-masking.json")), rules)}
    entry = "component/structuredBody/component/section/entry"
    procedure = rows[f"{entry}/observation/effectiveTime/@value (section=47519-4)"]
    result = next(r for label, r in rows.items()
                  if label.startswith(f"{entry}/organizer/component/observation/effectiveTime/@value")
                  and "section=30954-2" in label)
    assert procedure["algorithm"] == "SERVICE_DATE"
    assert result["config_name"] == "dateShift" and result["algorithm"] is None


def test_a_timestamp_keeps_the_samples_time_and_zone():
    assert cda._fit("2026-03-01", "20141015103026-0500") == "20260301103026-0500"
    assert cda._fit("2026-03-01", "19501219") == "20260301"
    assert cda._fit("Smith", "Jones") == "Smith"


def test_the_download_is_a_zip_of_documents_and_the_checks_pass(sample, rules):
    entries = _accept_all(cda.propose(sample, [], rules))
    packed = b"".join(cda.package(cda.stream(sample, entries, _members(4), rules, seed="t"),
                                  "CCD"))
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert archive.namelist() == [f"CCD-0000{i}.xml" for i in range(1, 5)]
        assert archive.read("CCD-00002.xml").decode("utf-8").startswith("<?xml")
    report = cda.check(packed, sample.profile)
    assert report["passed"], report
    assert report["summary"] == "4 documents; no errors."
    text = "".join(cda.stream(sample, entries, _members(2), rules, seed="t"))
    assert cda.check(text, sample.profile)["passed"]


def test_the_checks_find_what_is_wrong(sample, rules):
    entries = _accept_all(cda.propose(sample, [], rules))
    docs = cda.documents("".join(cda.stream(sample, entries, _members(2), rules, seed="t")))
    broken = (docs[0].replace('code="F" displayName="Female"', 'code="X"', 1)
              .replace("<custodian>", "<other>", 1).replace("</custodian>", "</other>", 1))
    twice = docs[0] + docs[0] + "<?xml version=\"1.0\"?>\n<ClinicalDocument"
    report = cda.check(broken, sample.profile)
    found = {f["rule"] for level in report["levels"] for f in level["findings"]}
    assert not report["passed"]
    assert {"required", "code:recordTarget/patientRole/patient/administrativeGenderCode/@code"} <= found
    report = cda.check(twice, sample.profile)
    found = {f["rule"] for level in report["levels"] for f in level["findings"]}
    assert {"id-unique", "well-formed"} <= found


def test_the_same_records_and_seed_write_the_same_documents(sample, rules):
    entries = _accept_all(cda.propose(sample, [], rules))
    one = "".join(cda.stream(sample, entries, _members(3), rules, seed="t"))
    assert one == "".join(cda.stream(sample, entries, _members(3), rules, seed="t"))


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


def test_read_save_as_write_and_check_ccds_for_a_pattern(client, made):
    from tests.test_pattern_routes import FILTERS, _member_ids

    read = _ok(client.post("/api/samples/read", json={
        "sample": _text("ccd.xml"), "sampleName": "ccd.xml",
        "master": _text("master-cda-masking.json"), "masterName": "CDAR2Config.json"})).json()
    assert (read["format"], read["kind"], read["subject"], read["validation"]) == \
        ("cda", "CCD", "claims", "cda")
    rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
    saved = _ok(client.post("/api/samples/configs", json={
        "name": "test cda sample ccd (made by tests)", "sample": _text("ccd.xml"),
        "sampleName": "ccd.xml", "rows": rows})).json()
    made.append(saved["id"])
    listed = next(c for c in _ok(client.get("/api/samples/configs")).json()["configs"]
                  if c["id"] == saved["id"])
    assert listed["extension"] == ".zip"

    analysis = _ok(client.post("/api/claims/analyze", json=FILTERS)).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    body = {"format": f"sample:{saved['id']}", "rows": 40,
            "pattern": {"profile": pattern, "domain": "claims", "filters": FILTERS, "k": None,
                        "cluster_id": pattern["cluster_id"]}}
    response = _ok(client.post("/api/export", json=body))
    assert response.headers["content-type"].startswith("application/zip")
    assert ".zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        docs = [archive.read(n).decode("utf-8") for n in archive.namelist()]
    assert docs
    members = {_value(d, '<id extension="', ) for d in docs}
    patients = {d.split("<patientRole>", 1)[1].split('extension="', 1)[1].split('"', 1)[0]
                for d in docs}
    held = _member_ids(pattern)
    assert len(members) == len(docs), "every document its own id"
    assert held is None or patients <= held, "documents for people outside the pattern"

    preview = _ok(client.post("/api/export", json={**body, "packaged": False}))
    assert preview.headers["content-type"].startswith("application/xml")
    assert len(cda.documents(preview.text)) == len(docs)

    report = _ok(client.post("/api/samples/check", json={"export": body})).json()
    assert report["passed"], report
    assert report["summary"] == f"{len(docs)} documents; no errors."


# ---------------------------------------------------------------------------
# Optional and repeating places (a config's vary)
# ---------------------------------------------------------------------------

def _master():
    return cda.read_config(_text("master-cda-masking.json"))


def _telecoms(doc: str) -> list[str]:
    from lxml import etree

    root = etree.fromstring(doc.encode("utf-8"))
    v3 = "{urn:hl7-org:v3}"
    return [t.get("value") for t in root.findall(
        f"{v3}recordTarget/{v3}patientRole/{v3}telecom")]


def test_an_element_is_in_some_documents_and_not_others_and_repeats(sample, rules):
    from sdg.writers.samples import engine

    places = {p["at"]: p for p in cda.structure(sample)}
    assert places["recordTarget/patientRole/telecom"]["repeats"]
    entries = _accept_all(cda.propose(sample, _master(), rules))
    vary = engine.read_vary({"vary": [
        {"at": "recordTarget/patientRole/telecom", "present": 60, "min": 1, "max": 3},
        {"at": "recordTarget/patientRole/patient/raceCode", "present": 50}]})
    text = "".join(cda.stream(sample, entries, _members(9) * 1 + [
        {**m, "MEMBER_ID": m["MEMBER_ID"] + "b"} for m in _members(9)] * 2, rules,
        seed="t", vary=vary))
    docs = cda.documents(text)
    counts = [len(_telecoms(d)) for d in docs]
    assert {0, 1, 3} <= set(counts) and max(counts) <= 3
    races = [d.count("<raceCode") for d in docs]
    assert 0 in races and 1 in races
    three = next(d for d in docs if len(_telecoms(d)) == 3)
    phones = [t for t in _telecoms(three) if t.startswith("tel:")]
    assert len(phones) == len(set(phones)), "a copy is a phone of its own"
    report = cda.check(text, sample.profile)
    assert report["passed"], report
    assert text == "".join(cda.stream(sample, entries, _members(9) * 1 + [
        {**m, "MEMBER_ID": m["MEMBER_ID"] + "b"} for m in _members(9)] * 2, rules,
        seed="t", vary=vary))


def test_a_members_documents_agree_on_what_is_the_members(sample, rules):
    """The patient is the member's in each of the member's documents: varied,
    every one of them has the same telecoms."""
    from sdg.writers.samples import engine

    entries = _accept_all(cda.propose(sample, _master(), rules))
    vary = engine.read_vary({"vary": [
        {"at": "recordTarget/patientRole/telecom", "present": 60, "min": 1, "max": 3}]})
    docs = cda.documents("".join(cda.stream(sample, entries, _claims(4), rules, seed="t",
                                            vary=vary)))
    from lxml import etree

    v3 = "{urn:hl7-org:v3}"
    by_member: dict[str, set[int]] = {}
    for d in docs:
        member = etree.fromstring(d.encode("utf-8")).findtext(
            f"{v3}recordTarget/{v3}patientRole/{v3}patient/{v3}name/{v3}family")
        by_member.setdefault(member, set()).add(len(_telecoms(d)))
    assert len(by_member) == 4 and len(docs) > 4
    assert all(len(n) == 1 for n in by_member.values()), by_member


def test_the_entries_still_go_where_the_sample_had_them_when_the_head_varies(sample, rules):
    from sdg.writers.samples import engine

    entries = _accept_all(cda.propose(sample, _master(), rules))
    plain = cda.documents("".join(cda.stream(sample, entries, _claims(2), rules, seed="t")))
    vary = engine.read_vary({"vary": [{"at": "author", "present": 0},
                                      {"at": "recordTarget/patientRole/id", "min": 3}]})
    varied = cda.documents("".join(cda.stream(sample, entries, _claims(2), rules, seed="t",
                                              vary=vary)))
    from lxml import etree

    v3 = "{urn:hl7-org:v3}"
    assert len(plain) == len(varied)
    for a, b in zip(plain, varied):
        was, now = etree.fromstring(a.encode("utf-8")), etree.fromstring(b.encode("utf-8"))
        assert was.findall(f"{v3}author") and not now.findall(f"{v3}author")
        assert len(now.findall(f"{v3}recordTarget/{v3}patientRole/{v3}id")) == 3
        # The procedures are where they were: the same section, the same count.
        def procedures(doc):
            return [len(s.findall(f"{v3}entry")) for s in doc.iter(f"{v3}section")]
        assert procedures(was) == procedures(now)

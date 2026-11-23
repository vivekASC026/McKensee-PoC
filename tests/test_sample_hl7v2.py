"""HL7 v2 modeled on a sample: the same engine as X12 and FHIR, the segment
reader X12 uses, and HL7 profiles. Samples and master are the masking tool's
(edi-mask samples/hl7, configs/HL7v251Config.json), as they are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sdg.writers import samples
from sdg.writers.samples import hl7v2 as hs

pytestmark = pytest.mark.db

HERE = Path(__file__).resolve().parent / "fixtures" / "hl7"


def _text(name: str) -> str:
    return (HERE / name).read_bytes().decode("utf-8")


@pytest.fixture(scope="module")
def rules():
    return samples.algorithm_rules()


def _accept_all(rows):
    for r in rows:
        r["algorithm"] = r["algorithm"] or r["suggested"]
    return hs.read_config(hs.write_config(rows))


def _segments(text: str, seg: str) -> list[list[str]]:
    return [line.split("|") for line in text.split("\r") if line.startswith(seg + "|")]


def _claims(claims: int = 3) -> list[dict]:
    """Claim lines: claim c has c+1 lines, every claim its own member."""
    out = []
    for c in range(claims):
        for line in range(c + 1):
            out.append({"MEMBER_ID": f"MBR{c:04d}", "LAST_NAME": f"LAST{c}", "SEX": "F",
                        "CLAIM_ID": f"CLM{c}", "CHARGE_AMOUNT": 10.25 + line,
                        "SERVICE_DATE": f"2026-03-0{line + 1}", "PROCEDURE_CODE": "99213"})
    return out


def test_the_format_and_message_type_are_read_off_the_sample():
    assert samples.detect(_text("adt_a01.hl7")) is hs
    adt = hs.read_sample(_text("adt_a01.hl7"))
    assert (adt.kind, adt.version) == ("ADT", "2.5.1")
    assert hs.facts(adt)[1] == "1 messages · 1 patients"
    assert hs.read_sample(_text("dft_p03_charge.hl7")).kind == "DFT"
    assert hs.subject("ADT") == "members" and hs.subject("DFT") == "claims"
    oru = hs.read_sample(_text("oru_r01_result.hl7"))
    assert oru.kind == "ORU" and hs.subject("ORU") == "claims"
    assert hs.facts(oru)[1] == "1 messages · 1 orders · 1 results"
    unknown = _text("adt_a01.hl7").replace("ADT^A01^ADT_A01", "QBP^Q22^QBP_Q21")
    with pytest.raises(samples.SampleError,
                       match="modeled on ACK, ADT, DFT, MDM, ORM, ORU, SIU, VXU"):
        hs.read_sample(unknown)


def test_the_masking_config_is_read_as_a_master_and_fields_are_proposed(rules):
    entries = hs.read_config(_text("master-hl7-masking.json"))
    assert len({e.segment for e in entries}) > 40
    sample = hs.read_sample(_text("adt_a01.hl7"))
    rows = {r["label"]: r for r in hs.propose(sample, entries, rules)}
    assert rows["PID-5.1"]["algorithm"] == "LAST_NAME" and rows["PID-5.1"]["config_name"] == "lastName"
    assert rows["PID-5.2"]["algorithm"] == "FIRST_NAME"
    assert rows["PID-7"]["suggested"] == "DOB" and rows["PID-19"]["suggested"] == "SSN"
    assert rows["IN1-17"]["suggested"] == "RELATIONSHIP_CODE"
    # MSH counts its field separator: MSH-7 is the message time, MSH-10 the control id.
    assert rows["MSH-7"]["example"] == "20260910083015"
    assert "MSH-10" not in rows, "the control id is the structure's (rename), nobody's choice"
    # A doctor's name in PV1 is not the member's, whatever the masking name says.
    doctor = rows["PV1-7.2"]
    assert doctor["config_name"] and doctor["algorithm"] is None and doctor["suggested"] is None


def test_an_adt_writes_one_message_per_member(rules):
    sample = hs.read_sample(_text("adt_a01.hl7"))
    entries = _accept_all(hs.propose(sample, hs.read_config(_text("master-hl7-masking.json")),
                                     rules))
    records = [{"LAST_NAME": f"PERSON{i}", "MEMBER_ID": f"M{i}", "SEX": "F",
                "RELATIONSHIP_CODE": "S"} for i in range(4)]
    text = "".join(hs.stream(sample, entries, records, rules, seed="t"))
    assert "\n" not in text, "segments end with CR, as the sample's do"
    msh = _segments(text, "MSH")
    assert len(msh) == 4
    assert len({m[9] for m in msh}) == 4 and all(m[9].startswith("MSG00001-") for m in msh)
    assert [m[2] for m in msh] == ["EPICADT"] * 4, "the sample's sending application, copied"
    pids = _segments(text, "PID")
    assert [p[5].split("^")[0] for p in pids] == [f"PERSON{i}" for i in range(4)]
    assert {p[8] for p in pids} == {"F"}
    # PID-3 repeats: the first identifier is the record's, the second its own.
    first, second = pids[0][3].split("~")
    assert first.split("^")[0] == "M0" and first.split("^")[4] == "MR"
    assert second.split("^")[0] != "M0" and second.split("^")[4] == "MB"
    assert {i[17] for i in _segments(text, "IN1")} == {"SPO"}, "relationship as HL7 0063 codes it"
    # The insured's address is generated whole, not a new ZIP on the sample's street.
    for pid, in1 in zip(pids, _segments(text, "IN1")):
        assert in1[19].split("^")[2] == pid[11].split("^")[2]
    # A generated diagnosis code is not described by the sample's text.
    assert {d[3].split("^")[1] for d in _segments(text, "DG1")} == {""}


def test_a_dft_writes_a_message_per_claim_and_an_ft1_per_line(rules):
    sample = hs.read_sample(_text("dft_p03_charge.hl7"))
    entries = _accept_all(hs.propose(sample, [], rules))
    text = "".join(hs.stream(sample, entries, _claims(3), rules, seed="t"))
    messages = [m for m in text.split("MSH|")[1:]]
    assert len(messages) == 3
    for c, message in enumerate(messages):
        ft1 = _segments("MSH|" + message, "FT1")
        assert [f[1] for f in ft1] == [str(i) for i in range(1, c + 2)], "FT1-1 counts from 1"
        # The masking tool's DFT carries FT1-4 (the service date) but leaves the
        # procedure and amounts empty; only what a sample holds is generated.
        assert [f[4].split("^")[0] for f in ft1] == [f"2026030{i + 1}" for i in range(c + 1)]
        # The claim's own segments after its charges are written once, after them.
        ids = [line.split("|")[0] for line in ("MSH|" + message).split("\r") if line]
        assert ids.count("IN1") == 1 and ids.index("IN1") > max(
            i for i, s in enumerate(ids) if s == "FT1")
        assert _segments("MSH|" + message, "PID")[0][5].split("^")[0] == f"LAST{c}"


def test_written_messages_pass_the_hl7_checks(rules):
    adt = hs.read_sample(_text("adt_a01.hl7"))
    entries = _accept_all(hs.propose(adt, hs.read_config(_text("master-hl7-masking.json")),
                                     rules))
    records = [{"LAST_NAME": f"PERSON{i}", "MEMBER_ID": f"M{i}", "SEX": "F", "DOB": "1980-02-29",
                "RELATIONSHIP_CODE": "S"} for i in range(4)]
    report = hs.check("".join(hs.stream(adt, entries, records, rules, seed="t")), adt.profile)
    assert report["passed"], report
    assert report["summary"] == "4 messages; no errors."
    assert not any(lv["findings"] for lv in report["levels"])

    dft = hs.read_sample(_text("dft_p03_charge.hl7"))
    entries = _accept_all(hs.propose(dft, [], rules))
    report = hs.check("".join(hs.stream(dft, entries, _claims(3), rules, seed="t")),
                      dft.profile)
    assert report["passed"] and report["summary"] == "3 messages; no errors.", report


def test_the_hl7_checks_find_what_is_wrong():
    adt = hs.read_sample(_text("adt_a01.hl7"))
    message = _text("adt_a01.hl7").rstrip("\r")
    second = (message.replace("|19800415|F|", "|19801345|X|")
              .replace("\rEVN|A01|20260910083000", ""))
    batch = "\r".join(["BHS|^~\\&|EPICADT", message, second, "BTS|5", ""])
    report = hs.check(batch, adt.profile)
    assert not report["passed"]
    found = {(f["level"], f["rule"]) for lv in report["levels"] for f in lv["findings"]}
    assert found == {(1, "batch-count"), (1, "required-segment"), (2, "control-id-unique"),
                     (3, "date"), (3, "code:PID PID08")}, found
    messages = [f["message"] for lv in report["levels"] for f in lv["findings"]]
    assert "PID-7 is '19801345', not an HL7 date (YYYYMMDD[HHMM[SS]][+ZZZZ])." in messages
    assert "BTS-1 is 5; the batch has 2 messages." in messages
    assert "No EVN segment." in messages
    assert hs.check("hello", adt.profile)["summary"] == "Not an HL7 v2 file."
    assert [hs._date(v) for v in ("2026", "202602", "20260229", "20280229",
                                  "202602281230", "20260228123059.1234-0500", "2026022",
                                  "20260230", "202602282460")] == [
        True, True, False, True, True, True, False, False, False]


def test_an_oru_writes_a_message_per_claim_and_an_order_per_line(rules):
    """Lab work is billed as claim lines: an ORU per claim, an order (ORC, OBR
    and its results, notes and specimen) per line, each order its own numbers."""
    sample = hs.read_sample(_text("oru_r01_result.hl7"))
    entries = _accept_all(hs.propose(sample, hs.read_config(_text("master-hl7-masking.json")),
                                     rules))
    text = "".join(hs.stream(sample, entries, _claims(3), rules, seed="t"))
    messages = ["MSH|" + m for m in text.split("MSH|")[1:]]
    assert len(messages) == 3
    order = ["ORC", "OBR", "CTD", "OBX", "NTE", "SPM", "SAC", "TQ2", "CTI"]
    for c, message in enumerate(messages):
        ids = [line.split("|")[0] for line in message.split("\r") if line]
        assert ids == ["MSH", "PID", "PV1", *order * (c + 1), "DSC"], ids
        obr = _segments(message, "OBR")
        assert [o[1] for o in obr] == [str(i) for i in range(1, c + 2)], "OBR-1 counts from 1"
        assert [o[7] for o in obr] == [f"2026030{i + 1}" for i in range(c + 1)], "the line's date"
        assert [o[14] for o in _segments(message, "OBX")] == [o[7] for o in obr]
        assert _segments(message, "PID")[0][5].split("^")[0] == f"LAST{c}"
    numbers = [o[2] for o in _segments(text, "OBR")] + [o[2] for o in _segments(text, "ORC")]
    assert len(set(numbers)) == len(numbers) == 12, "every order its own placer number"
    report = hs.check(text, sample.profile)
    assert report["passed"] and report["summary"] == "3 messages; no errors.", report


def test_the_same_records_and_seed_write_the_same_file(rules):
    sample = hs.read_sample(_text("adt_a01.hl7"))
    entries = _accept_all(hs.propose(sample, [], rules))
    records = [{"MEMBER_ID": f"M{i}"} for i in range(3)]
    one = "".join(hs.stream(sample, entries, records, rules, seed="t"))
    assert one == "".join(hs.stream(sample, entries, records, rules, seed="t"))


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


def test_read_save_as_and_write_an_adt_for_a_pattern(client, made):
    from tests.test_pattern_routes import FILTERS, _sexes

    read = _ok(client.post("/api/samples/read", json={
        "sample": _text("adt_a01.hl7"), "sampleName": "adt_a01.hl7",
        "master": _text("master-hl7-masking.json"),
        "masterName": "HL7v251Config.json"})).json()
    assert (read["format"], read["kind"], read["subject"]) == ("hl7v2", "ADT", "members")
    rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
    saved = _ok(client.post("/api/samples/configs", json={
        "name": "test hl7 sample adt (made by tests)", "sample": _text("adt_a01.hl7"),
        "sampleName": "adt_a01.hl7", "rows": rows})).json()
    made.append(saved["id"])

    analysis = _ok(client.post("/api/patterns/analyze", json={"filters": FILTERS, "k": 3})).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    body = {"format": f"sample:{saved['id']}", "members": 6,
            "pattern": {"profile": pattern, "domain": "members", "filters": FILTERS, "k": 3,
                        "cluster_id": pattern["cluster_id"]}}
    response = _ok(client.post("/api/export", json=body))
    assert response.headers["content-type"].startswith("application/hl7-v2")
    text = response.text
    assert len(_segments(text, "MSH")) == 6
    sexes = {p[8] for p in _segments(text, "PID")}
    if _sexes(pattern):
        assert sexes <= _sexes(pattern) | {"U"}

    report = _ok(client.post("/api/samples/check", json={"export": body})).json()
    assert report["passed"], report
    assert report["summary"] == "6 messages; no errors."

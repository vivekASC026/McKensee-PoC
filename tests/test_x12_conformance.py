"""What a trading partner's validator checks first in an 834 or 837P.

ISA15 says whether a file is test data (T) or production (P): a synthetic file
is T unless SDG_X12_USAGE=P. Each file has its own control number (ISA13,
GS06, GE02, IEA02), the same for the same seed, so a second file of the day is
not rejected as a duplicate. And the 834's identifiers, codes and loops are
where X220A1 puts them.
"""

from __future__ import annotations

from sdg.writers.edi_formats import load_format, stream_format
from sdg.writers.edi_formats.registry import interchange_number

MEMBER = {"MEMBER_ID": "M1", "SUBSCRIBER_ID": "S0000001", "GROUP_KEY": "G17",
          "LAST_NAME": "RIVERA", "FIRST_NAME": "ANA", "DOB": "1980-04-02", "SEX": "F",
          "MARITAL_STATUS": "D", "SSN": "123456789", "EFFECTIVE_DATE": "2026-01-01",
          "COVERAGE_PLAN_ID": "PLN001", "ADDRESS_CITY": "DAYTON", "ADDRESS_STATE": "OH",
          "ADDRESS_ZIP": "45402", "RELATIONSHIP_CODE": "18"}


def _segments(fmt: str, records, seed: str = "t") -> list[list[str]]:
    out = "".join(stream_format(load_format(fmt), iter(records), as_of="2026-06-30",
                                seed=seed, rules={}))
    return [line.rstrip("~").split("*") for line in out.splitlines() if line.strip()]


def _first(segments, seg_id, qualifier=None):
    return next(s for s in segments if s[0] == seg_id and (qualifier is None or s[1] == qualifier))


def test_a_synthetic_interchange_is_test_data_unless_said(monkeypatch):
    monkeypatch.delenv("SDG_X12_USAGE", raising=False)
    isa = _first(_segments("834", [MEMBER]), "ISA")
    assert isa[15] == "T" and len("*".join(isa)) == 105, isa
    monkeypatch.setenv("SDG_X12_USAGE", "P")
    assert _first(_segments("834", [MEMBER]), "ISA")[15] == "P"


def test_each_file_has_its_own_control_number_and_a_file_written_again_the_same():
    one, again, other = (_segments("834", [MEMBER], seed) for seed in ("r1", "r1", "r2"))
    number = interchange_number("r1")
    assert _first(one, "ISA")[13] == f"{number:09d}" == _first(again, "ISA")[13]
    assert _first(one, "GS")[6] == str(number) == _first(one, "GE")[2]
    assert _first(one, "IEA")[2] == f"{number:09d}"
    assert _first(other, "ISA")[13] != _first(one, "ISA")[13]
    assert 1 <= number <= 999_999_999


def test_a_long_partner_id_keeps_the_isa_its_fixed_width():
    out = "".join(stream_format(load_format("834"), iter([MEMBER]), as_of="2026-06-30",
                                sender_id="A_SENDER_ID_LONGER_THAN_15", rules={}))
    assert len(out.split("~", 1)[0]) == 105


def test_the_834_puts_its_identifiers_codes_and_dates_where_x220a1_does():
    segments = _segments("834", [MEMBER, {**MEMBER, "MEMBER_ID": "M2", "MARITAL_STATUS": "W"}])
    ins = _first(segments, "INS")
    assert ins[8] == "FT", "INS08, employment status, is required"
    assert _first(segments, "REF", "0F")[2] == "S0000001", "REF*0F is the subscriber"
    assert _first(segments, "REF", "1L")[2] == "G17", "REF*1L is the group or policy"
    assert not [s for s in segments if s[0] == "REF" and s[2] == "123456789"], \
        "the SSN is NM109's, not a REF's"
    assert [s[4] for s in segments if s[0] == "DMG"] == ["D", "W"], "code list 1067"
    ids = [s[0] for s in segments]
    hd = ids.index("HD")
    assert ids[hd + 1] == "DTP" and segments[hd + 1][1] == "348", \
        "benefit begin is loop 2300's, after HD"


def test_the_837p_billing_zip_is_zip_plus_4_and_the_subscriber_has_an_address():
    claim = {**MEMBER, "CLAIM_ID": "C1", "CHARGE_AMOUNT": "80.00", "DIAGNOSIS_CODE": "E119",
             "PROCEDURE_CODE": "99213", "SERVICE_UNITS": "1", "LINE_NUMBER": "1",
             "SERVICE_DATE": "2026-05-01"}
    segments = _segments("837P", [claim])
    n4s = [s for s in segments if s[0] == "N4"]
    assert len(n4s[0][3]) == 9, "2010AA N403 is ZIP+4"
    ids = [s[0] for s in segments]
    subscriber = next(i for i, s in enumerate(segments) if s[0] == "NM1" and s[1] == "IL")
    assert ids[subscriber + 1:subscriber + 3] == ["N3", "N4"]
    assert segments[subscriber + 2][1:4] == ["DAYTON", "OH", "45402"]


def test_an_837p_claim_for_a_dependent_names_the_subscriber_then_the_patient():
    """X222A1: the subscriber is loop 2000B, the patient who is someone else
    is 2000C under it. SBR02 is 18 only when they are the same person, and the
    subscriber's DMG goes only then; the patient's DMG is in 2010CA."""
    line = {"CHARGE_AMOUNT": "80.00", "PROCEDURE_CODE": "99213", "SERVICE_UNITS": "1",
            "LINE_NUMBER": "1", "SERVICE_DATE": "2026-05-01"}
    own = {**MEMBER, **line, "CLAIM_ID": "C1"}
    child = {**MEMBER, **line, "CLAIM_ID": "C2", "RELATIONSHIP_CODE": "D",
             "FIRST_NAME": "LUIS", "DOB": "2015-09-01", "SEX": "M",
             "SUBSCRIBER_FIRST_NAME": "ANA", "SUBSCRIBER_LAST_NAME": "RIVERA",
             "SUBSCRIBER_MEMBER_ID": "S000000100"}
    segments = _segments("837P", [own, child])
    hls = [s for s in segments if s[0] == "HL"]
    assert [s[1:] for s in hls] == [["1", "", "20", "1"], ["2", "1", "22", "0"],
                                    ["3", "1", "22", "1"], ["4", "3", "23", "0"]], hls
    sbrs = [s for s in segments if s[0] == "SBR"]
    assert sbrs[0][2] == "18" and sbrs[1][2] == "", sbrs

    second = segments[segments.index(hls[2]):]
    ids = [s[0] for s in second]
    subscriber = _first(second, "NM1", "IL")
    assert subscriber[3:5] == ["RIVERA", "ANA"] and subscriber[9] == "S000000100"
    assert ids.index("DMG") > ids.index("PAT"), "the subscriber who is not the patient has no DMG"
    assert _first(second, "PAT")[1] == "19"
    patient = _first(second, "NM1", "QC")
    assert patient[3:5] == ["RIVERA", "LUIS"]
    assert _first(second, "DMG")[2:4] == ["20150901", "M"]
    assert ids.index("CLM") > ids.index("DMG")


def test_an_835_for_a_dependent_names_the_subscriber_after_the_patient():
    claim = {**MEMBER, "CLAIM_ID": "C2", "RELATIONSHIP_CODE": "D", "FIRST_NAME": "LUIS",
             "SUBSCRIBER_FIRST_NAME": "ANA", "SUBSCRIBER_LAST_NAME": "RIVERA",
             "SUBSCRIBER_MEMBER_ID": "S000000100", "CLAIM_STATUS": "1",
             "PROCEDURE_CODE": "99213", "CHARGE_AMOUNT": "80.00", "SERVICE_UNITS": "1",
             "SERVICE_DATE": "2026-05-01", "PAID_DATE": "2026-05-20"}
    segments = _segments("835", [claim])
    ids = [s[0] + "*" + s[1] for s in segments]
    assert ids.index("NM1*IL") == ids.index("NM1*QC") + 1
    assert _first(segments, "NM1", "QC")[3:5] == ["RIVERA", "LUIS"]
    assert _first(segments, "NM1", "IL")[3:5] == ["RIVERA", "ANA"]
    assert not [s for s in _segments("835", [{**claim, "SUBSCRIBER_LAST_NAME": ""}])
                if s[:2] == ["NM1", "IL"]], "the patient who is the subscriber is named once"


def _claims(*claims):
    """Claim lines: (status, charge of each line) per claim."""
    return [{**MEMBER, "CLAIM_ID": f"C{c}", "CLAIM_STATUS": status, "PROCEDURE_CODE": "99213",
             "CHARGE_AMOUNT": charge, "SERVICE_UNITS": "1", "SERVICE_DATE": "2026-05-01"}
            for c, (status, charges) in enumerate(claims, start=1) for charge in charges]


def _cas_total(segment) -> float:
    return sum(float(segment[at]) for at in (3, 6, 9) if len(segment) > at and segment[at])


def test_an_835_pays_a_share_and_every_line_and_claim_balances():
    segments = _segments("835", _claims(*[("02", ["120.00", "45.50", "18.00"])] * 8,
                                        ("01", ["950.00"])))
    claims, line = [], None
    for s in segments:
        if s[0] == "CLP":
            claims.append({"clp": s, "lines": []})
        elif s[0] == "SVC":
            line = {"svc": s, "cas": [], "amt": None}
            claims[-1]["lines"].append(line)
        elif s[0] == "CAS":
            line["cas"].append(s)
        elif s[0] == "AMT":
            line["amt"] = s
    paid = 0.0
    for claim in claims:
        clp = claim["clp"]
        assert clp[2] == "1"
        owed = 0.0
        for line in claim["lines"]:
            svc = line["svc"]
            assert round(float(svc[2]) - sum(_cas_total(c) for c in line["cas"]), 2) == float(svc[3])
            assert line["amt"][1] == "B6" and float(line["amt"][2]) <= float(svc[2])
            owed += sum(_cas_total(c) for c in line["cas"] if c[1] == "PR")
        assert round(float(clp[3]) - float(clp[4]) - sum(
            _cas_total(c) for line in claim["lines"] for c in line["cas"]), 2) == 0
        assert round(owed, 2) == float(clp[5])
        paid += float(clp[4])
    assert round(paid, 2) == float(_first(segments, "BPR")[2])
    shares = [float(c["clp"][4]) / float(c["clp"][3]) for c in claims]
    assert max(shares) < 1 and min(shares) > 0, "a share of what was charged, never all of it"
    ids = [s[0] for s in segments]
    assert ids.index("CAS") > ids.index("SVC") and "AMT" in ids


def test_an_835_denies_a_claim_its_status_says_was_denied():
    segments = _segments("835", _claims(("04", ["300.00", "20.00"])))
    clp = _first(segments, "CLP")
    assert clp[2:5] == ["4", "320.00", "0.00"] and len(clp) > 6 and clp[5] == ""
    assert [s[3] for s in segments if s[0] == "SVC"] == ["0.00", "0.00"]
    assert [s[1:] for s in segments if s[0] == "CAS"] == [["CO", "96", "300.00"],
                                                         ["CO", "96", "20.00"]]
    assert not [s for s in segments if s[0] == "AMT"]


def test_an_837p_writes_a_subscriber_once_for_their_claims_in_a_row():
    dependent = {"SUBSCRIBER_LAST_NAME": "RIVERA", "SUBSCRIBER_FIRST_NAME": "ANA",
                 "SUBSCRIBER_MEMBER_ID": "S000000100", "RELATIONSHIP_CODE": "D"}
    claim = {**MEMBER, "MEMBER_ID": "S000000100", "PROCEDURE_CODE": "99213",
             "CHARGE_AMOUNT": "50.00", "SERVICE_UNITS": "1", "SERVICE_DATE": "2026-05-01"}
    records = [{**claim, "CLAIM_ID": "C1"}, {**claim, "CLAIM_ID": "C2"},
               {**claim, **dependent, "MEMBER_ID": "S000000101", "FIRST_NAME": "LUIS",
                "CLAIM_ID": "C3"},
               {**claim, **dependent, "MEMBER_ID": "S000000101", "FIRST_NAME": "LUIS",
                "CLAIM_ID": "C4"},
               {**claim, **dependent, "MEMBER_ID": "S000000102", "FIRST_NAME": "KAI",
                "CLAIM_ID": "C5"},
               {**claim, "MEMBER_ID": "T000000200", "CLAIM_ID": "C6"}]
    segments = _segments("837P", records)
    levels = [s[1:5] for s in segments if s[0] == "HL"]
    assert levels == [["1", "", "20", "1"], ["2", "1", "22", "0"], ["3", "1", "22", "1"],
                      ["4", "3", "23", "0"], ["5", "3", "23", "0"], ["6", "1", "22", "0"]]
    ids = [s[0] for s in segments]
    assert ids.count("CLM") == 6 and ids.count("SBR") == 3 and ids.count("PAT") == 2
    assert [s[3:5] for s in segments if s[:2] == ["NM1", "QC"]] == [["RIVERA", "LUIS"],
                                                                  ["RIVERA", "KAI"]]
    # Members whose IDs are not held are drawn per claim: never the same person.
    drawn = [{k: v for k, v in r.items() if k != "MEMBER_ID"} for r in records[:2]]
    assert [s[0] for s in _segments("837P", drawn)].count("SBR") == 2

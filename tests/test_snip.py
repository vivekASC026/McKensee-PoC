"""SNIP-style checks, driven by rule files a customer can change.

A correct 837P must pass levels 1-4 -- the rules must not flag what is right
-- and each defect planted below must be caught at its level. A customer's own
rule file, code set and trading partner take effect with no change to code.
"""

import json

import pytest

from sdg.writers.snip import validate

GOOD_837P = "~".join([
    "ISA*00*          *00*          *ZZ*SUBMITTER      *ZZ*RECEIVER       *260630*1200*^*00501*000000001*0*P*:",
    "GS*HC*SUBMITTER*RECEIVER*20260630*1200*1*X*005010X222A1",
    "ST*837*0001*005010X222A1",
    "BHT*0019*00*REF001*20260630*1200*CH",
    "NM1*41*2*SUBMITTER INC*****46*S001",
    "PER*IC*JANE DOE*TE*5555551234",
    "NM1*40*2*RECEIVER CO*****46*R001",
    "HL*1**20*1",
    "NM1*85*2*CLINIC LLC*****XX*1234567893",
    "N3*1 MAIN ST",
    "N4*SPRINGFIELD*IL*62701",
    "REF*EI*123456789",
    "HL*2*1*22*0",
    "SBR*P*18*******CI",
    "NM1*IL*1*DOE*JOHN****MI*M12345",
    "DMG*D8*19800101*M",
    "NM1*PR*2*PAYER CO*****PI*P001",
    "CLM*C001*150.00***11:B:1*Y*A*Y*Y",
    "HI*ABK:E119",
    "LX*1",
    "SV1*HC:99213*100.00*UN*1***1",
    "DTP*472*D8*20260601",
    "LX*2",
    "SV1*HC:85025*50.00*UN*1***1",
    "DTP*472*D8*20260601",
    "SE*24*0001",
    "GE*1*1",
    "IEA*1*000000001",
]) + "~"


@pytest.fixture(autouse=True)
def _no_customer_rules(tmp_path, monkeypatch):
    """Each test starts with an empty customer folder of its own."""
    monkeypatch.setenv("SDG_SNIP_DIR", str(tmp_path / "snip"))


def _levels(report):
    return {lv["level"]: lv for lv in report["levels"]}


def _rules_failed(report, level):
    return {f["rule"] for f in _levels(report)[level]["findings"] if f["severity"] == "error"}


def test_a_correct_837p_passes_levels_one_to_four():
    report = validate(GOOD_837P)
    levels = _levels(report)
    for level in (1, 2, 3, 4):
        assert levels[level]["status"] == "pass", (level, levels[level]["findings"])
    # Without the ICD-10 and HCPCS lists, membership is said not to be checked.
    assert levels[5]["status"] == "partial"
    assert any("icd10cm" in n for n in levels[5]["notes"])
    assert levels[7]["status"] == "not_checked"
    assert report["passed"]


@pytest.mark.parametrize("change, level, rule", [
    (("SE*24*0001", "SE*23*0001"), 1, "se-count"),
    (("LX*2~", "LX*2*~"), 1, "trailing-separator"),
    (("NM1*PR*2*PAYER CO*****PI*P001~", ""), 2, "payer-name"),
    (("CLM*C001*150.00", "CLM*C001*175.00"), 3, "claim-balances"),
    (("SBR*P*18", "SBR*P*19"), 4, "patient-loop-when-not-self"),
    (("DTP*472*D8*20260601~LX*2", "DTP*472*D8*20261341~LX*2"), 4, "dates-match-format"),
    (("11:B:1", "98:B:1"), 5, "place-of-service"),
    (("XX*1234567893", "XX*1234567890"), 5, "billing-npi"),
    (("HC:85025", "HC:8502"), 5, "procedure-form"),
])
def test_each_defect_is_caught_at_its_level(change, level, rule):
    old, new = change
    assert old in GOOD_837P
    text = GOOD_837P.replace(old, new, 1)
    if new == "":   # a segment removed: SE's count moves with it
        text = text.replace("SE*24*0001", "SE*24*0001")
    report = validate(text)
    assert rule in _rules_failed(report, level), _levels(report)[level]
    assert not report["passed"]


def test_a_customer_changes_the_rules_without_changing_code(tmp_path):
    folder = tmp_path / "snip"
    (folder / "codesets").mkdir(parents=True)
    (folder / "partners").mkdir()
    (folder / "837P.json").write_text(json.dumps({
        "id": "837P-acme", "transaction": "837", "version": "005010X222A1",
        "rules": [
            # Switch a shipped rule off, and add one of their own.
            {"id": "submitter-contact", "enabled": False},
            {"id": "claim-id-prefix", "level": 2, "kind": "element", "segment": "CLM",
             "position": 1, "pattern": "CL[0-9]+", "message": "Claim ids start with CL here."},
        ],
    }), encoding="utf-8")
    # Their ICD-10 list, which does not hold E119.
    (folder / "codesets" / "icd10cm.txt").write_text("E1165\nI10\n", encoding="utf-8")
    (folder / "partners" / "acme.json").write_text(json.dumps({
        "name": "ACME", "rules": [
            {"id": "acme-submitter", "kind": "element", "segment": "NM1",
             "when": {"position": 1, "equals": "41"}, "position": 9, "codes": ["S999"]},
        ],
    }), encoding="utf-8")

    no_contact = GOOD_837P.replace("PER*IC*JANE DOE*TE*5555551234~", "").replace(
        "SE*24*0001", "SE*24*0001")
    report = validate(no_contact, partner="acme")
    levels = _levels(report)
    assert "submitter-contact" not in _rules_failed(report, 2), "a switched-off rule still ran"
    assert "claim-id-prefix" in _rules_failed(report, 2)
    assert "diagnosis-code-set" in _rules_failed(report, 5), "the customer's code set was not used"
    assert levels[5]["status"] == "fail"
    assert "acme-submitter" in _rules_failed(report, 7)
    assert levels[7]["rules"] >= 1


def test_a_set_with_no_rule_file_says_so_rather_than_passing():
    text = GOOD_837P.replace("005010X222A1", "005010X223A2")   # an 837I version
    report = validate(text)
    assert _levels(report)[2]["status"] == "not_checked"
    assert any("No rule file" in n for n in _levels(report)[2]["notes"])


@pytest.mark.db
def test_the_screen_checks_exactly_the_file_it_writes():
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    client = TestClient(app)
    listed = client.get("/api/edi/snip").json()
    assert {r["id"] for r in listed["rules"] if r["origin"] == "shipped"} >= {"834", "837P", "835"}
    response = client.post("/api/edi/snip/check", json={
        "export": {"format": "edi:837P", "subject": "claims", "rows": 20}})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["transaction"] == "837" and report["segments"] > 10
    assert _levels(report)[1]["status"] in ("pass", "fail")

    uploaded = client.post("/api/edi/snip/check-file",
                           files={"file": ("claims.x12", GOOD_837P.encode(), "text/plain")})
    assert uploaded.status_code == 200 and uploaded.json()["passed"]


@pytest.mark.db
@pytest.mark.parametrize("fmt, subject", [("834", "members"), ("837P", "claims"), ("835", "claims")])
def test_what_sdm_writes_passes_levels_one_to_four(fmt, subject):
    """SDM's own files must pass the checks it offers. Codes come from the
    pattern store, so level 5 has nothing to find either; membership in the
    ICD-10 and HCPCS lists is checked only where those lists are installed."""
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    report = TestClient(app).post("/api/edi/snip/check", json={
        "export": {"format": f"edi:{fmt}", "subject": subject, "rows": 300}}).json()
    levels = _levels(report)
    for level in (1, 2, 3, 4, 5):
        assert levels[level]["errors"] == 0, (fmt, level, levels[level]["findings"][:3])

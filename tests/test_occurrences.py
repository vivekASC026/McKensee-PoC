"""How often something occurs: exactly, from one count to another, a mix, or
as a pattern measured it -- for an element of a document (FHIR, HL7 v3), a
segment or field of an HL7 v2 message, and the claims of a member and the
lines of a claim.
"""

from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app
from sdg.writers import elements, samples
from sdg.writers.samples import SampleError, engine, hl7v2, standard

client = TestClient(app)


def test_a_mix_is_read_however_it_is_written():
    assert engine.read_mix("1:50, 2:30, 3:20") == ((1, 50.0), (2, 30.0), (3, 20.0))
    assert engine.read_mix([[0, 1], [4, 3]]) == ((0, 1.0), (4, 3.0))
    assert engine.read_mix({"2": 100}) == ((2, 100.0),)
    assert engine.read_mix("") is None
    for bad in ("1-50", "1:50, x:2", "60:1", "1:0, 2:0"):
        with pytest.raises(SampleError):
            engine.read_mix(bad)


def test_a_mix_is_drawn_in_its_shares():
    vary = engine.read_vary({"vary": [{"at": "X", "mix": "1:50, 2:30, 3:20"}]})[0]
    assert vary.mix and vary.low is None
    drawn = Counter(engine._times_v2("seed", vary, row, 0, 1) for row in range(4000))
    assert set(drawn) == {1, 2, 3}
    assert abs(drawn[1] / 4000 - 0.5) < 0.03 and abs(drawn[3] / 4000 - 0.2) < 0.03
    # Without a mix, version 2 is version 1.
    plain = engine.read_vary({"vary": [{"at": "X", "present": 70, "min": 1, "max": 3}]})[0]
    assert [engine._times_v2("s", plain, r, 0, 1) for r in range(50)] == \
        [engine._times_v1("s", plain, r, 0, 1) for r in range(50)]


def test_a_config_keeps_its_mix():
    rows = engine.write_vary([{"at": "Patient telecom[*]", "present": 80, "mix": "1:70, 2:30",
                               "min": 1, "max": 9}])
    assert rows == [{"at": "Patient telecom[*]", "present": 80, "mix": [[1, 70.0], [2, 30.0]]}]


def _adt(vary_rows: list[dict], members: int = 60) -> tuple[str, list[str]]:
    from sdg.analyzer.services.layout_service import rules_by_element

    found = standard.files("hl7-adt")
    sample = hl7v2.read_sample(found["sample"])
    entries = hl7v2.read_config(found["master"])
    rules = samples.algorithm_rules(rules_by_element())
    wanted = sorted(n for n in hl7v2.used(entries) if n in rules)
    records = elements.complete(elements.new_members(members), wanted, rules, seed="occ",
                                as_of="2026-06-30")
    text = "".join(hl7v2.stream(sample, entries, records, rules, seed="occ",
                                vary=engine.read_vary({"vary": vary_rows})))
    return text, text.replace("\r", "\n").split("\n")


@pytest.mark.db
def test_an_hl7_v2_segment_is_optional_or_repeats_and_a_field_repeats():
    found = standard.files("hl7-adt")
    places = {p["at"] for p in hl7v2.structure(hl7v2.read_sample(found["sample"]))}
    assert {"NK1", "PID-3", "PID-13"} <= places
    assert "MSH" not in places and "PID" not in places, "what a message is built on is fixed"

    text, lines = _adt([{"at": "NK1", "present": 75, "mix": "1:50, 2:50"},
                        {"at": "PID-13", "min": 2, "max": 2}])
    messages = text.replace("\r", "\n").split("MSH|")[1:]
    assert len(messages) == 60
    per_message = Counter(m.count("\nNK1|") for m in messages)
    assert set(per_message) == {0, 1, 2}, per_message
    for message in messages:
        nk1 = [l for l in message.split("\n") if l.startswith("NK1|")]
        assert [l.split("|")[1] for l in nk1] == [str(i) for i in range(1, len(nk1) + 1)]
    phones = [l.split("|")[13] for l in lines if l.startswith("PID|")]
    assert all(p.count("~") == 1 for p in phones), phones[:3]

    report = hl7v2.check(text, hl7v2.read_sample(found["sample"]).profile)
    assert "no errors" in report.get("summary", ""), report.get("summary")


@pytest.mark.db
def test_an_hl7_v2_segment_left_out_everywhere():
    _text, lines = _adt([{"at": "NK1", "present": 0}], members=10)
    assert not [l for l in lines if l.startswith("NK1|")]


def test_claims_each_and_lines_each_follow_a_mix_or_a_count():
    members = [{"MEMBER_ID": f"M{i}"} for i in range(400)]
    rules: dict[str, dict] = {}
    lines = list(elements.claims_for(
        members, rules, claim_level=[], line_level=[], seed="occ",
        claims_of=elements.claims_of("occ", 100, 1, 1, ((1, 60.0), (2, 30.0), (3, 10.0))),
        lines_of=elements.lines_of("occ", 2, 2)))
    claims = Counter(Counter(l["MEMBER_ID"] for l in lines if l["LINE_NUMBER"] == 1).values())
    assert set(claims) == {1, 2, 3} and claims[1] > claims[2] > claims[3]
    assert all(n == 2 for n in Counter(l["CLAIM_ID"] for l in lines).values())

    ranged = list(elements.claims_for(
        members[:100], rules, claim_level=[], line_level=[], seed="occ",
        lines_of=elements.lines_of("occ", 1, 1, ((1, 50.0), (4, 50.0)))))
    assert set(Counter(l["CLAIM_ID"] for l in ranged).values()) == {1, 4}


@pytest.mark.db
def test_as_measured_is_the_claims_patterns_own_mix():
    from sdg.analyzer import db

    pattern = next((p for p in db.get_all_patterns()
                    if p.get("profile_json") and '"claims_pattern": true' in p["profile_json"]),
                   None)
    if pattern is None:
        pytest.skip("no stored claims pattern")
    shape = client.get(f"/api/patterns/{pattern['id']}/claim-shape").json()
    assert shape["members"] > 0 and shape["claims"] >= shape["members"]
    for mix in (shape["claims_each"], shape["lines_each"]):
        assert mix and all(1 <= n <= 50 for n, _s in mix)
        assert 99 <= sum(s for _n, s in mix) <= 101

    # Claims for its members in that mix: the file has the claims each says.
    written = client.post("/api/export", json={
        "format": "csv", "subject": "claims", "pattern_id": pattern["id"], "rows": 400,
        "claims_mix": [[2, 100]], "lines_mix": [[3, 100]]})
    assert written.status_code == 200, written.text
    rows = written.text.strip().split("\r\n")
    header = rows[0].split(",")
    per_claim = Counter(r.split(",")[header.index("CLAIM_ID")] for r in rows[1:])
    assert set(per_claim.values()) == {3}
    bad = client.post("/api/export", json={"format": "csv", "subject": "claims",
                                           "pattern_id": pattern["id"], "rows": 5,
                                           "claims_mix": "nonsense"})
    assert bad.status_code == 422

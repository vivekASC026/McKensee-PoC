"""The standards audit's Medium findings in the writers, each held by a test.

Pure Python: no database is opened. Batches 26-29 of AUDIT.md.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pyodbc
import pytest

from sdg.writers import elements, rule_values
from sdg.writers.edi_formats import registry
from sdg.writers.samples import cda, fhir
from sdg.writers.samples.base import SampleError
from sdg.writers.snip import engine as snip

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# EDI formats and SNIP partners: only names that are there (path traversal)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["..\\..\\pyproject", "../../x", "../edi_formats/834",
                                  "834.json", ""])
def test_a_format_name_outside_the_shipped_ones_is_refused(name):
    with pytest.raises(FileNotFoundError, match="No EDI format"):
        registry.load_format(name)


def test_a_shipped_format_still_loads_whatever_its_case():
    assert registry.load_format("837P").id == registry.load_format("837p").id


def _snip_dir(tmp_path: Path, monkeypatch) -> Path:
    customer = tmp_path / "snip"
    (customer / "partners").mkdir(parents=True)
    monkeypatch.setenv("SDG_SNIP_DIR", str(customer))
    return customer


def test_a_partner_name_never_reaches_a_file_outside_the_partners_folder(tmp_path, monkeypatch):
    customer = _snip_dir(tmp_path, monkeypatch)
    # A rules file one folder up, where "../evil" would have found it.
    (customer / "evil.json").write_text(json.dumps(
        {"rules": [{"id": "evil", "kind": "element", "segment": "ST", "position": 1,
                    "codes": ["nothing"]}]}), encoding="utf-8")
    rules = snip.ruleset_for("834", "005010X220A1", "../evil")
    assert rules is not None
    assert "evil" not in {r["id"] for r in rules["rules"]}


def test_a_partner_that_is_there_is_applied(tmp_path, monkeypatch):
    customer = _snip_dir(tmp_path, monkeypatch)
    (customer / "partners" / "acme.json").write_text(json.dumps(
        {"rules": [{"id": "acme-1", "kind": "element", "segment": "ST", "position": 1}]}),
        encoding="utf-8")
    rules = snip.ruleset_for("834", "005010X220A1", "acme")
    assert {"id": "acme-1", "level": 7} in [
        {k: r[k] for k in ("id", "level")} for r in rules["rules"] if r["id"] == "acme-1"]


def test_a_broken_rule_file_is_named_not_a_crash(tmp_path, monkeypatch):
    customer = _snip_dir(tmp_path, monkeypatch)
    (customer / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(snip.RuleFileError, match="broken.json"):
        snip.rule_files()
    (customer / "broken.json").write_text(json.dumps({"rules": [{"kind": "element"}]}),
                                          encoding="utf-8")
    with pytest.raises(snip.RuleFileError, match="without an id"):
        snip.rule_files()


# ---------------------------------------------------------------------------
# SNIP regular expressions
# ---------------------------------------------------------------------------

def test_a_decimal_is_read_as_before():
    for good in ("1", "-1", "1.", "1.25", ".5", "-0.5"):
        assert snip._type_ok("R", good) is None, good
    for bad in ("", "-", ".", "1.2.3", "a", "1e5"):
        assert snip._type_ok("R", bad) is not None, bad


def test_a_long_run_of_digits_is_checked_at_once():
    started = time.perf_counter()
    assert snip._type_ok("R", "1" * 50_000 + "x") is not None
    assert time.perf_counter() - started < 1.0


def _tx(*segments: str):
    from sdg.writers.snip.x12 import Segment, Transaction

    made = [Segment(i + 1, s.split("*")[0], s.split("*")[1:], s)
            for i, s in enumerate(segments)]
    return Transaction(made[0], made, made[-1])


def test_a_bad_pattern_in_a_rule_file_is_noted_not_a_crash():
    tx = _tx("ST*834*0001", "NM1*IL*1*A", "SE*3*0001")
    report, seen = snip.Report(), {}
    rule = {"id": "bad", "kind": "element", "segment": "NM1", "position": 3,
            "pattern": "([unclosed"}
    snip._run_rule(rule, tx, [{} for _ in tx.segments], ":", report, seen)
    assert not report.findings
    assert any("not a valid regular expression" in n for n in report.notes[2])


def test_an_invalid_pattern_compiles_to_none():
    assert snip._pattern("([unclosed") is None
    assert snip._pattern("[0-9]{9}").fullmatch("123456789")


# ---------------------------------------------------------------------------
# Rule values and birth dates
# ---------------------------------------------------------------------------

def test_a_rule_that_cannot_make_a_value_is_logged_once(caplog, monkeypatch):
    monkeypatch.setattr(rule_values, "_logged", set())
    rule = {"element": "AUDIT_FIELD", "gen_method": "formatted"}   # a template-less rule
    with caplog.at_level(logging.WARNING, logger="sdg.writers.rule_values"):
        assert rule_values.produce(rule, seed="s", row_index=0) == ""
        assert rule_values.produce(rule, seed="s", row_index=1) == ""
    said = [r for r in caplog.records if "AUDIT_FIELD" in r.getMessage()]
    assert len(said) == 1


def test_a_birth_date_that_is_not_a_date_leaves_no_parent_date():
    assert elements._years_before("01/02/1990", 30) == ""
    assert elements._years_before("1990-02-28", 30) == "1960-02-28"


# ---------------------------------------------------------------------------
# FHIR: the sample's Bundle is not copied into every file
# ---------------------------------------------------------------------------

def _bundle() -> dict:
    return {
        "resourceType": "Bundle", "id": "sample-bundle-id", "type": "searchset",
        "identifier": {"system": "urn:ietf:rfc:3986",
                       "value": "urn:uuid:11111111-2222-3333-4444-555555555555"},
        "meta": {"lastUpdated": "2020-01-01T00:00:00Z", "versionId": "7",
                 "source": "#sample", "profile": ["http://example.org/p"]},
        "timestamp": "2026-10-14T09:00:00Z",
        "total": 99,
        "link": [{"relation": "self", "url": "http://example.org/Patient?identifier=S1"}],
        "signature": {"data": "c2lnbmVk"},
        "entry": [],
    }


def test_the_bundle_shell_has_the_requests_own_id_and_identifier():
    shell = fhir._bundle_shell(_bundle(), "seed-1")
    assert shell["id"] != "sample-bundle-id"
    assert shell["identifier"]["value"].startswith("urn:uuid:")
    assert "11111111" not in shell["identifier"]["value"]
    assert shell["identifier"]["system"] == "urn:ietf:rfc:3986"
    assert shell["meta"] == {"profile": ["http://example.org/p"]}
    for gone in ("link", "signature", "total", "entry"):
        assert gone not in shell
    assert shell["timestamp"] == "2026-10-14T09:00:00Z"
    assert fhir._bundle_shell(_bundle(), "seed-1") == shell, "the same seed, the same file"
    assert fhir._bundle_shell(_bundle(), "seed-2")["id"] != shell["id"]


def test_an_entry_wrapper_keeps_no_conditional_search_or_response_details():
    wrapper = {"fullUrl": "urn:uuid:x",
               "request": {"method": "PUT", "url": "Patient?identifier=S1",
                           "ifNoneExist": "identifier=S1", "ifMatch": 'W/"3"'},
               "response": {"status": "201", "location": "Patient/S1/_history/1",
                            "etag": 'W/"1"'},
               "search": {"mode": "match"}}
    fhir._entry_wrapper(wrapper, "Patient", {"id": "p1"})
    assert wrapper["request"] == {"method": "PUT", "url": "Patient/p1"}
    assert wrapper["response"] == {"status": "201"}
    assert wrapper["search"] == {"mode": "match"}


def test_a_bundle_total_counts_the_entries_written():
    sample = fhir.read_sample((FIXTURES / "fhir" / "claims-bundle.json").read_text("utf-8"))
    sample.bundle["total"] = 1
    reader = fhir.Reader(sample, "seed")
    before = reader.envelope(sample)[0][0]
    reader.closing(sample, 7)
    doc = json.loads(before + reader.after(sample)[0])        # the entries left out
    assert doc["total"] == 7
    assert doc["id"] != "claims-sample"


def test_a_json_sample_nested_too_deeply_is_a_sample_error():
    deep = "[" * 5000 + "]" * 5000
    with pytest.raises(SampleError, match="nests deeper"):
        fhir.read_sample('{"resourceType": "Bundle", "x": ' + deep + "}")
    shallow = json.dumps({"resourceType": "Bundle", "x": [[[[[[[[[[[[]]]]]]]]]]]]})
    with pytest.raises(SampleError, match="no entries"):
        fhir.read_sample(shallow)


def test_a_file_nested_too_deeply_fails_the_check_not_the_server():
    report = fhir.check('{"a": ' + "[" * 5000 + "]" * 5000 + "}", {})
    assert report["passed"] is False
    assert "too deeply" in report["summary"]


# ---------------------------------------------------------------------------
# CDA: comments, prolog, encoding and narrative
# ---------------------------------------------------------------------------

_CDA = """<?xml version="1.0" encoding="{encoding}"?>
<?xml-stylesheet type="text/xsl" href="CDA.xsl"?>
<!-- Patient: Isabella Sample, MRN 12345 -->
<ClinicalDocument xmlns="urn:hl7-org:v3">
\t<!-- a note about the patient -->
\t<title>Summary</title>
\t<component><structuredBody><component><section>
\t\t<text><paragraph>Isabella, born 19 Dec 1950, {word}</paragraph><?pi data?></text>
\t</section></component></structuredBody></component>
</ClinicalDocument>
"""


def test_comments_and_the_prolog_are_not_copied():
    prolog, root, _prefixes = cda._parse(_CDA.format(encoding="UTF-8", word="well"))
    written = prolog + cda.etree.tostring(root, encoding="unicode")
    assert prolog == ('<?xml version="1.0" encoding="UTF-8"?>\n'
                      '<?xml-stylesheet type="text/xsl" href="CDA.xsl"?>\n')
    assert "<!--" not in written and "MRN" not in written and "<?pi" not in written
    assert "\n\t<title>Summary</title>" in written, "the whitespace stays as it was"


def test_a_latin_1_sample_is_read_as_the_text_it_is_and_written_as_utf_8():
    prolog, root, _prefixes = cda._parse(_CDA.format(encoding="ISO-8859-1", word="José"))
    assert 'encoding="UTF-8"' in prolog
    assert "José" in cda.etree.tostring(root, encoding="unicode")


def test_a_narrative_says_the_values_written_not_the_samples():
    _prolog, root, _prefixes = cda._parse(_CDA.format(encoding="UTF-8", word="Isabellas"))
    said: dict[str, str] = {}
    cda._record_said(said, "Isabella", "Zed")
    cda._record_said(said, "19501219", "19800203")
    cda._record_said(said, "F", "M")                  # too short to look for
    cda._reword_narrative(root, said)
    text = cda.etree.tostring(root, encoding="unicode")
    assert "<paragraph>Zed, born 03 Feb 1980, Isabellas</paragraph>" in text
    assert "<title>Summary</title>" in text


# ---------------------------------------------------------------------------
# Validation: a store that cannot be read is said, not passed
# ---------------------------------------------------------------------------

class _Registry:
    def __init__(self, error: Exception):
        self.error = error

    def query(self, *_args, **_kwargs):
        raise self.error


def _first_store_elements() -> list[str]:
    from sdg.plan.combinations import STORE_ELEMENTS

    store, named = next(iter(STORE_ELEMENTS.items()))
    return list(named.values())[:2]


def test_a_store_read_failure_is_recorded():
    from sdg.writers.validate import _StoreCombinations

    wanted = _first_store_elements()
    history = _StoreCombinations(_Registry(pyodbc.OperationalError("08S01", "link down")))
    assert history.held(wanted) is None
    failed = history.failure(wanted)
    assert failed is not None and failed[1] == "OperationalError"


@pytest.mark.parametrize("error", [KeyError("denorm"),
                                   pyodbc.ProgrammingError("42S02", "Invalid object name")])
def test_no_store_to_compare_with_is_not_a_failure(error):
    from sdg.writers.validate import _StoreCombinations

    wanted = _first_store_elements()
    history = _StoreCombinations(_Registry(error))
    assert history.held(wanted) is None
    assert history.failure(wanted) is None

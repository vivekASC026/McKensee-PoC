"""The second full review's findings: the providers refusal at every surface
that makes rows, the bank seed's demo-machine guard, the subscriber ID
surrogate in the FACETS extract, and a narrative matched whatever its case.
No database is written."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).parents[1]
SAMPLE = ROOT / "examples" / "plan-sample.json"  # generates providers


def _provider_plan():
    from sdg.plan.model import Plan

    plan = Plan.load(SAMPLE)
    assert plan.table("dbo.CMC_PRPR_PROV").mode == "generate"
    return plan


# --- providers: refused at every surface, and only providers ------------------

def test_the_refusal_passes_a_plan_that_reuses_providers():
    from sdg.engine.execute import refuse_never_generated

    plan = _provider_plan()
    plan.table("dbo.CMC_PRPR_PROV").mode = "reuse"
    assert refuse_never_generated(plan) is None, "other generated tables are allowed"


def test_a_dry_run_from_the_screens_is_refused(monkeypatch, tmp_path):
    from sdg.api.service import Service
    from sdg.engine.execute import NeverGenerated

    service = Service(work_dir=tmp_path)
    monkeypatch.setattr(Service, "require_plan", lambda self: _provider_plan())
    monkeypatch.setattr(Service, "reference_sets_for_run",
                        lambda self: pytest.fail("read references before refusing"))
    with pytest.raises(NeverGenerated, match="never generated"):
        service.dry_run(3)


def test_a_load_is_refused_before_anything_is_written(monkeypatch, tmp_path):
    import sdg.engine.execute
    import sdg.engine.starts
    from sdg.api import service as module
    from sdg.api.service import Service
    from sdg.engine.execute import NeverGenerated

    monkeypatch.setenv("SDG_DATABASE_GENERATION", "yes")
    service = Service(work_dir=tmp_path)
    service.registry.register("target", "SDMT_A")
    plan = _provider_plan()
    monkeypatch.setattr(Service, "require_plan", lambda self: plan)
    monkeypatch.setattr(Service, "require_model", lambda self: None)
    monkeypatch.setattr(Service, "reference_sets_with_sources", lambda self, p: (None, []))
    monkeypatch.setattr(sdg.engine.starts, "resolve", lambda *a, **k: (plan, []))
    monkeypatch.setattr(sdg.engine.execute, "run_plan",
                        lambda *a, **k: pytest.fail("rows were made"))
    monkeypatch.setattr(module, "write", lambda *a, **k: pytest.fail("rows were written"))
    with pytest.raises(NeverGenerated, match="Nothing was generated"):
        service.generate()


@pytest.mark.parametrize("fmt", ["fixed", "json", "xml", "edi834"])
def test_files_from_the_plan_are_refused_in_every_format(monkeypatch, fmt):
    from fastapi.testclient import TestClient

    from sdg.api import app as app_module

    monkeypatch.setattr(app_module.service, "require_plan", _provider_plan)
    response = TestClient(app_module.app).post(
        "/api/export", json={"format": fmt, "subject": "members", "root_limit": 3})
    assert response.status_code == 409, response.text
    assert "never generated" in response.json()["detail"]


def test_writing_a_load_again_is_refused(monkeypatch):
    from sdg.analyzer.services import generation_requests as gr
    from sdg.api import load_requests
    from sdg.engine.execute import NeverGenerated

    plan = _provider_plan()
    monkeypatch.setattr(gr, "snapshot", lambda value: value)
    monkeypatch.setattr(load_requests.Plan, "from_dict", staticmethod(lambda value: plan))
    original = {"params": {}, "snapshots": {"plan": {}, "algorithms": {}}}
    with pytest.raises(NeverGenerated):
        load_requests.regenerate(
            original, load_references=lambda p: pytest.fail("read references"),
            who="test", version="0", build="0")


@pytest.mark.parametrize("args", [["dryrun"], ["generate", "--dry"]])
def test_the_command_line_refuses_in_words(monkeypatch, tmp_path, args):
    from sdg.cli.main import app

    monkeypatch.setenv("SDG_WORK_DIR", str(tmp_path))
    (tmp_path / "plan.json").write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "never generated" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --- the bank seed needs a machine that says it is for demos ------------------

@pytest.mark.parametrize("env", ["", "customer"])
def test_the_sample_bank_is_refused_off_a_demo_machine(monkeypatch, env):
    import pyodbc

    from seed import bank

    monkeypatch.setattr(pyodbc, "connect", lambda *a, **k: pytest.fail("connected"))
    monkeypatch.setenv("SDG_ENV", env)
    monkeypatch.setenv("SDG_DENORM_SERVER", "localhost")
    with pytest.raises(bank.Refused, match="Nothing was changed"):
        bank.check("SDMT_BANK")


# --- the FACETS extract writes a keyed surrogate for the subscriber ID -------

def test_the_extract_needs_its_key_before_reading(monkeypatch):
    from sdg.extract import run

    monkeypatch.delenv("SDG_EXTRACT_KEY", raising=False)
    with pytest.raises(ValueError, match="SDG_EXTRACT_KEY.*Nothing was extracted"):
        run.extract("FACETS", registry=SimpleNamespace(
            settings=SimpleNamespace(denorm_schema="sdm"),
            connect=lambda name: pytest.fail("connected before the key was checked")))


def test_the_subscriber_id_is_written_as_its_surrogate_in_every_table():
    from sdg.extract import run

    key = b"k" * 16

    class Read:
        description = [("MEME_CK",), ("SBSB_ID",)]

        def __init__(self):
            self.rows = [[(1, "SB0001"), (2, "SB0001"), (3, None)]]

        def execute(self, sql):
            return self

        def fetchmany(self, size):
            return self.rows.pop(0) if self.rows else []

    written: list = []

    class Writer:
        fast_executemany = False

        def execute(self, sql):
            pass

        def executemany(self, sql, rows):
            written.extend(rows)

    read = SimpleNamespace(cursor=lambda: Read())
    write = SimpleNamespace(cursor=lambda: Writer(), commit=lambda: None, rollback=lambda: None)
    run._one_table(read, write, "FACETS", "sdm", "member_denorm", "SELECT", {}, key=key)
    surrogate = run.surrogate("SB0001", key)
    assert written == [(1, surrogate), (2, surrogate), (3, None)]
    assert surrogate != "SB0001" and len(surrogate) == 9
    assert run.surrogate("SB0001", b"x" * 16) != surrogate, "another key, another surrogate"
    assert run.surrogate_columns("FACETS_QA") == set(), "the demo extract is unchanged"


# --- a narrative is matched whatever its case ---------------------------------

def test_a_narrative_in_capitals_says_the_written_value():
    from sdg.writers.samples import cda

    text = ('<?xml version="1.0" encoding="UTF-8"?>\n<ClinicalDocument xmlns="urn:hl7-org:v3">'
            '<component><structuredBody><component><section><text>'
            '<paragraph>JONES, Isabella</paragraph></text></section></component>'
            '</structuredBody></component></ClinicalDocument>\n')
    _prolog, root, _prefixes = cda._parse(text)
    said: dict[str, str] = {}
    cda._record_said(said, "Jones", "Zed")
    cda._record_said(said, "Isabella", "Ann")
    cda._reword_narrative(root, said)
    written = cda.etree.tostring(root, encoding="unicode")
    assert "<paragraph>Zed, Ann</paragraph>" in written
    assert "JONES" not in written

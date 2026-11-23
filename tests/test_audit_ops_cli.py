"""The command line refuses damaged work/ files in words, and refuses work/
that belongs to another database, before touching anything (audit batch 30)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sdg.cli.main import app
from sdg.config import get_settings

runner = CliRunner()


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setenv("SDG_WORK_DIR", str(tmp_path))
    return tmp_path


def test_a_damaged_run_record_is_refused_in_words(work):
    (work / "run-manifest.json").write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["cleanup", "--yes"])
    assert result.exit_code == 1
    assert "could not be read" in result.output
    assert not isinstance(result.exception, ValueError)


def test_a_run_record_without_tables_is_refused_in_words(work):
    (work / "run-manifest.json").write_text(json.dumps(["not", "a", "record"]),
                                            encoding="utf-8")
    result = runner.invoke(app, ["cleanup", "--yes"])
    assert result.exit_code == 1
    assert "is not a run record" in result.output


def test_a_damaged_plan_is_refused_in_words(work):
    (work / "plan.json").write_text(json.dumps({"meta": "old"}), encoding="utf-8")
    result = runner.invoke(app, ["dryrun"])
    assert result.exit_code == 1
    assert "could not be read" in result.output
    assert "sdg plan" in result.output


@pytest.mark.parametrize("args", [
    ["dryrun"], ["generate", "--dry", "--csv", "out"], ["cleanup", "--yes"],
    ["patterns", "--clear"],
])
def test_work_of_another_database_is_refused(work, args):
    settings = get_settings()
    (work / "working-database.txt").write_text(
        f"{settings.mssql.server}__SomeOtherDb", encoding="utf-8")
    (work / "review-decisions.json").write_text("{}", encoding="utf-8")
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "belongs to SomeOtherDb" in result.output
    # Nothing was changed: the decisions file is as it was.
    assert (work / "review-decisions.json").read_text(encoding="utf-8") == "{}"

"""Reading the two demo sources' landscapes, end to end.

FACETS_QA (the health demo source) and BANK_DEMO (the sample bank seed/bank.py
builds for the banking pack's demo) are read the way a team at a new customer
would read theirs: `landscape.read` by name, nothing else known. The model and
profile are kept in tests/fixtures/accelerator/ for the proposer's tests,
which then need no database; a run compares itself with them, and
    set SDM_RECORD_BASELINES=1 && pytest tests/test_accelerator_db.py
records them afresh. Only reads are sent; nothing is created or changed.

The demo machine's login can write (it builds the demo databases), so these
reads pass allow_write_login, as a person would pass --allow-write-login; the
check itself still runs, and what it found is kept in the summary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sdg.accelerator import landscape
from sdg.discovery.model import PhysicalModel
from sdg.profiling.model import ProfileReport

pytestmark = [pytest.mark.db, pytest.mark.slow]

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
RECORDING = os.getenv("SDM_RECORD_BASELINES") == "1"

SOURCES = {"facets_qa": "FACETS_QA", "bank": "BANK_DEMO"}

# Columns holding a person's details, named here by hand rather than by the
# catalog the profile was cleared against: a profile must keep no value of any.
PERSONAL = {
    "facets_qa": {
        "dbo.CMC_MEME_MEMBER": ["MEME_FIRST_NAME", "MEME_LAST_NAME", "MEME_MID_INIT",
                                "MEME_BIRTH_DT", "MEME_SSN", "MEME_ZIP", "MEME_CITY"],
        "dbo.CMC_SBSB_SUBSC": ["SBSB_FIRST_NAME", "SBSB_LAST_NAME"],
    },
    "bank": {
        "dbo.CUSTOMER": ["FIRST_NAME", "LAST_NAME", "BIRTH_DATE", "ADDR_CITY", "ADDR_ZIP",
                         "EMAIL"],
    },
}


@pytest.fixture(scope="module")
def read():
    found = {}

    def reading(key: str) -> landscape.Landscape:
        if key not in found:
            found[key] = landscape.read(SOURCES[key], allow_write_login=True)
        return found[key]

    return reading


def _keys(model: PhysicalModel) -> list[tuple]:
    return sorted((t.name, tuple(k.columns), k.parent_table, tuple(k.parent_columns), k.kind)
                  for t in model.tables for k in t.foreign_keys)


def _structure(model: PhysicalModel) -> list[tuple]:
    return [(t.name, tuple(c.name for c in t.columns), tuple(t.primary_key))
            for t in model.tables]


@pytest.mark.parametrize("key", sorted(SOURCES))
def test_a_source_is_read_and_kept(key, read):
    found = read(key)
    summary = found.summary
    assert summary["tables"] == len(found.model.tables) > 0
    assert summary["shortlisted"], "the tables with rows are shortlisted"
    profiled = {p.table for p in found.profile.columns}
    assert profiled == set(summary["shortlisted"])
    # Ratios and dependencies were measured: the proposer needs both.
    assert found.profile.ratios
    assert found.profile.dependencies

    model_path = FIXTURES / f"{key}-model.json"
    profile_path = FIXTURES / f"{key}-profile.json"
    if not RECORDING:
        kept_model = PhysicalModel.load(model_path)
        kept_profile = ProfileReport.load(profile_path)
        assert _structure(found.model) == _structure(kept_model)
        assert _keys(found.model) == _keys(kept_model)
        assert ({(p.table, p.column) for p in found.profile.columns}
                == {(p.table, p.column) for p in kept_profile.columns})

    _no_personal_values(key, found.profile)
    _only_allowed_values(found.model, found.profile)
    login = summary["login"]
    from sdg.accelerator.sources import WRITE_PERMISSIONS

    assert set(login["permissions"]) == set(WRITE_PERMISSIONS)
    assert {"INSERT", "UPDATE", "DELETE", "ALTER", "CREATE TABLE", "EXECUTE",
            "CONTROL"} <= set(login["permissions"])
    assert login["allowed_write_login"] == login["can_write"]
    if RECORDING:
        found.model.save(model_path)
        found.profile.save(profile_path)


def _no_personal_values(key: str, report: ProfileReport) -> None:
    for table, columns in PERSONAL[key].items():
        for column in columns:
            held = report.column(table, column)
            assert held is not None, f"{table}.{column} was profiled"
            assert held.top_values is None, f"{table}.{column} kept its values"
            assert held.minimum is None and held.maximum is None, f"{table}.{column} kept a range"
            for measure in (held.distribution or {}, held.sequence or {}):
                assert "min" not in measure and "max" not in measure


def _only_allowed_values(model: PhysicalModel, report: ProfileReport) -> None:
    """landscape.scrub's allowlist: no text range, no range or common values
    for a column of unique or many values."""
    for held in report.columns:
        table = model.table(held.table)
        column = table.column(held.column) if table else None
        where = f"{held.table}.{held.column}"
        if column is None or column.is_character or held.cardinality in {"unique", "high"}:
            assert held.minimum is None and held.maximum is None, f"{where} kept a range"
            for measure in (held.distribution or {}, held.sequence or {}):
                assert "min" not in measure and "max" not in measure, f"{where} kept a range"
        if held.cardinality not in {"low", "constant"}:
            assert held.top_values is None, f"{where} kept its values"


@pytest.mark.parametrize("key", sorted(SOURCES))
def test_the_kept_profiles_hold_no_personal_values(key):
    """What is in the repository, whichever run wrote it."""
    report = ProfileReport.load(FIXTURES / f"{key}-profile.json")
    _no_personal_values(key, report)
    _only_allowed_values(PhysicalModel.load(FIXTURES / f"{key}-model.json"), report)


def test_the_summary_says_what_a_person_reads_first(read):
    bank = read("bank").summary
    assert bank["declared_keys"] >= 5
    assert "dbo.CUSTOMER" in bank["person_like_tables"]
    assert {"dbo.BRANCH", "dbo.PRODUCT", "dbo.MCC"} <= set(bank["code_tables"])
    assert bank["hubs"][0]["referenced_by"] >= 1

    health = read("facets_qa").summary
    assert health["inferred_keys"] > 0, "inference finds the keys the demo source leaves out"
    assert "dbo.CMC_MEME_MEMBER" in health["person_like_tables"]
    assert any((pair["start"], pair["end"]) == ("CSPI_EFF_DT", "CSPI_TERM_DT")
               for pair in health["date_pair_tables"].get("dbo.CMC_CSPI_CS_PLAN", []))


def test_a_landscape_is_saved_and_loaded(read):
    found = read("bank")
    folder = landscape.save(found)
    assert folder.name == "BANK_DEMO" and folder.parent.name == "accelerator"
    again = landscape.load("BANK_DEMO")
    assert _keys(again.model) == _keys(found.model)
    assert json.loads(json.dumps(again.summary, default=str)) == json.loads(
        json.dumps(found.summary, default=str))


def test_the_time_limit_holds_back_the_pairwise_measures():
    """Past the limit every table still gets its column measures, and its
    pairwise ones are listed as left out, naming the setting."""
    found = landscape.read("BANK_DEMO", max_minutes=1e-9, allow_write_login=True)
    filled = {t.name for t in found.model.tables if t.row_count}
    held_back = {s["table"] for s in found.summary["skipped"]
                 if s["analysis"] == "pairwise" and "SDG_PROFILE_MAX_MINUTES" in s["reason"]}
    assert held_back == filled & set(found.summary["shortlisted"])
    assert not found.profile.dependencies
    assert {p.table for p in found.profile.columns} == set(found.summary["shortlisted"])

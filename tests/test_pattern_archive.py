"""Patterns carry the build they were found under; an earlier build's are archived.

docs/day0-accelerator-phase4-plan.md, step 2. On a real store made by
`customer-tables` (SDMT_PATTERN_ARCHIVE, made and dropped here by its exact
name):
- a pattern written while a build wrote the running pack is stamped with the
  pack's id and the build's hash; one written under a pack no build wrote is
  not;
- archiving under another build marks the earlier build's patterns, frees
  each key for the new build's walk, and keeps the rest of the row;
- an archived pattern is read-only: save, unsave, origin, edit and certify
  are refused in plain words, and the list says it is archived;
- the new build's walk writes the same key as a new row; archiving again
  under the same build changes nothing; a pattern of no build is untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pyodbc
import pytest

pytestmark = [pytest.mark.db, pytest.mark.slow]

ROOT = Path(__file__).resolve().parent.parent
STORE = "SDMT_PATTERN_ARCHIVE"
BUILD_A, BUILD_B = "a" * 64, "b" * 64


def _master() -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _drop() -> None:
    assert STORE == "SDMT_PATTERN_ARCHIVE"
    with _master() as master:
        master.execute(f"IF DB_ID(N'{STORE}') IS NOT NULL BEGIN ALTER DATABASE [{STORE}] "
                       f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{STORE}]; END")


@pytest.fixture(scope="module")
def store():
    from sdg.config import load_env, not_a_demo_machine

    load_env()
    reason = not_a_demo_machine()
    if reason:
        pytest.skip(f"this proof makes and drops a database: this machine is {reason}")
    with _master() as master:
        if master.execute("SELECT DB_ID(?)", STORE).fetchone()[0] is not None:
            pytest.fail(f"{STORE} is there already: drop it by hand if it is an earlier run's.",
                        pytrace=False)
        master.execute(f"CREATE DATABASE [{STORE}]")
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("SDG_PACKS_DIR", "SDG_PACK")}
        env.update(SDG_DB_DENORM=STORE, SDG_DENORM_SCHEMA="sdm")
        done = subprocess.run([sys.executable, "-m", "sdg.cli.main", "customer-tables",
                               "--no-indexes"], cwd=ROOT, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=900)
        assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
        yield
    finally:
        _drop()


@pytest.fixture
def on_store(store, monkeypatch):
    from sdg.analyzer import db

    monkeypatch.setenv("SDG_DB_DENORM", STORE)
    monkeypatch.setenv("SDG_DENORM_SCHEMA", "sdm")
    monkeypatch.setattr(db, "_TABLE_SHAPE", {})
    return db


def _stamped(monkeypatch, db, version: str | None) -> None:
    monkeypatch.setattr(db, "build_stamp", lambda: {} if version is None else
                        {"pack_id": "gen_pack", "plan_version": version})


def _row(db, pattern_id: int) -> dict:
    return db.get_pattern(pattern_id)


def _combo(level: str):
    from sdg.analyzer.services.discovery_service import canonical_combo

    return canonical_combo({"grgr_ck": [1], "cspi_id": [level]}, entity="claims")


def test_the_build_is_stamped_and_an_earlier_build_is_archived_read_only(on_store,
                                                                          monkeypatch):
    from sdg.analyzer.services import discovery_service, pattern_service

    db = on_store
    _stamped(monkeypatch, db, BUILD_A)
    first = db.upsert_pattern(_combo("SDMT-A"), 7, "SDMT build A pattern", 5, 0.1, 0.2, {"k": 1})
    db.mark_pattern_saved(first, "tester")
    before = _row(db, first)
    assert (before["pack_id"], before["plan_version"], before["archived_at"]) == \
        ("gen_pack", BUILD_A, None)
    _stamped(monkeypatch, db, None)
    other = db.upsert_pattern(_combo("SDMT-H"), 3, "SDMT no build pattern", 4, 0.1, 0.2)
    assert _row(db, other)["pack_id"] is None and _row(db, other)["plan_version"] is None

    assert db.archive_other_builds("gen_pack", BUILD_B) == 1
    after = _row(db, first)
    assert after["archived_at"] is not None
    assert after["pattern_key"] == f"{before['pattern_key'][:188]}#{first}"
    for kept in ("contextual_name", "profile_json", "saved_by", "saved_at", "scope_json",
                 "member_count", "plan_version"):
        assert after[kept] == before[kept], kept
    assert _row(db, other)["archived_at"] is None

    for write in (lambda: db.mark_pattern_saved(first), lambda: db.mark_pattern_unsaved(first),
                  lambda: db.mark_pattern_origin(first, "user_defined")):
        with pytest.raises(db.PatternArchived, match="earlier build of the pack .* read-only"):
            write()
    assert _row(db, first)["saved_by"] == "tester"
    shown = next(p for p in discovery_service.list_saved() if p["pattern_id"] == first)
    assert shown["archived"] is True and shown["plan_version"] == BUILD_A
    row = pattern_service._row_to_pattern(_row(db, first))
    assert row["archived"] is True and row["editable"] is False

    # The new build's walk writes the same slot as a new row.
    _stamped(monkeypatch, db, BUILD_B)
    second = db.upsert_pattern(_combo("SDMT-A"), 7, "SDMT build B pattern", 6, 0.1, 0.2)
    assert second != first and _row(db, second)["plan_version"] == BUILD_B
    assert _row(db, second)["pattern_key"] == before["pattern_key"]
    assert db.archive_other_builds("gen_pack", BUILD_B) == 0
    assert _row(db, second)["archived_at"] is None


def test_an_archived_defined_pattern_is_not_edited_or_certified(on_store, monkeypatch):
    from sdg.analyzer.services import pattern_service

    db = on_store
    _stamped(monkeypatch, db, BUILD_A)
    made = pattern_service.create_user_pattern("SDMT archived defined", {"cspi_id": ["SDMT-D"]},
                                               entity="claims", labels={"plan": "Plan D"})
    pid = made["pattern_id"]
    assert db.archive_other_builds("gen_pack", BUILD_B) >= 1
    with pytest.raises(db.PatternArchived, match="read-only"):
        pattern_service.update_user_pattern(pid, name="SDMT renamed")
    found = db.upsert_pattern(_combo("SDMT-D2"), 1, "SDMT found", 3, 0.1, 0.2)
    with pytest.raises(db.PatternArchived, match="read-only"):
        pattern_service.certify(pid, found)
    assert _row(db, pid)["contextual_name"] == "SDMT archived defined"
    # The same combination may be defined again under the new build.
    _stamped(monkeypatch, db, BUILD_B)
    again = pattern_service.create_user_pattern("SDMT defined again", {"cspi_id": ["SDMT-D"]},
                                                entity="claims", labels={"plan": "Plan D"})
    assert again["pattern_id"] != pid and _row(db, again["pattern_id"])["plan_version"] == BUILD_B


def test_another_packs_patterns_are_never_archived(on_store, monkeypatch):
    db = on_store
    monkeypatch.setattr(db, "build_stamp", lambda: {"pack_id": "other_pack",
                                                    "plan_version": BUILD_A})
    theirs = db.upsert_pattern(_combo("SDMT-O"), 2, "SDMT other pack", 3, 0.1, 0.2)
    db.archive_other_builds("gen_pack", BUILD_B, every=True)
    assert _row(db, theirs)["archived_at"] is None


def test_a_row_written_before_stamping_takes_the_running_build(on_store, monkeypatch):
    db = on_store
    _stamped(monkeypatch, db, None)
    old = db.upsert_pattern(_combo("SDMT-U"), 4, "SDMT unstamped", 3, 0.1, 0.2)
    assert _row(db, old)["pack_id"] is None
    _stamped(monkeypatch, db, BUILD_A)
    db.upsert_pattern(_combo("SDMT-U"), 4, "SDMT unstamped", 3, 0.1, 0.2)
    assert (_row(db, old)["pack_id"], _row(db, old)["plan_version"]) == ("gen_pack", BUILD_A)
    # Stamped once; a later build's walk does not take it over in place.
    _stamped(monkeypatch, db, BUILD_B)
    db.upsert_pattern(_combo("SDMT-U"), 4, "SDMT unstamped", 3, 0.1, 0.2)
    assert _row(db, old)["plan_version"] == BUILD_A


def test_archived_patterns_are_kept_out_of_new_work(on_store, monkeypatch):
    db = on_store
    _stamped(monkeypatch, db, BUILD_A)
    old = db.upsert_pattern(_combo("SDMT-W"), 6, "SDMT for matching", 3, 0.1, 0.2)
    db.archive_other_builds("gen_pack", BUILD_B)
    assert old in {p["id"] for p in db.get_all_patterns()}
    assert old not in {p["id"] for p in db.get_all_patterns(include_archived=False)}


def test_an_archived_pattern_is_no_source_of_new_work(on_store, monkeypatch):
    from sdg.analyzer.services import pattern_data, pattern_service

    db = on_store
    _stamped(monkeypatch, db, BUILD_A)
    old = db.upsert_pattern(_combo("SDMT-C"), 8, "SDMT to clone", 3, 0.1, 0.2)
    db.archive_other_builds("gen_pack", BUILD_B)
    with pytest.raises(db.PatternArchived, match="read-only"):
        pattern_service.clone_pattern(old, "SDMT clone")
    with pytest.raises(pattern_data.StalePattern, match="earlier build"):
        pattern_data.rows_for(old)


def test_a_store_without_the_archive_columns_archives_nothing(on_store, monkeypatch):
    db = on_store
    monkeypatch.setattr(db, "_table_shape", lambda cur, table: (True, {"id", "pattern_key"}))
    assert db.archive_other_builds("gen_pack", BUILD_B) is None

"""Approval against a real store: every conditional UPDATE holds only as written.

tests/test_accelerator_approval.py proves the approval rules against an
in-memory fake of the statements; this proves the statements themselves on
SQL Server. A throwaway store, SDMT_ACCEL_APPROVAL_STORE, is created by its
exact name (refused when it is there already, and never dropped then), its
sdm.source_plans made the way a store set up before built_plan_hash was
added has it, then brought up to date by ensure_table, and dropped by its
exact name afterwards. Demo machines only; no source is read.

For each conditional UPDATE (APPROVE, CHANGED, WITHDRAW, BUILT) every
predicate is broken on its own, and the statement must touch no row; then
all hold, and it touches exactly one. The functions built on them (approve,
changed, withdraw, record_build) are run against the store too.
"""

from __future__ import annotations

import json
import re
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

import pyodbc
import pytest

pytestmark = [pytest.mark.db, pytest.mark.slow]

STORE = "SDMT_ACCEL_APPROVAL_STORE"
FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


# --- the throwaway store ------------------------------------------------------------

def _master() -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _exists() -> bool:
    with closing(_master()) as master:
        return master.execute("SELECT DB_ID(?)", STORE).fetchone()[0] is not None


@contextmanager
def _connect():
    """A connection to the throwaway store, closed afterwards."""
    from sdg.config import get_settings

    with closing(pyodbc.connect(get_settings().mssql.connection_string(STORE),
                                autocommit=False)) as conn:
        yield conn


def _built_plan_hash_width() -> int | None:
    with _connect() as conn:
        return conn.execute("SELECT COL_LENGTH(N'sdm.source_plans', N'built_plan_hash')"
                            ).fetchone()[0]


@pytest.fixture(scope="module")
def store():
    from sdg.config import load_env, not_a_demo_machine

    load_env()
    reason = not_a_demo_machine()
    if reason:
        pytest.skip(f"the approval store proof creates and drops a database: this machine is "
                    f"{reason}")
    assert re.fullmatch(r"SDMT_[A-Z_]+", STORE)
    if _exists():
        pytest.fail(f"{STORE} is there already, and this test drops only the database it "
                    f"creates: drop it by hand if it is an earlier run's, then run again.",
                    pytrace=False)
    mp = pytest.MonkeyPatch()
    created = False
    try:
        with closing(_master()) as master:
            master.execute(f"CREATE DATABASE [{STORE}]")
        created = True
        mp.setenv("SDG_DB_DENORM", STORE)
        mp.setenv("SDG_DENORM_SCHEMA", "sdm")
        with _connect() as conn:
            conn.execute("CREATE SCHEMA sdm")
            conn.commit()
        # sdm.source_plans as a store set up before built_plan_hash was added
        # holds it, then brought up to date as every write does first.
        from sdg.accelerator import approval

        before = approval.DDL.replace("    built_plan_hash CHAR(64)    NULL,\n", "")
        assert "built_plan_hash" not in before
        with _connect() as conn:
            conn.execute(_sql(before))
            conn.execute(_sql(approval.INSERT), "SRC_OLD", "acme", "{}", HASH_A, "submitter",
                         datetime(2026, 11, 21, 8, 0))
            conn.commit()
        # The approval code writes to the store this test made, nowhere else.
        from sdg.analyzer.config import get_connection_string
        from sdg.config import get_settings

        assert get_settings().store.endpoint == get_settings().mssql.endpoint
        assert STORE in get_connection_string()
        # Read before the column is added: no build shown, nothing changed.
        old_row = approval.record("SRC_OLD")
        widths = [_built_plan_hash_width()]
        approval.ensure_table()
        widths.append(_built_plan_hash_width())
        approval.ensure_table()  # add-only: a second run changes nothing
        widths.append(_built_plan_hash_width())
        yield {"name": STORE, "widths": widths, "old_row": old_row}
    finally:
        mp.undo()
        if created:
            with closing(_master()) as master:
                master.execute(f"IF DB_ID(N'{STORE}') IS NOT NULL BEGIN ALTER DATABASE [{STORE}] "
                               f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{STORE}]; "
                               f"END")
        assert not _exists(), f"{STORE} was not dropped"


def _sql(statement: str) -> str:
    from sdg.accelerator import approval

    return approval._sql(statement)


def _rowcount(statement: str, *params) -> int:
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(_sql(statement), *params)
        count = cursor.rowcount
        conn.commit()
        return count


def _row(source: str) -> dict | None:
    from sdg.accelerator import approval

    with _connect() as conn:
        found = conn.cursor().execute(_sql(approval.SELECT), source).fetchone()
        return dict(zip(approval.COLUMNS, found)) if found else None


def _put(source: str, status: str, plan_hash: str) -> None:
    """One row in a known state, written by the store's own INSERT, then set."""
    from sdg.accelerator import approval

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(_sql("DELETE FROM sdm.source_plans WHERE source = ?"), source)
        cursor.execute(_sql(approval.INSERT), source, "acme", "{}", plan_hash, "submitter",
                       datetime(2026, 11, 21, 9, 0))
        cursor.execute(_sql("UPDATE sdm.source_plans SET status = ?, approved_by = ? "
                            "WHERE source = ?"),
                       status, "approver" if status == "approved" else None, source)
        conn.commit()


# --- the table ------------------------------------------------------------------------

def test_a_table_made_before_built_plan_hash_gains_it(store):
    assert store["widths"] == [None, 64, 64]
    old = store["old_row"]
    assert (old["status"], old["plan_hash"], old["built_plan_hash"]) == (
        "pending_review", HASH_A, None)


# --- each statement, each predicate ------------------------------------------------------

def test_the_statements_name_every_predicate_they_hold_to():
    from sdg.accelerator import approval

    def where(statement: str) -> str:
        return " ".join(statement.split("WHERE", 1)[1].split())

    assert where(approval.APPROVE) == "source = ? AND status = 'pending_review' AND plan_hash = ?"
    assert where(approval.CHANGED) == "source = ? AND status <> 'draft' AND plan_hash <> ?"
    assert where(approval.WITHDRAW) == "source = ? AND status IN ('pending_review', 'approved')"
    assert where(approval.BUILT) == "source = ? AND status = 'approved' AND plan_hash = ?"
    assert "built_plan_hash = plan_hash" in approval.BUILT
    for name in ("APPROVE", "CHANGED", "WITHDRAW", "BUILT", "RESUBMIT"):
        assert "source = ?" in getattr(approval, name), name


def test_approve_holds_only_while_pending_and_for_the_hash_submitted(store):
    from sdg.accelerator import approval

    at = datetime(2026, 11, 21, 10, 0)
    _put("SRC_APPROVE", "pending_review", HASH_A)
    # Another source, another hash, another status: no row each time.
    assert _rowcount(approval.APPROVE, "r", at, "r", "SRC_OTHER", HASH_A) == 0
    assert _rowcount(approval.APPROVE, "r", at, "r", "SRC_APPROVE", HASH_B) == 0
    _put("SRC_APPROVE", "draft", HASH_A)
    assert _rowcount(approval.APPROVE, "r", at, "r", "SRC_APPROVE", HASH_A) == 0
    _put("SRC_APPROVE", "approved", HASH_A)
    assert _rowcount(approval.APPROVE, "r", at, "r", "SRC_APPROVE", HASH_A) == 0
    _put("SRC_APPROVE", "pending_review", HASH_A)
    assert _row("SRC_APPROVE")["status"] == "pending_review"
    assert _rowcount(approval.APPROVE, "r", at, "c", "SRC_APPROVE", HASH_A) == 1
    row = _row("SRC_APPROVE")
    assert (row["status"], row["approved_by"], row["confirmed_by"]) == ("approved", "r", "c")


def test_changed_drops_only_a_submitted_or_approved_plan_whose_hash_differs(store):
    from sdg.accelerator import approval

    _put("SRC_CHANGED", "approved", HASH_A)
    assert _rowcount(approval.CHANGED, "SRC_OTHER", HASH_B) == 0
    assert _rowcount(approval.CHANGED, "SRC_CHANGED", HASH_A) == 0  # the same plan
    assert _row("SRC_CHANGED")["status"] == "approved"
    _put("SRC_CHANGED", "draft", HASH_A)
    assert _rowcount(approval.CHANGED, "SRC_CHANGED", HASH_B) == 0  # a draft already
    for status in ("pending_review", "approved"):
        _put("SRC_CHANGED", status, HASH_A)
        assert _rowcount(approval.CHANGED, "SRC_CHANGED", HASH_B) == 1, status
        row = _row("SRC_CHANGED")
        assert (row["status"], row["approved_by"], row["approved_at"]) == ("draft", None, None)


def test_withdraw_takes_back_only_a_submitted_or_approved_plan(store):
    from sdg.accelerator import approval

    _put("SRC_WITHDRAW", "draft", HASH_A)
    assert _rowcount(approval.WITHDRAW, "SRC_WITHDRAW") == 0
    _put("SRC_WITHDRAW", "approved", HASH_A)
    assert _rowcount(approval.WITHDRAW, "SRC_OTHER") == 0
    for status in ("pending_review", "approved"):
        _put("SRC_WITHDRAW", status, HASH_A)
        assert _rowcount(approval.WITHDRAW, "SRC_WITHDRAW") == 1, status
        row = _row("SRC_WITHDRAW")
        assert (row["status"], row["approved_by"]) == ("draft", None)


def test_built_is_recorded_only_for_the_plan_approved(store):
    from sdg.accelerator import approval

    at = datetime(2026, 11, 21, 11, 0)
    for status in ("draft", "pending_review"):
        _put("SRC_BUILT", status, HASH_A)
        assert _rowcount(approval.BUILT, HASH_C, at, "SRC_BUILT", HASH_A) == 0, status
    _put("SRC_BUILT", "approved", HASH_A)
    assert _rowcount(approval.BUILT, HASH_C, at, "SRC_BUILT", HASH_B) == 0
    assert _rowcount(approval.BUILT, HASH_C, at, "SRC_OTHER", HASH_A) == 0
    assert _row("SRC_BUILT")["built_hash"] is None
    assert _rowcount(approval.BUILT, HASH_C, at, "SRC_BUILT", HASH_A) == 1
    row = _row("SRC_BUILT")
    assert (row["built_hash"], row["built_plan_hash"]) == (HASH_C, HASH_A)


# --- the functions built on them ---------------------------------------------------------

@pytest.fixture
def saved(store, tmp_path, monkeypatch):
    """The bank fixture's plan, complete, saved where approved_plan reads it,
    with nobody set as second approver."""
    from sdg.accelerator import approval, sources
    from sdg.accelerator import plan as planning
    from sdg.accelerator.landscape import Landscape
    from sdg.discovery.model import PhysicalModel
    from sdg.profiling.model import ProfileReport

    read = Landscape(name="BANK", model=PhysicalModel.load(FIXTURES / "bank-model.json"),
                     profile=ProfileReport.load(FIXTURES / "bank-profile.json"), summary={})
    held = planning.propose(read)
    graph = planning.graph_for(read, held)
    held = planning.name_record(held, "primary", "customer", "customers", graph=graph)
    held = planning.name_record(held, "child", "account", "accounts", graph=graph)
    held = planning.name_pack(held, "acme_bank", "Acme bank")
    planning.save(held, tmp_path)
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    monkeypatch.setattr(approval, "second_approver", lambda: (None, False))
    with _connect() as conn:
        conn.execute(_sql("DELETE FROM sdm.source_plans WHERE source = ?"), "BANK")
        conn.commit()
    return held, graph


def test_submit_approve_build_change_and_withdraw_against_the_store(saved):
    from sdg.accelerator import approval
    from sdg.accelerator import plan as planning

    held, graph = saved
    digest = planning.plan_hash(held)
    approval.submit("BANK", held, "submitter", graph)
    assert _row("BANK")["status"] == "pending_review"
    with pytest.raises(approval.ApprovalRefused, match="changed since it was submitted"):
        approval.approve("BANK", HASH_B, "approver", "approver")
    approval.approve("BANK", digest, "approver", "approver")
    assert approval.approved_plan("BANK").to_dict() == held.to_dict()

    installed: list[str] = []
    approval.record_build("BANK", digest, HASH_C, install=lambda: installed.append("in"),
                          undo=lambda: installed.append("undo"))
    row = _row("BANK")
    assert installed == ["in"] and (row["built_hash"], row["built_plan_hash"]) == (HASH_C,
                                                                                  digest)
    # The same plan saved again keeps its approval; another drops it.
    approval.changed("BANK", digest)
    assert _row("BANK")["status"] == "approved"
    approval.changed("BANK", HASH_B)
    assert _row("BANK")["status"] == "draft"
    with pytest.raises(approval.ApprovalRefused, match="not approved: nothing was built"):
        approval.record_build("BANK", digest, HASH_C, install=lambda: installed.append("in"),
                              undo=lambda: None)
    assert installed == ["in"]

    approval.submit("BANK", held, "submitter", graph)
    approval.withdraw("BANK")
    assert _row("BANK")["status"] == "draft"
    with pytest.raises(approval.ApprovalRefused, match="draft already"):
        approval.withdraw("BANK")
    # The earlier build stays recorded, with the plan it was built from.
    assert _row("BANK")["built_plan_hash"] == digest
    json.dumps(_row("BANK"), default=str)  # the row reads back whole

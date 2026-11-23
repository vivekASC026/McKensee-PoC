"""Approval of a source plan, against an in-memory store.

The fake store below runs approval.py's own statements -- matched by their
text, so a statement changed in the module and not here fails -- with SQL
Server's semantics for each: the conditional UPDATEs count the rows they
change, and a transaction rolled back leaves the row as it was. Plans are
proposed from the recorded bank fixture: no database.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from sdg.accelerator import approval, plan, sources, workbench
from sdg.accelerator.landscape import Landscape
from sdg.discovery.model import PhysicalModel
from sdg.profiling.model import ProfileReport

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "BANK"


# --- the fake store -----------------------------------------------------------

class FakeStore:
    """sdm.source_plans in memory: one row per source."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.exists = False
        self.executed: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self.rowcount = -1
        self._found: tuple | None = None

    def execute(self, sql: str, *params) -> "FakeCursor":
        store, rows = self.conn.store, self.conn.rows
        store.executed.append((sql, params))
        assert all(not isinstance(p, (list, dict)) for p in params)
        if sql == approval._sql(approval.DDL):
            store.exists = True
            return self
        if sql == approval._sql(approval.DDL_ADDED):
            assert store.exists, "a column was added before the table was ensured"
            return self
        if sql == approval.EXISTS:
            assert params == (approval.table(),)
            self._found = (1 if store.exists else None,)
            return self
        if sql == approval.HAS_BUILT_PLAN_HASH:
            assert params == (approval.table(),) and store.exists
            self._found = (64,)
            return self
        assert store.exists, "a statement ran before the table was ensured"
        if sql in (approval._sql(approval.SELECT), approval._sql(approval.SELECT_HELD)):
            (source,) = params
            row = rows.get(source)
            self._found = tuple(row[c] for c in approval.COLUMNS) if row else None
        elif sql == approval._sql(approval.INSERT):
            source, pack_id, body, digest, by, at = params
            assert source not in rows, "UQ_source_plans_source"
            rows[source] = {c: None for c in approval.COLUMNS} | {
                "status": "pending_review", "pack_id": pack_id, "plan_json": body,
                "plan_hash": digest, "submitted_by": by, "submitted_at": at}
            self.rowcount = 1
        elif sql == approval._sql(approval.RESUBMIT):
            pack_id, body, digest, by, at, source = params
            self.rowcount = self._update(lambda r: r["source"] == source, {
                "status": "pending_review", "pack_id": pack_id, "plan_json": body,
                "plan_hash": digest, "submitted_by": by, "submitted_at": at,
                "approved_by": None, "approved_at": None, "confirmed_by": None})
        elif sql == approval._sql(approval.APPROVE):
            by, at, confirmed, source, digest = params
            self.rowcount = self._update(
                lambda r: r["source"] == source and r["status"] == "pending_review"
                and r["plan_hash"] == digest,
                {"status": "approved", "approved_by": by, "approved_at": at,
                 "confirmed_by": confirmed})
        elif sql == approval._sql(approval.WITHDRAW):
            (source,) = params
            self.rowcount = self._update(
                lambda r: r["source"] == source and r["status"] in ("pending_review", "approved"),
                {"status": "draft", "approved_by": None, "approved_at": None,
                 "confirmed_by": None})
        elif sql == approval._sql(approval.CHANGED):
            source, digest = params
            self.rowcount = self._update(
                lambda r: r["source"] == source and r["status"] != "draft"
                and r["plan_hash"] != digest,
                {"status": "draft", "approved_by": None, "approved_at": None,
                 "confirmed_by": None})
        else:
            raise AssertionError(f"a statement the fake store does not know: {sql}")
        return self

    def _update(self, where, values: dict) -> int:
        hit = [s for s, r in self.conn.rows.items() if where(r | {"source": s})]
        for source in hit:
            self.conn.rows[source].update(values)
        return len(hit)

    def fetchone(self):
        found, self._found = self._found, None
        return found


class FakeConn:
    """One connection: its writes are seen by the store only when committed."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.rows = copy.deepcopy(store.rows)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.store.rows = copy.deepcopy(self.rows)
        self.store.commits += 1

    def rollback(self) -> None:
        self.rows = copy.deepcopy(self.store.rows)
        self.store.rollbacks += 1

    def close(self) -> None:
        pass


@pytest.fixture
def store(monkeypatch, tmp_path) -> FakeStore:
    """The fake store, no second approver, the computer's user "submitter",
    and the plan saved in a folder of the test's own."""
    held = FakeStore()
    monkeypatch.setattr(approval, "_connect", lambda: FakeConn(held))
    monkeypatch.setattr(approval, "second_approver", lambda: (None, False))
    monkeypatch.setattr(approval, "who", lambda: "submitter")
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    return held


def _second(monkeypatch, name: str | None, unreadable: bool = False) -> None:
    monkeypatch.setattr(approval, "second_approver", lambda: (name, unreadable))


# --- plans ----------------------------------------------------------------------

_LANDSCAPE: list[Landscape] = []
_PROPOSED: list[dict] = []


def _landscape() -> Landscape:
    if not _LANDSCAPE:
        _LANDSCAPE.append(Landscape(
            name=SOURCE, model=PhysicalModel.load(FIXTURES / "bank-model.json"),
            profile=ProfileReport.load(FIXTURES / "bank-profile.json"), summary={}))
    return _LANDSCAPE[0]


def _proposed() -> plan.SourcePlan:
    if not _PROPOSED:
        _PROPOSED.append(plan.propose(_landscape()).to_dict())
    return plan.SourcePlan.from_dict(json.loads(json.dumps(_PROPOSED[0])))


def _graph(held: plan.SourcePlan):
    return plan.graph_for(_landscape(), held)


def _complete() -> plan.SourcePlan:
    """A plan with nothing left to do before submit."""
    held = _proposed()
    graph = _graph(held)
    held = plan.name_record(held, "primary", "customer", "customers", graph=graph)
    held = plan.name_record(held, "child", "account", "accounts", graph=graph)
    return plan.name_pack(held, "acme_bank", "Acme bank")


def _saved(folder: Path) -> plan.SourcePlan:
    held = _complete()
    plan.save(held, folder)
    return held


# --- problems -------------------------------------------------------------------

def test_a_complete_plan_has_no_problems():
    assert approval.problems(_complete()) == []


def test_problems_on_a_plan_just_proposed_name_what_is_left_to_do():
    found = approval.problems(_proposed())
    assert any("Name the primary record kind" in p for p in found), found
    assert any("Name the child record kind" in p for p in found), found
    assert any("Name the pack" in p for p in found), found


def test_problems_on_a_plan_without_a_child_or_walk_levels():
    held = _complete()
    held.record_kinds["child"] = None
    for level in held.levels("primary"):
        level.selected = False
    found = approval.problems(held)
    assert any("Choose the child record kind" in p for p in found), found
    assert any("walk level for the primary" in p for p in found), found


def test_a_walk_level_its_record_does_not_reach_is_a_problem():
    held = _complete()
    walks = [lv for lv in held.levels("primary") if lv.selected and lv.kind == "walk"]
    assert walks
    for level in walks:
        level.table, level.path = "dbo.ELSEWHERE", []
    assert any("walk level for the primary" in p for p in approval.problems(held))


def test_problems_on_a_plan_without_structure_or_with_a_column_unclassed():
    held = _complete()
    held.structure[0].columns[0].privacy = "unknown"
    found = approval.problems(held)
    assert any("privacy class" in p and held.structure[0].columns[0].name in p
               for p in found), found
    held.structure = []
    found = approval.problems(held)
    assert any("primary record kind has no denorm table" in p for p in found), found
    assert any("child record kind has no denorm table" in p for p in found), found


def test_the_generated_config_hook_is_one_of_the_gates(monkeypatch):
    monkeypatch.setattr(approval, "generated_config_problems",
                        lambda held, graph=None: [
                            "A config key names a column not in the structure."])
    assert approval.problems(_complete()) == [
        "A config key names a column not in the structure."]


# --- submit ---------------------------------------------------------------------

def test_submit_keeps_one_row_per_source_with_the_snapshot_and_hash(store, tmp_path):
    held = _saved(tmp_path)
    done = approval.submit(SOURCE, held, "submitter")
    assert done["status"] == "pending_review" and done["plan_hash"] == plan.plan_hash(held)
    row = store.rows[SOURCE]
    assert row["status"] == "pending_review" and row["submitted_by"] == "submitter"
    assert row["pack_id"] == "acme_bank" and isinstance(row["submitted_at"], datetime)
    assert plan.plan_hash(plan.SourcePlan.from_dict(json.loads(row["plan_json"]))) \
        == row["plan_hash"]
    # The same plan again, by another: refused, kept in its submitter's name.
    with pytest.raises(approval.ApprovalRefused, match="waiting for review already"):
        approval.submit(SOURCE, held, "other")
    assert store.rows[SOURCE]["submitted_by"] == "submitter"
    # A changed plan, by another: still one row, theirs now.
    approval.submit(SOURCE, plan.name_pack(held, "acme_bank", "Acme retail bank"), "other")
    assert list(store.rows) == [SOURCE] and store.rows[SOURCE]["submitted_by"] == "other"


def test_submit_refuses_a_plan_with_problems_and_writes_nothing(store):
    with pytest.raises(approval.ApprovalRefused, match="cannot be submitted yet"):
        approval.submit(SOURCE, _proposed(), "submitter")
    assert store.rows == {}


def test_submit_refuses_another_sources_plan(store):
    with pytest.raises(approval.ApprovalRefused, match="not OTHER's"):
        approval.submit("OTHER", _complete(), "submitter")


def test_submitting_again_clears_an_approval_of_an_earlier_plan(store, tmp_path, monkeypatch):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    approval.approve(SOURCE, plan.plan_hash(held), "submitter", "submitter")
    with pytest.raises(approval.ApprovalRefused, match="approved already"):
        approval.submit(SOURCE, held, "submitter")
    assert store.rows[SOURCE]["status"] == "approved"
    held = plan.name_pack(held, "acme_bank", "Acme retail bank")
    approval.submit(SOURCE, held, "submitter")
    row = store.rows[SOURCE]
    assert row["status"] == "pending_review" and row["approved_by"] is None
    assert row["approved_at"] is None and row["confirmed_by"] is None


# --- approve --------------------------------------------------------------------

def test_approve_records_who_approved_and_who_confirmed(store, tmp_path, monkeypatch):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    _second(monkeypatch, "Second Approver")
    monkeypatch.setattr(approval, "who", lambda: "reviewer")
    approved_by, confirmed_by = approval.approver("  second approver ")
    assert (approved_by, confirmed_by) == ("Second Approver", "reviewer")
    done = approval.approve(SOURCE, plan.plan_hash(held), approved_by, confirmed_by)
    assert done["status"] == "approved"
    row = store.rows[SOURCE]
    assert row["status"] == "approved" and row["approved_by"] == "Second Approver"
    assert row["confirmed_by"] == "reviewer" and isinstance(row["approved_at"], datetime)
    # Approved once: approving again is refused.
    with pytest.raises(approval.ApprovalRefused, match="is approved"):
        approval.approve(SOURCE, plan.plan_hash(held), approved_by, confirmed_by)


def test_with_no_second_approver_the_computers_user_approves(store, tmp_path):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    assert approval.approver(None) == ("submitter", "submitter")
    approval.approve(SOURCE, plan.plan_hash(held), "submitter", "submitter")
    assert store.rows[SOURCE]["status"] == "approved"


def test_the_approver_must_be_typed_and_a_setting_that_cannot_be_read_refuses(store,
                                                                              monkeypatch):
    _second(monkeypatch, "Second Approver")
    for typed in (None, "", "someone else"):
        with pytest.raises(approval.ApproverRefused, match="Type the second approver"):
            approval.approver(typed)
    _second(monkeypatch, None, unreadable=True)
    with pytest.raises(approval.ApproverRefused, match="can't be read"):
        approval.approver("anyone")
    with pytest.raises(approval.ApproverRefused, match="can't be read"):
        approval.approve(SOURCE, "0" * 64, "anyone", "anyone")


def test_approve_checks_the_approver_whoever_calls_it(store, tmp_path, monkeypatch):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    _second(monkeypatch, "Second Approver")
    for approved_by, confirmed_by in (("someone else", "reviewer"), ("Second Approver", None),
                                      ("Second Approver", " "), ("", "reviewer")):
        with pytest.raises(approval.ApproverRefused):
            approval.approve(SOURCE, plan.plan_hash(held), approved_by, confirmed_by)
    _second(monkeypatch, None)
    with pytest.raises(approval.ApproverRefused):
        approval.approve(SOURCE, plan.plan_hash(held), "submitter", None)
    assert store.rows[SOURCE]["status"] == "pending_review"


def test_self_approval_is_refused_when_a_second_approver_is_set(store, tmp_path, monkeypatch):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "Second Approver")
    _second(monkeypatch, "second approver")
    with pytest.raises(approval.ApprovalRefused, match="cannot approve"):
        approval.approve(SOURCE, plan.plan_hash(held), " SECOND APPROVER", "reviewer")
    row = store.rows[SOURCE]
    assert row["status"] == "pending_review" and row["approved_by"] is None
    assert store.rollbacks >= 1


def test_edit_then_approve_is_refused(store, tmp_path):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    submitted = plan.plan_hash(held)
    # An edit, saved and told to the store, as workbench.edit does.
    edited = plan.name_pack(held, "acme_bank", "Acme retail bank")
    plan.save(edited, tmp_path)
    approval.changed(SOURCE, plan.plan_hash(edited))
    assert store.rows[SOURCE]["status"] == "draft"
    for digest in (submitted, plan.plan_hash(edited)):
        with pytest.raises(approval.ApprovalRefused, match="is draft"):
            approval.approve(SOURCE, digest, "submitter", "submitter")
    assert store.rows[SOURCE]["status"] == "draft"


def test_approving_a_plan_other_than_the_one_submitted_is_refused(store, tmp_path):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    # A hand edit no command saw: the store still says pending_review.
    edited = plan.name_pack(held, "acme_bank", "Acme retail bank")
    with pytest.raises(approval.ApprovalRefused, match=approval.CHANGED_SINCE):
        approval.approve(SOURCE, plan.plan_hash(edited), "submitter", "submitter")
    assert store.rows[SOURCE]["status"] == "pending_review"


def test_approving_a_plan_never_submitted_is_refused(store):
    with pytest.raises(approval.ApprovalRefused, match="never submitted"):
        approval.approve(SOURCE, "0" * 64, "submitter", "submitter")


def test_the_approval_update_is_conditional_on_status_and_hash(store, tmp_path, monkeypatch):
    """Should the row change between the read and the write, the UPDATE
    itself changes nothing and the approval is refused."""
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    real = approval._row

    def raced(cursor, source, held=False):
        row = real(cursor, source, held=held)
        cursor.conn.rows[source]["plan_hash"] = "f" * 64
        return row

    monkeypatch.setattr(approval, "_row", raced)
    with pytest.raises(approval.ApprovalRefused, match=approval.CHANGED_SINCE):
        approval.approve(SOURCE, plan.plan_hash(held), "submitter", "submitter")
    assert store.rows[SOURCE]["status"] == "pending_review"


# --- changed --------------------------------------------------------------------

def test_an_edit_that_leaves_the_plan_as_it_was_keeps_the_approval(store, tmp_path):
    held = _saved(tmp_path)
    approval.submit(SOURCE, held, "submitter")
    approval.approve(SOURCE, plan.plan_hash(held), "submitter", "submitter")
    approval.changed(SOURCE, plan.plan_hash(held))
    assert store.rows[SOURCE]["status"] == "approved"
    approval.changed(SOURCE, "e" * 64)
    row = store.rows[SOURCE]
    assert row["status"] == "draft" and row["approved_by"] is None


def test_changed_creates_nothing_in_a_store_without_the_table(store):
    approval.changed(SOURCE, "e" * 64)
    assert not store.exists and store.rows == {}
    assert [sql for sql, _ in store.executed] == [approval.EXISTS]


def test_every_workbench_edit_tells_the_store(store, tmp_path, monkeypatch):
    for name in ("model", "profile"):
        (tmp_path / f"{name}.json").write_text(
            (FIXTURES / f"bank-{name}.json").read_text("utf-8"), encoding="utf-8")
    (tmp_path / "landscape.json").write_text(json.dumps({"source": SOURCE}), encoding="utf-8")
    plan.save(plan.propose(workbench.saved_landscape(SOURCE)), tmp_path)
    told: list[tuple[str, str]] = []
    monkeypatch.setattr(approval, "changed", lambda source, digest: told.append((source, digest)))
    view = workbench.edit(SOURCE, lambda held, graph: plan.name_record(
        held, "primary", "customer", "customers", graph=graph))
    assert told == [(SOURCE, plan.plan_hash(plan.SourcePlan.from_dict(view["plan"])))]
    assert told[0][1] == plan.plan_hash(plan.load(tmp_path))
    # A refused edit saves nothing and tells nothing.
    with pytest.raises(ValueError):
        workbench.edit(SOURCE, lambda held, graph: plan.name_record(
            held, "primary", "", "", graph=graph))
    assert len(told) == 1


# --- withdraw -------------------------------------------------------------------

def test_withdraw_takes_a_submitted_or_approved_plan_back_to_draft(store, tmp_path):
    held = _saved(tmp_path)
    with pytest.raises(approval.ApprovalRefused, match="never submitted"):
        approval.withdraw(SOURCE)
    approval.submit(SOURCE, held, "submitter")
    assert approval.withdraw(SOURCE)["status"] == "draft"
    assert store.rows[SOURCE]["status"] == "draft"
    with pytest.raises(approval.ApprovalRefused, match="draft already"):
        approval.withdraw(SOURCE)
    approval.submit(SOURCE, held, "submitter")
    approval.approve(SOURCE, plan.plan_hash(held), "submitter", "submitter")
    approval.withdraw(SOURCE)
    row = store.rows[SOURCE]
    assert row["status"] == "draft" and row["approved_by"] is None
    assert row["confirmed_by"] is None and row["approved_at"] is None


# --- approved_plan --------------------------------------------------------------

def test_approved_plan_is_the_plan_approved_and_refuses_a_changed_one(store, tmp_path):
    held = _saved(tmp_path)
    with pytest.raises(approval.ApprovalRefused, match="never submitted"):
        approval.approved_plan(SOURCE)
    approval.submit(SOURCE, held, "submitter")
    with pytest.raises(approval.ApprovalRefused, match="pending review, not approved"):
        approval.approved_plan(SOURCE)
    approval.approve(SOURCE, plan.plan_hash(held), "submitter", "submitter")
    assert plan.plan_hash(approval.approved_plan(SOURCE)) == plan.plan_hash(held)
    # Its status alone, in the file, changes nothing approved.
    held.status = "approved"
    plan.save(held, tmp_path)
    assert plan.plan_hash(approval.approved_plan(SOURCE)) == plan.plan_hash(held)
    # Edited by hand, with no command to tell the store: refused all the same.
    plan.save(plan.name_pack(held, "acme_bank", "Acme retail bank"), tmp_path)
    with pytest.raises(approval.ApprovalRefused, match="changed since it was approved"):
        approval.approved_plan(SOURCE)


# --- the commands ---------------------------------------------------------------

def test_the_commands_submit_approve_and_withdraw(store, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sdg.cli.accelerate import accelerate

    for name in ("model", "profile"):
        (tmp_path / f"{name}.json").write_text(
            (FIXTURES / f"bank-{name}.json").read_text("utf-8"), encoding="utf-8")
    (tmp_path / "landscape.json").write_text(json.dumps({"source": SOURCE}), encoding="utf-8")
    runner = CliRunner()

    def run(*args: str, code: int = 0) -> str:
        done = runner.invoke(accelerate, list(args))
        assert done.exit_code == code, (args, done.output)
        return " ".join(done.output.split())

    run("propose", SOURCE, "--offline")
    said = run("submit", SOURCE, code=1)
    assert "cannot be submitted yet" in said and SOURCE not in store.rows
    run("name", SOURCE, "primary", "customer", "customers")
    run("name", SOURCE, "child", "account", "accounts")
    run("pack-name", SOURCE, "acme_bank", "Acme bank")
    assert "waiting for review" in run("submit", SOURCE)
    assert plan.load(tmp_path).status == "pending_review"
    assert store.rows[SOURCE]["plan_hash"] == plan.plan_hash(plan.load(tmp_path))

    # With a second approver set, the name must be typed, and not the submitter's.
    _second(monkeypatch, "Second Approver")
    assert "Type the second approver" in run("approve", SOURCE, code=1)
    monkeypatch.setattr(approval, "who", lambda: "reviewer")
    said = run("approve", SOURCE, "--approver", "second approver")
    assert "approved by Second Approver, confirmed by reviewer" in said
    assert store.rows[SOURCE]["status"] == "approved"
    assert plan.load(tmp_path).status == "approved"

    # An edit through the commands drops the approval.
    run("pack-name", SOURCE, "acme_bank", "Acme retail bank")
    assert store.rows[SOURCE]["status"] == "draft"
    with pytest.raises(approval.ApprovalRefused):
        approval.approved_plan(SOURCE)

    run("submit", SOURCE)
    assert "a draft again" in run("withdraw", SOURCE)
    assert store.rows[SOURCE]["status"] == "draft"
    assert "draft already" in run("withdraw", SOURCE, code=1)


# --- the table ------------------------------------------------------------------

def _statement(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("IF OBJECT_ID(N'sdm.source_plans', N'U') IS NULL")
    return text[start:text.index("\nGO\n", start)]


@pytest.mark.parametrize("path", [ROOT / "scripts" / "customer" / "setup_tables.sql",
                                  ROOT / "seed" / "ddl_sdm.sql"], ids=lambda p: p.name)
def test_the_setup_scripts_create_the_table_as_ensure_table_does(path):
    assert _statement(path) == approval.DDL


def test_the_table_is_add_only_and_ensured_before_use(store):
    assert approval.DDL.startswith("IF OBJECT_ID(N'sdm.source_plans', N'U') IS NULL\n")
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|ALTER)\b", approval.DDL)
    # A read creates nothing; a write ensures the table first.
    assert approval.record(SOURCE) is None and not store.exists
    with pytest.raises(approval.ApprovalRefused):
        approval.withdraw(SOURCE)
    assert store.executed[1][0] == approval._sql(approval.DDL) and store.exists


def test_every_statement_takes_its_values_as_parameters():
    for name in ("SELECT", "SELECT_HELD", "RESUBMIT", "INSERT", "APPROVE", "WITHDRAW",
                 "CHANGED"):
        statement = getattr(approval, name)
        assert "source = ?" in statement or "VALUES (?" in statement, name
        assert "{" not in statement and "%" not in statement, name

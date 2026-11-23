"""Source setup's phase 2 API: criteria, structure, approval and build.

Fast and read from the recorded bank fixture (tests/fixtures/accelerator/):
the landscape is put in a folder of the test's own and proposed offline,
approvals are kept in the in-memory store of test_accelerator_approval (with
the statement a build adds, from test_accelerator_build), and the pack is
built into tmp_path. Starting SDM on a built pack (startup.check) is proved
in test_accelerator_build; here it is answered at once. Nothing is sent to a
database, and nothing is written in the repository.

Every refusal is checked for its kind (`refusal`) and for its words: the
screen is never told to run a command or pass a flag.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pyodbc
import pytest
from fastapi.testclient import TestClient

from sdg.accelerator import approval, builder, plan, sources, startup, workbench
from sdg.accelerator import source_read
from sdg.accelerator.source_read import NotARead
from sdg.api import jobs
from sdg.api.app import app
from sdg.config import ProtectedDatabase
from tests.test_accelerator_approval import FakeStore
from tests.test_accelerator_build import BuildConn

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
BASE = "/api/accelerator/sources/BANK"
CLI = re.compile(r"--[a-z]|sdg accelerate|`")
HEALTH = re.compile(r"\b(members?|claims?|subscribers?|diagnos\w*|providers?|FACETS)\b", re.I)
STATUS = "child:values:dbo.ACCOUNT.ACCT_STATUS"
SECRET = "Pat Q Example"

client = TestClient(app)

class _FakeRegistry(sources.SourceRegistry):
    def __init__(self) -> None:
        super().__init__(reads_production=True)
        self.login = {"can_write": False, "permissions": {}, "allowed_write_login": False}


def _no_source(*args, **kwargs):
    pytest.fail("this route must not open the source")


@pytest.fixture
def work(tmp_path, monkeypatch):
    """The bank's landscape and proposal in a folder of the test's own, the
    fake store, nobody set as second approver, packs built under tmp_path."""
    folder = tmp_path / "work"
    folder.mkdir()
    for name in ("model", "profile"):
        (folder / f"{name}.json").write_text(
            (FIXTURES / f"bank-{name}.json").read_text("utf-8"), encoding="utf-8")
    (folder / "landscape.json").write_text(json.dumps({"source": "BANK"}), encoding="utf-8")
    store = FakeStore()
    monkeypatch.setattr(sources, "folder_for", lambda name: folder)
    monkeypatch.setattr(sources, "registry_for", _no_source)
    monkeypatch.setattr(approval, "_connect", lambda: BuildConn(store))
    monkeypatch.setattr(approval, "second_approver", lambda: (None, False))
    monkeypatch.setattr(approval, "who", lambda: "submitter")
    monkeypatch.setenv("SDG_PACKS_DIR", str(tmp_path / "packs"))
    monkeypatch.setattr(startup, "check", lambda folder, timeout=300: [])
    started = client.post(f"{BASE}/propose", json={"offline": True})
    assert started.status_code == 200, started.text
    assert _wait(started.json())["state"] == "done"
    return folder, store


def _wait(job: dict) -> dict:
    for _ in range(600):
        if job["state"] != "running":
            return job
        time.sleep(0.05)
        job = client.get(f"/api/accelerator/jobs/{job['id']}").json()
    pytest.fail(f"the job did not finish: {job}")


def _refused(response, status: int, kind: str, said: str = "") -> str:
    """The answer is a refusal of this status and kind, in the screen's words."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["refusal"] == kind, body
    detail = body["detail"]
    assert isinstance(detail, str) and said in detail, detail
    assert not CLI.search(detail), detail
    assert not HEALTH.search(detail), detail
    return detail


def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def _criterion(view: dict, criterion_id: str) -> dict:
    found = [c for role in ("primary", "child") for c in view["plan"]["criteria"][role]
             if c["id"] == criterion_id]
    assert found, criterion_id
    return found[0]


def _column(view: dict, table: str, column: str) -> list[dict]:
    return [c for t in view["plan"]["structure"] for c in t["columns"]
            if c["source_table"] == table and c["source_column"] == column]


def _ready() -> dict:
    """Both record kinds and the pack named: nothing left before submit."""
    _ok(client.post(f"{BASE}/proposal/name",
                    json={"role": "primary", "one": "customer", "many": "customers"}))
    _ok(client.post(f"{BASE}/proposal/name",
                    json={"role": "child", "one": "account", "many": "accounts"}))
    return _ok(client.post(f"{BASE}/pack-name", json={"id": "acme_bank", "name": "Acme bank"}))


def _approved() -> dict:
    _ready()
    shown = _ok(client.post(f"{BASE}/submit"))
    return _ok(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"]}))


# --- criteria ------------------------------------------------------------------------

def test_criteria_are_read_with_their_shares(work):
    view = _ok(client.get(f"{BASE}/criteria"))
    assert view["source"] == "BANK" and view["stale"] is False
    assert set(view["criteria"]) == {"primary", "child"}
    status = next(c for c in view["criteria"]["child"] if c["id"] == STATUS)
    # Proposed empty, for the person to fill: no value read from the source.
    assert status["kind"] == "values" and status["values"] == []
    assert status["selected"] is False and status["share_kept"] is None
    assert status["estimated"] is False
    assert view["notes"] == []


def test_a_criterion_is_set_with_typed_values_and_selected(work):
    folder, _ = work
    typed = _ok(client.post(f"{BASE}/criteria/set",
                            json={"id": STATUS, "values": ["O'Neil", "OPEN"], "op": "not_in"}))
    held = _criterion(typed, STATUS)
    assert held["values"] == ["O'Neil", "OPEN"] and held["op"] == "not_in"
    on = _ok(client.post(f"{BASE}/criteria/select", json={"id": STATUS, "selected": True}))
    assert _criterion(on, STATUS)["selected"] is True
    # Saved: the plan on disk says the same.
    saved = plan.load(folder)
    assert next(c for c in saved.all_criteria() if c.id == STATUS).selected is True


@pytest.mark.parametrize("path, body, said", [
    ("select", {"id": "child:values:dbo.NOWHERE.X", "selected": True}, "no criterion"),
    ("select", {"id": STATUS, "selected": True}, "has no values to keep"),
    ("set", {"id": STATUS, "months": 12}, "is not a window"),
    ("set", {"id": STATUS, "values": ["OPEN"], "op": "maybe"}, "is not in or not_in"),
    ("set", {"id": STATUS, "values": ["x" * 61]}, "at most 60 characters"),
    ("set", {"id": STATUS, "values": ["a\tb"]}, "control character"),
    ("set", {"id": STATUS, "values": [str(n) for n in range(51)]}, "at most 50 values"),
    ("set", {"id": STATUS}, "nothing to set"),
])
def test_a_refused_criterion_edit_is_said_and_saves_nothing(work, path, body, said):
    folder, _ = work
    kept = (folder / "proposal.json").read_text(encoding="utf-8")
    _refused(client.post(f"{BASE}/criteria/{path}", json=body), 400, "invalid", said)
    assert (folder / "proposal.json").read_text(encoding="utf-8") == kept


def test_measuring_is_a_job_and_a_sampled_share_is_marked_estimated(work, monkeypatch):
    folder, _ = work
    _ok(client.post(f"{BASE}/criteria/set", json={"id": STATUS, "values": ["OPEN"]}))
    sent: list[str] = []

    def query(registry, sql, **kwargs):
        sent.append(sql)
        return [{"rows_all": 1000, "rows_kept": 250}]

    monkeypatch.setattr(workbench, "open_source", lambda name, **kw: _FakeRegistry())
    monkeypatch.setattr(source_read, "query", query)
    monkeypatch.setattr(source_read, "is_sampled", lambda table: True)
    job = _wait(_ok(client.post(f"{BASE}/criteria/measure", json={})))
    assert job["state"] == "done", job
    assert job["label"] == "Measure BANK" and job["source"] == "BANK"
    # The bank proposes one criterion; one count was sent for it.
    assert job["result"]["measured"] == 1 and len(sent) == 1
    assert sent[0].lstrip().upper().startswith("SELECT")
    assert "[ACCOUNT]" in sent[0] and "[ACCT_STATUS]" in sent[0]

    view = _ok(client.get(f"{BASE}/criteria"))
    status = next(c for c in view["criteria"]["child"] if c["id"] == STATUS)
    assert status["share_kept"] == 0.25 and status["estimated"] is True
    assert any(n.startswith("Measured on a sample") for n in view["notes"])
    proposal = _ok(client.get(f"{BASE}/proposal"))
    assert STATUS in proposal["estimated"]
    # Setting it again clears the share: it was measured for the values before.
    again = _ok(client.post(f"{BASE}/criteria/set", json={"id": STATUS, "values": ["SHUT"]}))
    assert _criterion(again, STATUS)["share_kept"] is None
    assert STATUS not in again["estimated"]


def test_a_criterion_changed_while_it_was_counted_keeps_no_share(work, monkeypatch):
    _ok(client.post(f"{BASE}/criteria/set", json={"id": STATUS, "values": ["OPEN"]}))

    def query(registry, sql, **kwargs):
        # The person types other values while the count runs.
        changed = client.post(f"{BASE}/criteria/set", json={"id": STATUS, "values": ["SHUT"]})
        assert changed.status_code == 200, changed.text
        return [{"rows_all": 100, "rows_kept": 40}]

    monkeypatch.setattr(workbench, "open_source", lambda name, **kw: _FakeRegistry())
    monkeypatch.setattr(source_read, "query", query)
    assert _wait(_ok(client.post(f"{BASE}/criteria/measure", json={})))["state"] == "done"
    status = next(c for c in _ok(client.get(f"{BASE}/criteria"))["criteria"]["child"]
                  if c["id"] == STATUS)
    assert status["values"] == ["SHUT"] and status["share_kept"] is None


def test_a_typed_value_is_counted_as_a_quoted_literal(work, monkeypatch):
    """Values reach the count only through criteria.literal: a quote is doubled."""
    _ok(client.post(f"{BASE}/criteria/set", json={"id": STATUS, "values": ["O'Neil"]}))
    sent: list[str] = []

    def query(registry, sql, **kwargs):
        sent.append(sql)
        return [{"rows_all": 10, "rows_kept": 1}]

    monkeypatch.setattr(workbench, "open_source", lambda name, **kw: _FakeRegistry())
    monkeypatch.setattr(source_read, "query", query)
    assert _wait(_ok(client.post(f"{BASE}/criteria/measure", json={})))["state"] == "done"
    (statement,) = sent
    assert "'O''Neil'" in statement and "'O'Neil'" not in statement


@pytest.mark.parametrize("raised, status, kind", [
    (ProtectedDatabase(f"BANK is a protected database ({SECRET})"), 409, "protected"),
    (sources.SourceError("SDG_SOURCE_BANK_SERVER is not set"), 409, "source"),
    (NotARead("only reads are sent to a source"), 409, "not_a_read"),
    (pyodbc.OperationalError("08001", f"[08001] cannot reach '{SECRET}' (17) (SQLDriverConnect)"),
     502, "unreadable"),
    (RuntimeError(f"unexpected value '{SECRET}'"), 500, "failed"),
])
def test_opening_the_source_to_measure_refused_is_said_by_its_kind(work, monkeypatch, caplog,
                                                                    raised, status, kind):
    def open_source(name, **kwargs):
        raise raised

    monkeypatch.setattr(workbench, "open_source", open_source)
    detail = _refused(client.post(f"{BASE}/criteria/measure", json={}), status, kind)
    if kind in ("unreadable", "failed"):
        # A driver's or any other failure's own words are never said or logged.
        assert SECRET not in detail and SECRET not in caplog.text


def test_measuring_with_a_login_that_can_write_is_refused_until_allowed(work, monkeypatch):
    asked: list[bool] = []

    def open_source(name, *, allow_write_login=False):
        asked.append(allow_write_login)
        if not allow_write_login:
            raise sources.WriteLogin("This login can INSERT in the source, or pass "
                                     "--allow-write-login to go on with this one.")
        return _FakeRegistry()

    monkeypatch.setattr(workbench, "open_source", open_source)
    monkeypatch.setattr(source_read, "query", lambda registry, sql, **kw: [
        {"rows_all": 10, "rows_kept": 5}])
    detail = _refused(client.post(f"{BASE}/criteria/measure", json={}), 409, "write_login")
    assert detail == "This login can INSERT in the source."
    job = _wait(_ok(client.post(f"{BASE}/criteria/measure", json={"allow_write_login": True})))
    assert job["state"] == "done" and asked == [False, True]


def test_a_failure_while_measuring_is_said_by_its_kind_only(work, monkeypatch):
    def failing(registry, sql, **kwargs):
        raise RuntimeError(f"unexpected value '{SECRET}'")

    _ok(client.post(f"{BASE}/criteria/set", json={"id": STATUS, "values": ["OPEN"]}))
    monkeypatch.setattr(workbench, "open_source", lambda name, **kw: _FakeRegistry())
    monkeypatch.setattr(source_read, "query", failing)
    job = _wait(_ok(client.post(f"{BASE}/criteria/measure", json={})))
    assert job["state"] == "failed"
    assert SECRET not in job["error"] and "BANK could not be measured (RuntimeError)" in job["error"]


# --- structure and the pack's name -------------------------------------------------------

def test_the_structure_lists_every_column_with_its_use_and_privacy(work):
    view = _ok(client.get(f"{BASE}/structure"))
    assert view["stale"] is False
    names = {t["name"]: t for t in view["structure"]}
    assert set(names) == {"customer_denorm", "account_denorm"}
    assert names["account_denorm"]["grain"] == "lines"
    for table in view["structure"]:
        for column in table["columns"]:
            assert column["serves"] and column["privacy"] in ("kept", "withheld", "surrogate")
    assert view["pack"] == {"id": None, "name": None}


def test_a_locked_column_is_never_made_kept(work):
    folder, _ = work
    added = _ok(client.post(f"{BASE}/structure/extra",
                            json={"role": "primary", "table": "dbo.CUSTOMER",
                                  "column": "LAST_NAME"}))
    (column,) = _column(added, "dbo.CUSTOMER", "LAST_NAME")
    assert column["serves"] == "extra"
    assert column["privacy"] == "withheld" and column["locked"] is True
    kept = (folder / "proposal.json").read_text(encoding="utf-8")
    _refused(client.post(f"{BASE}/structure/privacy", json={
        "table": "dbo.CUSTOMER", "column": "LAST_NAME", "privacy": "kept"}),
        400, "invalid", "is locked")
    assert (folder / "proposal.json").read_text(encoding="utf-8") == kept


def test_a_privacy_class_is_set_and_kept_across_edits(work):
    changed = _ok(client.post(f"{BASE}/structure/privacy", json={
        "table": "dbo.CUSTOMER", "column": "SEGMENT", "privacy": "withheld"}))
    assert {c["privacy"] for c in _column(changed, "dbo.CUSTOMER", "SEGMENT")} == {"withheld"}
    # Another edit proposes the structure again, keeping the person's choice.
    named = _ok(client.post(f"{BASE}/proposal/name",
                            json={"role": "primary", "one": "customer", "many": "customers"}))
    assert {c["privacy"] for c in _column(named, "dbo.CUSTOMER", "SEGMENT")} == {"withheld"}


@pytest.mark.parametrize("path, body, said", [
    ("privacy", {"table": "dbo.CUSTOMER", "column": "SEGMENT", "privacy": "public"},
     "is not a privacy class"),
    ("privacy", {"table": "dbo.CUSTOMER", "column": "CUSTOMER_ID", "privacy": "withheld"},
     "join on"),
    ("privacy", {"table": "dbo.CUSTOMER", "column": "BIRTH_DATE", "privacy": "withheld"},
     "derived feature's input"),
    ("privacy", {"table": "dbo.CUSTOMER", "column": "EMAIL", "privacy": "kept"},
     "in no denorm table"),
    ("extra", {"role": "primary", "table": "dbo.GL_ENTRY", "column": "AMOUNT"},
     "does not read dbo.GL_ENTRY"),
    ("extra", {"role": "primary", "table": "dbo.CUSTOMER", "column": "NOTHING"},
     "has no column"),
    ("extra", {"role": "primary", "table": "dbo.CUSTOMER", "column": "SEGMENT"},
     "already"),
    ("extra", {"role": "nobody", "table": "dbo.CUSTOMER", "column": "EMAIL"}, "not a role"),
])
def test_a_refused_structure_edit_is_said_and_saves_nothing(work, path, body, said):
    folder, _ = work
    kept = (folder / "proposal.json").read_text(encoding="utf-8")
    _refused(client.post(f"{BASE}/structure/{path}", json=body), 400, "invalid", said)
    assert (folder / "proposal.json").read_text(encoding="utf-8") == kept


@pytest.mark.parametrize("body, said", [
    ({"id": "Acme Bank", "name": "Acme"}, "lowercase"),
    ({"id": "banking", "name": "Acme"}, "shipped with SDM"),
    ({"id": "acme", "name": "<b>Acme</b>"}, "plain words"),
])
def test_naming_the_pack(work, body, said):
    named = _ok(client.post(f"{BASE}/pack-name", json={"id": "acme_bank", "name": "Acme bank"}))
    assert named["plan"]["pack"] == {"id": "acme_bank", "name": "Acme bank"}
    _refused(client.post(f"{BASE}/pack-name", json=body), 400, "invalid", said)
    assert _ok(client.get(f"{BASE}/structure"))["pack"]["id"] == "acme_bank"


# --- approval -------------------------------------------------------------------------------

def test_a_plan_with_problems_is_not_submitted(work):
    shown = _ok(client.get(f"{BASE}/approval"))
    assert shown["status"] == "draft" and len(shown["plan_hash"]) == 64
    assert any("Name the pack" in p for p in shown["problems"])
    assert shown["submitted_by"] is None and shown["second_approver"] is None
    _refused(client.post(f"{BASE}/submit"), 409, "not_ready", "cannot be submitted yet")
    assert _ok(client.get(f"{BASE}/approval"))["status"] == "draft"


def test_submit_approve_and_withdraw(work):
    folder, store = work
    _ready()
    assert _ok(client.get(f"{BASE}/approval"))["problems"] == []
    shown = _ok(client.post(f"{BASE}/submit"))
    assert shown["status"] == "pending_review" and shown["submitted_by"] == "submitter"
    assert shown["submitted_at"] and shown["approved_by"] is None
    assert store.rows["BANK"]["plan_hash"] == shown["plan_hash"]
    assert plan.load(folder).status == "pending_review"
    _refused(client.post(f"{BASE}/submit"), 409, "approval", "waiting for review already")

    approved = _ok(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"]}))
    assert approved["status"] == "approved"
    assert approved["approved_by"] == approved["confirmed_by"] == "submitter"
    assert approved["approved_at"]
    _refused(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"]}),
             409, "approval", "only a plan waiting for review")

    back = _ok(client.post(f"{BASE}/withdraw"))
    assert back["status"] == "draft" and back["approved_by"] is None
    assert plan.load(folder).status == "draft"
    _refused(client.post(f"{BASE}/withdraw"), 409, "approval", "a draft already")


def test_withdrawing_or_approving_a_plan_never_submitted_is_refused(work):
    _refused(client.post(f"{BASE}/withdraw"), 409, "approval", "never submitted")
    shown = _ok(client.get(f"{BASE}/approval"))
    _refused(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"]}),
             409, "approval", "never submitted")


def test_approving_a_plan_other_than_the_one_shown_is_refused(work):
    _ready()
    shown = _ok(client.post(f"{BASE}/submit"))
    _refused(client.post(f"{BASE}/approve", json={"plan_hash": "0" * 64}), 409, "changed",
             "changed since this page showed it")
    assert _ok(client.get(f"{BASE}/approval"))["status"] == "pending_review"
    assert shown["status"] == "pending_review"


def test_the_second_approver_must_be_typed_to_confirm(work, monkeypatch):
    _ready()
    shown = _ok(client.post(f"{BASE}/submit"))
    monkeypatch.setattr(approval, "second_approver", lambda: ("Rana Second", False))
    assert _ok(client.get(f"{BASE}/approval"))["second_approver"] == "Rana Second"
    for typed in (None, "", "Rana", "Rana Secondd", "submitter"):
        _refused(client.post(f"{BASE}/approve",
                             json={"plan_hash": shown["plan_hash"], "approver": typed}),
                 403, "approver", "Type the second approver's name")
    assert _ok(client.get(f"{BASE}/approval"))["status"] == "pending_review"
    approved = _ok(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"],
                                                        "approver": "  rana second "}))
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "Rana Second" and approved["confirmed_by"] == "submitter"


def test_the_one_who_submitted_may_not_approve_when_a_second_approver_is_set(work, monkeypatch):
    _ready()
    shown = _ok(client.post(f"{BASE}/submit"))
    monkeypatch.setattr(approval, "second_approver", lambda: ("submitter", False))
    _refused(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"],
                                                  "approver": "submitter"}),
             409, "approval", "cannot approve it")


def test_an_approval_setting_that_cannot_be_read_refuses(work, monkeypatch):
    _ready()
    shown = _ok(client.post(f"{BASE}/submit"))
    monkeypatch.setattr(approval, "second_approver", lambda: (None, True))
    assert _ok(client.get(f"{BASE}/approval"))["approver_unreadable"] is True
    _refused(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"]}),
             403, "approver", "can't be read")


def test_approved_is_shown_only_while_the_plan_saved_is_the_one_approved(work):
    folder, _ = work
    approved = _approved()
    assert approved["status"] == "approved"
    # The file changed behind the screen's back (the store was not told).
    held = json.loads((folder / "proposal.json").read_text(encoding="utf-8"))
    held["pack"]["name"] = "Another name"
    (folder / "proposal.json").write_text(json.dumps(held), encoding="utf-8")
    shown = _ok(client.get(f"{BASE}/approval"))
    assert shown["status"] == "changed"
    assert shown["approved_by"] is None and shown["approved_at"] is None
    # Nor is it built: the approval was given to another plan.
    _refused(client.post(f"{BASE}/build"), 409, "changed", "changed since it was approved")


def test_a_plan_changed_behind_the_screen_while_waiting_is_not_approved(work):
    folder, _ = work
    _ready()
    shown = _ok(client.post(f"{BASE}/submit"))
    held = json.loads((folder / "proposal.json").read_text(encoding="utf-8"))
    held["pack"]["name"] = "Another name"
    (folder / "proposal.json").write_text(json.dumps(held), encoding="utf-8")
    assert _ok(client.get(f"{BASE}/approval"))["status"] == "changed"
    _refused(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"]}), 409,
             "changed", "changed since this page showed it")
    # The hash of the plan as it is now was never submitted: the store refuses it too.
    now = _ok(client.get(f"{BASE}/approval"))["plan_hash"]
    _refused(client.post(f"{BASE}/approve", json={"plan_hash": now}), 409, "changed",
             "changed since it was submitted")


def test_any_edit_drops_an_approval(work):
    _approved()
    _ok(client.post(f"{BASE}/proposal/name",
                    json={"role": "primary", "one": "client", "many": "clients"}))
    assert _ok(client.get(f"{BASE}/approval"))["status"] == "draft"


def test_a_store_that_cannot_be_reached_is_said_by_its_codes(work, monkeypatch, caplog):
    def down():
        raise pyodbc.OperationalError("08001", f"[08001] cannot reach '{SECRET}' (17) "
                                               f"(SQLDriverConnect)")

    monkeypatch.setattr(approval, "_connect", down)
    detail = _refused(client.get(f"{BASE}/approval"), 502, "store", "could not be reached")
    assert SECRET not in detail and "08001" in detail
    assert SECRET not in caplog.text
    # An edit is saved, and said as saved, when its approval cannot be dropped.
    detail = _refused(client.post(f"{BASE}/proposal/name", json={
        "role": "primary", "one": "client", "many": "clients"}), 502, "store",
        "The change is saved")
    assert SECRET not in detail


# --- build ------------------------------------------------------------------------------------

def test_build_is_refused_before_it_starts_until_the_plan_is_approved(work, monkeypatch):
    _refused(client.post(f"{BASE}/build"), 409, "pack_id", "Name it under Approval")
    _ready()
    _refused(client.post(f"{BASE}/build"), 409, "approval", "never submitted")
    shown = _ok(client.post(f"{BASE}/submit"))
    _refused(client.post(f"{BASE}/build"), 409, "approval", "not approved")
    _ok(client.post(f"{BASE}/approve", json={"plan_hash": shown["plan_hash"]}))
    monkeypatch.delenv("SDG_PACKS_DIR")
    monkeypatch.setattr(builder, "packs_folder", _unset_packs_dir)
    _refused(client.post(f"{BASE}/build"), 409, "packs_dir", "Set SDG_PACKS_DIR")
    _refused(client.get(f"{BASE}/files"), 409, "packs_dir", "Set SDG_PACKS_DIR")


def _unset_packs_dir(packs_dir=None):
    raise builder.BuildRefused("Set SDG_PACKS_DIR in .env to a folder outside SDM, where "
                               "generated packs are kept, then build again.")


def test_build_writes_the_files_and_lists_them(work, tmp_path):
    _approved()
    before = _ok(client.get(f"{BASE}/files"))
    assert before["pack_id"] == "acme_bank"
    assert [f["name"] for f in before["files"]] == builder.built_files("BANK")
    assert not any(f["built"] for f in before["files"])
    _refused(client.get(f"{BASE}/files/README.txt"), 404, "not_built", "build the pack first")

    job = _wait(_ok(client.post(f"{BASE}/build")))
    assert job["state"] == "done", job
    assert job["label"] == "Build BANK"
    result = job["result"]
    assert result["placed"] is True and result["problems"] == []
    assert result["pack_id"] == "acme_bank"
    assert Path(result["packs_dir"]) == (tmp_path / "packs").resolve()
    assert sorted(result["files"]) == sorted(builder.built_files("BANK"))

    listed = _ok(client.get(f"{BASE}/files"))
    assert all(f["built"] and f["bytes"] > 0 for f in listed["files"])
    shown = _ok(client.get(f"{BASE}/approval"))
    assert shown["status"] == "approved" and shown["built_at"]


def test_a_built_file_is_shown_as_plain_text_and_downloaded_by_name(work):
    _approved()
    assert _wait(_ok(client.post(f"{BASE}/build")))["state"] == "done"
    shown = client.get(f"{BASE}/files/README.txt")
    assert shown.status_code == 200
    assert shown.headers["content-type"].startswith("text/plain")
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in shown.headers
    assert "To install it" in shown.text
    sql = client.get(f"{BASE}/files/extract/BANK.sql", params={"download": True})
    assert sql.status_code == 200
    assert sql.headers["content-disposition"] == 'attachment; filename="BANK.sql"'
    assert "-- table: " in sql.text


@pytest.mark.parametrize("asked", [
    "secret.txt", ".env", "README.TXT", "sql", "extract/OTHER.sql", "..%2Fsecret.txt",
    "..%2F..%2Fsecret.txt", "%2e%2e/%2e%2e/secret.txt", "sql%2F..%2F..%2Fsecret.txt",
    "C:%5CWindows%5Cwin.ini", "%2Fetc%2Fpasswd", "README.txt%00.sql", ".sdm-build.json",
])
@pytest.mark.parametrize("download", ["", "?download=true"])
def test_a_file_not_on_the_list_a_build_writes_is_refused(work, tmp_path, asked, download):
    _approved()
    assert _wait(_ok(client.post(f"{BASE}/build")))["state"] == "done"
    (tmp_path / "packs" / "secret.txt").write_text(SECRET, encoding="utf-8")
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    response = client.get(f"{BASE}/files/{asked}{download}")
    assert SECRET not in response.text
    assert "content-disposition" not in response.headers
    _refused(response, 404, "unknown_file", "not a file a build writes")


def test_a_built_file_that_is_a_link_is_not_shown(work, tmp_path):
    _approved()
    assert _wait(_ok(client.post(f"{BASE}/build")))["state"] == "done"
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET, encoding="utf-8")
    readme = tmp_path / "packs" / "acme_bank" / "README.txt"
    readme.unlink()
    try:
        readme.symlink_to(secret)
    except OSError:
        pytest.skip("this account may not make symbolic links")
    response = client.get(f"{BASE}/files/README.txt")
    assert SECRET not in response.text
    _refused(response, 404, "not_built", "a link to somewhere else")
    listed = _ok(client.get(f"{BASE}/files"))
    assert next(f for f in listed["files"] if f["name"] == "README.txt")["built"] is False


def _junction(link: Path, target: Path) -> None:
    """A directory junction (Windows; no rights needed), else skipped."""
    import subprocess

    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip("directory junctions are not available here")


def test_a_built_folder_reached_through_a_junction_is_not_read(work, tmp_path):
    _approved()
    assert _wait(_ok(client.post(f"{BASE}/build")))["state"] == "done"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "sql").mkdir(parents=True)
    (elsewhere / "sql" / "store_tables.sql").write_text(SECRET, encoding="utf-8")
    pack = tmp_path / "packs" / "acme_bank"

    # A folder inside the pack that leads elsewhere: its file is not shown.
    real_sql = tmp_path / "real_sql"
    (pack / "sql").rename(real_sql)
    _junction(pack / "sql", elsewhere / "sql")
    response = client.get(f"{BASE}/files/sql/store_tables.sql")
    assert SECRET not in response.text
    _refused(response, 404, "not_built", "a link to somewhere else")

    # The pack's own folder leading elsewhere: nothing in it is listed or read.
    pack.rename(tmp_path / "moved_pack")
    _junction(pack, elsewhere)
    for path in ("files", "files/sql/store_tables.sql"):
        response = client.get(f"{BASE}/{path}")
        assert SECRET not in response.text
        _refused(response, 409, "packs_dir", "a link to somewhere else")


def test_a_file_too_large_to_show_is_refused(work, monkeypatch):
    _approved()
    assert _wait(_ok(client.post(f"{BASE}/build")))["state"] == "done"
    monkeypatch.setattr(workbench, "FILE_MAX_BYTES", 10)
    _refused(client.get(f"{BASE}/files/README.txt"), 409, "too_large", "pack folder")


def test_a_built_file_changed_by_hand_to_other_than_text_is_refused(work, tmp_path):
    _approved()
    assert _wait(_ok(client.post(f"{BASE}/build")))["state"] == "done"
    (tmp_path / "packs" / "acme_bank" / "README.txt").write_bytes(b"\xff\xfe\x00not text")
    _refused(client.get(f"{BASE}/files/README.txt"), 409, "not_text", "Build the pack again")


def test_a_pack_sdm_would_not_start_on_is_left_aside_with_its_problems(work, monkeypatch):
    _approved()
    monkeypatch.setattr(startup, "check", lambda folder, timeout=300: [
        "sdg.example does not import: KeyError: 'loaders'"])
    job = _wait(_ok(client.post(f"{BASE}/build")))
    assert job["state"] == "done"
    assert job["result"]["placed"] is False and job["result"]["folder"] is None
    assert job["result"]["problems"] == ["sdg.example does not import: KeyError: 'loaders'"]
    assert not any(f["built"] for f in _ok(client.get(f"{BASE}/files"))["files"])
    shown = _ok(client.get(f"{BASE}/approval"))
    assert shown["status"] == "approved" and shown["built_at"] is None


def test_a_failure_while_building_is_said_by_its_kind_only(work, monkeypatch):
    _approved()

    def failing(source, *, by, packs_dir=None):
        raise RuntimeError(f"unexpected value '{SECRET}'")

    monkeypatch.setattr(builder, "build", failing)
    job = _wait(_ok(client.post(f"{BASE}/build")))
    assert job["state"] == "failed"
    assert SECRET not in job["error"] and "BANK could not be built (RuntimeError)" in job["error"]


def test_a_build_the_builder_refuses_is_said_in_its_words(work, monkeypatch):
    _approved()

    def refused(source, *, by, packs_dir=None):
        raise builder.BuildRefused("A pack is being built already: wait for it to finish.")

    monkeypatch.setattr(builder, "build", refused)
    job = _wait(_ok(client.post(f"{BASE}/build")))
    assert job["state"] == "failed"
    assert job["error"] == "A pack is being built already: wait for it to finish."


def test_one_job_at_a_time(work, monkeypatch):
    _approved()
    monkeypatch.setattr(jobs, "running", lambda kind=None: [object()])
    _refused(client.post(f"{BASE}/build"), 409, "busy", "Wait for it to finish")
    _refused(client.post(f"{BASE}/criteria/measure", json={}), 409, "busy")


# --- the state of the source -------------------------------------------------------------------

def test_a_plan_older_than_the_landscape_is_shown_and_not_changed(work):
    folder, _ = work
    _ready()
    profile = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    profile["profiled"] = "2099-01-01T00:00:00+00:00"
    (folder / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    assert _ok(client.get(f"{BASE}/criteria"))["stale"] is True
    assert _ok(client.get(f"{BASE}/structure"))["stale"] is True
    assert _ok(client.get(f"{BASE}/approval"))["problems"] == [workbench.STALE_SAID]
    for method, path, body in [
        ("post", "criteria/select", {"id": STATUS, "selected": False}),
        ("post", "criteria/set", {"id": STATUS, "values": ["OPEN"]}),
        ("post", "criteria/measure", {}),
        ("post", "structure/privacy", {"table": "dbo.CUSTOMER", "column": "SEGMENT",
                                       "privacy": "withheld"}),
        ("post", "structure/extra", {"role": "primary", "table": "dbo.CUSTOMER",
                                     "column": "LAST_NAME"}),
        ("post", "pack-name", {"id": "other", "name": "Other"}),
        ("post", "submit", None),
        ("post", "build", None),
    ]:
        _refused(getattr(client, method)(f"{BASE}/{path}", json=body), 409, "stale",
                 "earlier read")


def test_without_a_read_or_a_proposal_or_with_a_bad_name(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    for path in ("criteria", "structure", "approval", "files"):
        _refused(client.get(f"{BASE}/{path}"), 409, "not_read", "read it under Sources")
    (tmp_path / "landscape.json").write_text('{"source": "BANK"}', encoding="utf-8")
    for name in ("model", "profile"):
        (tmp_path / f"{name}.json").write_text(
            (FIXTURES / f"bank-{name}.json").read_text("utf-8"), encoding="utf-8")
    for path in ("criteria", "structure", "approval", "files"):
        _refused(client.get(f"{BASE}/{path}"), 409, "no_proposal", "propose first")
    _refused(client.post(f"{BASE}/submit"), 409, "no_proposal")
    _refused(client.get("/api/accelerator/sources/1bad/criteria"), 400, "source_name")



# --- the phase 2 full review -------------------------------------------------------------

def test_a_build_is_shown_only_for_the_plan_it_was_built_from(work):
    _approved()
    job = _wait(_ok(client.post(f"{BASE}/build")))
    assert job["state"] == "done", job
    shown = _ok(client.get(f"{BASE}/approval"))
    assert shown["status"] == "approved" and shown["built_at"]
    # Changed, submitted and approved again: the earlier build is not this plan's.
    _ok(client.post(f"{BASE}/pack-name", json={"id": "acme_bank", "name": "Acme retail bank"}))
    again = _ok(client.post(f"{BASE}/submit"))
    shown = _ok(client.post(f"{BASE}/approve", json={"plan_hash": again["plan_hash"]}))
    assert shown["status"] == "approved" and shown["built_at"] is None


def test_a_key_proposed_a_surrogate_is_kept_only_with_the_persons_word(work):
    child_key = ("dbo.ACCOUNT", "ACCOUNT_ID")
    (column,) = _column(_ok(client.get(f"{BASE}/proposal")), *child_key)
    assert (column["privacy"], column["locked"], column["confirmed_not_personal"]) == (
        "surrogate", False, False)
    body = {"table": child_key[0], "column": child_key[1], "privacy": "kept"}
    _refused(client.post(f"{BASE}/structure/privacy", json=body), 400, "invalid",
             "confirm it holds nothing personal")
    shown = _ok(client.post(f"{BASE}/structure/privacy",
                            json={**body, "confirm_not_personal": True}))
    (column,) = _column(shown, *child_key)
    assert column["privacy"] == "kept" and column["confirmed_not_personal"] is True


def test_the_structure_says_which_links_grow_the_grain_and_refuses_another(work):
    view = _ok(client.get(f"{BASE}/structure"))
    assert view["grain_links"] == {"primary": [], "child": []}
    assert view["grain_accepted"] == {"primary": [], "child": []}
    _refused(client.post(f"{BASE}/structure/grain",
                         json={"role": "primary", "table": "dbo.CUSTOMER"}),
             400, "invalid", "no grain change to accept")


def test_a_join_is_stated_with_its_columns_checked(work):
    body = {"role": "child", "from_table": "dbo.ACCOUNT", "from_columns": ["CUSTOMER_ID"],
            "to_table": "dbo.CUSTOMER", "to_columns": ["CUSTOMER_ID"]}
    shown = _ok(client.post(f"{BASE}/proposal/join", json=body))
    assert {"from_table": "dbo.ACCOUNT", "from_columns": ["CUSTOMER_ID"],
            "to_table": "dbo.CUSTOMER", "to_columns": ["CUSTOMER_ID"], "kind": "stated",
            "coverage": None} in shown["plan"]["joins"]["stated"]
    _refused(client.post(f"{BASE}/proposal/join", json={**body, "to_columns": ["NO_SUCH"]}),
             400, "invalid", "has no column")
    assert client.post(f"{BASE}/proposal/join",
                       json={**body, "from_columns": ["x" * 131]}).status_code == 422


def test_the_view_lists_the_joins_to_the_primary_and_one_named_replaces_another(work):
    """Several joins from the child to the primary are offered for the person
    to choose; naming one replaces a join named before, so one is named."""
    view = _ok(client.get(f"{BASE}/proposal"))
    found = view["primary_joins"]
    assert [j["from_columns"] for j in found] == [["CUSTOMER_ID"]]
    assert found[0]["named"] is False
    other = {"role": "child", "from_table": "dbo.ACCOUNT", "from_columns": ["ACCOUNT_ID"],
             "to_table": "dbo.CUSTOMER", "to_columns": ["CUSTOMER_ID"]}
    two = _ok(client.post(f"{BASE}/proposal/join", json=other))["primary_joins"]
    assert sorted(j["from_columns"][0] for j in two) == ["ACCOUNT_ID", "CUSTOMER_ID"]
    assert [j["from_columns"] for j in two if j["named"]] == [["ACCOUNT_ID"]]
    # The one found by inference is named instead: the stated one goes.
    chosen = _ok(client.post(f"{BASE}/proposal/join",
                             json={**other, "from_columns": ["CUSTOMER_ID"]}))
    assert [j["from_columns"] for j in chosen["primary_joins"] if j["named"]] == [
        ["CUSTOMER_ID"]]
    assert [j["from_columns"] for j in chosen["plan"]["joins"]["stated"]
            if j["to_table"] == "dbo.CUSTOMER"] == [["CUSTOMER_ID"]]
    # As the next load builds it.
    again = _ok(client.get(f"{BASE}/proposal"))["primary_joins"]
    assert [(j["from_columns"], j["named"]) for j in again] == [(["CUSTOMER_ID"], True)]


def test_the_view_says_which_links_grow_the_grain(work):
    view = _ok(client.get(f"{BASE}/proposal"))
    assert view["grain"] == {"primary": [], "child": []}


def test_a_link_without_dates_is_shown_and_its_grain_change_accepted(tmp_path, monkeypatch):
    """The second fixture's primary reaches a level through a link table;
    with its dates taken away, the record repeats once per link row until
    the person accepts it."""
    from sdg.accelerator import criteria

    folder = tmp_path / "work"
    folder.mkdir()
    for name in ("model", "profile"):
        (folder / f"{name}.json").write_text(
            (FIXTURES / f"facets_qa-{name}.json").read_text("utf-8"), encoding="utf-8")
    (folder / "landscape.json").write_text('{"source": "QA"}', encoding="utf-8")
    monkeypatch.setattr(sources, "folder_for", lambda name: folder)
    monkeypatch.setattr(sources, "registry_for", _no_source)
    monkeypatch.setattr(approval, "changed", lambda source, new_hash: None)
    monkeypatch.setattr(criteria, "best_pair", lambda graph, table: None)
    base = "/api/accelerator/sources/QA"
    assert _wait(_ok(client.post(f"{base}/propose", json={"offline": True})))["state"] == "done"
    level, link = "dbo.CMC_PRPR_PROV.PRPR_ID", "dbo.CMC_MEPR_PRIM_PROV"
    view = _ok(client.post(f"{base}/proposal/select-level",
                           json={"role": "primary", "level_id": level, "selected": True}))
    assert view["grain"]["primary"] == [{"table": link, "levels": ["PRPR_ID"],
                                         "accepted": False}]
    accepted = _ok(client.post(f"{base}/structure/grain",
                               json={"role": "primary", "table": link, "accept": True}))
    assert accepted["grain"]["primary"] == [{"table": link, "levels": ["PRPR_ID"],
                                             "accepted": True}]
    assert accepted["plan"]["grain_accepted"]["primary"] == [link]
    back = _ok(client.post(f"{base}/structure/grain",
                           json={"role": "primary", "table": link, "accept": False}))
    assert back["grain"]["primary"][0]["accepted"] is False

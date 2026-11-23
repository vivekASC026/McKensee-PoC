"""Source setup's API: thin routes over the accelerator, refusals in its words.

Fast and read from the recorded fixtures only (tests/fixtures/accelerator/):
the sample bank's model and profile are put in a folder of the test's own,
proposed offline, and every edit is made through the routes. Nothing is
sent to a database: opening a source is replaced wherever a route would.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pyodbc
import pytest
from fastapi.testclient import TestClient

from sdg.accelerator import landscape as reading
from sdg.accelerator import plan as planning
from sdg.accelerator import sources, workbench
from sdg.accelerator.landscape import Landscape
from sdg.api import jobs
from sdg.api.app import app
from sdg.discovery.model import PhysicalModel
from sdg.profiling.model import ProfileReport

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
BASE = "/api/accelerator/sources/BANK"

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_store(monkeypatch):
    """Every edit tells the store (approval.changed), which would reach the
    real one: here an edit's approval is tested by test_accelerator_approval."""
    from sdg.accelerator import approval

    monkeypatch.setattr(approval, "changed", lambda source, new_hash: None)


def _no_source(*args, **kwargs):
    pytest.fail("this route must not open the source")


@pytest.fixture
def bank(tmp_path, monkeypatch):
    """The bank's landscape in a folder of the test's own; the source is never opened."""
    for name in ("model", "profile"):
        (tmp_path / f"{name}.json").write_text(
            (FIXTURES / f"bank-{name}.json").read_text("utf-8"), encoding="utf-8")
    (tmp_path / "landscape.json").write_text(json.dumps({
        "source": "BANK", "tables": 8, "columns": 60, "declared_keys": 7, "inferred_keys": 2,
        "shortlisted": ["dbo.CUSTOMER", "dbo.ACCOUNT"],
        "skipped": [{"table": "dbo.TXN", "analysis": "pairwise", "reason": "budget spent"}],
        "hubs": [{"table": "dbo.CUSTOMER", "referenced_by": 2, "rows": 2000}],
        "code_tables": ["dbo.MCC"], "date_pair_tables": {}, "person_like_tables": {},
        "login": {"can_write": False}, "seconds": 3.5}), encoding="utf-8")
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    monkeypatch.setattr(sources, "registry_for", _no_source)
    return tmp_path


def _wait(job: dict) -> dict:
    for _ in range(200):
        if job["state"] != "running":
            return job
        time.sleep(0.05)
        job = client.get(f"/api/accelerator/jobs/{job['id']}").json()
    pytest.fail(f"the job did not finish: {job}")


def _proposed(bank) -> dict:
    response = client.post(f"{BASE}/propose", json={"offline": True})
    assert response.status_code == 200, response.text
    job = _wait(response.json())
    assert job["state"] == "done", job
    assert (bank / "proposal.json").exists()
    held = client.get(f"{BASE}/proposal")
    assert held.status_code == 200, held.text
    return held.json()


def _tables(view: dict) -> set[str]:
    return {t["table"] for t in view["plan"]["tables"]}


def _level(view: dict, role: str, level_id: str) -> dict:
    found = [lv for lv in view["plan"]["hierarchies"][role]["levels"] if lv["id"] == level_id]
    assert found, f"no {role} level {level_id}"
    return found[0]


# --- proposing ------------------------------------------------------------------

def test_propose_runs_as_a_job_and_the_proposal_carries_tables_and_alternatives(bank):
    assert client.get(f"{BASE}/proposal").json()["plan"] is None
    view = _proposed(bank)
    assert view["stale"] is False
    kinds = view["plan"]["record_kinds"]
    assert kinds["primary"]["table"] == "dbo.CUSTOMER"
    assert kinds["child"]["table"] == "dbo.ACCOUNT"
    assert {"dbo.CUSTOMER", "dbo.ACCOUNT", "dbo.PRODUCT"} <= _tables(view)
    # Every alternative offered is one swap_level takes, and none is a level already.
    for role in ("primary", "child"):
        taken = {lv["id"] for lv in view["plan"]["hierarchies"][role]["levels"]}
        assert not taken & {a["id"] for a in view["alternatives"][role]}


def test_a_second_proposal_needs_force(bank):
    _proposed(bank)
    kept = (bank / "proposal.json").read_text(encoding="utf-8")
    response = client.post(f"{BASE}/propose", json={"offline": True})
    assert response.status_code == 409
    assert response.json()["refusal"] == "proposal_saved"
    assert "replaces it" in response.json()["detail"]
    assert (bank / "proposal.json").read_text(encoding="utf-8") == kept
    response = client.post(f"{BASE}/propose", json={"offline": True, "force": True})
    assert _wait(response.json())["state"] == "done"


def test_propose_without_a_landscape_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    response = client.post(f"{BASE}/propose", json={"offline": True})
    assert response.status_code == 409
    assert response.json() == {"detail": "BANK has not been read yet: read it under Sources "
                                         "first.", "refusal": "not_read"}


def test_the_landscape_summary(bank, tmp_path):
    summary = client.get(f"{BASE}/landscape").json()["summary"]
    assert summary["tables"] == 8 and summary["inferred_keys"] == 2
    assert summary["shortlisted"] == 2
    assert summary["hubs"][0] == {"table": "dbo.CUSTOMER", "referenced_by": 2, "rows": 2000}
    assert summary["skipped"][0]["analysis"] == "pairwise"
    (bank / "landscape.json").unlink()
    assert client.get(f"{BASE}/landscape").json()["summary"] is None


# --- edits ------------------------------------------------------------------------

def test_every_edit_returns_the_plan_with_its_tables_derived_again(bank):
    _proposed(bank)
    product = "dbo.PRODUCT.PRODUCT_CODE"
    off = client.post(f"{BASE}/proposal/select-level",
                      json={"role": "child", "level_id": product, "selected": False})
    assert off.status_code == 200, off.text
    assert _level(off.json(), "child", product)["selected"] is False
    assert "dbo.PRODUCT" not in _tables(off.json())
    # Saved: the next read of the proposal says the same.
    assert "dbo.PRODUCT" not in _tables(client.get(f"{BASE}/proposal").json())

    on = client.post(f"{BASE}/proposal/select-level",
                     json={"role": "child", "level_id": product, "selected": True})
    assert "dbo.PRODUCT" in _tables(on.json())

    selected = [lv["id"] for lv in sorted(on.json()["plan"]["hierarchies"]["child"]["levels"],
                                          key=lambda lv: lv["order"]) if lv["selected"]]
    turned = client.post(f"{BASE}/proposal/reorder",
                         json={"role": "child", "ids": list(reversed(selected))})
    assert turned.status_code == 200, turned.text
    after = sorted(turned.json()["plan"]["hierarchies"]["child"]["levels"],
                   key=lambda lv: lv["order"])
    assert [lv["id"] for lv in after if lv["selected"]] == list(reversed(selected))
    assert _tables(turned.json()) == _tables(on.json())


def test_a_swap_for_a_level_the_proposal_offers_takes_its_place(bank):
    view = _proposed(bank)
    old, new = "dbo.PRODUCT.PRODUCT_CODE", "dbo.PRODUCT.PRODUCT_TYPE"
    assert _level(view, "child", old)["selected"] and not _level(view, "child", new)["selected"]
    place = _level(view, "child", old)["order"]
    swapped = client.post(f"{BASE}/proposal/swap", json={
        "role": "child", "level_id": old, "table": "dbo.PRODUCT", "column": "PRODUCT_TYPE"})
    assert swapped.status_code == 200, swapped.text
    assert _level(swapped.json(), "child", new)["selected"] is True
    assert _level(swapped.json(), "child", new)["order"] == place
    assert _level(swapped.json(), "child", old)["selected"] is False
    # Swapping an unselected level is refused, in the engine's words.
    again = client.post(f"{BASE}/proposal/swap", json={
        "role": "child", "level_id": old, "table": "dbo.PRODUCT", "column": "PRODUCT_TYPE"})
    assert again.status_code == 400 and "not selected" in again.json()["detail"]


def test_a_swap_for_a_new_column_from_the_alternatives_is_taken(tmp_path, monkeypatch):
    """The health demo's fixture has columns the proposal does not offer as
    levels; any one of them offered as an alternative is taken by a swap."""
    for name in ("model", "profile"):
        (tmp_path / f"{name}.json").write_text(
            (FIXTURES / f"facets_qa-{name}.json").read_text("utf-8"), encoding="utf-8")
    (tmp_path / "landscape.json").write_text('{"source": "SDMT_DEMO"}', encoding="utf-8")
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    monkeypatch.setattr(sources, "registry_for", _no_source)
    base = "/api/accelerator/sources/SDMT_DEMO"
    assert _wait(client.post(f"{base}/propose", json={"offline": True}).json())["state"] == "done"
    view = client.get(f"{base}/proposal").json()
    role = "primary"
    choices = view["alternatives"][role]
    assert choices, "the fixture offers alternatives that are not levels yet"
    level = next(lv for lv in view["plan"]["hierarchies"][role]["levels"] if lv["selected"])
    chosen = choices[0]
    swapped = client.post(f"{base}/proposal/swap", json={
        "role": role, "level_id": level["id"], "table": chosen["table"],
        "column": chosen["column"]})
    assert swapped.status_code == 200, swapped.text
    ids = {lv["id"] for lv in swapped.json()["plan"]["hierarchies"][role]["levels"]}
    assert chosen["id"] in ids and level["id"] not in ids
    assert _level(swapped.json(), role, chosen["id"])["selected"] is True
    assert chosen["id"] not in {a["id"] for a in swapped.json()["alternatives"][role]}


def test_naming_and_features_and_choosing_a_record(bank):
    view = _proposed(bank)
    named = client.post(f"{BASE}/proposal/name",
                        json={"role": "primary", "one": "customer", "many": "customers"})
    assert named.status_code == 200, named.text
    assert named.json()["plan"]["record_kinds"]["primary"]["name"] == {
        "one": "customer", "many": "customers"}

    feature = view["plan"]["hierarchies"]["primary"]["features"][0]
    off = client.post(f"{BASE}/proposal/select-feature",
                      json={"role": "primary", "feature_id": feature["id"], "selected": False})
    assert off.status_code == 200, off.text
    assert not next(f for f in off.json()["plan"]["hierarchies"]["primary"]["features"]
                    if f["id"] == feature["id"])["selected"]

    # The bank offers one child candidate and several primary ones: the
    # primary is chosen from its candidates, a child by a table of the person's own.
    others = [c["table"] for c in view["plan"]["candidates"]["primary"]
              if c["table"] != "dbo.CUSTOMER"]
    assert others, "the bank's fixture offers other primary candidates"
    chosen = client.post(f"{BASE}/proposal/choose-record",
                         json={"role": "primary", "table": others[0]})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["plan"]["record_kinds"]["primary"]["table"] == others[0]
    assert others[0] in _tables(chosen.json())

    child = client.post(f"{BASE}/proposal/choose-record",
                        json={"role": "child", "table": "dbo.GL_ENTRY"})
    assert child.status_code == 200, child.text
    assert child.json()["plan"]["record_kinds"]["child"]["table"] == "dbo.GL_ENTRY"
    assert "dbo.GL_ENTRY" in _tables(child.json())


@pytest.mark.parametrize("path, body, said", [
    ("select-level", {"role": "child", "level_id": "dbo.NOT.A_LEVEL", "selected": True},
     "no level"),
    ("select-level", {"role": "nobody", "level_id": "x", "selected": True}, "not a role"),
    ("swap", {"role": "child", "level_id": "dbo.PRODUCT.PRODUCT_CODE", "table": "dbo.CUSTOMER",
              "column": "SEGMENT"}, "primary record"),
    ("name", {"role": "primary", "one": "<b>x</b>", "many": "xs"}, "plain words"),
    ("choose-record", {"role": "child", "table": "dbo.NOWHERE"}, "no table"),
    ("reorder", {"role": "child", "ids": ["dbo.BRANCH.BRANCH_ID", "dbo.BRANCH.BRANCH_ID"]},
     "named twice"),
    ("reorder", {"role": "child", "ids": ["dbo.PRODUCT.PRODUCT_TYPE"]}, "only selected levels"),
    ("reorder", {"role": "child", "ids": ["dbo.NOT.A_LEVEL"]}, "no level"),
    ("select-feature", {"role": "primary", "feature_id": "dbo.CUSTOMER.NOTHING",
                        "selected": True}, "no feature"),
])
def test_a_refused_edit_is_a_400_in_the_engines_words_and_saves_nothing(bank, path, body, said):
    _proposed(bank)
    kept = (bank / "proposal.json").read_text(encoding="utf-8")
    response = client.post(f"{BASE}/proposal/{path}", json=body)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert said in detail
    # The screen is never told to run a command.
    assert "--" not in detail and "sdg accelerate" not in detail
    assert (bank / "proposal.json").read_text(encoding="utf-8") == kept


@pytest.mark.parametrize("path, body", [
    ("select-level", {"role": "child", "level_id": "dbo.PRODUCT.PRODUCT_CODE", "selected": False}),
    ("reorder", {"role": "child", "ids": ["dbo.BRANCH.BRANCH_ID"]}),
    ("select-feature", {"role": "primary", "feature_id": "dbo.CUSTOMER.SEGMENT",
                        "selected": False}),
])
def test_an_edit_without_a_proposal_is_a_409(bank, path, body):
    response = client.post(f"{BASE}/proposal/{path}", json=body)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "BANK has no proposal yet: propose first.",
                               "refusal": "no_proposal"}


def test_a_proposal_older_than_the_landscape_is_shown_stale_and_not_edited(bank):
    _proposed(bank)
    profile = json.loads((bank / "profile.json").read_text(encoding="utf-8"))
    profile["profiled"] = "2099-01-01T00:00:00+00:00"
    (bank / "profile.json").write_text(json.dumps(profile), encoding="utf-8")

    view = client.get(f"{BASE}/proposal").json()
    assert view["stale"] is True and view["stale_reason"] == workbench.STALE_SAID
    assert view["alternatives"] == {"primary": [], "child": []}
    response = client.post(f"{BASE}/proposal/name",
                           json={"role": "primary", "one": "customer", "many": "customers"})
    assert response.status_code == 409
    assert response.json() == {"detail": workbench.STALE_SAID, "refusal": "stale"}

    redone = client.post(f"{BASE}/propose", json={"offline": True, "force": True})
    assert _wait(redone.json())["state"] == "done"
    assert client.get(f"{BASE}/proposal").json()["stale"] is False


# --- reading --------------------------------------------------------------------------

WRITE_LOGIN = ("This login can INSERT in the source, or pass --allow-write-login to go on "
               "with this one.")


class _FakeRegistry(sources.SourceRegistry):
    def __init__(self, allowed: bool):
        super().__init__(reads_production=True)
        self.login = {"can_write": allowed, "permissions": {}, "allowed_write_login": allowed}


def test_a_login_that_can_write_is_refused_until_the_request_allows_it(bank, monkeypatch):
    asked: list[bool] = []

    def registry_for(name, *, allow_write_login=False):
        asked.append(allow_write_login)
        if not allow_write_login:
            raise sources.WriteLogin(WRITE_LOGIN)
        return _FakeRegistry(True)

    def read(name, *, registry, max_minutes=None, on_stage=None, **kwargs):
        assert registry.login["allowed_write_login"]
        on_stage("structure")
        on_stage("profile")
        return Landscape(name, PhysicalModel.load(FIXTURES / "bank-model.json"),
                         ProfileReport.load(FIXTURES / "bank-profile.json"),
                         {"tables": 8, "login": registry.login})

    real = planning.propose
    monkeypatch.setattr(sources, "registry_for", registry_for)
    monkeypatch.setattr(reading, "read", read)
    monkeypatch.setattr(planning, "propose", lambda held, registry=None, **kw: real(held))

    refused = client.post(f"{BASE}/read", json={})
    assert refused.status_code == 409
    # The engine's words, without its command-line advice.
    assert refused.json() == {"detail": "This login can INSERT in the source.",
                              "refusal": "write_login"}
    started = client.post(f"{BASE}/read", json={"allow_write_login": True})
    assert started.status_code == 200, started.text
    job = _wait(started.json())
    assert asked == [False, True]
    assert job["state"] == "done", job
    assert job["result"]["proposed"] is True
    assert job["result"]["summary"]["login_can_write"] is True
    assert job["done"] == job["total"] == 3
    assert (bank / "proposal.json").exists()

    # Read again: the proposal and its choices are kept, never replaced unasked.
    again = _wait(client.post(f"{BASE}/read", json={"allow_write_login": True}).json())
    assert again["state"] == "done" and again["result"]["proposed"] is False
    assert again["total"] == 2


def test_a_database_error_is_said_by_its_codes_only(bank, monkeypatch):
    secret = "Pat Q Example"

    def failing(name, *, allow_write_login=False):
        raise pyodbc.DataError("22018", f"[22018] Conversion failed when converting the "
                                        f"varchar value '{secret}' to data type int. (245) "
                                        f"(SQLExecDirectW)")

    monkeypatch.setattr(sources, "registry_for", failing)
    response = client.post(f"{BASE}/read", json={})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "22018" in detail and secret not in detail


def test_a_refusal_inside_a_job_is_its_error_as_said(bank, monkeypatch):
    def refused(*args, **kwargs):
        raise workbench.Conflict("The landscape went away.")

    monkeypatch.setattr(workbench, "propose_source", refused)
    job = _wait(client.post(f"{BASE}/propose", json={"offline": True}).json())
    assert job["state"] == "failed" and job["error"] == "The landscape went away."


SECRET = "Pat Q Example"


@pytest.mark.parametrize("step, route, body", [
    ("read_source", "read", {}),
    ("propose_source", "propose", {"offline": True}),
])
def test_any_other_failure_inside_a_job_is_said_by_its_kind_only(bank, monkeypatch, caplog,
                                                                  step, route, body):
    """A failure that is not one of the engine's refusals can quote a value it
    was reading: the job says, and logs, its kind, never its text."""
    def failing(*args, **kwargs):
        raise ValueError(f"could not convert string to float: '{SECRET}'")

    monkeypatch.setattr(workbench, "open_source", lambda name, **kw: _FakeRegistry(False))
    monkeypatch.setattr(workbench, step, failing)
    caplog.set_level(logging.DEBUG)
    started = client.post(f"{BASE}/{route}", json=body)
    assert started.status_code == 200, started.text
    job = _wait(started.json())
    assert job["state"] == "failed"
    assert SECRET not in job["error"] and "convert" not in job["error"]
    assert "BANK could not be read or proposed (ValueError)" in job["error"]
    assert SECRET not in caplog.text


def test_any_other_failure_in_a_request_is_said_by_its_kind_only(bank, monkeypatch, caplog):
    def failing(*args, **kwargs):
        raise RuntimeError(f"unexpected value '{SECRET}'")

    monkeypatch.setattr(workbench, "edit", failing)
    caplog.set_level(logging.DEBUG)
    response = client.post(f"{BASE}/proposal/name",
                           json={"role": "primary", "one": "customer", "many": "customers"})
    assert response.status_code == 500
    assert SECRET not in response.text and "RuntimeError" in response.json()["detail"]
    assert SECRET not in caplog.text


def test_a_login_that_can_write_is_refused_on_propose_until_allowed(bank, monkeypatch):
    asked: list[bool] = []

    def registry_for(name, *, allow_write_login=False):
        asked.append(allow_write_login)
        if not allow_write_login:
            raise sources.WriteLogin(WRITE_LOGIN)
        return _FakeRegistry(True)

    real = planning.propose
    monkeypatch.setattr(sources, "registry_for", registry_for)
    monkeypatch.setattr(planning, "propose", lambda held, registry=None, **kw: real(held))
    refused = client.post(f"{BASE}/propose", json={"offline": False})
    assert refused.status_code == 409
    assert refused.json() == {"detail": "This login can INSERT in the source.",
                              "refusal": "write_login"}
    assert not (bank / "proposal.json").exists()
    started = client.post(f"{BASE}/propose", json={"offline": False, "allow_write_login": True})
    assert started.status_code == 200, started.text
    assert _wait(started.json())["state"] == "done"
    assert asked == [False, True]
    assert (bank / "proposal.json").exists()


# --- one change at a time -----------------------------------------------------------

def test_only_one_of_many_concurrent_starts_runs(bank, monkeypatch):
    """Eight requests at once: the check that nothing runs and the start are
    one step, so exactly one starts and the rest are refused."""
    real_start = jobs.start
    release = threading.Event()

    def slow_start(kind, label, work):
        time.sleep(0.2)  # widen the window between the check and the start
        return real_start(kind, label, work)

    def held_work(name, registry, stage):
        release.wait(10)
        return {"source": name, "proposed": True}

    monkeypatch.setattr(jobs, "start", slow_start)
    monkeypatch.setattr(workbench, "propose_source", held_work)
    answers: list[int] = []
    started: list[dict] = []

    def ask() -> None:
        response = TestClient(app).post(f"{BASE}/propose", json={"offline": True})
        answers.append(response.status_code)
        if response.status_code == 200:
            started.append(response.json())

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    release.set()
    assert sorted(answers) == [200] + [409] * 7, answers
    assert _wait(started[0])["state"] == "done"


def test_concurrent_edits_each_keep_their_change(bank, monkeypatch):
    """Edits made at once, each changing a different thing: every one is in
    the saved proposal, none written over by another."""
    _proposed(bank)
    real_save = planning.save

    def slow_save(held, folder):
        time.sleep(0.05)  # widen the window between loading and saving
        return real_save(held, folder)

    monkeypatch.setattr(planning, "save", slow_save)
    edits = [
        ("name", {"role": "primary", "one": "customer", "many": "customers"}),
        ("select-level", {"role": "child", "level_id": "dbo.PRODUCT.PRODUCT_CODE",
                          "selected": False}),
        ("select-level", {"role": "child", "level_id": "dbo.BRANCH.BRANCH_STATE",
                          "selected": True}),
        ("select-feature", {"role": "primary", "feature_id": "dbo.CUSTOMER.SEGMENT",
                            "selected": False}),
        ("name", {"role": "child", "one": "account", "many": "accounts"}),
        ("select-level", {"role": "primary", "level_id": "dbo.CUSTOMER.SEGMENT",
                          "selected": True}),
    ]
    answers: list[int] = []

    def make(path: str, body: dict) -> None:
        answers.append(TestClient(app).post(f"{BASE}/proposal/{path}", json=body).status_code)

    threads = [threading.Thread(target=make, args=edit) for edit in edits]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert answers == [200] * len(edits), answers

    held = planning.load(bank)
    assert held.record_kinds["primary"]["name"] == {"one": "customer", "many": "customers"}
    assert held.record_kinds["child"]["name"] == {"one": "account", "many": "accounts"}
    levels = {(role, lv.id): lv.selected for role in ("primary", "child")
              for lv in held.levels(role)}
    assert levels[("child", "dbo.PRODUCT.PRODUCT_CODE")] is False
    assert levels[("child", "dbo.BRANCH.BRANCH_STATE")] is True
    assert levels[("primary", "dbo.CUSTOMER.SEGMENT")] is True
    assert not next(f for f in held.features("primary")
                    if f.id == "dbo.CUSTOMER.SEGMENT").selected


def test_one_read_at_a_time_and_unknown_jobs(bank, monkeypatch):
    monkeypatch.setattr(jobs, "running", lambda kind=None: [object()])
    response = client.post(f"{BASE}/propose", json={"offline": True})
    assert response.status_code == 409 and "already" in response.json()["detail"]
    assert client.get("/api/accelerator/jobs/nosuchjob").status_code == 404


# --- naming sources -------------------------------------------------------------------

def test_sources_are_listed_from_env_and_added_for_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "_added", set())
    monkeypatch.setattr(workbench, "_accelerator_folder", lambda: tmp_path)
    monkeypatch.setattr(sources, "not_a_demo_machine", lambda: None)
    monkeypatch.setenv("SDG_SOURCE_SDMT_LISTED_TRUST_CERT", "yes")
    monkeypatch.setenv("SDG_SOURCE_SDMT_LISTED_PASSWORD", "not-shown-anywhere")
    (tmp_path / "SDMT_READ_BEFORE").mkdir()
    (tmp_path / "SDMT_READ_BEFORE" / "landscape.json").write_text("{}", encoding="utf-8")

    listed = client.get("/api/accelerator/sources")
    assert listed.status_code == 200
    assert "not-shown-anywhere" not in listed.text
    names = {s["name"]: s for s in listed.json()["sources"]}
    assert names["SDMT_LISTED"]["origins"] == ["env"]
    assert names["SDMT_LISTED"]["database"] == "SDMT_LISTED"
    assert names["SDMT_READ_BEFORE"]["read"] is True
    assert "SDMT_ADDED" not in names

    added = client.post("/api/accelerator/sources", json={"name": "sdmt_added"})
    assert added.status_code == 200, added.text
    names = {s["name"]: s for s in added.json()["sources"]}
    assert names["SDMT_ADDED"]["origins"] == ["added"]
    assert names["SDMT_ADDED"]["read"] is False

    bad = client.post("/api/accelerator/sources", json={"name": "1 bad name"})
    assert bad.status_code == 400 and "not a source name" in bad.json()["detail"]


def test_a_source_without_its_own_login_is_refused_on_a_customer_machine(monkeypatch):
    monkeypatch.setattr(workbench, "_added", set())
    monkeypatch.setattr(sources, "not_a_demo_machine", lambda: "a customer's machine")
    response = client.post("/api/accelerator/sources", json={"name": "SDMT_NO_LOGIN"})
    assert response.status_code == 409
    assert "own login is required" in response.json()["detail"]
    assert "SDMT_NO_LOGIN" not in workbench._added

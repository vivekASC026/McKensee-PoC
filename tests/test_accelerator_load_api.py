"""Source setup's phase 3 API: the Load tab's routes.

Fast: the engine (sdg/accelerator/loading.py, proved against SQL Server in
test_accelerator_load) is stood in for, so this proves only what the routes
add. Each answers in the engine's words with a refusal kind, the pack, the
store and the source's login are checked before a load's job starts, a stop
reaches the job, and nothing runs while another job does. No database is
reached.
"""

from __future__ import annotations

import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from sdg.accelerator import loading, sources, workbench
from sdg.api import jobs
from sdg.api.app import app

BASE = "/api/accelerator/sources/BANK"
CLI = re.compile(r"--[a-z]|sdg accelerate|`")

client = TestClient(app)


def _wait(job: dict) -> dict:
    for _ in range(600):
        if job["state"] != "running":
            return job
        time.sleep(0.02)
        job = client.get(f"/api/accelerator/jobs/{job['id']}").json()
    pytest.fail(f"the job did not finish: {job}")


def _refused(response, status: int, kind: str, words: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["refusal"] == kind and words in body["detail"], body
    assert not CLI.search(body["detail"]), body["detail"]
    return body


@pytest.fixture
def engine(monkeypatch):
    """The engine stood in for; what each call was given is recorded."""
    seen: dict = {"opened": []}

    def opened(name, *, allow_write_login=False):
        seen["opened"].append((name, allow_write_login))
        return "checked registry"

    monkeypatch.setattr(workbench, "open_source", opened)
    monkeypatch.setattr(loading, "status", lambda name: {"source": name, "built": {}})
    monkeypatch.setattr(loading, "create_tables", lambda name: {"store": {"ready": True}})
    monkeypatch.setattr(loading, "refuse_loading", lambda name: None)
    return seen


def test_the_status_and_creating_the_tables_answer_in_the_engines_words(engine, monkeypatch):
    assert client.get(f"{BASE}/load").json() == {"source": "BANK", "built": {}}
    assert client.post(f"{BASE}/store-tables").json() == {"store": {"ready": True}}

    def not_built(name):
        raise loading.LoadRefused("BANK's pack has not been built: build it under Build first.",
                                  "not_built")

    monkeypatch.setattr(loading, "status", not_built)
    _refused(client.get(f"{BASE}/load"), 409, "not_built", "build it under Build")

    def dba(name):
        raise loading.LoadRefused("The store's login may not create tables in sdm: give "
                                  "sql/store_tables.sql to the DBA to run.", "dba")

    monkeypatch.setattr(loading, "create_tables", dba)
    _refused(client.post(f"{BASE}/store-tables"), 409, "dba", "to the DBA")


def test_a_store_that_cannot_be_reached_is_said_by_its_codes(engine, monkeypatch):
    import pyodbc

    def down(name):
        raise pyodbc.OperationalError("08001", "[08001] cannot open server 'Pat Q Example' (17)")

    monkeypatch.setattr(loading, "status", down)
    body = _refused(client.get(f"{BASE}/load"), 502, "store", "could not be reached")
    assert "Pat Q Example" not in body["detail"]


def test_a_load_is_refused_before_its_job_for_the_tables_and_the_login(engine, monkeypatch):
    def not_ready(name):
        raise loading.LoadRefused("customers_denorm is not ready in the store: create the "
                                  "tables first.", "tables")

    monkeypatch.setattr(loading, "refuse_loading", not_ready)
    _refused(client.post(f"{BASE}/load", json={}), 409, "tables", "create the tables first")
    assert engine["opened"] == []

    monkeypatch.setattr(loading, "refuse_loading", lambda name: None)

    def writes(name, *, allow_write_login=False):
        raise sources.WriteLogin("This login can INSERT in the source. Ask for a read-only login, or pass "
                                 "--allow-write-login to go on with this one.")

    monkeypatch.setattr(workbench, "open_source", writes)
    _refused(client.post(f"{BASE}/load", json={}), 409, "write_login", "This login can INSERT")


def test_a_load_runs_as_a_job_with_the_checked_login_and_a_stop(engine, monkeypatch):
    given: dict = {}
    inside, go_on = threading.Event(), threading.Event()

    def load(name, told, stop, *, checker=None, allow_write_login=False):
        given.update(name=name, checker=checker, before=stop())
        told(0, 2, "Loading")
        inside.set()
        go_on.wait(5)
        given["stopped"] = stop()
        return {"source": name, "stopped": given["stopped"]}

    monkeypatch.setattr(loading, "load", load)
    job = client.post(f"{BASE}/load", json={"allow_write_login": True}).json()
    assert job["label"] == "Load BANK" and job["source"] == "BANK"
    assert engine["opened"] == [("BANK", True)]
    assert inside.wait(5)
    _refused(client.post(f"{BASE}/store-tables"), 409, "busy", "built or loaded already")
    _refused(client.post(f"{BASE}/load", json={}), 409, "busy", "Wait for it to finish")
    asked = client.post(f"/api/accelerator/jobs/{job['id']}/stop").json()
    assert asked["asked"] is True
    go_on.set()
    done = _wait(job)
    assert done["state"] == "done" and done["result"] == {"source": "BANK", "stopped": True}
    # Not stopped until asked, then stopped.
    assert given == {"name": "BANK", "checker": "checked registry", "before": False,
                     "stopped": True}


def test_a_refusal_inside_the_load_is_the_jobs_error_as_said(engine, monkeypatch):
    def drifted(name, told, stop, *, checker=None, allow_write_login=False):
        raise loading.LoadRefused("The source changed since the plan was built, so nothing "
                                  "was loaded. dbo.CUSTOMER.EMAIL is not in BANK any more.",
                                  "drift")

    monkeypatch.setattr(loading, "load", drifted)
    done = _wait(client.post(f"{BASE}/load", json={}).json())
    assert done["state"] == "failed"
    assert done["error"].startswith("The source changed since the plan was built")


def test_a_driver_failure_inside_the_load_is_said_by_its_codes(engine, monkeypatch):
    import pyodbc

    def failed(name, told, stop, *, checker=None, allow_write_login=False):
        raise pyodbc.DataError("22018", "[22018] Conversion failed when converting the varchar "
                                        "value 'Pat Q Example' to data type int. (245)")

    monkeypatch.setattr(loading, "load", failed)
    done = _wait(client.post(f"{BASE}/load", json={}).json())
    assert done["state"] == "failed" and "Pat Q Example" not in done["error"]
    assert "22018" in done["error"]


def test_stopping_a_job_that_is_not_one_of_source_setups_is_refused(engine):
    other = jobs.start("baseline", "someone else's", lambda progress: None)
    assert client.post(f"/api/accelerator/jobs/{other.id}/stop").status_code == 404
    assert client.post("/api/accelerator/jobs/nosuchjob/stop").status_code == 404

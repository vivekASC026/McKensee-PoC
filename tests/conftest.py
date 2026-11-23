"""The suite works in a copy of the working directory, never in work/ itself.

Tests that import the app use whatever working directory the settings name, and
that used to be the real one: the suite wrote plans and decisions into the
user's own working files, and ran against whichever database had last been
chosen on Connect. Now that choosing a database moves the whole pipeline, that
would mean shelving the user's files and testing against something else.

So the session starts from the source database's working state -- copied from
wherever it is, in work/ or set aside under work/databases -- with no database
chosen, and the copy is removed at the end. When the source has no state to
copy -- another database is being worked on and the source was offboarded, or
this is a fresh machine -- it is built in the copy: read, the project's
recorded review decisions applied, planned. The suite never depends on what
somebody happens to have onboarded.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_COPY: Path | None = None

# The API answers only its own screens: a call carries the server's token and
# names the machine (sdg/api/guard.py). The suite's server is given a known
# token, its in-process clients send it, and their "testserver" host is one of
# this machine's names for the run. tests/test_api_guard.py turns them off.
TEST_TOKEN = "sdm-test-token"
os.environ["SDG_API_TOKEN"] = TEST_TOKEN
os.environ["SDG_ALLOWED_HOSTS"] = ",".join(
    n for n in (os.environ.get("SDG_ALLOWED_HOSTS", ""), "testserver") if n)


def _clients_send_the_token() -> None:
    from starlette.testclient import TestClient

    if getattr(TestClient, "_sends_token", False):
        return
    made = TestClient.__init__

    def __init__(self, *args, headers=None, **kwargs):
        made(self, *args, headers={"X-SDM-Token": TEST_TOKEN, **(headers or {})}, **kwargs)

    TestClient.__init__ = __init__
    TestClient._sends_token = True


_clients_send_the_token()


def _source_files(real: Path, source_key: str, marker_name: str) -> Path | None:
    """Where the source database's working files are, if anywhere."""
    if not real.exists():
        return None
    marker = real / marker_name
    owner = marker.read_text(encoding="utf-8").strip() if marker.exists() else source_key
    if owner == source_key and (real / "physical-model.json").exists():
        return real
    shelved = real / "databases" / source_key
    if (shelved / "physical-model.json").exists():
        return shelved
    return None


def pytest_configure(config):
    global _COPY
    if os.environ.get("SDG_WORK_DIR"):
        return  # somebody chose one deliberately

    from sdg.api.service import Service
    from sdg.config import get_settings

    settings = get_settings()
    real = settings.work_dir
    source_key = Service._key_for(settings.mssql.server, settings.source_db)
    _COPY = Path(tempfile.mkdtemp(prefix="sdm-suite-work-"))

    # What belongs to the installation rather than to one database.
    for name in ("ui-settings.json",):
        if (real / name).exists():
            shutil.copy2(real / name, _COPY / name)
    for found in real.glob("landscape-*.json") if real.exists() else []:
        shutil.copy2(found, _COPY / found.name)

    chosen = _COPY / "ui-settings.json"
    if chosen.exists():
        data = json.loads(chosen.read_text(encoding="utf-8"))
        data.pop("targetDb", None)
        chosen.write_text(json.dumps(data, indent=2), encoding="utf-8")

    origin = _source_files(real, source_key, Service.MARKER)
    if origin is not None:
        for item in origin.iterdir():
            if item.is_file() and item.name not in Service.STAYS \
                    and not item.name.startswith("landscape-"):
                shutil.copy2(item, _COPY / item.name)
    (_COPY / Service.MARKER).write_text(source_key, encoding="utf-8")
    os.environ["SDG_WORK_DIR"] = str(_COPY)

    if origin is None:
        _build_source_state(_COPY)
    else:
        _without_applied_patterns(_COPY)


def _without_applied_patterns(work: Path) -> None:
    """The copy's plan as the tool worked it out, without the patterns somebody
    applied to this database on the screens: a pattern applied there (a cohort
    over 60% of families, say) would be in every load the suite makes, and its
    tests count on a plain load and on scenarios that are the only cohorts."""
    from sdg.api.service import ARTIFACTS, Service
    from sdg.plan.patterns import clear_from_decisions

    decisions = work / ARTIFACTS["decisions"]
    if not decisions.exists():
        return
    document, removed = clear_from_decisions(json.loads(decisions.read_text(encoding="utf-8")))
    if not removed:
        return
    decisions.write_text(json.dumps(document, indent=2), encoding="utf-8")
    Service(work_dir=work).build()


def _build_source_state(work: Path) -> None:
    """Read the source and plan it, as first-run does, inside the copy."""
    import importlib.util

    from sdg.api.service import Service

    service = Service(work_dir=work)
    service.connect()
    service.discover()
    service.profile()
    service.map()
    service.review()
    spec = importlib.util.spec_from_file_location("seed_decisions", ROOT / "seed" / "decisions.py")
    decisions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decisions)
    for decision in decisions.DECIDED:
        service.record_decision(dict(decision))
    service.build()


# Saved patterns are people's work, and nothing recreates them. Every saved
# pattern there when the suite starts must be there, unchanged, when it ends --
# a test may delete only a row it created. Twice on 2026-10-04 a cleanup
# removed somebody's pattern because saving returned theirs; this fails the
# run if anything like that happens again.
_SAVED_AT_START: dict | None = None


def _saved_patterns() -> dict | None:
    try:
        import hashlib

        from sdg.analyzer import db
        from sdg.analyzer.config import SQL_SCHEMA

        with db.get_connection() as conn:
            rows = conn.cursor().execute(
                f"SELECT id, pattern_key, contextual_name, updated_at, saved_at, profile_json "
                f"FROM {SQL_SCHEMA}.patterns WHERE saved_by IS NOT NULL"
            ).fetchall()
    except Exception:
        return None  # no database: nothing to protect
    return {r[0]: (r[1], r[2], str(r[3]), str(r[4]),
                   hashlib.sha256((r[5] or "").encode()).hexdigest()) for r in rows}


def pytest_sessionstart(session):
    global _SAVED_AT_START
    _SAVED_AT_START = _saved_patterns()


# A run that must prove the database, slow and browser tests (the full run
# before a zip) sets SDM_REQUIRE_DB_TESTS=1: any of them skipped then fails
# the run, so "they passed" can never mean "they were skipped".
_REQUIRED_MARKS = ("db", "slow", "browser")
_SKIPPED_REQUIRED: list[str] = []


def pytest_runtest_logreport(report):
    if report.skipped and os.environ.get("SDM_REQUIRE_DB_TESTS") == "1":
        marks = set(getattr(report, "keywords", {}) or {})
        if marks & set(_REQUIRED_MARKS):
            _SKIPPED_REQUIRED.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _SKIPPED_REQUIRED:
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            f"REQUIRED TESTS SKIPPED ({len(_SKIPPED_REQUIRED)}): "
            + ", ".join(_SKIPPED_REQUIRED[:10]), red=True, bold=True)
        session.exitstatus = 1
    if _SAVED_AT_START is None:
        return
    now = _saved_patterns() or {}
    gone = sorted(set(_SAVED_AT_START) - set(now))
    changed = sorted(i for i in _SAVED_AT_START if i in now and now[i] != _SAVED_AT_START[i])
    if gone or changed:
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            f"SAVED PATTERNS DISTURBED -- deleted: {gone}, changed: {changed}. "
            f"A test touched patterns it did not create.", red=True, bold=True)
        session.exitstatus = 1


def pytest_unconfigure(config):
    if _COPY is not None:
        os.environ.pop("SDG_WORK_DIR", None)
        shutil.rmtree(_COPY, ignore_errors=True)


@pytest.fixture
def second_approver():
    """A second approver configured for layouts while the test runs: a layout
    then writes files only once approved. Restored to what it was after."""
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    client = TestClient(app)
    before = client.get("/api/layouts/approval").json()["secondApprover"]
    assert client.put("/api/layouts/approval",
                      json={"secondApprover": "SDMT approver"}).status_code == 200
    try:
        yield "SDMT approver"
    finally:
        client.put("/api/layouts/approval", json={"secondApprover": before})

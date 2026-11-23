"""Day 0 at a new customer, end to end, through the running product.

The whole job the on-ground team does, on two sources of unrelated domains
(the sample bank, built here, and the FACETS_QA sample, read only), through
the HTTP API of real SDM server processes, as the Source setup screen drives
it:

 1. read the source (the login can write: refused, then gone on with);
 2. the proposal: record kinds chosen where the person would, named, the
    pack named; nothing else changed -- the proposal as it stands;
 3. measure the criteria; submit; approve;
 4. build the pack (SDM is started on it in a process of its own);
 5. create the tables; load them; every check passes; withheld columns are
    empty, surrogates are 16 characters, row counts are the load's;
 6. restart SDM on the generated pack (a second server process): pattern
    discovery finds patterns for both record kinds, each stamped with the
    build; one is saved;
 7. change the plan (a feature chosen): approval drops; submit, approve,
    rebuild -- a new build;
 8. the server still running the old build refuses analysis (409, restart),
    while other screens answer;
 9. load the new build: the old build's patterns are archived, the saved one
    with them; the old pattern is read-only (409); no copy is left beside the
    tables;
10. restart again: discovery works under the new build, its patterns stamped
    with it; the saved list shows the old one archived;
11. refresh: nothing more is archived, and the counts stand.

Throughout, no value of a withheld source column appears in any answer the
servers gave, and the FACETS_QA source is unchanged. Only the SDMT_DAY0_*
databases named here are made, and each is dropped afterwards by its exact
name; work and pack folders are temporary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pyodbc
import pytest

from tests.conftest import TEST_TOKEN
from tests.test_browser import _free_port

pytestmark = [pytest.mark.db, pytest.mark.slow]

ROOT = Path(__file__).resolve().parent.parent
# A key for this test's surrogates only: never a customer's.
TEST_EXTRACT_KEY = "day0-e2e-key-not-a-secret"
CREATED = ("SDMT_DAY0_BANK", "SDMT_DAY0_BANK_STORE", "SDMT_DAY0_QA_STORE")
JOB_SECONDS = 1800


@dataclass
class Domain:
    source: str
    store: str
    pack_id: str
    names: dict[str, tuple[str, str]]
    records: dict[str, str] | None  # chosen by the person, where they would
    built_here: bool


DOMAINS = {
    "bank": Domain("SDMT_DAY0_BANK", "SDMT_DAY0_BANK_STORE", "day0_bank",
                   {"primary": ("customer", "customers"), "child": ("account", "accounts")},
                   {"primary": "dbo.CUSTOMER", "child": "dbo.ACCOUNT"}, True),
    "facets_qa": Domain("FACETS_QA", "SDMT_DAY0_QA_STORE", "day0_events",
                        {"primary": ("person", "people"), "child": ("event", "events")},
                        None, False),
}


# --- databases ------------------------------------------------------------------

def _master() -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _checked(database: str) -> str:
    assert database in CREATED and re.fullmatch(r"SDMT_DAY0_[A-Z_]+", database), database
    return database


def _exists(database: str) -> bool:
    with _master() as master:
        return master.execute("SELECT DB_ID(?)", database).fetchone()[0] is not None


def _drop(database: str) -> None:
    name = _checked(database)
    with _master() as master:
        master.execute(f"IF DB_ID(N'{name}') IS NOT NULL BEGIN ALTER DATABASE [{name}] "
                       f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{name}]; END")


def _connect(database: str) -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string(database), autocommit=True)


def _rows(database: str, sql: str, *params) -> list[tuple]:
    conn = _connect(database)
    try:
        return [tuple(r) for r in conn.execute(sql, *params).fetchall()]
    finally:
        conn.close()


def _scalar(database: str, sql: str, *params):
    return _rows(database, sql, *params)[0][0]


def _make_store(database: str, created: list[str]) -> None:
    """A store as the table setup makes it (customer-tables)."""
    name = _checked(database)
    if _exists(name):
        pytest.fail(f"{name} is there already: drop it by hand if it is an earlier run's.",
                    pytrace=False)
    created.append(name)
    with _master() as master:
        master.execute(f"CREATE DATABASE [{name}]")
    env = {k: v for k, v in os.environ.items() if k not in ("SDG_PACKS_DIR", "SDG_PACK")}
    env.update(SDG_DB_DENORM=name, SDG_DENORM_SCHEMA="sdm")
    done = subprocess.run([sys.executable, "-m", "sdg.cli.main", "customer-tables",
                           "--no-indexes"], cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


# --- a server process ---------------------------------------------------------------

class Server:
    """One SDM server process, and every answer it gave (for the value scan)."""

    def __init__(self, env: dict[str, str], said: list[str]):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.said = said
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(self.port),
             "--log-level", "warning"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(480):
            try:
                urllib.request.urlopen(self.url + "/", timeout=2)
                return
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("the server stopped while starting")
                time.sleep(0.5)
        raise RuntimeError("the server did not start")

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        request = urllib.request.Request(
            self.url + path, method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-SDM-Token": TEST_TOKEN})
        try:
            with urllib.request.urlopen(request, timeout=600) as answer:
                status, text = answer.status, answer.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status, text = exc.code, exc.read().decode("utf-8")
        self.said.append(text)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def ok(self, method: str, path: str, body: dict | None = None):
        status, answer = self.call(method, path, body)
        assert status == 200, (method, path, status, answer)
        return answer

    def job(self, started: dict) -> dict:
        """Wait for an accelerator job; its outcome."""
        job = started
        deadline = time.monotonic() + JOB_SECONDS
        while job.get("state") == "running":
            assert time.monotonic() < deadline, job
            time.sleep(1)
            job = self.ok("GET", f"/api/accelerator/jobs/{job['id']}")
        assert job["state"] == "done", job
        return job["result"]

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _env(domain: Domain, work: Path, packs: Path, pack: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("SDG_PACKS_DIR", "SDG_PACK", "SDG_EXTRACT_KEY")}
    env.update(SDG_WORK_DIR=str(work), SDG_PACKS_DIR=str(packs), SDG_DB_DENORM=domain.store,
               SDG_DENORM_SCHEMA="sdm", SDG_EXTRACT_KEY=TEST_EXTRACT_KEY,
               **{f"SDG_SOURCE_{domain.source}_DATABASE": domain.source})
    if pack:
        env["SDG_PACK"] = pack
    return env


# --- the journey ----------------------------------------------------------------------

def _base(domain: Domain) -> str:
    return f"/api/accelerator/sources/{domain.source}"


def _write_login_then_go_on(server: Server, path: str, body: dict) -> dict:
    """What the screen does: the login that can write is refused, said; the
    person goes on with it."""
    status, said = server.call("POST", path, body)
    assert status == 409 and said["refusal"] == "write_login", (status, said)
    return server.ok("POST", path, {**body, "allow_write_login": True})


def _set_up(server: Server, domain: Domain) -> dict:
    base = _base(domain)
    server.ok("POST", "/api/accelerator/sources", {"name": domain.source})
    read = server.job(_write_login_then_go_on(server, f"{base}/read", {}))
    assert read["summary"]["tables"] > 0 and read["proposed"] is True
    for role, table in (domain.records or {}).items():
        server.ok("POST", f"{base}/proposal/choose-record", {"role": role, "table": table})
    for role, (one, many) in domain.names.items():
        server.ok("POST", f"{base}/proposal/name", {"role": role, "one": one, "many": many})
    view = server.ok("GET", f"{base}/proposal")
    plan = view["plan"]
    assert plan["record_kinds"]["primary"] and plan["record_kinds"]["child"]
    assert plan["tables"], "the tables the hierarchy needs are listed"
    for role in ("primary", "child"):
        assert any(lv["selected"] for lv in plan["hierarchies"][role]["levels"]), role
    server.ok("POST", f"{base}/pack-name", {"id": domain.pack_id, "name": "Day zero"})
    measured = server.job(_write_login_then_go_on(server, f"{base}/criteria/measure", {}))
    assert "measured" in measured
    return view


def _approve(server: Server, domain: Domain) -> dict:
    base = _base(domain)
    shown = server.ok("POST", f"{base}/submit")
    assert shown["status"] == "pending_review", shown
    approved = server.ok("POST", f"{base}/approve", {"plan_hash": shown["plan_hash"]})
    assert approved["status"] == "approved", approved
    return approved


def _build(server: Server, domain: Domain) -> tuple[dict, str]:
    built = server.job(server.ok("POST", f"{_base(domain)}/build"))
    assert built["placed"] is True and built["problems"] == [], built
    marker = json.loads((Path(built["folder"]) / ".sdm-build.json").read_text(encoding="utf-8"))
    return built, marker["built_hash"]


def _load(server: Server, domain: Domain) -> dict:
    done = server.job(_write_login_then_go_on(server, f"{_base(domain)}/load", {}))
    assert done["failed"] == 0 and done["stopped"] is False, done
    assert done["staged"] is True, done["notes"]
    failed = [c for c in done["checks"] if not c["passed"]]
    assert not failed, failed
    return done


def _privacy_holds(domain: Domain, built: dict) -> list[str]:
    """Withheld columns are empty and surrogates are 16 characters, in every
    table; and the source's own values of the withheld columns, to look for."""
    marker = json.loads((Path(built["folder"]) / ".sdm-build.json").read_text(encoding="utf-8"))
    pack = json.loads((Path(built["folder"]) / "pack.json").read_text(encoding="utf-8"))
    withheld = {c.upper() for c in pack["extract"].get("withheld", {}).get(domain.source, [])}
    surrogates = {c.upper() for c in pack["extract"].get("surrogates", {}).get(domain.source, [])}
    looked = []
    for table in marker["store"]:
        for column in table["columns"]:
            name, at = column["name"], f"sdm.[{table['name']}]"
            if column["privacy"] == "withheld":
                assert name.upper() in withheld
                assert _scalar(domain.store, f"SELECT COUNT([{name}]) FROM {at}") == 0, name
            if column["privacy"] == "surrogate":
                assert name.upper() in surrogates
                assert _scalar(domain.store, f"SELECT COUNT(*) FROM {at} WHERE [{name}] IS NOT "
                                             f"NULL AND LEN([{name}]) <> 16") == 0, name
    # The source values behind the withheld columns: what no answer may hold.
    for name, table in marker["reads"].items():
        for column in table["columns"]:
            if column.upper() not in withheld:
                continue
            for (value,) in _rows(domain.source, f"SELECT DISTINCT TOP 40 CAST([{column}] AS "
                                                 f"nvarchar(200)) FROM [{table['schema']}]."
                                                 f"[{table['table']}] WHERE [{column}] IS NOT NULL"):
                text = str(value).strip()
                if len(text) >= 5 and not text.isdigit():
                    looked.append(text)
    return looked


def _entities(built: dict) -> list[str]:
    pack = json.loads((Path(built["folder"]) / "pack.json").read_text(encoding="utf-8"))
    return list(pack["entities"])


def _discover(server: Server, entities: list[str]) -> dict[str, int]:
    found = {}
    for entity in entities:
        body = server.ok("GET", f"/api/discovery?entity={entity}&max_depth=1")
        found[entity] = body["n_discovered"]
        assert body["n_discovered"] > 0, (entity, body)
    return found


def _patterns(domain: Domain, pack_id: str) -> list[tuple]:
    return _rows(domain.store, "SELECT id, plan_version, archived_at, saved_at FROM sdm.patterns "
                               "WHERE pack_id = ?", pack_id)


def _change(server: Server, domain: Domain) -> None:
    """The person changes the plan after approval: a feature chosen (or, with
    none left to choose, one let go)."""
    base = _base(domain)
    plan = server.ok("GET", f"{base}/proposal")["plan"]
    features = plan["hierarchies"]["child"]["features"]
    offered = [f for f in features if not f["selected"]]
    feature, on = (offered[0], True) if offered else (features[-1], False)
    server.ok("POST", f"{base}/proposal/select-feature",
              {"role": "child", "feature_id": feature["id"], "selected": on})
    status = server.ok("GET", f"{base}/approval")["status"]
    assert status != "approved", "an edit drops the approval"


def _added_columns(first: dict, second: dict) -> list[tuple[str, str]]:
    """The (table, column) pairs the second build has and the first did not."""
    def layout(built: dict) -> set[tuple[str, str]]:
        marker = json.loads((Path(built["folder"]) / ".sdm-build.json")
                            .read_text(encoding="utf-8"))
        return {(t["name"], c["name"]) for t in marker["store"] for c in t["columns"]
                if c["privacy"] != "withheld"}
    return sorted(layout(second) - layout(first))


def _leftovers(domain: Domain) -> list[str]:
    return [r[0] for r in _rows(domain.store, "SELECT name FROM sys.tables WHERE "
                                              "SCHEMA_NAME(schema_id) = 'sdm' AND (name LIKE "
                                              "'%[_][_]new%' OR name LIKE '%[_][_]old')")]


def _source_fingerprint(domain: Domain) -> list[tuple]:
    """Row counts of every table in the source, and every object in it with
    when it last changed: the journey changes nothing there."""
    counts = _rows(domain.source, "SELECT s.name + '.' + t.name, SUM(p.rows) FROM sys.tables t "
                                  "JOIN sys.schemas s ON s.schema_id = t.schema_id JOIN "
                                  "sys.partitions p ON p.object_id = t.object_id AND p.index_id "
                                  "IN (0, 1) GROUP BY s.name, t.name ORDER BY 1")
    objects = _rows(domain.source, "SELECT SCHEMA_NAME(schema_id) + '.' + name, type, "
                                   "CONVERT(varchar(30), modify_date, 126) FROM sys.objects "
                                   "WHERE is_ms_shipped = 0 ORDER BY 1, 2")
    return counts + objects


HEALTH = re.compile(r"\b(members?|claims?|subscribers?|diagnos\w*|providers?|FACETS)\b", re.I)


@pytest.mark.parametrize("which", ["bank", "facets_qa"])
def test_day_zero_end_to_end(which):
    from sdg.config import load_env, not_a_demo_machine

    load_env()
    reason = not_a_demo_machine()
    if reason:
        pytest.skip(f"the day-0 journey makes and drops databases: this machine is {reason}")
    domain = DOMAINS[which]
    created: list[str] = []
    servers: list[Server] = []
    said: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="sdm-day0-work-"))
    packs = Path(tempfile.mkdtemp(prefix="sdm-day0-packs-"))
    try:
        if domain.built_here:
            from seed import bank as sample

            _checked(domain.source)
            if _exists(domain.source):
                pytest.fail(f"{domain.source} is there already.", pytrace=False)
            created.append(domain.source)
            sample.build(domain.source, customers=600)
        before = _source_fingerprint(domain)
        _make_store(domain.store, created)

        # 1-5: set up, approve, build, create, load, on the setup server.
        setup = Server(_env(domain, work, packs), said)
        servers.append(setup)
        _set_up(setup, domain)
        _approve(setup, domain)
        built, first_build = _build(setup, domain)
        tables = setup.ok("POST", f"{_base(domain)}/store-tables")
        assert tables["store"]["ready"] is True, tables
        loaded = _load(setup, domain)
        for table in loaded["load"]["tables"]:
            assert table["rows"] > 0, table
            assert _scalar(domain.store, f"SELECT COUNT(*) FROM sdm.[{table['table']}]") == \
                table["rows"]
        values = _privacy_holds(domain, built)
        status = setup.ok("GET", f"{_base(domain)}/load")
        assert status["analyzing"] is False and status["running_pack"] != domain.pack_id

        # 6: restart on the generated pack: discovery, stamped; one saved.
        old = Server(_env(domain, work, packs, domain.pack_id), said)
        servers.append(old)
        entities = _entities(built)
        _discover(old, entities)
        first = _patterns(domain, domain.pack_id)
        assert first and all(r[1] == first_build and r[2] is None for r in first), first
        kept = first[0][0]
        old.ok("POST", "/api/discovery/save", {"pattern_id": kept})
        assert kept in {p["pattern_id"] for p in old.ok("GET", "/api/discovery/saved")["patterns"]}

        # 7: change, re-approve, rebuild.
        _change(setup, domain)
        _approve(setup, domain)
        rebuilt, second_build = _build(setup, domain)
        assert second_build != first_build

        # 8: the server on the old build refuses analysis; other screens answer.
        status_code, refused = old.call("GET", f"/api/discovery?entity={entities[0]}&max_depth=1")
        assert status_code == 409 and refused["refusal"] == "rebuilt", refused
        assert old.call("GET", "/api/accelerator/sources")[0] == 200

        # 9: load the new build: the old build's patterns archived, read-only.
        # A column the new build adds is added to the loaded table, empty in
        # every row it had, before the load fills it.
        added = _added_columns(built, rebuilt)
        setup.ok("POST", f"{_base(domain)}/store-tables")
        rows_before = {t["table"]: t["rows"] for t in loaded["load"]["tables"]}
        for table, column in added:
            assert _scalar(domain.store, f"SELECT COUNT([{column}]) FROM sdm.[{table}]") == 0
            assert _scalar(domain.store, f"SELECT COUNT(*) FROM sdm.[{table}]") == \
                rows_before[table]
        reloaded = _load(setup, domain)
        assert reloaded["archived"] == len(first), reloaded
        rows = {r[0]: r for r in _patterns(domain, domain.pack_id)}
        assert all(rows[r[0]][2] is not None for r in first)
        assert rows[kept][3] is not None, "archived, and still saved"
        assert _leftovers(domain) == []
        old.stop()
        servers.remove(old)

        # 10: restart on the new build: discovery, stamped with it.
        new = Server(_env(domain, work, packs, domain.pack_id), said)
        servers.append(new)
        _discover(new, entities)
        current = [r for r in _patterns(domain, domain.pack_id) if r[2] is None]
        assert current and all(r[1] == second_build for r in current), current
        listed = {p["pattern_id"]: p for p in new.ok("GET", "/api/discovery/saved")["patterns"]}
        assert listed[kept]["archived"] is True
        status_code, refused = new.call("POST", "/api/discovery/unsave", {"pattern_id": kept})
        assert status_code == 409 and "read-only" in refused["detail"], refused
        assert new.ok("GET", f"{_base(domain)}/load")["analyzing"] is True

        # 11: refresh: nothing more archived; the counts stand.
        counts = {t["table"]: t["rows"] for t in reloaded["load"]["tables"]}
        again = _load(setup, domain)
        assert again["archived"] == 0
        assert {t["table"]: t["rows"] for t in again["load"]["tables"]} == counts
        assert all(r[2] is None for r in _patterns(domain, domain.pack_id)
                   if r[1] == second_build)

        if added:
            assert any(_scalar(domain.store, f"SELECT COUNT([{c}]) FROM sdm.[{t}]") > 0
                       for t, c in added), "the reload fills the added columns"
        # Throughout: no withheld value in any answer, and the source unchanged.
        everything = "\n".join(said)
        if domain.built_here:  # the bank: no health word reaches any answer
            assert not HEALTH.search(everything), HEALTH.search(everything)
        leaked = [v for v in values if v in everything]
        assert not leaked, f"{len(leaked)} withheld source values appear in answers"
        assert _source_fingerprint(domain) == before
    finally:
        for server in servers:
            server.stop()
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(packs, ignore_errors=True)
        failures = []
        for name in created:
            try:
                _drop(name)
            except Exception as exc:  # each is tried; every failure is said below
                failures.append(f"{name}: {type(exc).__name__}")
        left = [name for name in created if _exists(name)]
        assert not failures and not left, (failures, left)

"""An installation set to make files only never writes generated rows to a database.

SDG_DATABASE_GENERATION=no in .env turns off the database load, and only that:
the database is still read and profiled, patterns are still found, and files
are still written. The refusal is in the writer itself, so the screens and the
command line cannot get past it, and it is said before any rows are made.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from sdg.api.app import app
from sdg.config import DATABASE_GENERATION_OFF, database_generation

client = TestClient(app)


@pytest.fixture
def files_only(monkeypatch):
    monkeypatch.setenv("SDG_DATABASE_GENERATION", "no")


def test_on_unless_turned_off(monkeypatch):
    monkeypatch.delenv("SDG_DATABASE_GENERATION", raising=False)
    monkeypatch.setenv("SDG_ENV", "local")
    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)
    assert database_generation()
    for off in ("no", "No", "false", "0"):
        monkeypatch.setenv("SDG_DATABASE_GENERATION", off)
        assert not database_generation()


@pytest.mark.parametrize("machine", [
    {"SDG_ENV": "customer"}, {"SDG_ENV": ""}, {"SDG_ENV": "prod"},
    {"SDG_ENV": "local", "SDG_DENORM_SERVER": "TDMSQL01"},
])
def test_off_unless_the_machine_says_it_is_for_development_or_demos(monkeypatch, machine):
    """Without the setting, only a local or demo machine writes into a
    database: a customer's, or one that does not say, does not."""
    monkeypatch.delenv("SDG_DATABASE_GENERATION", raising=False)
    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)
    for name, value in machine.items():
        monkeypatch.setenv(name, value)
    assert not database_generation()
    monkeypatch.setenv("SDG_DATABASE_GENERATION", "yes")
    assert database_generation(), "said outright, it is on"


def test_production_is_never_written_or_deleted_from(monkeypatch):
    from sdg.config import ProtectedDatabase
    from sdg.writers.sqlserver import cleanup, write

    monkeypatch.setenv("SDG_DATABASE_GENERATION", "yes")
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODDB")

    class Registry:  # names the database; any connection would fail the test
        def database_for(self, name):
            return "proddb"

        def connect(self, *args, **kwargs):
            raise AssertionError("connected to production")

    with pytest.raises(ProtectedDatabase, match="PRODDB|proddb"):
        write(Registry(), None, None, None)
    with pytest.raises(ProtectedDatabase, match="cannot delete from"):
        cleanup(Registry(), {"generation_order": [], "tables": [], "keys": {}})


def test_on_a_customer_machine_the_source_is_protected_too(monkeypatch):
    from sdg.config import protected_databases

    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODA, prodb")
    monkeypatch.setenv("SDG_DB_SOURCE", "FACETS_LIVE")
    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)
    monkeypatch.setenv("SDG_ENV", "local")
    assert protected_databases() == {"PRODA", "PRODB"}
    monkeypatch.setenv("SDG_ENV", "customer")
    assert protected_databases() == {"PRODA", "PRODB", "FACETS_LIVE"}


def test_production_is_what_the_setting_names_and_a_customer_must_name_it(monkeypatch):
    """No production database is built in: a demo machine protects what the
    setting names; a customer's machine refuses until it names one."""
    from sdg.config import ProtectedDatabase, protected_databases

    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)
    monkeypatch.setenv("SDG_ENV", "local")
    for setting, protected in (("", set()), (" , ", set()), ("otherdb", {"OTHERDB"})):
        monkeypatch.setenv("SDG_PROTECTED_DATABASES", setting)
        assert protected_databases() == protected, setting
    monkeypatch.setenv("SDG_ENV", "customer")
    for setting in ("", " , "):
        monkeypatch.setenv("SDG_PROTECTED_DATABASES", setting)
        with pytest.raises(ProtectedDatabase, match="SDG_PROTECTED_DATABASES is not set"):
            protected_databases()


def test_no_role_connects_to_production_except_the_extract(monkeypatch):
    import pyodbc

    from sdg.config import ProtectedDatabase
    from sdg.discovery.connections import ConnectionRegistry

    opened = []
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODDB")
    monkeypatch.setattr(pyodbc, "connect", lambda *a, **k: opened.append(a) or _Closing())
    registry = ConnectionRegistry()
    for role in ("source", "ref", "target", "landscape"):
        registry.register(role, "proddb")
        with pytest.raises(ProtectedDatabase, match="cannot connect to"):
            with registry.connect(role):
                pass
    assert opened == []
    extract = ConnectionRegistry(reads_production=True)
    extract.register("source", "PRODDB")
    with extract.connect("source"):
        pass
    assert len(opened) == 1
    # The extract reads production as its source only, never as the store it writes.
    extract.register("denorm", "PRODDB")
    with pytest.raises(ProtectedDatabase, match="cannot connect to"):
        with extract.connect("denorm"):
            pass
    assert len(opened) == 1


class _Closing:
    def close(self):
        pass


def test_the_screens_refuse_to_read_or_choose_production(monkeypatch):
    from sdg.api.app import service

    monkeypatch.setenv("SDG_PROTECTED_DATABASES", service.registry.database_for("target"))
    for step in ("connect", "discover", "profile", "dryrun", "validate", "cleanup"):
        refused = client.post(f"/api/{step}")
        assert refused.status_code == 409, (step, refused.text)
        assert "is a protected production database" in refused.json()["detail"], step
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "SOMEWHERE_ELSE")
    refused = client.post("/api/target", json={"database": "somewhere_else"})
    assert refused.status_code == 409 and "protected production" in refused.json()["detail"]


def test_the_writer_refuses_before_touching_the_database(files_only):
    from sdg.writers.sqlserver import DatabaseGenerationOff, write

    # Nothing it is given is looked at: no connection is made.
    with pytest.raises(DatabaseGenerationOff, match="turned off"):
        write(None, None, None, None)


@pytest.mark.db
def test_the_screens_are_told_and_a_load_is_refused(files_only):
    state = client.get("/api/state").json()
    assert state["databaseGeneration"] is False

    refused = client.post("/api/generate")
    assert refused.status_code == 409
    assert refused.json()["detail"] == DATABASE_GENERATION_OFF


@pytest.mark.db
def test_the_screens_are_told_when_it_is_on(monkeypatch):
    monkeypatch.delenv("SDG_DATABASE_GENERATION", raising=False)
    assert client.get("/api/state").json()["databaseGeneration"] is True


@pytest.mark.db
def test_reading_patterns_and_files_still_work(files_only):
    assert client.get("/api/schema").status_code == 200
    found = client.post("/api/claims/analyze", json={"k": 2})
    assert found.status_code == 200, found.text
    pattern = client.get("/api/patterns/for-files").json()["patterns"][0]
    written = client.post("/api/export", json={
        "format": "csv", "subject": pattern["domain"],
        "pattern_id": pattern["pattern_id"], "rows": 10, "members": 10,
    })
    assert written.status_code == 200, written.text
    assert len(written.text.strip().splitlines()) > 1


def test_the_command_line_refuses_a_load(files_only):
    from sdg.cli.main import app as cli

    result = CliRunner().invoke(cli, ["generate"])
    assert result.exit_code == 1
    assert "turned off" in result.output and "--csv" in result.output

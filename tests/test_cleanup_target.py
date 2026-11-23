"""Cleanup deletes only from the database the run wrote to.

A run's manifest lists the keys it wrote, not where it wrote them; run against
another database, the same keys find someone else's rows. Cleanup refuses
unless the target is the database (and server, when recorded) in the
manifest, and the command line names the database before it asks.

Nothing here connects to a server: any connection attempt fails the test.
"""

from __future__ import annotations

import json

import pyodbc
import pytest
from typer.testing import CliRunner

from sdg.config import MssqlSettings
from sdg.writers.sqlserver import WrongDatabase, check_cleanup_target, cleanup


class Registry:
    """Names the target; any connection fails the test."""

    def __init__(self, database: str, server: str = "localhost", port: int = 1433) -> None:
        self.database, self.server, self.port = database, server, port

    def database_for(self, name):
        return self.database

    def settings_for(self, name):
        return MssqlSettings(server=self.server, port=self.port, user="", password="",
                             driver="ODBC Driver 18 for SQL Server", trust_cert=True)

    def connect(self, *args, **kwargs):
        raise AssertionError("connected before refusing")


def _manifest(database: str | None, server: str | None = None) -> dict:
    run = {"connection": "target"}
    if database is not None:
        run["database"] = database
    if server is not None:
        run["server"] = server
    return {"run": run, "plan": {"seed": 7}, "generation_order": ["dbo.CMC_SBSB_SUBSC"],
            "tables": [{"table": "dbo.CMC_SBSB_SUBSC", "rows_written": 2,
                        "key_columns": ["SBSB_CK"]}],
            "keys": {"dbo.CMC_SBSB_SUBSC": [1, 2]}}


@pytest.fixture(autouse=True)
def not_production(monkeypatch):
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "")
    monkeypatch.setenv("SDG_ENV", "local")
    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)


def test_a_run_from_another_database_is_refused_before_connecting():
    with pytest.raises(WrongDatabase, match="wrote its rows to SDMT_A, but the target is now "
                                            "SDMT_B. Nothing was deleted"):
        cleanup(Registry("SDMT_B"), _manifest("SDMT_A"))


def test_a_run_from_another_server_is_refused():
    with pytest.raises(WrongDatabase, match="on other-sql.*Nothing was deleted"):
        cleanup(Registry("SDMT_A", server="localhost"), _manifest("SDMT_A", "other-sql"))


def test_a_run_on_another_instance_of_the_same_host_is_refused():
    with pytest.raises(WrongDatabase, match="on localhost,1435.*Nothing was deleted"):
        cleanup(Registry("SDMT_A", server="localhost", port=1434),
                _manifest("SDMT_A", "localhost,1435"))
    assert check_cleanup_target(Registry("SDMT_A", "localhost", 1434),
                                _manifest("SDMT_A", "localhost,1434")) == "SDMT_A"


def test_a_record_that_names_the_host_alone_means_port_1433():
    """A run on port 1433 is recorded as the host alone. It cleans up there,
    and never on another instance of that host."""
    assert check_cleanup_target(Registry("SDMT_A", "localhost", 1433),
                                _manifest("SDMT_A", "localhost")) == "SDMT_A"
    with pytest.raises(WrongDatabase, match="without a port.*1433.*Nothing was deleted"):
        check_cleanup_target(Registry("SDMT_A", "localhost", 5000),
                             _manifest("SDMT_A", "localhost"))
    with pytest.raises(WrongDatabase, match="on other-sql"):
        check_cleanup_target(Registry("SDMT_A", "localhost", 1434),
                             _manifest("SDMT_A", "other-sql"))


def test_a_run_that_does_not_say_where_it_wrote_is_refused():
    with pytest.raises(WrongDatabase, match="does not say which database"):
        cleanup(Registry("SDMT_A"), _manifest(None))


def test_the_same_database_passes_whatever_its_case():
    assert check_cleanup_target(Registry("sdmt_a"), _manifest("SDMT_A")) == "sdmt_a"
    assert check_cleanup_target(Registry("SDMT_A", "LOCALHOST"),
                                _manifest("SDMT_A", "localhost")) == "SDMT_A"


@pytest.fixture
def cli_work(tmp_path, monkeypatch):
    monkeypatch.setenv("SDG_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("SDG_DB_TARGET", "SDMT_B")
    monkeypatch.setattr(pyodbc, "connect", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("connected before refusing")))
    return tmp_path


def test_the_command_line_refuses_before_it_asks(cli_work):
    from sdg.cli.main import app as cli

    (cli_work / "run-manifest.json").write_text(json.dumps(_manifest("SDMT_A")), encoding="utf-8")
    result = CliRunner().invoke(cli, ["cleanup"], input="y\n")
    assert result.exit_code == 1, result.output
    assert "wrote its rows to SDMT_A" in result.output
    assert "Delete these rows" not in result.output
    assert (cli_work / "run-manifest.json").exists(), "the manifest is kept"


def test_the_command_line_names_the_database_it_will_delete_from(cli_work):
    from sdg.cli.main import app as cli

    (cli_work / "run-manifest.json").write_text(json.dumps(_manifest("SDMT_B")), encoding="utf-8")
    result = CliRunner().invoke(cli, ["cleanup"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "from SDMT_B on " in result.output
    assert "Delete these rows from SDMT_B?" in result.output
    assert "Nothing was deleted." in result.output


def test_a_run_into_another_database_is_refused_while_the_last_is_not_cleaned_up(tmp_path):
    """The two runs' keys would share one record, and cleanup would delete the
    first run's keys from the second database."""
    from sdg.api.service import Service, ServiceError

    service = Service(work_dir=tmp_path)
    service.path("manifest").write_text(json.dumps(_manifest("SDMT_A")), encoding="utf-8")
    service.registry.register("target", "SDMT_B")
    with pytest.raises(ServiceError, match="last run's rows in SDMT_A have not been removed"
                                           ".*Nothing was generated"):
        service.generate()


def test_a_record_that_does_not_say_where_its_rows_are_is_named(tmp_path):
    from sdg.api.service import Service, ServiceError

    service = Service(work_dir=tmp_path)
    service.path("manifest").write_text(json.dumps(_manifest(None)), encoding="utf-8")
    with pytest.raises(ServiceError, match="does not say which database.*Nothing was generated"):
        service.generate()


@pytest.fixture
def loads(tmp_path, monkeypatch):
    """A service whose load steps are stand-ins: no plan, no database."""
    import sdg.engine.execute
    import sdg.engine.starts
    from sdg.api import service as module
    from sdg.api.service import Service

    monkeypatch.setenv("SDG_DATABASE_GENERATION", "yes")
    calls = []
    plan = type("Plan", (), {"warnings": [], "tables": [],
                             "meta": type("Meta", (), {"seed": "s"})()})()
    service = Service(work_dir=tmp_path)
    service.registry.register("target", "SDMT_A")
    monkeypatch.setattr(Service, "require_plan", lambda self: plan)
    monkeypatch.setattr(Service, "require_model", lambda self: None)
    monkeypatch.setattr(Service, "reference_sets_with_sources", lambda self, p: (None, []))
    monkeypatch.setattr(sdg.engine.starts, "resolve", lambda *a, **k: (plan, []))
    monkeypatch.setattr(sdg.engine.execute, "run_plan",
                        lambda *a, **k: calls.append("run_plan"))

    def write(*args, **kwargs):
        calls.append("write")
        raise module.PartialWrite(RuntimeError("the server went away"),
                                 type("Report", (), {"tables": []})())

    monkeypatch.setattr(module, "write", write)
    monkeypatch.setattr(module, "build_manifest",
                        lambda *a, **k: {**_manifest("SDMT_A"),
                                         "keys": {"dbo.CMC_SBSB_SUBSC": [2, 3]}})
    return service, calls


def test_a_refused_run_makes_and_writes_nothing(loads):
    from sdg.api.service import ServiceError

    service, calls = loads
    service.path("manifest").write_text(json.dumps(_manifest("SDMT_B")), encoding="utf-8")
    with pytest.raises(ServiceError, match="rows in SDMT_B have not been removed"):
        service.generate()
    assert calls == []


def test_a_load_that_stops_partway_keeps_the_last_runs_keys(loads):
    """Its record is merged with the last run's: both runs' rows stay findable."""
    from sdg.api.service import ServiceError

    service, calls = loads
    service.path("manifest").write_text(json.dumps(_manifest("SDMT_A")), encoding="utf-8")
    with pytest.raises(ServiceError, match="stopped partway"):
        service.generate()
    assert calls == ["run_plan", "write"]
    kept = json.loads(service.path("manifest").read_text(encoding="utf-8"))
    assert kept["keys"]["dbo.CMC_SBSB_SUBSC"] == [1, 2, 3]
    assert kept["run"]["database"] == "SDMT_A"


def test_the_same_database_on_another_server_is_said_as_such(loads):
    from sdg.api.service import ServiceError

    service, calls = loads
    service.path("manifest").write_text(json.dumps(_manifest("SDMT_A", "other-sql")),
                                        encoding="utf-8")
    with pytest.raises(ServiceError, match="SDMT_A on other-sql have not been removed.*"
                                           "Connect to other-sql and run cleanup first"):
        service.generate()
    assert calls == []


def test_a_damaged_record_is_said_in_words(loads):
    from sdg.api.service import ServiceError

    service, calls = loads
    service.path("manifest").write_text("{not a record", encoding="utf-8")
    with pytest.raises(ServiceError, match="cannot be read.*Nothing was generated"):
        service.generate()
    assert calls == []


def test_the_demo_keeps_its_rows_only_when_no_earlier_run_waits(cli_work):
    """--keep would write the demo's record over an earlier run's; it refuses instead."""
    from sdg.cli.main import app as cli

    earlier = json.dumps(_manifest("SDMT_B"))
    (cli_work / "run-manifest.json").write_text(earlier, encoding="utf-8")
    result = CliRunner().invoke(cli, ["demo", "--keep"])
    assert result.exit_code == 1, result.output
    assert "Run `sdg cleanup` first" in result.output
    assert (cli_work / "run-manifest.json").read_text(encoding="utf-8") == earlier


@pytest.mark.parametrize("record", ["[]", "null", '"a run"'])
def test_a_record_that_is_not_a_run_is_said_in_words(loads, record):
    from sdg.api.service import ServiceError

    service, calls = loads
    service.path("manifest").write_text(record, encoding="utf-8")
    with pytest.raises(ServiceError, match="is not a run record.*Nothing was generated"):
        service.generate()
    assert calls == []

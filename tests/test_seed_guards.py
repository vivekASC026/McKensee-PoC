"""The demo seed scripts refuse production and customer machines.

seed/create_databases.py --drop drops whole databases, and seed/build.py runs
DDL that drops every CMC_* and REF_* table. Each refuses before it connects
when a database it would destroy is protected (one named in
SDG_PROTECTED_DATABASES), and on a machine set up as a customer's. The DDL
scripts themselves stop in a production database, for when they are run by
hand.

Nothing here connects to a server: any connection attempt fails the test.
"""

from __future__ import annotations

from pathlib import Path

import pyodbc
import pytest

from sdg.config import ProtectedDatabase
from seed import build, create_databases

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_server(monkeypatch):
    def connect(*_args, **_kwargs):
        raise AssertionError("connected to the server before refusing")

    monkeypatch.setattr(pyodbc, "connect", connect)
    monkeypatch.setenv("SDG_DB_SOURCE", "SDMT_FACETS")
    monkeypatch.setenv("SDG_DB_REF", "SDMT_REF")
    monkeypatch.setenv("SDG_ENV", "local")
    monkeypatch.setenv("SDG_DENORM_SERVER", "localhost")
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODDB")


@pytest.mark.parametrize("setting", ["SDG_DB_SOURCE", "SDG_DB_REF"])
@pytest.mark.parametrize("argv", [["--drop"], []])
def test_create_databases_refuses_a_protected_database(no_server, monkeypatch, setting, argv):
    monkeypatch.setenv(setting, "PRODDB")
    with pytest.raises(ProtectedDatabase, match="PRODDB is a protected"):
        create_databases.main(argv)


def test_create_databases_refuses_one_named_in_the_settings(no_server, monkeypatch):
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "SDMT_REF")
    with pytest.raises(ProtectedDatabase, match="cannot drop and recreate it"):
        create_databases.main(["--drop"])


def test_create_databases_will_not_drop_on_a_customer_machine(no_server, monkeypatch):
    monkeypatch.setenv("SDG_ENV", "customer")
    with pytest.raises(SystemExit, match="set up as a customer's.*Nothing was dropped"):
        create_databases.main(["--drop"])


@pytest.mark.parametrize("setting", ["SDG_DB_SOURCE", "SDG_DB_REF"])
def test_build_refuses_a_protected_database(no_server, monkeypatch, setting):
    monkeypatch.setenv(setting, "PRODDB")
    with pytest.raises(ProtectedDatabase, match="cannot drop and rebuild the tables in it"):
        build.main(["--small"])


def test_build_refuses_on_a_customer_machine(no_server, monkeypatch):
    monkeypatch.setenv("SDG_DENORM_SERVER", r"customer-sql.example\SDM")
    with pytest.raises(SystemExit, match="set up as a customer's.*Nothing was changed"):
        build.main(["--small"])


@pytest.mark.parametrize("env", ["", "production"])
def test_the_demo_steps_need_a_machine_that_says_it_is_for_demos(no_server, monkeypatch, env):
    monkeypatch.setenv("SDG_ENV", env)
    with pytest.raises(SystemExit, match="not set up as a demo machine.*Nothing was dropped"):
        create_databases.main(["--drop"])
    with pytest.raises(SystemExit, match="not set up as a demo machine.*Nothing was changed"):
        build.main(["--small"])


def _code(batch: str) -> list[str]:
    return [line.strip() for line in batch.splitlines()
            if line.strip() and not line.strip().startswith("--")]


@pytest.mark.parametrize("name", ["ddl_facets.sql", "ddl_mdm.sql"])
def test_the_ddl_stops_in_production_before_any_drop(name):
    from tests.test_customer_scripts_guard import guard_lines

    batches = [b for b in build._split_on_go((ROOT / "seed" / name).read_text(encoding="utf-8"))
               if _code(b)]
    first, second = (_code(b) for b in batches[:2])
    expected_first, expected_second = guard_lines("")
    assert first[:3] == expected_first[:3] and first[3].startswith("THROW 50000"), first
    assert "never in a production database. Nothing was changed." in first[3]
    assert second == expected_second, second
    assert "DROP" in " ".join(_code(batches[2])).upper()

"""The customer's SQL scripts refuse to run in a production database.

No production database is named in the scripts: each guard holds the
placeholder {{PROTECTED_DATABASES}}, which SDM fills with the names in
SDG_PROTECTED_DATABASES when it runs the script (seed/build.run_sql_script).
Run by hand with the placeholder left in, anywhere it is left, the script
stops. Each script's first batch stops it with an error, and the next keeps
every later batch from running (SET NOEXEC ON), in SSMS as in sqlcmd. Tried
here on this machine's master database by naming it as production: the guard
throws, and a statement after it does not run. Nothing is created or changed
anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pyodbc
import pytest

ROOT = Path(__file__).resolve().parent.parent
# SDM's own scripts, and every business pack's: each is run by hand somewhere.
SCRIPTS = sorted((ROOT / "scripts" / "customer").glob("*.sql")) + sorted(
    (ROOT / "sdg" / "packs").glob("*/sql/*.sql"))
PLACEHOLDER = "{{PROTECTED_DATABASES}}"


def _named(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


REFUSAL = ("This script runs in the SDM platform database, never in a production database. "
           "Nothing was changed. Choose the SDM platform database and run it again.")
UNFILLED = ("This script names no production database yet: SDM names them when it runs it; "
            "run by hand, replace the placeholder in its first lines with their names, "
            "separated by commas. Nothing was changed.")
# Each condition holds the placeholder once, and stops when it is left in.
STOP = ("IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', "
        "N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END "
        f"FROM (SELECT N'{PLACEHOLDER}' AS v) AS p) = 1")


def _batches(path: Path) -> list[str]:
    from seed.build import _split_on_go

    return [b for b in _split_on_go(path.read_text(encoding="utf-8")) if b.strip()]


def _code(batch: str) -> list[str]:
    return [line.strip() for line in batch.splitlines()
            if line.strip() and not line.strip().startswith("--")]


def guard_lines(refusal: str = REFUSAL) -> tuple[list[str], list[str]]:
    """The code of a script's first two batches, as every script has them."""
    return ([f"IF N'{PLACEHOLDER}' LIKE N'{{{{%'", f"THROW 50000, N'{UNFILLED}', 1;",
             STOP, f"THROW 50000, N'{refusal}', 1;"],
            [STOP, "SET NOEXEC ON;"])


def test_every_customer_script_is_found():
    assert {_named(p) for p in SCRIPTS} >= {
        "scripts/customer/00_check.sql", "scripts/customer/01_sdm_upgrade.sql",
        "scripts/customer/setup_tables.sql",
        "sdg/packs/health/sql/02_denorm_indexes_optional.sql",
        "sdg/packs/health/sql/03_denorm_837_tables.sql",
        "sdg/packs/health/sql/04_denorm_837_extract.sql",
        "sdg/packs/health/sql/ddl_denorm.sql", "sdg/packs/health/sql/denorm_indexes.sql",
        "sdg/packs/health/sql/default_rules.sql",
        "sdg/packs/health/sql/setup_tables.sql",
        "sdg/packs/health/sql/truncate_extract_tables.sql"}


@pytest.mark.parametrize("path", SCRIPTS, ids=_named)
def test_the_first_batches_refuse_production(path):
    first, second = _batches(path)[:2]
    expected_first, expected_second = guard_lines()
    assert _code(first) == expected_first, _code(first)
    assert _code(second) == expected_second, _code(second)


def test_no_production_database_is_named_in_any_script():
    """Nothing of one customer ships to another: the guards name none."""
    for path in [*SCRIPTS, *sorted((ROOT / "seed").glob("*.sql")),
                 *sorted((ROOT / "sdg" / "packs").glob("*/sample/*.sql"))]:
        text = path.read_text(encoding="utf-8")
        assert "DB_NAME() = N'" not in text, _named(path)
        if "DB_NAME()" in text:
            assert text.count(PLACEHOLDER) == 4, _named(path)  # a comment and 3 guards


def _master():
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _stopped(cur) -> bool:
    """Whether the batches after the guard are kept from running."""
    cur.execute("SELECT 42")
    try:
        cur.fetchall()
    except pyodbc.ProgrammingError:
        cur.execute("SET NOEXEC OFF")
        assert cur.execute("SELECT 43").fetchone()[0] == 43
        return True
    return False


@pytest.mark.db
@pytest.mark.parametrize("path", SCRIPTS, ids=_named)
def test_in_a_production_database_the_script_stops_at_once(path):
    first, second = _batches(path)[:2]
    conn = _master()
    try:
        cur = conn.cursor()
        here = cur.execute("SELECT DB_NAME()").fetchone()[0]
        named = f"SDMT_OTHER_PROD, {here.lower()}"  # spaces and case as a person writes them
        with pytest.raises(pyodbc.Error, match="never in a production database"):
            cur.execute(first.replace(PLACEHOLDER, named))
        cur.execute(second.replace(PLACEHOLDER, named))
        assert _stopped(cur)
    finally:
        conn.close()


@pytest.mark.db
@pytest.mark.parametrize("path", SCRIPTS, ids=_named)
def test_run_by_hand_with_the_placeholder_left_in_the_script_stops(path):
    first, second = _batches(path)[:2]
    conn = _master()
    try:
        cur = conn.cursor()
        with pytest.raises(pyodbc.Error, match="names no production database yet"):
            cur.execute(first)
        cur.execute(second)
        assert _stopped(cur)
    finally:
        conn.close()


@pytest.mark.db
def test_a_placeholder_left_in_one_place_still_stops_the_script():
    """Filled everywhere but the last guard (a Find and Replace that missed
    one): the batches after it still never run."""
    first, second = _batches(SCRIPTS[0])[:2]
    conn = _master()
    try:
        cur = conn.cursor()
        cur.execute(first.replace(PLACEHOLDER, "SDMT_OTHER_PROD"))  # elsewhere: passes
        cur.execute(second)  # left in
        assert _stopped(cur)
        # And a first batch filled in one of its two conditions only.
        half = first.replace(f"IF N'{PLACEHOLDER}' LIKE", "IF N'SDMT_OTHER_PROD' LIKE")
        assert half != first and PLACEHOLDER in half
        with pytest.raises(pyodbc.Error, match="never in a production database"):
            cur.execute(half)
    finally:
        conn.close()


@pytest.mark.db
@pytest.mark.parametrize("path", SCRIPTS, ids=_named)
def test_anywhere_else_the_guard_does_nothing(path):
    first, second = _batches(path)[:2]
    conn = _master()
    try:
        cur = conn.cursor()
        for named in ("SDMT_OTHER_PROD,SDMT_PROD_TWO", ""):
            cur.execute(first.replace(PLACEHOLDER, named))
            cur.execute(second.replace(PLACEHOLDER, named))
            assert not _stopped(cur), named
    finally:
        conn.close()


@pytest.mark.db
def test_sdm_names_the_protected_databases_when_it_runs_a_script(monkeypatch, tmp_path):
    """seed/build.run_sql_script fills the guard from SDG_PROTECTED_DATABASES:
    a script run by SDM in a database named there stops before its body."""
    from seed import build

    first, second = _batches(SCRIPTS[0])[:2]
    script = tmp_path / "guarded.sql"
    script.write_text(f"{first}\nGO\n{second}\nGO\nSELECT 42 AS body_ran\n", encoding="utf-8")
    conn = _master()
    try:
        here = conn.cursor().execute("SELECT DB_NAME()").fetchone()[0]
        monkeypatch.setenv("SDG_PROTECTED_DATABASES", f"SDMT_OTHER_PROD,{here}")
        with pytest.raises(pyodbc.Error, match="never in a production database"):
            build.run_sql_script(conn, script)
        conn.cursor().execute("SET NOEXEC OFF")
        monkeypatch.setenv("SDG_PROTECTED_DATABASES", "SDMT_OTHER_PROD")
        build.run_sql_script(conn, script)  # elsewhere: the body runs
    finally:
        conn.close()


def test_the_placeholder_is_filled_with_every_protected_name(monkeypatch):
    from sdg.config import ProtectedDatabase, fill_protected

    monkeypatch.setenv("SDG_ENV", "local")
    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "prodb, PRODA")
    text = f"x {PLACEHOLDER} y {PLACEHOLDER}"
    assert fill_protected(text) == "x PRODA,PRODB y PRODA,PRODB"
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "")
    assert fill_protected(text) == "x  y "
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PROD'A")
    with pytest.raises(ProtectedDatabase, match="cannot hold"):
        fill_protected(text)


def test_a_customer_machine_that_names_no_production_database_refuses(monkeypatch):
    from sdg.config import ProtectedDatabase, fill_protected, protected_databases

    monkeypatch.setenv("SDG_ENV", "customer")
    monkeypatch.setenv("SDG_DB_SOURCE", "SDMT_SOURCE")
    for setting in ("", " , "):
        monkeypatch.setenv("SDG_PROTECTED_DATABASES", setting)
        with pytest.raises(ProtectedDatabase, match="SDG_PROTECTED_DATABASES is not set"):
            protected_databases()
        with pytest.raises(ProtectedDatabase, match="SDG_PROTECTED_DATABASES is not set"):
            fill_protected(PLACEHOLDER)
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODDB")
    assert protected_databases() == {"PRODDB", "SDMT_SOURCE"}


@pytest.mark.parametrize("function, field", [("ensure_store", "denorm_db"),
                                             ("refresh_source_views", "source_db")])
def test_the_setup_code_refuses_production_before_connecting(function, field, monkeypatch):
    """Whoever calls them: the guard at the top of a script is the second line."""
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT / "seed"))
    import bootstrap

    from sdg.config import ProtectedDatabase

    def connect(*args, **kwargs):
        raise AssertionError("connected to a protected database")

    monkeypatch.setattr(bootstrap.pyodbc, "connect", connect)
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODDB")
    settings = SimpleNamespace(denorm_db="SDM_DENORM", source_db="FACETS_QA",
                               denorm_schema="sdm")
    setattr(settings, field, "PRODDB")
    with pytest.raises(ProtectedDatabase, match="PRODDB is a protected production database"):
        getattr(bootstrap, function)(settings)


@pytest.mark.parametrize("name", ["PROD DB", "PROD\nDB", "PROD\tDB"])
def test_a_protected_name_the_guard_could_not_match_is_refused(monkeypatch, name):
    """The guards take spaces out of the list a person writes ("A, B"): a
    name with a space or line break could never match, so it is refused
    rather than left unguarded."""
    from sdg.config import ProtectedDatabase, fill_protected

    monkeypatch.setenv("SDG_ENV", "local")
    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", f"OTHER,{name}")
    with pytest.raises(ProtectedDatabase, match="cannot hold"):
        fill_protected(PLACEHOLDER)


def test_the_examples_placeholder_left_in_the_setting_is_refused(monkeypatch):
    from sdg.config import ProtectedDatabase, protected_databases

    monkeypatch.setenv("SDG_ENV", "local")
    monkeypatch.delenv("SDG_DENORM_SERVER", raising=False)
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "<production database names>")
    with pytest.raises(ProtectedDatabase, match="placeholder"):
        protected_databases()

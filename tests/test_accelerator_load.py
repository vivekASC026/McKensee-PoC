"""Build and load on screen: the Load tab's engine (sdg/accelerator/loading.py).

docs/day0-accelerator-phase3-plan.md, step 5. The sample bank (seed/bank.py)
is built in a database of its own, read and proposed as in the parity test
(primary dbo.CUSTOMER, child dbo.ACCOUNT with its lines dbo.TXN), one
identifying column added withheld and the customer's key made a keyed
surrogate, then submitted, approved and built into tmp_path. Then, in this
process and with no restart: create the tables, load, check, load again.

Negative controls: an edited store script is not run; an extract edited to
write is refused before anything is read; a column renamed in the source
refuses the load; another session holding the store's extract lock refuses
it; a stop after the first table leaves the next one as it was; a protected
store and a source never built are refused.

One store database holds the approval and the loaded tables. Only the two
SDMT_ACCEL_LOAD* databases named below are created, and each is dropped
afterwards by its exact name.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pyodbc
import pytest

pytestmark = [pytest.mark.db, pytest.mark.slow]

SOURCE = "SDMT_ACCEL_LOAD_SRC"
STORE = "SDMT_ACCEL_LOAD_STORE"
CREATED = (SOURCE, STORE)
PACK_ID = "load_bank"
# A key for this test's surrogates only: never a customer's.
TEST_EXTRACT_KEY = "load-test-key-not-a-secret"


def _master() -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _checked(database: str) -> str:
    assert database in CREATED, database
    assert re.fullmatch(r"SDMT_ACCEL_LOAD_[A-Z]+", database), database
    return database


def _exists(database: str) -> bool:
    with _master() as master:
        return master.execute("SELECT DB_ID(?)", _checked(database)).fetchone()[0] is not None


def _drop(database: str) -> None:
    name = _checked(database)
    with _master() as master:
        master.execute(f"IF DB_ID(N'{name}') IS NOT NULL BEGIN ALTER DATABASE [{name}] "
                       f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{name}]; END")


def _fresh(database: str, created: list[str]) -> str:
    name = _checked(database)
    if _exists(name):
        pytest.fail(f"{name} is there already, and this test drops only the databases it "
                    f"creates: drop it by hand if it is an earlier run's, then run again.",
                    pytrace=False)
    created.append(name)
    return name


def _connect(database: str, autocommit: bool = False) -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string(_checked(database)),
                          autocommit=autocommit)


def _scalar(database: str, sql: str) -> int:
    with _connect(database) as conn:
        return int(conn.cursor().execute(sql).fetchone()[0])


def _cli(runner, *args: str) -> str:
    from sdg.cli.accelerate import accelerate

    done = runner.invoke(accelerate, list(args))
    assert done.exit_code == 0, (args, done.output, done.exception)
    return done.output


def _quiet(done: int, total: int, words: str) -> None:
    pass


def _never() -> bool:
    return False


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from sdg.config import load_env, not_a_demo_machine

    load_env()
    reason = not_a_demo_machine()
    if reason:
        pytest.skip(f"the load proof builds and drops databases: this machine is {reason}")
    from typer.testing import CliRunner

    from sdg.accelerator import approval, sources
    from sdg.accelerator import plan as planning
    from seed import bank as sample

    packs_dir = tmp_path_factory.mktemp("load") / "packs"
    mp = pytest.MonkeyPatch()
    created: list[str] = []
    try:
        counts = sample.build(_fresh(SOURCE, created))
        with _master() as master:
            master.execute(f"CREATE DATABASE [{_fresh(STORE, created)}]")
        # The store as the table setup makes it (patterns, source_plans, the log).
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k not in ("SDG_PACKS_DIR", "SDG_PACK")}
        env.update(SDG_DB_DENORM=STORE, SDG_DENORM_SCHEMA="sdm")
        made = subprocess.run([sys.executable, "-m", "sdg.cli.main", "customer-tables",
                               "--no-indexes"], cwd=Path(__file__).resolve().parent.parent,
                              env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=900)
        assert made.returncode == 0, made.stdout[-2000:] + made.stderr[-2000:]
        mp.setenv("SDG_DB_DENORM", STORE)
        mp.setenv("SDG_DENORM_SCHEMA", "sdm")
        mp.setenv("SDG_PACKS_DIR", str(packs_dir))
        mp.setenv("SDG_EXTRACT_KEY", TEST_EXTRACT_KEY)
        mp.setattr(approval, "second_approver", lambda: (None, False))

        runner = CliRunner()
        _cli(runner, "landscape", SOURCE, "--allow-write-login")
        _cli(runner, "propose", SOURCE, "--allow-write-login", "--force",
             "--primary", "dbo.CUSTOMER", "--child", "dbo.ACCOUNT")
        _cli(runner, "name", SOURCE, "primary", "customer", "customers")
        _cli(runner, "name", SOURCE, "child", "account", "accounts")
        _cli(runner, "pack-name", SOURCE, PACK_ID, "Loaded bank")
        _cli(runner, "extra", SOURCE, "primary", "dbo.CUSTOMER", "EMAIL")
        _cli(runner, "privacy", SOURCE, "dbo.CUSTOMER", "CUSTOMER_ID", "surrogate")
        _cli(runner, "privacy", SOURCE, "dbo.ACCOUNT", "ACCOUNT_ID", "kept",
             "--confirm-not-personal")
        # A feature of each line (the transaction's amount), offered and chosen.
        from sdg.accelerator import workbench

        workbench.edit(SOURCE, lambda held, graph: planning.select_feature(
            held, "child", "dbo.TXN.AMOUNT", True, graph=graph))
        _cli(runner, "submit", SOURCE)
        _cli(runner, "approve", SOURCE)
        _cli(runner, "build", SOURCE)
        held = planning.load(sources.folder_for(SOURCE))
        yield {"plan": held, "folder": packs_dir / PACK_ID, "counts": counts, "mp": mp}
    finally:
        mp.undo()
        failures = []
        for name in created:
            try:
                _drop(name)
            except Exception as exc:  # each is tried; every failure is said below
                failures.append(f"{name}: {type(exc).__name__}")
        left = [name for name in created if _exists(name)]
        assert not failures and not left, (failures, left)


def _table(held, role: str):
    return next(t for t in held.structure if t.role == role)


def _edited(path: Path, change):
    """Edit a built file for the length of a block, then put it back."""
    class Edit:
        def __enter__(self):
            self.text = path.read_bytes()
            path.write_bytes(change(self.text.decode("utf-8")).encode("utf-8"))

        def __exit__(self, *exc):
            path.write_bytes(self.text)
    return Edit()


# --- in order: the tables, the load, a refresh ---------------------------------

def test_1_before_the_tables_the_store_says_what_is_missing(built):
    from sdg.accelerator import loading

    state = loading.status(SOURCE)
    assert state["built"]["pack_id"] == PACK_ID and state["built"]["edited"] == []
    assert state["store"]["may_create"] is True and state["store"]["ready"] is False
    assert {t["name"] for t in state["store"]["tables"]} == {t.name for t in built["plan"].structure}
    assert not any(t["exists"] for t in state["store"]["tables"])
    assert state["analyzing"] is False and state["loads"] == []
    with pytest.raises(loading.LoadRefused, match="create the tables first") as refused:
        loading.load(SOURCE, _quiet, _never, allow_write_login=True)
    assert refused.value.kind == "tables"


def test_2_an_edited_store_script_is_the_dbas(built):
    from sdg.accelerator import loading

    path = built["folder"] / "sql" / "store_tables.sql"
    with _edited(path, lambda text: text + "\n-- an edit\n"):
        assert loading.status(SOURCE)["built"]["edited"] == ["sql/store_tables.sql"]
        with pytest.raises(loading.LoadRefused, match="edited after the build") as refused:
            loading.create_tables(SOURCE)
        assert refused.value.kind == "dba"
    assert not any(t["exists"] for t in loading.status(SOURCE)["store"]["tables"])


def test_3_create_tables_makes_every_table_and_again_changes_nothing(built):
    from sdg.accelerator import loading

    done = loading.create_tables(SOURCE)
    assert done["store"]["ready"] is True
    for table in done["store"]["tables"]:
        assert table["exists"] and not table["lacking"] and not table["differs"], table
    again = loading.create_tables(SOURCE)
    assert again["store"] == done["store"]


def _counts(held) -> dict[str, int]:
    return {t.name: _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{t.name}]") for t in held.structure}


def _stamps() -> int:
    return _scalar(STORE, "SELECT COUNT(*) FROM sdm.extract_log")


def _lock_is_free() -> bool:
    """Whether a session of its own can take the store's extract lock now."""
    from sdg.extract.run import StoreBusy, hold_store, release_store

    other = _connect(STORE)
    try:
        hold_store(other)
    except StoreBusy:
        other.close()
        return False
    # Given back by name: a pooled session outlives close().
    release_store(other)
    other.close()
    return True


def test_4_load_fills_the_tables_withholds_and_checks(built):
    from sdg.accelerator import loading

    told: list = []
    done = loading.load(SOURCE, lambda d, t, w: told.append((d, t, w)), _never,
                        allow_write_login=True)
    assert done["failed"] == 0 and done["stopped"] is False, done
    primary, child = _table(built["plan"], "primary"), _table(built["plan"], "child")
    rows = {t["table"]: t["rows"] for t in done["load"]["tables"]}
    # What the extract says it wrote, what the source holds, what the store holds.
    assert rows[primary.name] == _scalar(SOURCE, "SELECT COUNT(*) FROM dbo.CUSTOMER") == \
        _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{primary.name}]")
    assert rows[child.name] == _scalar(SOURCE, "SELECT COUNT(*) FROM dbo.TXN") == \
        _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{child.name}]")
    for denorm in (primary, child):
        for column in denorm.columns:
            if column.privacy == "withheld":
                assert _scalar(STORE, f"SELECT COUNT([{column.name}]) FROM "
                                      f"sdm.[{denorm.name}]") == 0, column.name
            if column.privacy == "surrogate":
                assert _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{denorm.name}] WHERE "
                                      f"LEN([{column.name}]) <> 16") == 0, column.name
    assert any(c.privacy == "withheld" for c in primary.columns)
    # The customer's key is in the store as its surrogate, never as the source has it.
    key = next(c for c in primary.columns if c.privacy == "surrogate"
               and c.source_column == "CUSTOMER_ID")
    with _connect(STORE) as conn:
        stored = {r[0] for r in conn.execute(f"SELECT [{key.name}] FROM sdm.[{primary.name}]")}
    with _connect(SOURCE) as conn:
        real = {str(r[0]) for r in conn.execute("SELECT CUSTOMER_ID FROM dbo.CUSTOMER")}
    assert stored and not stored & real and len(stored) == len(real)
    built["surrogates"] = stored
    failed = [c for c in done["checks"] if not c["passed"]]
    assert not failed, failed
    assert {c["check"] for c in done["checks"]} >= {"rows", "key", "filled", "meets"}
    # The line's own amount, on every line as the source has it.
    amount = next(c for c in child.columns if c.serves == "feature"
                  and (c.source_table, c.source_column) == ("dbo.TXN", "AMOUNT"))
    assert any(c["check"] == "filled" and c["said"].startswith(f"{amount.name}:")
               for c in done["checks"])
    assert _scalar(STORE, f"SELECT COUNT([{amount.name}]) FROM sdm.[{child.name}]") ==         _scalar(SOURCE, "SELECT COUNT(AMOUNT) FROM dbo.TXN")
    assert _scalar(STORE, "SELECT COUNT(*) FROM sys.indexes WHERE name LIKE 'ix[_]%'") >= 2
    assert told[-1][0] == told[-1][1]
    loads = loading.status(SOURCE)["loads"]
    assert {r["table"] for r in loads} == {primary.name, child.name}
    assert all(r["error"] is None for r in loads)
    # The store's extract lock was given back.
    assert _lock_is_free()


def test_5_a_refresh_replaces_the_rows_it_does_not_add_to_them(built):
    from sdg.accelerator import loading

    before = _counts(built["plan"])
    done = loading.load(SOURCE, _quiet, _never, allow_write_login=True)
    assert done["failed"] == 0
    assert not [c for c in done["checks"] if not c["passed"]]
    assert {t["table"]: t["rows"] for t in done["load"]["tables"]} == before
    assert _counts(built["plan"]) == before
    # The same key, the same surrogate: one customer, one ID across refreshes.
    primary = _table(built["plan"], "primary")
    key = next(c for c in primary.columns if c.privacy == "surrogate"
               and c.source_column == "CUSTOMER_ID")
    with _connect(STORE) as conn:
        again = {r[0] for r in conn.execute(f"SELECT [{key.name}] FROM sdm.[{primary.name}]")}
    assert again == built["surrogates"]


# --- load then swap ------------------------------------------------------------------

def _leftovers() -> list[str]:
    with _connect(STORE) as conn:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sys.tables WHERE SCHEMA_NAME(schema_id) = 'sdm' "
            "AND (name LIKE '%[_][_]new%' OR name LIKE '%[_][_]old')")]


def test_analysis_reads_the_old_rows_until_every_table_is_swapped_in(built):
    """Mid-load, a reader of a live table is not kept waiting and sees the
    rows it had; once the load ends, the new ones, and nothing is left beside."""
    from sdg.accelerator import loading

    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"DELETE TOP (7) FROM sdm.[{names[0]}]")
    old = _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{names[0]}]")
    seen: list = []

    def told(done: int, total: int, words: str) -> None:
        if words.startswith(f"{names[0]}:") and "so far" not in words and not seen:
            reader = _connect(STORE)
            try:
                reader.execute("SET LOCK_TIMEOUT 2000")
                seen.append(reader.execute(f"SELECT COUNT(*) FROM sdm.[{names[0]}]")
                            .fetchone()[0])
                seen.append(sorted(_leftovers()))
            finally:
                reader.close()

    done = loading.load(SOURCE, told, _never, allow_write_login=True)
    assert done["staged"] is True and done["failed"] == 0
    assert seen[0] == old, "the live table kept its rows while the load ran"
    # Each copy has a name of this load's own beside its table.
    assert len(seen[1]) == len(names) and all(
        re.fullmatch(re.escape(n) + r"__new_[0-9a-f]{8}", left)
        for n, left in zip(sorted(names), seen[1])), seen[1]
    assert _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{names[0]}]") ==         _scalar(SOURCE, "SELECT COUNT(*) FROM dbo.CUSTOMER") > old
    assert _leftovers() == []
    assert not [c for c in done["checks"] if not c["passed"]]


def test_a_table_that_fails_leaves_every_table_as_it_was(built, monkeypatch):
    from sdg.accelerator import loading
    from sdg.extract import run

    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        for name in names:
            conn.execute(f"DELETE TOP (3) FROM sdm.[{name}]")
    before, stamps = _counts(built["plan"]), _stamps()
    real = run._one_table

    def second_fails(read, write, source, schema, table, *args, **kwargs):
        if table == names[1]:
            raise RuntimeError("the store refused a row")
        return real(read, write, source, schema, table, *args, **kwargs)

    monkeypatch.setattr(run, "_one_table", second_fails)
    try:
        done = loading.load(SOURCE, _quiet, _never, allow_write_login=True)
        monkeypatch.undo()
        errors = {t["table"]: t["error"] for t in done["load"]["tables"]}
        assert errors[names[0]] == loading.NOT_PLACED
        assert errors[names[1]].startswith(f"The load of {names[1]} failed")
        assert _counts(built["plan"]) == before
        assert _leftovers() == []
        # Each table's outcome is logged, a table not put in place with no rows.
        assert _stamps() == stamps + 2
        with _connect(STORE) as conn:
            logged = conn.execute("SELECT TOP 1 row_count FROM sdm.extract_log WHERE "
                                  "table_name = ? ORDER BY finished_at DESC",
                                  names[0]).fetchone()[0]
        assert logged == 0
    finally:
        monkeypatch.undo()
        assert _load()["failed"] == 0  # put back


def test_a_table_with_its_own_grant_is_replaced_where_it_stands(built):
    """A copy made beside a table would lose a grant on it: such a table is
    replaced in place, said, and keeps the grant."""
    from sdg.accelerator import loading

    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"GRANT SELECT ON sdm.[{names[0]}] TO public")
    try:
        done = _load()
        assert done["staged"] is False and done["failed"] == 0
        assert any("permissions, rules or indexes of its own" in n for n in done["notes"]),             done["notes"]
        with _connect(STORE) as conn:
            kept = conn.execute("SELECT COUNT(*) FROM sys.database_permissions WHERE class = 1 "
                                "AND major_id = OBJECT_ID(?)", f"sdm.{names[0]}").fetchone()[0]
        assert kept == 1
        assert _counts(built["plan"])[names[0]] == _scalar(SOURCE,
                                                            "SELECT COUNT(*) FROM dbo.CUSTOMER")
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"REVOKE SELECT ON sdm.[{names[0]}] FROM public")
    assert _load()["staged"] is True


def test_an_index_the_dba_added_is_on_the_new_rows(built):
    from sdg.accelerator import loading

    names = [t["name"] for t in loading.built(SOURCE).tables]
    column = next(c["name"] for t in loading.built(SOURCE).tables if t["name"] == names[1]
                  for c in t["columns"] if c["serves"] == "feature")
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"CREATE INDEX dba_extra ON sdm.[{names[1]}] ([{column}] DESC)")
    try:
        done = _load()
        assert done["staged"] is True and done["failed"] == 0
        with _connect(STORE) as conn:
            found = conn.execute("SELECT COUNT(*) FROM sys.indexes WHERE name = 'dba_extra' "
                                 "AND object_id = OBJECT_ID(?)", f"sdm.{names[1]}").fetchone()[0]
        assert found == 1
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"DROP INDEX IF EXISTS dba_extra ON sdm.[{names[1]}]")


def test_a_table_with_an_index_the_copy_cannot_make_is_replaced_where_it_stands(built):
    from sdg.accelerator import loading

    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"CREATE NONCLUSTERED COLUMNSTORE INDEX dba_cs ON sdm.[{names[1]}] "
                     "([ACCOUNT_ID])")
    try:
        done = _load()
        assert done["staged"] is False and done["failed"] == 0
        with _connect(STORE) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sys.indexes WHERE name = 'dba_cs'"
                                ).fetchone()[0] == 1
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"DROP INDEX IF EXISTS dba_cs ON sdm.[{names[1]}]")


def test_a_reader_holding_a_table_makes_the_swap_give_up_cleanly(built, monkeypatch):
    from sdg.accelerator import loading

    monkeypatch.setattr(loading, "SWAP_LOCK_TIMEOUT_MS", 1000)
    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"DELETE TOP (4) FROM sdm.[{names[0]}]")
    before = _counts(built["plan"])
    reader = _connect(STORE)
    try:
        # A reader that keeps its lock on the live table (repeatable read: a
        # serializable one would also hold a system table the load allocates
        # through, which no analysis query does).
        reader.execute(f"SELECT COUNT(*) FROM sdm.[{names[0]}] WITH (REPEATABLEREAD, TABLOCK)")
        done = _load()
    finally:
        reader.rollback()
        reader.close()
    assert any("a reader held" in n for n in done["notes"]), done["notes"]
    assert all(t["error"] == loading.NOT_PLACED for t in done["load"]["tables"])
    assert _counts(built["plan"]) == before and _leftovers() == []
    assert _load()["failed"] == 0


def _pattern(db, monkeypatch, version: str, level: str) -> int:
    from sdg.analyzer.services.discovery_service import canonical_combo

    monkeypatch.setattr(db, "build_stamp", lambda: {"pack_id": PACK_ID, "plan_version": version})
    monkeypatch.setattr(db, "_TABLE_SHAPE", {})
    combo = canonical_combo({"grgr_ck": [1], "cspi_id": [level]}, entity="claims")
    return db.upsert_pattern(combo, 5, f"SDMT {level}", 3, 0.1, 0.2)


def test_a_load_archives_the_patterns_of_the_packs_earlier_builds(built, monkeypatch):
    from sdg.accelerator import approval, loading
    from sdg.analyzer import db

    current = loading.built(SOURCE).marker["built_hash"]
    earlier = _pattern(db, monkeypatch, "e" * 64, "SDMT-EARLIER")
    mine = _pattern(db, monkeypatch, current, "SDMT-CURRENT")
    monkeypatch.undo()
    assert approval.loaded_hash(SOURCE) == current  # loaded by the tests before
    # A refresh of the same build archives another build's patterns only.
    done = _load()
    assert done["archived"] == 1
    assert db.get_pattern(earlier)["archived_at"] is not None
    assert db.get_pattern(mine)["archived_at"] is None
    # The first load of a build archives every current pattern of the pack:
    # one made before it describes the earlier rows.
    approval.record_load(SOURCE, "f" * 64)
    done = _load()
    assert done["archived"] == 1 and db.get_pattern(mine)["archived_at"] is not None
    assert approval.loaded_hash(SOURCE) == current


def test_a_store_that_cannot_archive_does_not_record_the_build_as_loaded(built, monkeypatch):
    """Without the archive's columns nothing is archived, and the build is not
    recorded as loaded: the first load after the table setup archives all."""
    from sdg.accelerator import approval
    from sdg.analyzer import db

    approval.record_load(SOURCE, "c" * 64)
    monkeypatch.setattr(db, "archive_other_builds", lambda *a, **k: None)
    done = _load()
    assert done["archived"] is None and any("table setup" in n for n in done["notes"])
    assert approval.loaded_hash(SOURCE) == "c" * 64
    monkeypatch.undo()
    _load()


def test_a_load_that_fails_archives_nothing(built, monkeypatch):
    from sdg.accelerator import loading
    from sdg.analyzer import db
    from sdg.extract import run

    earlier = _pattern(db, monkeypatch, "d" * 64, "SDMT-KEPT")
    monkeypatch.undo()
    names = [t["name"] for t in loading.built(SOURCE).tables]
    real = run._one_table

    def second_fails(read, write, source, schema, table, *args, **kwargs):
        if table == names[1]:
            raise RuntimeError("the store refused a row")
        return real(read, write, source, schema, table, *args, **kwargs)

    monkeypatch.setattr(run, "_one_table", second_fails)
    try:
        done = loading.load(SOURCE, _quiet, _never, allow_write_login=True)
        assert done["failed"] and done["archived"] is None
        assert db.get_pattern(earlier)["archived_at"] is None
    finally:
        monkeypatch.undo()
    assert _load()["archived"] == 1


def test_what_an_earlier_load_left_beside_the_tables_is_cleared(built):
    from sdg.accelerator import loading

    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"SELECT TOP 1 * INTO sdm.[{names[0]}__new] FROM sdm.[{names[0]}]")
        conn.execute(f"SELECT TOP 1 * INTO sdm.[{names[1]}__old] FROM sdm.[{names[1]}]")
    assert sorted(_leftovers()) == sorted([names[0] + "__new", names[1] + "__old"])
    assert _load()["failed"] == 0
    assert _leftovers() == []


# --- refusals: nothing is read or written ------------------------------------------

def _refused_untouched(built, call, kind: str, words: str):
    """`call` is refused with `kind` and `words`, and the store's tables and
    log are as they were."""
    from sdg.accelerator import loading

    before, stamps = _counts(built["plan"]), _stamps()
    with pytest.raises(loading.LoadRefused, match=words) as refused:
        call()
    assert refused.value.kind == kind
    assert _counts(built["plan"]) == before and _stamps() == stamps
    return refused.value


def _load():
    from sdg.accelerator import loading

    return loading.load(SOURCE, _quiet, _never, allow_write_login=True)


def test_an_edited_extract_is_not_run_from_here(built):
    """From the screen the extract runs as built: an edit that writes, one
    that names another table on its `-- table:` line, or a harmless one, is
    refused before anything is read, and the source is unchanged."""
    path = built["folder"] / "extract" / f"{SOURCE}.sql"
    emails = _scalar(SOURCE, "SELECT COUNT(EMAIL) FROM dbo.CUSTOMER")
    assert emails > 0
    primary = _table(built["plan"], "primary").name
    for change in (
            lambda text: text.replace("SELECT", "UPDATE dbo.CUSTOMER SET EMAIL = NULL; SELECT", 1),
            lambda text: text.replace(f"-- table: {primary}", "-- table: extract_log", 1),
            lambda text: text.replace(f"-- table: {primary}",
                                      f"-- table: {primary}]; DROP TABLE sdm.extract_log; --", 1),
            lambda text: text + "\n-- a harmless note\n"):
        with _edited(path, change):
            _refused_untouched(built, _load, "edited", "was edited after the build")
    assert _scalar(SOURCE, "SELECT COUNT(EMAIL) FROM dbo.CUSTOMER") == emails


def test_a_column_renamed_or_retyped_in_the_source_refuses_the_load(built):
    with _connect(SOURCE, autocommit=True) as conn:
        conn.execute("EXEC sp_rename 'dbo.CUSTOMER.EMAIL', 'EMAIL_ADDRESS', 'COLUMN'")
    try:
        _refused_untouched(built, _load, "drift", r"dbo\.CUSTOMER\.EMAIL is not in")
    finally:
        with _connect(SOURCE, autocommit=True) as conn:
            conn.execute("EXEC sp_rename 'dbo.CUSTOMER.EMAIL_ADDRESS', 'EMAIL', 'COLUMN'")

    with _connect(SOURCE) as conn:
        typed = conn.execute(
            "SELECT TYPE_NAME(user_type_id), max_length FROM sys.columns "
            "WHERE object_id = OBJECT_ID('dbo.CUSTOMER') AND name = 'EMAIL'").fetchone()
    assert typed[0] in ("varchar", "nvarchar"), typed
    length = typed[1] // (2 if typed[0] == "nvarchar" else 1)
    with _connect(SOURCE, autocommit=True) as conn:
        conn.execute(f"ALTER TABLE dbo.CUSTOMER ALTER COLUMN EMAIL {typed[0]}({length + 10}) NULL")
    try:
        _refused_untouched(built, _load, "drift", r"dbo\.CUSTOMER\.EMAIL changed type")
    finally:
        with _connect(SOURCE, autocommit=True) as conn:
            conn.execute(f"ALTER TABLE dbo.CUSTOMER ALTER COLUMN EMAIL {typed[0]}({length}) NULL")


def test_another_extract_holding_the_store_refuses_the_load(built):
    from sdg.extract.run import hold_store, release_store

    other = _connect(STORE)
    try:
        hold_store(other)
        _refused_untouched(built, _load, "busy", "Another extract")
    finally:
        release_store(other)  # by name: a pooled session outlives close()
        other.close()
    assert _lock_is_free()


def test_a_stop_leaves_the_tables_after_it_as_they_were(built):
    from sdg.accelerator import loading
    from sdg.extract.run import NOT_RUN

    asked: list = []

    def stop() -> bool:
        asked.append(1)
        return len(asked) > 1

    names = [t["name"] for t in loading.built(SOURCE).tables]
    try:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"DELETE TOP (5) FROM sdm.[{names[1]}]")
            conn.execute(f"DROP INDEX IF EXISTS {_one_index(conn, names[0])} ON sdm.[{names[0]}]")
        indexes = _scalar(STORE, "SELECT COUNT(*) FROM sys.indexes WHERE name LIKE 'ix[_]%'")
        left = _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{names[1]}]")
        done = loading.load(SOURCE, _quiet, stop, allow_write_login=True)
        assert done["stopped"] is True
        assert _leftovers() == []
        second = next(t for t in done["load"]["tables"] if t["table"] == names[1])
        assert second["error"] == NOT_RUN and second["rows"] == 0
        assert _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{names[1]}]") == left
        # A stopped load makes no indexes.
        assert _scalar(STORE, "SELECT COUNT(*) FROM sys.indexes WHERE name LIKE 'ix[_]%'") == \
            indexes
    finally:
        # Put back for whatever runs after (indexes too).
        assert _load()["failed"] == 0


def _one_index(conn, table: str) -> str:
    return conn.execute("SELECT TOP (1) name FROM sys.indexes WHERE object_id = "
                        "OBJECT_ID(?) AND name LIKE 'ix[_]%'", f"sdm.{table}").fetchone()[0]


def test_an_edited_index_script_is_not_run_and_is_said(built):
    from sdg.accelerator import loading

    names = [t["name"] for t in loading.built(SOURCE).tables]
    path = built["folder"] / "sql" / "store_indexes.sql"
    with _connect(STORE, autocommit=True) as conn:
        dropped = _one_index(conn, names[0])
        conn.execute(f"DROP INDEX {dropped} ON sdm.[{names[0]}]")
    try:
        with _edited(path, lambda text: text + "\n-- an edit\n"):
            done = _load()
        assert done["failed"] == 0
        assert any("Indexes not made" in n and "edited after the build" in n
                   for n in done["notes"]), done["notes"]
        assert _scalar(STORE, f"SELECT COUNT(*) FROM sys.indexes WHERE name = '{dropped}'") == 0
    finally:
        assert not [n for n in _load()["notes"] if "Indexes not made" in n]
    assert _scalar(STORE, f"SELECT COUNT(*) FROM sys.indexes WHERE name = '{dropped}'") == 1


def test_a_pack_json_edited_after_the_build_is_not_loaded(built):
    path = built["folder"] / "pack.json"
    with _edited(path, lambda text: text.replace('"withheld"', '"was_withheld"', 1)):
        _refused_untouched(built, _load, "edited", "pack.json was edited")


def test_a_marker_rewritten_to_pass_an_edit_is_refused(built):
    import json

    from sdg.accelerator import builder, loading

    marker = built["folder"] / builder.MARKER
    script = built["folder"] / "sql" / "store_tables.sql"

    def forged(text: str) -> str:
        held = json.loads(text)
        held["files"]["sql/store_tables.sql"] = loading._sha(
            script.read_bytes().decode("utf-8"))
        return json.dumps(held)

    with _edited(script, lambda text: text + "\n-- an edit\n"), _edited(marker, forged):
        _refused_untouched(built, lambda: loading.create_tables(SOURCE), "not_built",
                           "does not agree with itself")

    def shrunk(text: str) -> str:
        held = json.loads(text)
        held["reads"] = {name: table for name, table in list(held["reads"].items())[:1]}
        held["store"] = held["store"][:1]
        return json.dumps(held)

    with _edited(marker, shrunk):
        _refused_untouched(built, _load, "not_built", "does not agree with itself")


def test_a_plan_not_approved_now_is_not_created_or_loaded(built):
    """Withdrawn after the build: neither Create tables nor a load runs until
    it is approved again."""
    from typer.testing import CliRunner

    from sdg.accelerator import loading

    runner = CliRunner()
    _cli(runner, "withdraw", SOURCE)
    try:
        for call in (lambda: loading.create_tables(SOURCE), _load):
            _refused_untouched(built, call, "approval", "not approved now")
    finally:
        _cli(runner, "submit", SOURCE)
        _cli(runner, "approve", SOURCE)
    assert _load()["failed"] == 0


def test_a_table_that_is_someone_elses_is_never_changed_or_refilled(built):
    from sdg.accelerator import loading

    primary = _table(built["plan"], "primary").name
    # A column an earlier build had, in a table this source filled: kept and said.
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"ALTER TABLE sdm.[{primary}] ADD EARLIER int NULL")
    try:
        shown = next(t for t in loading.status(SOURCE)["store"]["tables"]
                     if t["name"] == primary)
        assert shown["extra"] == ["EARLIER"] and not shown["foreign"] and shown["ready"]
        # Said for the DBA as the statement to run; never run from here.
        assert loading.status(SOURCE)["store"]["drops"] == [
            f"ALTER TABLE [sdm].[{primary}] DROP COLUMN [EARLIER];"]
        assert _load()["failed"] == 0

        # The same table, never filled by this source: someone else's.
        with _connect(STORE, autocommit=True) as conn:
            # Literal, not a parameter: a prepared statement's #table ends with it.
            assert re.fullmatch(r"SDMT_[A-Z_]+", SOURCE)
            conn.execute(f"SELECT * INTO #ours FROM sdm.extract_log WHERE source = '{SOURCE}'")
            conn.execute(f"DELETE FROM sdm.extract_log WHERE source = '{SOURCE}'")
            try:
                with pytest.raises(loading.LoadRefused, match="not this build's table") as no:
                    loading.create_tables(SOURCE)
                assert no.value.kind == "foreign"
                assert next(t for t in loading.status(SOURCE)["store"]["tables"]
                            if t["name"] == primary)["foreign"]
            finally:
                conn.execute("INSERT INTO sdm.extract_log SELECT * FROM #ours")
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"ALTER TABLE sdm.[{primary}] DROP COLUMN EARLIER")

    # Filled from another source: someone else's, whatever its columns.
    with _connect(STORE, autocommit=True) as conn:
        conn.execute("INSERT INTO sdm.extract_log (source, table_name, row_count, finished_at) "
                     "VALUES ('SDMT_OTHER_SOURCE', ?, 1, SYSDATETIME())", primary)
    try:
        _refused_untouched(built, _load, "foreign", "filled from another source")
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute("DELETE FROM sdm.extract_log WHERE source = 'SDMT_OTHER_SOURCE' "
                         "AND table_name = ?", primary)
    assert loading.status(SOURCE)["store"]["ready"] is True


def test_a_column_of_another_type_in_its_own_table_is_the_dbas(built):
    """A table this source filled with a column narrowed since: not ready,
    and the load says the DBA alters it, before anything is emptied."""
    from sdg.accelerator import loading

    child = _table(built["plan"], "child")
    # A column no index of the build's depends on.
    column = next(c for c in child.columns if c.privacy == "kept"
                  and c.serves in ("level_label", "feature"))
    with _connect(STORE) as conn:
        base, length, precision, scale = conn.execute(
            "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = 'sdm' AND TABLE_NAME = ? AND COLUMN_NAME = ?",
            child.name, column.name).fetchone()
    # Another type that holds every value it has: a text column or a decimal widened.
    if base in ("varchar", "nvarchar", "char", "nchar") and length and length > 0:
        wanted, other = f"{base}({length})", f"{base}({length + 5})"
    else:
        assert base in ("decimal", "numeric") and precision and precision < 36, base
        wanted, other = f"{base}({precision},{scale})", f"{base}({precision + 2},{scale})"
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"ALTER TABLE sdm.[{child.name}] ALTER COLUMN [{column.name}] {other} NULL")
    try:
        shown = next(t for t in loading.status(SOURCE)["store"]["tables"]
                     if t["name"] == child.name)
        assert shown["differs"] == [column.name] and not shown["foreign"]
        assert not shown["ready"]
        _refused_untouched(built, _load, "tables", "Ask the DBA to alter")
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"ALTER TABLE sdm.[{child.name}] ALTER COLUMN [{column.name}] "
                         f"{wanted} NULL")
    assert loading.status(SOURCE)["store"]["ready"] is True


def test_the_privacy_classes_come_from_the_pack_json_whose_hash_was_checked(built,
                                                                            monkeypatch):
    """pack.json is read once: what a load nulls by is the text that was
    checked, never the file read again after."""
    from sdg import packs
    from sdg.accelerator import loading

    given: list = []
    real = packs.read_folder

    def reading(folder, pack_id, pack_text=None):
        given.append(pack_text)
        return real(folder, pack_id, pack_text)

    monkeypatch.setattr(packs, "read_folder", reading)
    held = loading.built(SOURCE)
    path = built["folder"] / "pack.json"
    assert given[-1] == path.read_bytes().decode("utf-8")
    assert held.pack.data["extract"] == __import__("json").loads(given[-1])["extract"]
    # An edited pack.json is never what a pack is built from here.
    with _edited(path, lambda text: text.replace('"withheld"', '"was_withheld"', 1)):
        assert "pack.json" in loading.built(SOURCE).edited and given[-1] is None


def test_a_protected_store_is_refused(built, monkeypatch):
    from sdg.accelerator import loading
    from sdg.config import ProtectedDatabase

    monkeypatch.setenv("SDG_PROTECTED_DATABASES", STORE)
    with pytest.raises(ProtectedDatabase):
        loading.create_tables(SOURCE)
    with pytest.raises(ProtectedDatabase):
        _load()


def test_the_guard_in_a_store_script_is_said_in_plain_words(built, monkeypatch):
    """A store script run in a database its guard names stops at the guard,
    said as the build's refusal, not as a driver error."""
    from sdg.accelerator import loading

    held = loading.built(SOURCE)
    script = held.folder / "sql" / "store_tables.sql"
    guarded = script.read_bytes().decode("utf-8").replace(
        "IF DB_NAME() IN (", f"IF DB_NAME() IN (N'{STORE}', ", 1)
    assert guarded != script.read_bytes().decode("utf-8")
    monkeypatch.setattr(loading, "as_built", lambda held, name, kind="dba": guarded)
    with pytest.raises(loading.LoadRefused, match="refused this database"):
        loading._run_script(held, loading.STORE_TABLES, loading._store())


def test_a_plan_approved_again_but_not_rebuilt_is_not_loaded(built):
    """Edited, submitted and approved again with no build: the pack on disk is
    the earlier plan's (its privacy classes too), so nothing is loaded from it
    until it is built again."""
    from typer.testing import CliRunner

    from sdg.accelerator import loading, plan as planning, sources, workbench

    runner = CliRunner()
    held = planning.load(sources.folder_for(SOURCE))
    feature = next(f for f in held.features("child") if not f.selected)
    workbench.edit(SOURCE, lambda h, g: planning.select_feature(h, "child", feature.id, True,
                                                                graph=g))
    _cli(runner, "submit", SOURCE)
    _cli(runner, "approve", SOURCE)
    try:
        for call in (lambda: loading.create_tables(SOURCE), _load):
            _refused_untouched(built, call, "not_built", "changed after the pack was built")
    finally:
        workbench.edit(SOURCE, lambda h, g: planning.select_feature(h, "child", feature.id,
                                                                    False, graph=g))
        _cli(runner, "submit", SOURCE)
        _cli(runner, "approve", SOURCE)
    assert _load()["failed"] == 0


def test_new_rows_with_a_key_on_two_rows_never_go_live(built, monkeypatch):
    """The checks run on the copies before the swap: copies whose key repeats
    are dropped, and every live table keeps its rows."""
    from sdg.accelerator import loading
    from sdg.extract import run

    names = [t["name"] for t in loading.built(SOURCE).tables]
    before, stamps = _counts(built["plan"]), _stamps()
    real = run._blocks

    def twice(cursor, size=run.BLOCK):
        for block in real(cursor, size):
            yield block + block  # every row read twice

    monkeypatch.setattr(run, "_blocks", twice)
    try:
        done = _load()
    finally:
        monkeypatch.undo()
    assert any("did not pass their checks" in n and "more than one row" in n
               for n in done["notes"]), done["notes"]
    assert all(t["error"] == loading.NOT_PLACED for t in done["load"]["tables"])
    assert _counts(built["plan"]) == before and _leftovers() == []
    assert _stamps() == stamps + len(names)
    assert _load()["failed"] == 0


def test_a_table_with_a_constraint_of_its_own_is_replaced_where_it_stands(built):
    from sdg.accelerator import loading

    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"ALTER TABLE sdm.[{names[0]}] ADD sdmt_flag int NULL "
                     f"CONSTRAINT df_sdmt_flag DEFAULT 0")
    try:
        done = _load()
        assert done["staged"] is False and done["failed"] == 0
        with _connect(STORE) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sys.default_constraints WHERE "
                                "name = 'df_sdmt_flag'").fetchone()[0] == 1
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"ALTER TABLE sdm.[{names[0]}] DROP CONSTRAINT df_sdmt_flag")
            conn.execute(f"ALTER TABLE sdm.[{names[0]}] DROP COLUMN sdmt_flag")


def test_tables_are_not_created_while_an_extract_fills_the_store(built):
    from sdg.accelerator import loading
    from sdg.extract.run import hold_store, release_store

    other = _connect(STORE)
    try:
        hold_store(other)
        with pytest.raises(loading.LoadRefused, match="create the tables again") as busy:
            loading.create_tables(SOURCE)
        assert busy.value.kind == "busy"
    finally:
        release_store(other)
        other.close()
    assert loading.create_tables(SOURCE)["store"]["ready"] is True


def test_the_model_counts_every_partition_and_trusts_only_unfiltered_unique_indexes(built):
    """A partitioned table's rows are the sum of its partitions; a filtered or
    disabled unique index proves nothing about every row."""
    from sdg.discovery.connections import ConnectionRegistry
    from sdg.discovery.metadata import extract

    with _connect(SOURCE, autocommit=True) as conn:
        conn.execute("CREATE PARTITION FUNCTION sdmt_pf (int) AS RANGE LEFT FOR VALUES (10)")
        conn.execute("CREATE PARTITION SCHEME sdmt_ps AS PARTITION sdmt_pf ALL TO ([PRIMARY])")
        conn.execute("CREATE TABLE dbo.SDMT_PARTED (id int NOT NULL) ON sdmt_ps(id)")
        conn.execute("INSERT dbo.SDMT_PARTED SELECT TOP 30 ROW_NUMBER() OVER (ORDER BY "
                     "(SELECT 1)) FROM sys.all_objects")
        conn.execute("CREATE TABLE dbo.SDMT_FILTERED (id int NOT NULL, live bit NOT NULL)")
        conn.execute("CREATE UNIQUE INDEX ux_live ON dbo.SDMT_FILTERED (id) WHERE live = 1")
        conn.execute("CREATE TABLE dbo.SDMT_DISABLED (id int NOT NULL)")
        conn.execute("CREATE UNIQUE INDEX ux_off ON dbo.SDMT_DISABLED (id)")
        conn.execute("ALTER INDEX ux_off ON dbo.SDMT_DISABLED DISABLE")
    try:
        registry = ConnectionRegistry()
        registry.register("source", SOURCE)
        model = extract(registry, "source")
        assert model.table("dbo.SDMT_PARTED").row_count == 30
        for name in ("dbo.SDMT_FILTERED", "dbo.SDMT_DISABLED"):
            table = model.table(name)
            assert not table.unique_constraints and not table.column("id").is_unique, name
    finally:
        with _connect(SOURCE, autocommit=True) as conn:
            for name in ("SDMT_PARTED", "SDMT_FILTERED", "SDMT_DISABLED"):
                conn.execute(f"DROP TABLE IF EXISTS dbo.[{name}]")
            conn.execute("DROP PARTITION SCHEME sdmt_ps")
            conn.execute("DROP PARTITION FUNCTION sdmt_pf")


def test_a_source_never_built_is_refused(built):
    from sdg.accelerator import loading

    with pytest.raises(loading.LoadRefused) as refused:
        loading.status("SDMT_ACCEL_NEVER_BUILT")
    assert refused.value.kind == "not_built"


def test_no_rows_go_live_only_over_a_table_with_no_rows_to_lose(built, monkeypatch):
    """A copy with no rows replaces a live table that is missing or empty (an
    empty source table loads); over a table that has rows it is held back.
    A key on two rows is held back whatever the live table holds."""
    from sdg.accelerator import loading

    held = loading.built(SOURCE)
    store = loading._store()
    live = held.tables[0]["name"]
    absent = "sdmt_no_such_table"

    def failing(check: str, table: str):
        return lambda held, at=None: [{"check": check, "table": table, "passed": False,
                                       "said": "No rows were loaded."}]

    assert _scalar(STORE, f"SELECT COUNT(*) FROM sdm.[{live}]") > 0
    monkeypatch.setattr(loading, "checks", failing("rows", live))
    assert loading.blocking(held, store, {}) == [f"{live}: No rows were loaded."]
    monkeypatch.setattr(loading, "checks", failing("rows", absent))
    assert loading.blocking(held, store, {}) == []
    monkeypatch.setattr(loading, "checks", failing("key", absent))
    assert loading.blocking(held, store, {}) != []
    # A live table with no rows has none to lose.
    empty = "sdmt_empty_probe"
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE sdm.[{empty}] (ID int)")
    try:
        assert loading._live_rows(store, empty) is False
        assert loading._live_rows(store, live) is True
        assert loading._live_rows(store, absent) is False
    finally:
        with _connect(STORE, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS sdm.[{empty}]")


def test_a_reader_that_lets_go_during_the_retries_is_waited_out(built, monkeypatch):
    """The swap is tried again while a reader holds a table: once it lets go,
    the new rows go live, and the long load is not thrown away."""
    from sdg.accelerator import loading

    monkeypatch.setattr(loading, "SWAP_LOCK_TIMEOUT_MS", 1000)
    monkeypatch.setattr(loading, "SWAP_PAUSE_SECONDS", 1)
    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"DELETE TOP (4) FROM sdm.[{names[0]}]")
    before = _counts(built["plan"])
    reader = _connect(STORE)
    reader.execute(f"SELECT COUNT(*) FROM sdm.[{names[0]}] WITH (REPEATABLEREAD, TABLOCK)")
    paused: list = []

    def pause(seconds: float) -> None:
        # The first try has waited out its lock timeout: the reader lets go.
        paused.append(seconds)
        reader.rollback()

    monkeypatch.setattr(loading, "time", SimpleNamespace(sleep=pause))
    try:
        done = _load()
    finally:
        reader.rollback()
        reader.close()
    assert paused == [1], "the swap was tried again after a wait"
    assert done["staged"] is True and done["failed"] == 0, done["notes"]
    assert not any("a reader held" in n for n in done["notes"])
    after = _counts(built["plan"])
    assert after[names[0]] == before[names[0]] + 4 and _leftovers() == []


def test_a_stop_while_a_reader_holds_a_table_gives_up_the_swap_at_once(built, monkeypatch):
    """Stop is asked between tries at the swap: the copies are given up, every
    table keeps its rows, and the load says it was stopped."""
    from sdg.accelerator import loading

    monkeypatch.setattr(loading, "SWAP_LOCK_TIMEOUT_MS", 1000)
    paused: list = []
    monkeypatch.setattr(loading, "time", SimpleNamespace(sleep=paused.append))
    names = [t["name"] for t in loading.built(SOURCE).tables]
    with _connect(STORE, autocommit=True) as conn:
        conn.execute(f"DELETE TOP (4) FROM sdm.[{names[0]}]")
    before = _counts(built["plan"])
    finished: list = []
    swap = loading._Stage._finish

    def watched(self, write, result):
        finished.append(1)  # every table has loaded: Stop is pressed from here on
        return swap(self, write, result)

    monkeypatch.setattr(loading._Stage, "_finish", watched)
    reader = _connect(STORE)
    try:
        reader.execute(f"SELECT COUNT(*) FROM sdm.[{names[0]}] WITH (REPEATABLEREAD, TABLOCK)")
        done = loading.load(SOURCE, _quiet, lambda: bool(finished), allow_write_login=True)
    finally:
        reader.rollback()
        reader.close()
    assert paused == [], "no second try once stopped"
    assert done["stopped"] is True
    assert any("stopped while a reader held" in n for n in done["notes"]), done["notes"]
    assert all(t["error"] == loading.NOT_PLACED for t in done["load"]["tables"])
    assert _counts(built["plan"]) == before and _leftovers() == []
    assert _load()["failed"] == 0

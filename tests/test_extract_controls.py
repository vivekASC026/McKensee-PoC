"""What a caller controls in an extract: the pack it reads (the source
accelerator loads a pack it has just built, before SDM is restarted on it), a
check every statement passes before anything opens, a stop between tables,
and the store lock that keeps two extracts from filling one store at once.

Fake connections: no database is read or written.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdg import packs
from sdg.extract import run


class Store:
    """A fake store session: answers the lock with `held`, records what is
    written and which statements ran."""

    def __init__(self, held: int = 0):
        self.held, self.ran, self.written, self.commits = held, [], [], 0

    def cursor(self):
        store = self

        class Cursor:
            fast_executemany = False

            def execute(self, sql, *params):
                store.ran.append(sql)
                return self

            def fetchone(self):
                return (store.held,)

            def executemany(self, sql, block):
                store.written.append((sql, list(block)))

        return Cursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class Source:
    """A fake source session: every SELECT answers with one row per column."""

    def __init__(self):
        self.read: list[str] = []

    def cursor(self):
        source = self

        class Cursor:
            description = [("ID",), ("NOTE",)]

            def __init__(self):
                self.rows = [[(1, "x")]]

            def execute(self, sql):
                if not sql.startswith("SET "):
                    source.read.append(sql)
                return self

            def fetchmany(self, size):
                return self.rows.pop(0) if self.rows else []

        return Cursor()


def _registry(store: Store, source: Source, opened: list[str] | None = None):
    import contextlib

    @contextlib.contextmanager
    def connect(name):
        if opened is not None:
            opened.append(name)
        yield store if name == "denorm" else source

    return SimpleNamespace(settings=SimpleNamespace(denorm_schema="sdm"), connect=connect)


def _pack(folder: Path, statements: dict[str, str], extract: dict | None = None):
    (folder / "extract").mkdir(parents=True, exist_ok=True)
    text = "\n".join(f"-- table: {t}\n{s}\n" for t, s in statements.items())
    (folder / "extract" / "SRC.sql").write_text(text, encoding="utf-8")
    return SimpleNamespace(extract_folder=folder / "extract", data={"extract": extract or {}})


@pytest.fixture
def no_log(monkeypatch):
    logged: list = []
    monkeypatch.setattr(run, "_log", lambda registry, schema, source, result:
                        logged.append(result))
    return logged


def test_a_pack_handed_in_is_read_instead_of_the_active_one(tmp_path, monkeypatch, no_log):
    monkeypatch.setattr(packs, "active", lambda: pytest.fail("read the active pack"))
    pack = _pack(tmp_path, {"one_denorm": "SELECT ID, NOTE FROM T"},
                 {"kept": {"SRC": ["ID"]}})
    store, source = Store(), Source()
    result = run.extract("SRC", registry=_registry(store, source), pack=pack)
    assert source.read == ["SELECT ID, NOTE FROM T"]
    assert [r for _, block in store.written for r in block] == [(1, None)]
    assert [t.table for t in result.tables] == ["one_denorm"]


def test_the_pack_handed_in_is_this_threads_alone(tmp_path, monkeypatch, no_log):
    """While a job extracts a built pack, the rest of the process still reads
    the active pack."""
    active = SimpleNamespace(data={"extract": {"surrogates": {"SRC": ["ACTIVE"]}}})
    monkeypatch.setattr(packs, "active", lambda: active)
    pack = _pack(tmp_path, {"one_denorm": "SELECT 1"}, {"surrogates": {"SRC": ["BUILT"]}})
    seen: dict[str, set] = {}
    inside, done = threading.Event(), threading.Event()

    class Waiting(Source):
        def cursor(self):
            seen["job"] = run.surrogate_columns("SRC")
            inside.set()
            done.wait(5)
            return super().cursor()

    monkeypatch.setenv(run.EXTRACT_KEY, "k" * 16)
    job = threading.Thread(target=run.extract, args=("SRC",),
                           kwargs={"registry": _registry(Store(), Waiting()), "pack": pack})
    job.start()
    assert inside.wait(5)
    seen["other"] = run.surrogate_columns("SRC")
    done.set()
    job.join(5)
    assert seen == {"job": {"BUILT"}, "other": {"ACTIVE"}}
    assert run.surrogate_columns("SRC") == {"ACTIVE"}


def test_a_statement_the_check_refuses_reads_nothing(tmp_path, no_log):
    pack = _pack(tmp_path, {"one_denorm": "SELECT 1", "two_denorm": "UPDATE T SET A = 1"})
    opened: list[str] = []

    def vet(select):
        if not select.startswith("SELECT"):
            raise ValueError("not a read")

    with pytest.raises(ValueError, match="not a read"):
        run.extract("SRC", registry=_registry(Store(), Source(), opened), pack=pack, vet=vet)
    assert opened == [] and no_log == []


def test_a_stop_leaves_the_tables_after_it_as_they_were(tmp_path, no_log):
    pack = _pack(tmp_path, {"one_denorm": "SELECT 1", "two_denorm": "SELECT 2",
                            "three_denorm": "SELECT 3"})
    store, source = Store(), Source()
    told: list = []
    result = run.extract("SRC", registry=_registry(store, source), pack=pack,
                         stop=lambda: len(source.read) >= 1,
                         progress=lambda table, rows, done, **kw: told.append((table, kw)))
    assert source.read == ["SELECT 1"]
    assert [(t.table, t.rows, t.error) for t in result.tables] == [
        ("one_denorm", 1, ""), ("two_denorm", 0, run.NOT_RUN), ("three_denorm", 0, run.NOT_RUN)]
    assert not any("TRUNCATE" in s and "two_denorm" in s for s in store.ran)
    assert ("two_denorm", {"error": run.NOT_RUN}) in told
    assert no_log[0].tables[1].error == run.NOT_RUN
    assert result.as_dict()["tables"][1]["error"] == run.NOT_RUN


def test_a_store_another_extract_holds_is_refused_before_any_read(tmp_path, no_log):
    pack = _pack(tmp_path, {"one_denorm": "SELECT 1"})
    store, source = Store(held=-1), Source()
    with pytest.raises(run.StoreBusy, match="Another extract into this store"):
        run.extract("SRC", registry=_registry(store, source), pack=pack)
    assert source.read == [] and store.written == []
    assert "sp_getapplock" in store.ran[0] and "'Session'" in store.ran[0]


def test_the_lock_is_taken_on_the_session_that_writes_before_the_source_opens(tmp_path,
                                                                              no_log):
    pack = _pack(tmp_path, {"one_denorm": "SELECT 1"})
    opened: list[str] = []
    store = Store()
    run.extract("SRC", registry=_registry(store, Source(), opened), pack=pack)
    assert opened == ["denorm", "source"]
    assert "sp_getapplock" in store.ran[0]
    assert any("TRUNCATE" in s for s in store.ran[1:])


def test_the_lock_is_given_back_when_the_extract_ends_or_fails(tmp_path, no_log, monkeypatch):
    """A pooled connection's session outlives close(), so the lock is released
    by name, on success and on failure alike."""
    pack = _pack(tmp_path, {"one_denorm": "SELECT 1"})
    store = Store()
    run.extract("SRC", registry=_registry(store, Source()), pack=pack)
    assert "sp_releaseapplock" in store.ran[-1]

    def broken(*args, **kwargs):
        raise RuntimeError("the table failed")

    monkeypatch.setattr(run, "_one_table", broken)
    store = Store()
    with pytest.raises(RuntimeError, match="the table failed"):
        run.extract("SRC", registry=_registry(store, Source()), pack=pack)
    assert "sp_releaseapplock" in store.ran[-1]


def test_a_surrogate_collision_keeps_its_words_and_a_driver_error_is_said_by_codes(
        tmp_path, no_log, monkeypatch):
    """A generated pack's table errors land in the log by kind and codes
    only; the collision's own words name the column and table alone, so the
    person is told what to do."""
    pack = _pack(tmp_path, {"one_denorm": "SELECT ID, NOTE FROM T"},
                 {"kept": {"SRC": ["NOTE"]}, "surrogates": {"SRC": ["ID"]}})
    monkeypatch.setenv(run.EXTRACT_KEY, "k" * 16)
    monkeypatch.setattr(run, "surrogate", lambda value, key, chars=9, **_: "AAAAAAAAA")

    class TwoKeys(Source):
        def cursor(self):
            made = super().cursor()
            made.rows = [[(1, "x"), (2, "y")]]
            return made

    result = run.extract("SRC", registry=_registry(Store(), TwoKeys()), pack=pack,
                         stop_on_error=False)
    assert result.tables[0].error.startswith("Two different values of ID share one surrogate")

    class Broken(Source):
        def cursor(self):
            made = super().cursor()

            def fetchmany(size):
                raise RuntimeError("Conversion failed for the value 'Pat Q Example'")
            made.fetchmany = fetchmany
            return made

    result = run.extract("SRC", registry=_registry(Store(), Broken()), pack=pack,
                         stop_on_error=False)
    assert result.tables[0].error == "The load of one_denorm failed (RuntimeError)."

    class Unreadable(Source):
        def cursor(self):
            made = super().cursor()

            def execute(sql):
                if sql.startswith("SET "):
                    return made
                raise RuntimeError("Conversion failed for the value 'Pat Q Example'")
            made.execute = execute
            return made

    # Stopping at the first failure (the command line's default): the raised
    # error is said by kind and codes too.
    with pytest.raises(run.SourceQueryError) as raised:
        run.extract("SRC", registry=_registry(Store(), Unreadable()), pack=pack)
    assert "Pat Q Example" not in str(raised.value) and "RuntimeError" in str(raised.value)


def test_names_from_a_definition_are_quoted_never_read_as_sql(tmp_path, no_log):
    pack = _pack(tmp_path, {"x]; DROP TABLE sdm.other; --": "SELECT 1"})
    store = Store()
    run.extract("SRC", registry=_registry(store, Source()), pack=pack)
    truncate = next(s for s in store.ran if s.startswith("TRUNCATE"))
    assert truncate == "TRUNCATE TABLE [sdm].[x]]; DROP TABLE sdm.other; --]"
    insert = store.written[0][0]
    assert insert.startswith("INSERT INTO [sdm].[x]]; DROP TABLE sdm.other; --] ([ID], [NOTE])")


def test_definitions_handed_in_are_run_instead_of_the_file(tmp_path, no_log):
    pack = _pack(tmp_path, {"one_denorm": "SELECT 'from the file'"})
    source = Source()
    run.extract("SRC", registry=_registry(Store(), source), pack=pack,
                definitions={"one_denorm": "SELECT 'handed in'"})
    assert source.read == ["SELECT 'handed in'"]


def test_past_the_tracked_count_a_surrogate_column_is_not_held_in_memory(monkeypatch):
    monkeypatch.setattr(run, "COLLISION_TRACKED", 3)
    seen: dict = {}
    for n in range(10):
        run._one_value_per_surrogate(seen, run.surrogate(f"V{n}", b"k" * 16, 16), f"V{n}",
                                     "ID", "t")
    assert len([k for k in seen if k >= 0]) == 3
    with pytest.raises(run.SurrogateCollision):
        first = next(k for k in seen if k >= 0)
        made = run.surrogate("V0", b"k" * 16, 16)
        assert int(made, 36) == first
        run._one_value_per_surrogate(seen, made, "ANOTHER", "ID", "t")


def test_a_stop_part_way_through_a_table_rolls_it_back_and_stops_the_read(tmp_path, no_log):
    """Stop is asked between blocks of rows too: the table being read is rolled
    back (it keeps its rows), the source read is cancelled, and the tables
    after it are not run."""
    pack = _pack(tmp_path, {"one_denorm": "SELECT 1", "two_denorm": "SELECT 2"})

    class Counted(Store):
        rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    cancelled: list = []

    class ThreeBlocks(Source):
        def cursor(self):
            made = super().cursor()
            made.rows = [[(1, "a")], [(2, "b")], [(3, "c")]]
            made.cancel = lambda: cancelled.append(1)
            return made

    store, source = Counted(), ThreeBlocks()
    blocks_read = lambda: sum(1 for sql, _ in store.written)  # noqa: E731
    told: list = []
    result = run.extract("SRC", registry=_registry(store, source), pack=pack,
                         stop=lambda: blocks_read() >= 1,
                         progress=lambda table, rows, done, **kw: told.append((table, kw)))
    assert [(t.table, t.rows, t.error) for t in result.tables] == [
        ("one_denorm", 0, run.STOPPED_PART_WAY), ("two_denorm", 0, run.NOT_RUN)]
    # One block was written, then rolled back: nothing of it was committed.
    assert len(store.written) == 1 and store.rollbacks >= 1 and cancelled == [1]
    assert source.read == ["SELECT 1"]
    assert ("one_denorm", {"error": run.STOPPED_PART_WAY}) in told

"""A privacy class is enforced on every row the extract writes, whatever its
SELECT says (pack.json extract.withheld and extract.kept): a withheld column
loads NULL, and where a pack names its kept columns, so does every column it
does not name, and a source it does not name is refused. A pack that names no
kept columns (health, banking) loads as before. And `sdg extract --configured`
reads the source .env names, else the pack's default source.

Fake read and write cursors: no database is read or written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from sdg import packs
from sdg.extract import run

KEY = b"k" * 16


def _load(columns: list[str], rows: list[tuple], source: str = "SRC") -> list[tuple]:
    """What the extract writes for these source rows."""

    class Read:
        description = [(c,) for c in columns]

        def __init__(self):
            self.blocks = [list(rows)]

        def execute(self, sql):
            return self

        def fetchmany(self, size):
            return self.blocks.pop(0) if self.blocks else []

    written: list = []

    class Writer:
        fast_executemany = False

        def execute(self, sql):
            pass

        def executemany(self, sql, block):
            written.extend(block)

    read = SimpleNamespace(cursor=lambda: Read())
    write = SimpleNamespace(cursor=lambda: Writer(), commit=lambda: None, rollback=lambda: None)
    run._one_table(read, write, source, "sdm", "record_denorm", "SELECT", {}, key=KEY)
    return written


@pytest.fixture
def declared(monkeypatch):
    """The active pack declares this extract section, and nothing else."""
    def declare(extract: dict) -> None:
        monkeypatch.setattr(packs, "active", lambda: SimpleNamespace(data={"extract": extract}))
    return declare


# --- withheld and kept --------------------------------------------------------

def test_withheld_and_undeclared_columns_load_null_and_a_surrogate_is_applied(declared):
    declared({"kept": {"SRC": ["record_id", "BRANCH", "full_name"]},
              "withheld": {"SRC": ["FULL_NAME"]},
              "surrogates": {"SRC": ["HOUSEHOLD"]}})
    written = _load(["RECORD_ID", "Full_Name", "HOUSEHOLD", "BRANCH", "ADDED_BY_HAND"],
                    [(1, "Ann Lee", "H001", "B7", "secret"),
                     (2, None, None, "B8", "other")])
    # A pack that names its kept columns is a generated one: 16-character surrogates.
    long = run.surrogate("H001", KEY, run.GENERATED_SURROGATE_CHARS)
    assert len(long) == 16 and long.startswith(run.surrogate("H001", KEY))
    assert written == [(1, None, long, "B7", None),
                       (2, None, None, "B8", None)]


def test_a_withheld_column_loads_null_even_when_it_is_a_surrogate(declared):
    declared({"withheld": {"SRC": ["HOUSEHOLD"]}, "surrogates": {"SRC": ["HOUSEHOLD"]}})
    assert _load(["ID", "HOUSEHOLD"], [(1, "H001")]) == [(1, None)]


def test_every_row_of_every_block_is_nulled(declared, monkeypatch):
    declared({"kept": {"SRC": ["ID"]}})
    monkeypatch.setattr(run, "BLOCK", 2)
    rows = [(n, f"v{n}") for n in range(5)]

    class Read:
        description = [("ID",), ("NOTE",)]

        def execute(self, sql):
            return self

        def fetchmany(self, size):
            taken, rows[:] = rows[:size], rows[size:]
            return taken

    written: list = []
    writer = SimpleNamespace(fast_executemany=False, execute=lambda sql: None,
                             executemany=lambda sql, block: written.extend(block))
    run._one_table(SimpleNamespace(cursor=lambda: Read()),
                   SimpleNamespace(cursor=lambda: writer, commit=lambda: None,
                                   rollback=lambda: None),
                   "SRC", "sdm", "record_denorm", "SELECT", {}, key=KEY)
    assert written == [(n, None) for n in range(5)]


def test_with_no_kept_columns_rows_pass_as_before(declared):
    declared({"withheld": {"SRC": ["FULL_NAME"]}})
    assert run.kept_columns("SRC") is None
    written = _load(["ID", "FULL_NAME", "BRANCH"], [(1, "Ann Lee", "B7")])
    assert written == [(1, None, "B7")]

    declared({})
    assert run.withheld_columns("SRC") == set() and run.kept_columns("SRC") is None
    rows = [(1, "Ann Lee", "B7"), (2, None, "B8")]
    assert _load(["ID", "FULL_NAME", "BRANCH"], rows) == rows


def test_each_source_has_its_own_classes(declared):
    declared({"kept": {"SRC": ["ID"], "OTHER": ["ID", "NOTE"]}, "withheld": {"SRC": ["NOTE"]}})
    assert run.kept_columns("OTHER") == {"ID", "NOTE"} and run.withheld_columns("OTHER") == set()
    assert _load(["ID", "NOTE"], [(1, "x")], source="OTHER") == [(1, "x")]
    assert run.kept_columns("SRC") == {"ID"} and run.withheld_columns("SRC") == {"NOTE"}


def test_a_source_the_kept_columns_do_not_name_is_refused_before_reading(declared,
                                                                         monkeypatch):
    declared({"kept": {"SRC": ["ID"]}})
    with pytest.raises(ValueError, match="not for Src.*Nothing was extracted"):
        run.kept_columns("Src")
    monkeypatch.setattr(run, "_statements", lambda source: {"record_denorm": "SELECT"})
    with pytest.raises(ValueError, match="not for OTHER"):
        run.extract("OTHER", registry=SimpleNamespace(
            settings=SimpleNamespace(denorm_schema="sdm"),
            connect=lambda name: pytest.fail("connected before the kept columns were checked")))


def test_an_empty_kept_list_nulls_every_column_but_the_surrogates(declared):
    declared({"kept": {"SRC": []}, "surrogates": {"SRC": ["HOUSEHOLD"]}})
    assert _load(["ID", "HOUSEHOLD"], [(1, "H001")]) == [
        (None, run.surrogate("H001", KEY, run.GENERATED_SURROGATE_CHARS))]


# --- the built-in packs load as before ----------------------------------------

def test_the_health_extract_passes_every_column_it_does_not_withhold(monkeypatch):
    monkeypatch.setenv("SDG_PACK", "health")
    packs.load.cache_clear()
    try:
        assert run.kept_columns("FACETS") is None
        assert run.withheld_columns("FACETS") == set()
        assert run.surrogate_columns("FACETS") == {"SBSB_ID"}
        columns = ["MEME_CK", "SBSB_ID", "MEME_BIRTH_DT", "MEME_FIRST_NAME", "ANY_NEW_COLUMN"]
        rows = [(1, "SB0001", "1980-01-02", None, "kept as read"),
                (2, "SB0001", None, "Ann", 7)]
        written = _load(columns, rows, source="FACETS")
        sbsb = run.surrogate("SB0001", KEY)
        assert written == [(1, sbsb, "1980-01-02", None, "kept as read"),
                           (2, sbsb, None, "Ann", 7)]
    finally:
        packs.load.cache_clear()


@pytest.mark.parametrize("pack_id", ["health", "banking"])
def test_the_built_in_packs_name_no_kept_or_withheld_columns(pack_id):
    extract = packs.load(pack_id).data.get("extract") or {}
    assert "kept" not in extract and "withheld" not in extract


def test_the_pack_schema_takes_kept_and_withheld_by_source():
    data = json.loads((packs.BUILT_IN / "banking" / "pack.json").read_text(encoding="utf-8"))
    data["extract"] = {"kept": {"BANK": ["CUSTOMER_ID"]}, "withheld": {"BANK": ["SSN"]},
                       "default_source": "BANK"}
    packs.check(data, "a test pack")
    data["extract"] = {"withheld": {"BANK": "SSN"}}
    with pytest.raises(packs.PackError, match="extract.withheld.BANK"):
        packs.check(data, "a test pack")


# --- sdg extract --configured ---------------------------------------------------

@pytest.fixture
def extract_cli(monkeypatch):
    """The extract command with its source reads and the store stubbed out:
    returns the sources it was asked to read."""
    import sdg.api.service
    import sdg.extract

    asked: list[str] = []

    def fake_extract(source, **kw):
        asked.append(source)
        return SimpleNamespace(tables=[], rows=0, seconds=0.0)

    monkeypatch.setattr(sdg.extract, "sources", lambda: ["BANK", "FACETS", "FACETS_QA"])
    monkeypatch.setattr(sdg.extract, "extract", fake_extract)
    monkeypatch.setattr(sdg.api.service, "Service", lambda: SimpleNamespace(manifest=None))
    return asked


def _cli(*args: str):
    from sdg.cli.main import app

    return CliRunner().invoke(app, ["extract", *args])


def test_configured_reads_the_health_source_when_env_names_none(extract_cli, monkeypatch):
    monkeypatch.delenv("SDG_EXTRACT_SOURCE", raising=False)
    monkeypatch.setenv("SDG_PACK", "health")
    packs.load.cache_clear()
    try:
        assert packs.active().data["extract"]["default_source"] == "FACETS"
        done = _cli("--configured", "--keep-going")
    finally:
        packs.load.cache_clear()
    assert done.exit_code == 0, done.output
    assert extract_cli == ["FACETS"]
    assert "reading FACETS into the analysis store" in done.output


def test_configured_refuses_when_neither_env_nor_pack_names_a_source(extract_cli, monkeypatch):
    monkeypatch.delenv("SDG_EXTRACT_SOURCE", raising=False)
    monkeypatch.setenv("SDG_PACK", "banking")
    packs.load.cache_clear()
    try:
        assert "default_source" not in (packs.active().data.get("extract") or {})
        done = _cli("--configured")
    finally:
        packs.load.cache_clear()
    assert done.exit_code == 1
    assert "No source to read" in done.output and "SDG_EXTRACT_SOURCE" in done.output
    assert extract_cli == []


def test_configured_reads_the_source_env_names(extract_cli, monkeypatch):
    monkeypatch.setenv("SDG_EXTRACT_SOURCE", " BANK ")
    done = _cli("--configured")
    assert done.exit_code == 0, done.output
    assert extract_cli == ["BANK"]


def test_configured_refuses_a_named_source_too(extract_cli, monkeypatch):
    monkeypatch.setenv("SDG_EXTRACT_SOURCE", "BANK")
    done = _cli("FACETS", "--configured")
    assert done.exit_code == 1
    assert "not both" in done.output and extract_cli == []


def test_configured_refuses_a_source_with_no_definition(extract_cli, monkeypatch):
    monkeypatch.setenv("SDG_EXTRACT_SOURCE", "NOPE")
    done = _cli("--configured")
    assert done.exit_code == 1
    assert "No extract definition for NOPE" in done.output and extract_cli == []


def test_a_named_source_is_read_as_before(extract_cli, monkeypatch):
    monkeypatch.setenv("SDG_EXTRACT_SOURCE", "BANK")
    done = _cli("FACETS_QA")
    assert done.exit_code == 0, done.output
    assert extract_cli == ["FACETS_QA"]


# --- a lock held too long (generated packs only) ------------------------------------------

def _sent(source: str = "SRC") -> list[str]:
    """Every statement the extract sends the source for one table."""
    sent: list[str] = []

    class Read:
        description = [("ID",)]

        def __init__(self):
            self.blocks = [[(1,)]]

        def execute(self, sql):
            sent.append(sql)
            return self

        def fetchmany(self, size):
            return self.blocks.pop(0) if self.blocks else []

    writer = SimpleNamespace(fast_executemany=False, execute=lambda sql: None,
                             executemany=lambda sql, block: None)
    run._one_table(SimpleNamespace(cursor=lambda: Read()),
                   SimpleNamespace(cursor=lambda: writer, commit=lambda: None,
                                   rollback=lambda: None),
                   source, "sdm", "record_denorm", "SELECT 1", {}, key=KEY)
    return sent


def test_a_generated_packs_extract_gives_up_on_a_lock_held_too_long(declared):
    declared({"kept": {"SRC": ["ID"]}})
    # And it gives way to the source's own work in a deadlock.
    assert _sent() == [f"SET LOCK_TIMEOUT {run.GENERATED_LOCK_TIMEOUT_MS}",
                       "SET DEADLOCK_PRIORITY LOW", "SELECT 1"]
    assert run.GENERATED_LOCK_TIMEOUT_MS == 60_000


@pytest.mark.parametrize("pack_id, source", [("health", "FACETS"), ("banking", "BANK")])
def test_a_built_in_packs_extract_sends_its_select_alone(monkeypatch, pack_id, source):
    held = packs.load(pack_id)
    monkeypatch.setattr(packs, "active", lambda: held)
    assert _sent(source) == ["SELECT 1"]


def test_a_surrogate_needs_the_key_and_is_no_encoding_of_the_value():
    """Another key, another surrogate; the same key, the same one; and none is
    the value written another way."""
    value = "ACCT-000123"
    made = run.surrogate(value, KEY, run.GENERATED_SURROGATE_CHARS)
    assert made == run.surrogate(value, KEY, run.GENERATED_SURROGATE_CHARS)
    assert made != run.surrogate(value, b"another key, 16+", run.GENERATED_SURROGATE_CHARS)
    import base64

    plain = {value, value.upper(), value.encode().hex().upper()[:16],
             base64.b32encode(value.encode()).decode()[:16]}
    assert made not in plain and len(made) == 16


# --- one value per surrogate (generated packs only) ----------------------------------------

def _surrogates_collide(monkeypatch) -> None:
    monkeypatch.setattr(run, "surrogate",
                        lambda value, key, chars=9, **_: None if value is None else "SAME00000")


def test_two_values_sharing_a_surrogate_are_refused_and_nothing_is_written(declared,
                                                                          monkeypatch):
    declared({"kept": {"SRC": ["ID"]}, "surrogates": {"SRC": ["HOUSEHOLD"]}})
    _surrogates_collide(monkeypatch)
    with pytest.raises(ValueError, match="share one surrogate in record_denorm") as caught:
        _load(["ID", "HOUSEHOLD"], [(1, "H001"), (2, "H002")])
    assert "H00" not in str(caught.value)  # the values are never said
    # One value on many rows is one record's key: no refusal.
    assert _load(["ID", "HOUSEHOLD"], [(1, "H001"), (2, " h001 ")]) == [
        (1, "SAME00000"), (2, "SAME00000")]


def test_a_pack_naming_no_kept_columns_is_not_checked_for_shared_surrogates(declared,
                                                                           monkeypatch):
    declared({"surrogates": {"SRC": ["HOUSEHOLD"]}})
    _surrogates_collide(monkeypatch)
    assert _load(["ID", "HOUSEHOLD"], [(1, "H001"), (2, "H002")]) == [
        (1, "SAME00000"), (2, "SAME00000")]


def test_keys_differing_only_in_case_stay_two_records_where_the_source_tells_case_apart(
        declared):
    """A build names the surrogate columns whose source collation tells case
    apart (extract.case_sensitive): their surrogates keep each value's case,
    so two such keys are two records and no collision. Every other surrogate
    folds case, as a case-insensitive source compares keys."""
    chars = run.GENERATED_SURROGATE_CHARS
    declared({"kept": {"SRC": ["ID"]}, "surrogates": {"SRC": ["HOUSEHOLD"]},
              "case_sensitive": {"SRC": ["household"]}})
    written = _load(["ID", "HOUSEHOLD"], [(1, "H001"), (2, "h001"), (3, " H001 ")])
    upper, lower = run.surrogate("H001", KEY, chars, fold=False),         run.surrogate("h001", KEY, chars, fold=False)
    assert upper != lower
    # Spaces at either end are still dropped: the source compares without them.
    assert written == [(1, upper), (2, lower), (3, upper)]
    assert run.surrogate("h001", KEY, chars) == run.surrogate("H001", KEY, chars)
    declared({"kept": {"SRC": ["ID"]}, "surrogates": {"SRC": ["HOUSEHOLD"]}})
    folded = run.surrogate("H001", KEY, chars)
    assert _load(["ID", "HOUSEHOLD"], [(1, "H001"), (2, "h001")]) == [(1, folded), (2, folded)]


def test_a_collision_is_told_by_the_value_as_the_source_compares_it():
    seen: dict = {}
    made = run.surrogate("K1", KEY, 16, fold=False)
    run._one_value_per_surrogate(seen, made, "K1", "ID", "t", fold=False)
    # Another value written to the same surrogate: refused, case kept.
    with pytest.raises(run.SurrogateCollision):
        run._one_value_per_surrogate(seen, made, "k1", "ID", "t", fold=False)
    folded: dict = {}
    run._one_value_per_surrogate(folded, made, "K1", "ID", "t")
    run._one_value_per_surrogate(folded, made, " k1", "ID", "t")  # one key, two spellings

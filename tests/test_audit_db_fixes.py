"""The standards audit's Medium findings on database reads and writes, the
analyzer's SQL services, the layout service and the customer SQL scripts.

Pure Python: fake connections and cursors stand in for SQL Server, and the
SQL scripts are read as text. Nothing here connects to a database.
"""

from __future__ import annotations

import contextlib
import csv
import io
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyodbc
import pytest

ROOT = Path(__file__).resolve().parent.parent


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeCursor:
    """Records every statement; raises pyodbc.Error on any whose text holds
    one of `fail_on`."""

    def __init__(self, fail_on=(), rows=()):
        self.fail_on = tuple(fail_on)
        self.executed: list[str] = []
        self.rows = list(rows)
        self.fast_executemany = False

    def execute(self, sql, *params):
        self.executed.append(sql)
        for text in self.fail_on:
            if text in sql:
                raise pyodbc.ProgrammingError("42000", f"failed: {text}")
        return self

    def executemany(self, sql, batch):
        self.execute(sql)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConn:
    def __init__(self, cursor=None, fail_commit=False):
        self._cursor = cursor or FakeCursor()
        self.commits = self.rollbacks = 0
        self.closed = False
        self.fail_commit = fail_commit

    def cursor(self):
        return self._cursor

    def commit(self):
        if self.fail_commit:
            raise pyodbc.OperationalError("08S01", "link failure")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


# ── Batch 4 #2: the writer always puts triggers and IDENTITY_INSERT back ─────

def _plan_table():
    fields = [SimpleNamespace(name="ID", generator="sequence"),
              SimpleNamespace(name="NAME", generator="faker")]
    return SimpleNamespace(name="T", schema_name="dbo", table_name="T", fields=fields)


def _write(monkeypatch, cursor, *, identity=True, triggers=True, conn=None):
    from sdg.writers import sqlserver

    monkeypatch.setattr(sqlserver, "needs_identity_insert", lambda *_: identity)
    model = SimpleNamespace(table=lambda name: None)
    conn = conn or FakeConn(cursor)
    return conn, sqlserver._write_table(
        conn, _plan_table(), [{"ID": 1, "NAME": "x"}], model,
        disable_triggers=triggers, batch_size=10)


def test_a_failed_identity_insert_still_reenables_the_triggers(monkeypatch, caplog):
    cursor = FakeCursor(fail_on=["IDENTITY_INSERT [dbo].[T] ON"])
    with pytest.raises(pyodbc.ProgrammingError, match="IDENTITY_INSERT"):
        _write(monkeypatch, cursor)
    assert any("ENABLE TRIGGER ALL" in s for s in cursor.executed), cursor.executed
    assert not any(s.startswith("INSERT INTO") for s in cursor.executed)


def test_a_cleanup_error_after_a_failed_write_keeps_the_original_error(monkeypatch, caplog):
    # The insert fails, and so does re-enabling the triggers: the caller sees
    # the insert's error, and the cleanup's is logged.
    cursor = FakeCursor(fail_on=["INSERT INTO", "ENABLE TRIGGER"])
    with caplog.at_level(logging.ERROR, logger="sdg.writers.sqlserver"):
        with pytest.raises(pyodbc.ProgrammingError, match="INSERT INTO"):
            conn, _ = _write(monkeypatch, cursor)
    assert any("re-enable the triggers" in r.getMessage() for r in caplog.records)
    # Every cleanup step is tried, whichever of them fails.
    assert any("IDENTITY_INSERT [dbo].[T] OFF" in s for s in cursor.executed)


def test_a_cleanup_error_after_a_good_write_is_raised(monkeypatch):
    from sdg.writers import sqlserver

    cursor = FakeCursor(fail_on=["ENABLE TRIGGER"])
    # Raised with the committed table's record, so the load can report it.
    with pytest.raises(sqlserver._CommittedThenFailed, match="ENABLE TRIGGER") as caught:
        _write(monkeypatch, cursor)
    assert isinstance(caught.value.error, pyodbc.ProgrammingError)
    assert caught.value.record.rows_written == 1
    # Both steps were tried even though one failed.
    assert any("IDENTITY_INSERT [dbo].[T] OFF" in s for s in cursor.executed)


def test_a_good_write_switches_back_and_commits(monkeypatch):
    cursor = FakeCursor()
    conn, record = _write(monkeypatch, cursor)
    assert record.rows_written == 1
    assert cursor.executed[0].endswith("DISABLE TRIGGER ALL")
    assert cursor.executed[-1].endswith("ENABLE TRIGGER ALL"), cursor.executed
    assert conn.commits == 2 and conn.rollbacks == 0


# ── Batch 4 #1: a failed extract log entry is said ───────────────────────────

def test_a_failed_extract_log_write_is_logged(caplog):
    from sdg.extract import run

    class Registry:
        @contextlib.contextmanager
        def connect(self, name):
            raise pyodbc.OperationalError("42000", "CREATE TABLE permission denied")
            yield  # pragma: no cover

    result = run.ExtractResult(source="FACETS",
                               tables=[run.TableResult(table="member_denorm", rows=3, seconds=0.1)])
    with caplog.at_level(logging.WARNING, logger="sdg.extract.run"):
        run._log(Registry(), "sdm", "FACETS", result)  # does not raise
    messages = [r.getMessage() for r in caplog.records]
    assert any("log entry could not be written" in m and "[sdm].[extract_log]" in m
               for m in messages), messages


# ── Batch 5 #3: claims_available tells "no table" from "cannot read" ─────────

def _claims_setup(monkeypatch, error):
    from sdg import packs
    from sdg.analyzer import claims_data_loader as cdl

    conn = FakeConn()
    monkeypatch.setattr(packs, "active", lambda: SimpleNamespace(child="claims"))
    monkeypatch.setattr(cdl.entity_loader, "table", lambda entity: "sdm.claims_denorm")
    monkeypatch.setattr(cdl, "_conn", lambda: conn)

    def read_sql(sql, c):
        try:
            raise error
        except pyodbc.Error as exc:
            # pandas wraps the driver's error, as it does for a pyodbc read.
            raise pd.errors.DatabaseError(f"Execution failed on sql: {exc}") from exc

    monkeypatch.setattr(cdl, "read_sql", read_sql)
    return cdl, conn


def test_a_missing_claims_table_is_no_claims(monkeypatch):
    cdl, conn = _claims_setup(
        monkeypatch, pyodbc.ProgrammingError("42S02", "Invalid object name"))
    assert cdl.claims_available() is False
    assert conn.closed


def test_a_claims_outage_is_not_reported_as_no_claims(monkeypatch):
    cdl, conn = _claims_setup(
        monkeypatch, pyodbc.OperationalError("08S01", "Communication link failure"))
    with pytest.raises(pd.errors.DatabaseError):
        cdl.claims_available()
    assert conn.closed, "the connection leaked when the query raised"


def test_a_pack_without_child_records_has_no_claims(monkeypatch):
    from sdg import packs
    from sdg.analyzer import claims_data_loader as cdl

    monkeypatch.setattr(packs, "active", lambda: SimpleNamespace(child=None))
    assert cdl.claims_available() is False


# ── Batch 5 #1, #2: discovery counts empties and says what failed ────────────

def _discovery(monkeypatch):
    # Imported before the pack is replaced: the modules read it on import.
    from sdg import packs
    from sdg.analyzer.services import claims_service  # noqa: F401
    from sdg.analyzer.services import discovery_service as ds

    monkeypatch.setattr(packs, "active", lambda: SimpleNamespace(primary="members"))
    return ds


def test_an_empty_combination_is_empty_not_failed(monkeypatch):
    from sdg.analyzer import entity_loader

    ds = _discovery(monkeypatch)

    def load(*args, **kwargs):
        raise entity_loader.no_rows("No members match.")

    monkeypatch.setattr(entity_loader, "load", load)
    got = ds._analyze_combo({"grgr_ck": [1]})
    assert got["empty"] is True and got["failed"] is False


def test_a_refused_combination_is_failed_and_logged(monkeypatch, caplog):
    from sdg.analyzer import entity_loader

    ds = _discovery(monkeypatch)

    def load(*args, **kwargs):
        raise ValueError("grgr_ck takes numbers, not 'abc'.")

    monkeypatch.setattr(entity_loader, "load", load)
    with caplog.at_level(logging.WARNING, logger=ds.__name__):
        got = ds._analyze_combo({"grgr_ck": ["abc"]})
    assert got["failed"] is True and got["empty"] is False
    assert any("takes numbers" in r.getMessage() for r in caplog.records)


def test_no_rows_is_still_a_value_error():
    from sdg.analyzer import entity_loader

    error = entity_loader.no_rows("No members match.")
    assert type(error) is ValueError and entity_loader.is_no_rows(error)
    assert not entity_loader.is_no_rows(ValueError("grgr_ck takes numbers."))


def test_a_claims_combination_with_no_rows_is_empty(monkeypatch):
    from sdg.analyzer import entity_loader
    from sdg.analyzer.services import claims_service as cs

    ds = _discovery(monkeypatch)

    def load(combo):
        raise entity_loader.no_rows("No claims match.")

    monkeypatch.setattr(cs, "_load", load)
    got = ds._analyze_claims_combo({"grgr_ck": [1]})
    assert got["failed"] is False and not got["result"].get("patterns")


def test_a_claims_combination_that_breaks_is_logged(monkeypatch, caplog):
    from sdg.analyzer.services import claims_service as cs

    ds = _discovery(monkeypatch)

    def load(combo):
        raise RuntimeError("clustering bug")

    monkeypatch.setattr(cs, "_load", load)
    with caplog.at_level(logging.WARNING, logger=ds.__name__):
        got = ds._analyze_claims_combo({"grgr_ck": [1]})
    assert got["failed"] is True
    assert any(r.exc_info and "clustering bug" in str(r.exc_info[1]) for r in caplog.records)


# ── Batch 5 #4: rollback and close failures on the walk connection are said ──

def test_a_failed_rollback_is_logged_and_the_walk_error_kept(monkeypatch, caplog):
    from sdg.analyzer import db
    from sdg.analyzer.services import discovery_service as ds

    class Conn(FakeConn):
        def rollback(self):
            raise pyodbc.OperationalError("08S01", "link failure")

        def close(self):
            raise pyodbc.OperationalError("08S01", "link failure")

    monkeypatch.setattr(db, "get_raw_connection", lambda: Conn())
    with caplog.at_level(logging.ERROR, logger=ds.__name__):
        with pytest.raises(RuntimeError, match="walk failed"):
            with ds._persist_connection("test"):
                raise RuntimeError("walk failed")
    messages = [r.getMessage() for r in caplog.records]
    assert any("could not roll back" in m for m in messages), messages
    assert any("could not close" in m for m in messages), messages


# ── Batch 6: layout rendering ────────────────────────────────────────────────

def test_eight_digit_dates_are_dates():
    from sdg.analyzer.services import layout_service as ls

    assert ls._infer_type(["20240131", "19991231"]) == "date"
    assert ls._detect_date_format(["20240131"]) == "YYYYMMDD"
    # Eight-digit ids, or digits that are no calendar date, stay numbers.
    assert ls._infer_type(["12345678", "20240131"]) == "int"
    assert ls._infer_type(["20241301"]) == "int"
    assert ls._infer_type(["42", "7"]) == "int"


def test_csv_values_holding_the_delimiter_are_quoted(monkeypatch):
    from sdg.analyzer.services import layout_service as ls

    monkeypatch.setattr(ls, "_rules_by_id", lambda: {})
    df = pd.DataFrame({"STREET": ["12 Main St, Apt 4", "9 Oak Ave"],
                       "NOTE": ['say "hi"', "plain"], "CITY": ["Columbus", "Dayton"]})
    layout = {"kind": "csv", "delimiter": ",", "fields": [
        {"name": n, "type": "text", "rule_id": None, "is_default": False}
        for n in ("STREET", "NOTE", "CITY")]}
    out = ls.render_through_layout(df, layout)
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0] == ["STREET", "NOTE", "CITY"]
    assert rows[1] == ["12 Main St, Apt 4", 'say "hi"', "Columbus"]
    # A value with nothing to quote is written exactly as before.
    assert out.split("\n")[2] == "9 Oak Ave,plain,Dayton"


def test_a_fixed_width_value_too_wide_is_refused_without_naming_it():
    from sdg.analyzer.services import layout_service as ls

    field = {"name": "LAST_NAME", "type": "text", "length": 5}
    with pytest.raises(ValueError, match="LAST_NAME") as caught:
        ls._format_field("Montgomery", field, "fixed")
    assert "Montgomery" not in str(caught.value)
    assert ls._format_field("Day", field, "fixed") == "Day  "


def test_a_date_typed_from_eight_digits_is_formatted():
    from sdg.analyzer.services import layout_service as ls

    field = {"name": "DOB", "type": ls._infer_type(["19800512"]), "length": 8,
             "date_format": ls._detect_date_format(["19800512"])}
    assert ls._format_field("1975-11-03", field, "fixed") == "19751103"


def test_remove_keeps_an_approved_layout_under_one_lock():
    from sdg.analyzer.services import layout_service as ls

    cursor = FakeCursor(rows=[("Layout A", "approved", "reviewer", None)])
    conn = FakeConn(cursor)
    assert ls._delete(conn, 7, keep_approved=True) is None
    assert "UPDLOCK" in cursor.executed[0]
    assert not any(s.startswith("DELETE") for s in cursor.executed)
    assert conn.rollbacks == 1 and conn.commits == 0


def test_remove_deletes_a_pending_layout_in_the_same_transaction():
    from sdg.analyzer.services import layout_service as ls

    cursor = FakeCursor(rows=[("Layout B", "pending_review", None, None)])
    conn = FakeConn(cursor)
    got = ls._delete(conn, 8, keep_approved=True)
    assert got["action"] == "deleted" and got["was_status"] == "pending_review"
    assert cursor.executed[1].startswith("DELETE") and conn.commits == 1


# ── Batch 7 #1, batch 9 #1: the SQL scripts cannot leave a partial load ──────

def _batches(path: Path) -> list[str]:
    from seed.build import _split_on_go

    return [b for b in _split_on_go(path.read_text(encoding="utf-8")) if b.strip()]


def test_every_lookup_seed_is_one_transaction():
    text = (ROOT / "scripts" / "customer" / "setup_tables.sql").read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n")
    seeds = re.findall(r"IF NOT EXISTS \(SELECT 1 FROM (sdm\.\w+)\)\nBEGIN\n(.*?)\nEND;\n",
                       text, flags=re.S)
    assert {t for t, _ in seeds} == {"sdm.lkp_first_names", "sdm.lkp_last_names",
                                     "sdm.lkp_street_names", "sdm.lkp_zip_city_state",
                                     "sdm.tag_vocabulary"}
    for table, body in seeds:
        lines = [line.strip() for line in body.splitlines()]
        assert lines[:2] == ["BEGIN TRY", "BEGIN TRANSACTION;"], table
        inserts = [i for i, line in enumerate(lines) if line.startswith("INSERT INTO")]
        commit = lines.index("COMMIT TRANSACTION;")
        assert inserts and max(inserts) < commit, table
        assert "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;" in lines and "THROW;" in lines
    assert "SET XACT_ABORT ON;" in text


def test_every_837_extract_section_is_one_transaction_in_order():
    batches = _batches(ROOT / "sdg" / "packs" / "health" / "sql" / "04_denorm_837_extract.sql")
    sections = [b for b in batches[3:]
                if any(line.strip() and not line.strip().startswith("--")
                       for line in b.splitlines())]
    assert "CREATE TABLE #extract_step" in batches[2]
    *loads, counts = sections
    for n, batch in enumerate(loads, start=1):
        code = [line.strip() for line in batch.splitlines()
                if line.strip() and not line.strip().startswith("--")]
        assert code[0] == f"IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> {n - 1}", code[0]
        assert "SET NOEXEC ON; RETURN; END;" in code[1]
        assert code[2] == "BEGIN TRANSACTION;"
        assert code[-2:] == [f"INSERT INTO #extract_step (step) VALUES ({n});",
                             "COMMIT TRANSACTION;"], code[-2:]
        for i, line in enumerate(code):
            if line.startswith("TRUNCATE TABLE"):
                assert 2 < i < len(code) - 2, line
    assert f"<> {len(loads)}" in counts and "Row counts" in counts


def test_the_cob_and_description_lookups_take_one_row():
    text = (ROOT / "sdg" / "packs" / "health" / "sql"
            / "04_denorm_837_extract.sql").read_text(encoding="utf-8")
    for table in ("CMC_CLCB_CL_COB", "CMC_CDCB_LI_COB", "CMC_PLDS_PLAN_DESC",
                  "CMC_PDDS_PROD_DESC"):
        assert f"LEFT JOIN dbo.{table}" not in text, table
        assert re.search(rf"OUTER APPLY \(SELECT TOP 1 [^;]*?FROM dbo\.{table} x", text, re.S), table


# ── Batch 8 #1 (user decision 2026-11-12): identifying fields are withheld ──

def test_the_extract_withholds_identifying_fields():
    text = (ROOT / "sdg" / "packs" / "health" / "extract" / "FACETS.sql").read_text(encoding="utf-8")
    for column in ("MEME_LAST_NAME", "MEME_FIRST_NAME", "MEME_MID_INIT", "MEME_SSN",
                   "MEME_MEDCD_NO", "MEME_HICN", "SBSB_LAST_NAME", "SBSB_FIRST_NAME",
                   "SBSB_MID_INIT", "SBSB_EMPLOY_ID", "EMAIL", "SBAD_ADDR1", "SBAD_ADDR2",
                   "SBAD_PHONE", "MECB_POLICY_ID", "MECB_PRIM_LAST_NM", "MECB_PRIM_FIRST_NM",
                   "MECB_PRIM_ID"):
        written = re.findall(rf"^\s*(.*?)\s+AS {column}\b", text, re.M)
        assert written, column
        assert all(w.startswith("CAST(NULL AS") for w in written), (column, written)
    # Read straight from FACETS nowhere else either.
    for read in ("m.MEME_SSN", "m.MEME_LAST_NAME", "s.SBSB_LAST_NAME", "SBAD_EMAIL",
                 "a.SBAD_ADDR1", "cb.MECB_POLICY_ID"):
        assert read not in text, read
    # Kept: birth date and the home geography patterns read.
    assert "CAST(m.MEME_BIRTH_DT AS date)" in text
    assert "a.SBAD_CITY, a.SBAD_STATE, a.SBAD_ZIP" in text
    assert "IDENTIFYING FIELDS ARE WITHHELD" in text

"""The review's findings on the AUDIT.md Medium fixes: each check closed where
the review found it open. No database."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pyodbc
import pytest

ROOT = Path(__file__).parents[1]


# --- the store's schema name is checked where the setting is read ----------

def test_a_schema_name_that_is_not_plain_is_refused(monkeypatch):
    from sdg import config

    monkeypatch.setattr(config, "load_env", lambda env_file=None: None)
    monkeypatch.setenv("SDG_DENORM_SCHEMA", "sdm; DROP TABLE x")
    with pytest.raises(ValueError, match="not a plain schema name"):
        config.get_settings()
    monkeypatch.setenv("SDG_DENORM_SCHEMA", "sdm_test")
    assert config.get_settings().denorm_schema == "sdm_test"


# --- a plan source's path is relative ----------------------------------------

def _path_pattern() -> str:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            path = node.get("path")
            if isinstance(path, dict) and "pattern" in path:
                found.append(path["pattern"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads((ROOT / "sdg" / "plan" / "schema.json").read_text(encoding="utf-8")))
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("path, allowed", [
    ("out/sample", True), ("work", True), ("a..b", True),
    ("a/../b", False), ("..", False), (r"C:\Windows\x", False), ("/etc/x", False),
    (r"\\host\share", False),
])
def test_a_plan_source_path_is_relative(path, allowed):
    assert bool(re.search(_path_pattern(), path)) is allowed


# --- permission denied is not "no readable key" -------------------------------

class _Cursor:
    def __init__(self, error):
        self.error = error

    def execute(self, sql):
        raise self.error


def _registry(error):
    @contextlib.contextmanager
    def connect(name):
        yield SimpleNamespace(cursor=lambda: _Cursor(error))

    return SimpleNamespace(connect=connect)


def test_a_key_start_the_login_cannot_read_stops_the_run():
    from sdg.engine.starts import _highest

    denied = pyodbc.ProgrammingError("42000", "SELECT permission was denied")
    with pytest.raises(RuntimeError, match="stopped before writing"):
        _highest(_registry(denied), "target", [("dbo.T", "[dbo].[T]", "ID")])


# --- a delimiter of more than one character ----------------------------------

def test_a_csv_line_with_a_two_character_delimiter_is_joined_and_quoted():
    from sdg.analyzer.services.layout_service import _csv_line

    assert _csv_line(["A", "B||C", 'say "hi"', "D"], "||") == 'A||"B||C"||"say ""hi"""||D'
    assert _csv_line(["A", "B"], ",") == "A,B"


# --- a lost connection stops the profile --------------------------------------

def test_a_lost_connection_stops_the_profile(monkeypatch):
    from sdg.discovery.model import PhysicalModel, Table
    from sdg.profiling import run

    def one(registry, model, table, *args):
        raise pyodbc.OperationalError("08S01", "Communication link failure")

    monkeypatch.setattr(run, "_profile_one", one)
    monkeypatch.setattr(run, "profile_ratios", lambda *a, **k: [])
    monkeypatch.setattr(run, "withhold_personal_values", lambda *a, **k: None)
    registry = SimpleNamespace(ref=lambda name: SimpleNamespace(as_dict=lambda: {"name": name}))
    model = PhysicalModel(tables=[Table(name="dbo.A", schema_name="dbo", table_name="A")])
    with pytest.raises(pyodbc.OperationalError):
        run.profile(registry, model, workers=1, max_minutes=0, catalog=object())


# --- wheel parts are unpacked only against a SHA-256 --------------------------

def _unpacker(tmp_path, monkeypatch):
    import sys
    import sysconfig
    import zipfile

    spec = importlib.util.spec_from_file_location(
        "unpack_wheels_under_test", ROOT / "scripts" / "unpack_wheels.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(sysconfig, "get_platform", lambda: "win-amd64")
    monkeypatch.setattr(sysconfig, "get_config_var", lambda name: None)
    tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    part = tmp_path / f"sdm-wheels-{tag}.001"
    with zipfile.ZipFile(part, "w") as zf:
        zf.writestr("wheels/demo-1.0-py3-none-any.whl", b"wheel")
    return module, tag, hashlib.sha256(part.read_bytes()).hexdigest()


def test_parts_without_a_checksum_are_not_unpacked(tmp_path, monkeypatch, capsys):
    module, _tag, _digest = _unpacker(tmp_path, monkeypatch)
    assert module.main([]) == 1
    assert "Nothing was unpacked" in capsys.readouterr().out
    assert not (tmp_path / "wheels").exists()


def test_parts_that_do_not_match_the_given_hash_are_not_unpacked(tmp_path, monkeypatch):
    module, _tag, _digest = _unpacker(tmp_path, monkeypatch)
    assert module.main(["--sha256", "0" * 64]) == 1
    assert not (tmp_path / "wheels").exists()


def test_parts_matching_the_given_hash_or_the_file_are_unpacked(tmp_path, monkeypatch):
    module, tag, digest = _unpacker(tmp_path, monkeypatch)
    assert module.main(["--sha256", digest.upper()]) == 0
    assert (tmp_path / "wheels" / "demo-1.0-py3-none-any.whl").exists()

    (tmp_path / f"sdm-wheels-{tag}.sha256").write_text(f"{digest}  sdm-wheels-{tag}.zip\n",
                                                       encoding="ascii")
    assert module.main([]) == 0


# --- the saved connection file is replaced whole, or not at all ---------------

def test_a_failed_permission_step_leaves_the_saved_connection_as_it_was(tmp_path, monkeypatch):
    from sdg.api import service

    path = tmp_path / "target-connection.json"
    path.write_text('{"server": "old"}', encoding="utf-8")

    def refuse(_path):
        raise service.ServiceError("Could not limit the file")

    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service, "_owner_only_on_windows", refuse)
    with pytest.raises(service.ServiceError):
        service._write_private(path, '{"server": "new", "password": "x"}')
    assert path.read_text(encoding="utf-8") == '{"server": "old"}'
    assert [p.name for p in tmp_path.iterdir()] == ["target-connection.json"]


# --- rows committed before a switch-back failure stay on the record ----------

class _SwitchCursor:
    fast_executemany = False

    def execute(self, sql, *args):
        if "ENABLE TRIGGER" in sql:
            raise pyodbc.OperationalError("08S01", "Communication link failure")

    def executemany(self, sql, rows):
        pass


class _SwitchConnection:
    def cursor(self):
        return _SwitchCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


def test_a_table_committed_before_its_triggers_failed_to_come_back_is_recorded(monkeypatch):
    from sdg.writers import sqlserver

    monkeypatch.setattr(sqlserver, "qualified", lambda table: "[dbo].[A]")
    monkeypatch.setattr(sqlserver, "needs_identity_insert", lambda table, model: False)
    table = SimpleNamespace(name="dbo.A", fields=[SimpleNamespace(name="ID", generator="sequence")])
    model = SimpleNamespace(table=lambda name: SimpleNamespace(primary_key=["ID"], triggers=[]))
    with pytest.raises(sqlserver._CommittedThenFailed) as caught:
        sqlserver._write_table(_SwitchConnection(), table, [{"ID": 1}, {"ID": 2}], model,
                               disable_triggers=True, batch_size=100)
    assert caught.value.record.rows_written == 2
    assert caught.value.record.key_values == [(1,), (2,)]


def test_the_load_reports_that_table_so_cleanup_can_find_its_rows(monkeypatch):
    from sdg.writers import sqlserver

    record = sqlserver.TableWrite(table="dbo.A", rows_written=2)

    def write_table(*args, **kwargs):
        raise sqlserver._CommittedThenFailed(
            pyodbc.OperationalError("08S01", "Communication link failure"), record)

    @contextlib.contextmanager
    def connect(name):
        yield object()

    monkeypatch.setattr(sqlserver, "_write_table", write_table)
    monkeypatch.setattr(sqlserver, "check_widths", lambda *args: None)
    registry = SimpleNamespace(database_for=lambda name: "SDMT_TARGET", connect=connect)
    plan = SimpleNamespace(tables_in_order=lambda: [SimpleNamespace(name="dbo.A")])
    result = SimpleNamespace(tables={"dbo.A": SimpleNamespace(rows=[{"ID": 1}, {"ID": 2}])})
    with pytest.raises(sqlserver.PartialWrite) as caught:
        sqlserver.write(registry, plan, result, model=None)
    assert caught.value.report.tables == [record]


# --- reading a baseline never creates its table ------------------------------

def test_a_baseline_never_computed_reads_as_none_without_creating_the_table(monkeypatch):
    from sdg.analyzer.services import baseline

    seen: list[str] = []

    class Cursor:
        def execute(self, sql, *args):
            seen.append(sql)

        def fetchone(self):
            return (None,)

    connection = SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None)
    monkeypatch.setattr(baseline, "_conn", lambda: connection)
    monkeypatch.setattr(baseline, "ensure_table",
                        lambda: (_ for _ in ()).throw(AssertionError("created the table")))
    assert baseline.get("members", "ALL") is None
    assert baseline.get_all("members") == []
    assert baseline.get_thresholds("members") is None
    assert all("OBJECT_ID" in sql for sql in seen)


# --- a constant personal column keeps no mean ---------------------------------

def test_a_constant_personal_column_keeps_no_distribution(monkeypatch):
    from sdg.profiling import personal

    column = SimpleNamespace(qualified="dbo.M.SSN", column="SSN", minimum=1, maximum=1,
                             top_values=None, fitted_format=None, sequence=None,
                             distribution={"kind": "constant", "min": 1, "max": 1, "mean": 123456789})
    monkeypatch.setattr(personal, "personal", lambda profile, catalog, keys: "SSN")
    personal.withhold_personal_values(SimpleNamespace(columns=[column]), catalog=None)
    assert column.distribution is None


# --- the log never holds the driver's message ---------------------------------

def test_a_database_error_is_logged_without_its_message(caplog):
    import logging

    import sdg  # noqa: F401 -- installs the log redaction

    error = pyodbc.IntegrityError(
        "23000", "[23000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Violation of "
        "PRIMARY KEY constraint. The duplicate key value is (M1234567). (2627)")
    log = logging.getLogger("sdg.test_logsafe")
    with caplog.at_level(logging.WARNING, logger=log.name):
        log.warning("Insert failed: %s", error)
        try:
            raise RuntimeError("the load stopped") from error
        except RuntimeError:
            log.exception("Load failed")
    text = caplog.text + "".join(r.exc_text or "" for r in caplog.records)
    assert "M1234567" not in text
    assert "23000" in text and "the load stopped" in text


# --- the saved target password is encrypted for this Windows account ----------

@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows only")
def test_the_saved_password_is_encrypted_and_read_back(tmp_path):
    from sdg.api import service

    path = tmp_path / "target-connection.json"
    stored = {"server": "sql1", "database": "SDMT_A", "user": "u", "password": "s3cret;}"}
    service._write_private(path, json.dumps(service._protected(stored)))
    written = path.read_text(encoding="utf-8")
    assert "s3cret" not in written and "password_protected" in written

    owner = SimpleNamespace(work=tmp_path)
    assert service.Service.target_connection(owner)["password"] == "s3cret;}"


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows only")
def test_a_plain_saved_password_is_rewritten_encrypted(tmp_path):
    from sdg.api import service

    path = tmp_path / "target-connection.json"
    path.write_text(json.dumps({"server": "sql1", "password": "old-plain"}), encoding="utf-8")
    owner = SimpleNamespace(work=tmp_path)
    assert service.Service.target_connection(owner)["password"] == "old-plain"
    assert "old-plain" not in path.read_text(encoding="utf-8")
    assert service.Service.target_connection(owner)["password"] == "old-plain"


def test_a_password_another_account_encrypted_is_a_value_error(tmp_path, monkeypatch):
    from sdg import dpapi
    from sdg.api import service

    def refuse(_text):
        raise ValueError("cannot be decrypted by this Windows account")

    monkeypatch.setattr(dpapi, "unprotect", refuse)
    path = tmp_path / "target-connection.json"
    path.write_text(json.dumps({"server": "sql1", "password_protected": "AAAA"}), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be decrypted"):
        service.Service.target_connection(SimpleNamespace(work=tmp_path))


# --- a store without names: each person's details made once ------------------

def test_a_store_without_names_gets_names_made_once_per_person(monkeypatch):
    from sdg.writers import elements

    monkeypatch.setattr(elements, "_made",
                        lambda element, rules, seed, index, as_of, context=None:
                        f"{element}:{seed}:{index}")
    rules = {"FIRST_NAME": {}, "LAST_NAME": {}, "SSN": {}}
    child = {"MEMBER_ID": "A02", "SUBSCRIBER_ID": "A", "RELATIONSHIP_CODE": "19"}
    once = elements._as_person(child, rules, "s", 3, "2026-06-30", nameless=True)
    assert once["FIRST_NAME"] == "FIRST_NAME:s:person:3"
    assert once["SSN"] == "SSN:s:person:3"
    assert elements._as_person(child, rules, "s", 3, "2026-06-30", nameless=True) == once
    assert "FIRST_NAME" not in child, "the store's record is not changed"

    # One household, one head: every dependent names the same head, and the
    # head's own rows carry that name.
    sibling = elements._as_person({**child, "MEMBER_ID": "A03"}, rules, "s", 7, "2026-06-30", nameless=True)
    head = elements._as_person({"MEMBER_ID": "A00", "SUBSCRIBER_ID": "A",
                                "RELATIONSHIP_CODE": "M"}, rules, "s", 1, "2026-06-30",
                                nameless=True)
    assert once["SUBSCRIBER_LAST_NAME"] == sibling["SUBSCRIBER_LAST_NAME"] == head["LAST_NAME"]
    assert once["SUBSCRIBER_FIRST_NAME"] == head["FIRST_NAME"]
    assert "SUBSCRIBER_LAST_NAME" not in head


def test_a_store_that_holds_names_is_written_as_it_always_was(monkeypatch):
    from sdg.writers import elements

    monkeypatch.setattr(elements, "_made", lambda *a, **k: "MADE")
    rules = {"FIRST_NAME": {}, "LAST_NAME": {}, "SSN": {}}
    named = {"MEMBER_ID": "A02", "RELATIONSHIP_CODE": "19", "LAST_NAME": "HELD"}
    blank = {"MEMBER_ID": "A03", "RELATIONSHIP_CODE": "19"}
    # Decided for the store: one record without a name among named ones is
    # left blank, as it always was.
    assert elements.holds_no_names([named, blank]) is False
    assert elements.holds_no_names([blank]) is True
    assert elements._as_person(blank, rules, "s", 3, "2026-06-30", nameless=False) == blank

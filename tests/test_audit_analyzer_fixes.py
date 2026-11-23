"""The analyzer group's fixes from the standards audit (AUDIT.md batches 11-15).

Pure Python: nothing here reaches a database. A failed store read is now said
(raised or reported) rather than read as "nothing there", the disk cache loads
only what it signed, and an upload is refused before it can unpack to more than
its limit.
"""

from __future__ import annotations

import io
import pickle
import zipfile
from types import SimpleNamespace

import pandas as pd
import pyodbc
import pytest

from sdg import packs


def _missing_table_error() -> Exception:
    """A missing-table failure as pandas raises it: the driver's error as cause."""
    try:
        raise pyodbc.ProgrammingError("42S02", "[42S02] Invalid object name")
    except pyodbc.ProgrammingError as cause:
        try:
            raise pd.errors.DatabaseError("Execution failed") from cause
        except pd.errors.DatabaseError as wrapped:
            return wrapped


class _Conn:
    def close(self) -> None:
        pass


# ── Batch 11: seed scripts ───────────────────────────────────────────────────

def test_the_fixture_build_refuses_keep_rather_than_ignoring_it():
    from seed.build import main

    with pytest.raises(SystemExit) as stopped:
        main(["--keep"])
    assert stopped.value.code == 2


@pytest.mark.parametrize("schema", ["sdm]; DROP TABLE x;--", "a b", "", "x'y", "1abc"])
def test_a_store_schema_that_is_not_a_plain_name_is_refused(schema):
    from seed.bootstrap import in_schema, plain_schema

    with pytest.raises(SystemExit):
        plain_schema(schema)
    with pytest.raises(SystemExit):
        in_schema("CREATE TABLE sdm.t (a int);", schema)


def test_a_plain_store_schema_is_used():
    from seed.bootstrap import in_schema

    assert in_schema("SELECT 1 FROM sdm.t", "store_2") == "SELECT 1 FROM store_2.t"


# ── Batch 13: pattern storage ────────────────────────────────────────────────

def test_an_edit_onto_another_patterns_combination_is_refused_before_writing(monkeypatch):
    from sdg.analyzer.services import pattern_service as ps

    row = {"id": 7, "origin": ps.USER_DEFINED, "contextual_name": "Mine", "combo_key": "OLD"}
    monkeypatch.setattr(ps.db, "get_pattern", lambda pid: row)
    monkeypatch.setattr(ps, "_entity_of", lambda r: packs.active().primary)
    monkeypatch.setattr(ps, "_scope_values", lambda r: {})
    monkeypatch.setattr(ps, "combo_key", lambda values, entity=None: "NEW")
    monkeypatch.setattr(ps.scope, "labels_of", lambda r: [])
    monkeypatch.setattr(ps.scope, "columns", lambda *a, **k: {})
    monkeypatch.setattr(ps, "_find_by_combo", lambda ck: [
        {"pattern_id": 9, "name": "Theirs", "origin": ps.USER_DEFINED}])

    def no_write():
        raise AssertionError("the store was written to")

    monkeypatch.setattr(ps, "_conn", no_write)
    feature = ps.FEATURES[0]
    with pytest.raises(ValueError, match="Theirs"):
        ps.update_user_pattern(7, features={feature: ["X"]})


def test_joined_values_are_empty_only_when_the_tables_are_missing(monkeypatch):
    from sdg.analyzer import sql
    from sdg.analyzer.services import defined_patterns as dp

    feature = {"key": "pcp_specialty", "column": "_pcp_specialty"}

    def missing(*a, **k):
        raise _missing_table_error()

    monkeypatch.setattr(sql, "read_sql", missing)
    assert dp._joined_options(feature, _Conn()) == []

    def refused(*a, **k):
        raise pyodbc.ProgrammingError("42000", "[42000] SELECT permission was denied")

    monkeypatch.setattr(sql, "read_sql", refused)
    with pytest.raises(pyodbc.ProgrammingError):
        dp._joined_options(feature, _Conn())


def _reference_field():
    field = SimpleNamespace(generator="reference",
                            params={"parent_table": "dbo.PLAN", "parent_column": "PLAN_ID"})
    plan_obj = SimpleNamespace(tables=[SimpleNamespace(name="dbo.PLAN", mode="reuse")])
    return field, plan_obj


def test_a_reference_read_that_fails_is_recorded_not_silenced(monkeypatch):
    import sys

    from sdg.analyzer.services import seed_elements as se

    def broken(*a, **k):
        raise pyodbc.OperationalError("08S01", "link failure")

    fake_app = SimpleNamespace(service=SimpleNamespace(registry=SimpleNamespace(query=broken)))
    monkeypatch.setitem(sys.modules, "sdg.api.app", fake_app)
    field, plan_obj = _reference_field()
    out = se._reference_values(field, plan_obj)
    assert "domain" not in out
    assert out["reference_error"].startswith("dbo.PLAN.PLAN_ID")


def test_refreshing_keeps_a_domain_its_reference_could_not_be_read_for(monkeypatch):
    from sdg.analyzer.services import layout_service as ls
    from sdg.analyzer.services import seed_elements as se

    # data_type differs, so the rule is refreshed; its domain must survive that.
    rule = {"id": 3, "field_name": "PLAN", "updated_by": "seed", "gen_method": "lookup",
            "data_type": "number", "domain": "P1,P2", "lookup_source": "dbo.PLAN.PLAN_ID"}
    updates = []
    monkeypatch.setattr(ls, "get_all_rules", lambda: [rule])
    monkeypatch.setattr(ls, "update_rule", lambda rid, changes, updated_by: updates.append(changes))
    column = {"name": "PLAN", "method": "lookup", "data_type": "text",
              "reference_error": "dbo.PLAN.PLAN_ID: OperationalError"}
    assert se._refresh([column]) == ["PLAN"]
    assert updates and all("domain" not in c and "lookup_source" not in c for c in updates)


# ── Batch 14: analyzer core and caches ───────────────────────────────────────

@pytest.fixture
def disk(monkeypatch, tmp_path):
    from sdg.analyzer.services import disk_cache as dc

    monkeypatch.setattr(dc, "CACHE_DIR", tmp_path / "api")
    monkeypatch.setattr(dc, "KEY_FILE", tmp_path / "cache.key")
    monkeypatch.setattr(dc, "_KEY", None)
    monkeypatch.setattr(dc, "_NS_INDEX", {})
    monkeypatch.setattr(dc, "_NS_FILES", {})
    monkeypatch.setattr(dc, "_NS_SCANNED", False)
    monkeypatch.setattr(dc, "data_fingerprint", lambda: "fixed")
    monkeypatch.delenv("SDG_CACHE_KEY", raising=False)
    return dc


def test_a_cache_entry_comes_back_when_its_signature_matches(disk):
    disk.put("k1", {"value": 42}, ttl=60, label="t(1)", namespace="t")
    assert disk.get("k1") == {"value": 42}
    assert disk.KEY_FILE.exists()


class _Planted:
    ran = False

    def __reduce__(self):
        return (_mark_planted, ())


def _mark_planted():
    _Planted.ran = True
    return "planted"


def test_a_pickle_the_cache_did_not_sign_is_never_loaded(disk):
    disk.put("k2", {"value": 1}, ttl=60, label="t(2)", namespace="t")
    blob, _ = disk._paths("k2")
    blob.write_bytes(pickle.dumps(_Planted()))
    _Planted.ran = False
    assert disk.get("k2") is None
    assert not _Planted.ran, "an unsigned pickle was unpickled"
    assert not blob.exists(), "the unsigned entry was kept"


def test_writers_of_one_key_each_use_their_own_temp_file(disk):
    import threading

    errors = []

    def write(n):
        try:
            for _ in range(20):
                disk.put("same", {"n": n}, ttl=60, label="t(same)", namespace="t")
        except Exception as e:  # pragma: no cover - the failure being tested for
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert not list(disk.CACHE_DIR.glob("*.tmp"))
    # Racing writers may leave a blob and sidecar from different writes, which
    # reads as a miss; a value is never served from another writer's signature.
    assert disk.get("same") in (None, *({"n": n} for n in range(4)))
    disk.put("same", {"n": 9}, ttl=60, label="t(same)", namespace="t")
    assert disk.get("same") == {"n": 9}


def test_refresh_replaces_the_value_held_in_memory_too(disk):
    calls = {"n": 0}

    @disk.two_tier("test.audit_refresh", ttl=60)
    def compute():
        calls["n"] += 1
        return calls["n"]

    compute.cache_clear()
    assert compute() == 1
    assert compute(refresh=True) == 2
    assert compute() == 2, "the next call answered from memory with the old value"


def test_a_fingerprint_that_cannot_be_read_is_not_a_constant_and_is_retried(monkeypatch):
    from sdg.analyzer.services import discovery_features
    from sdg.analyzer.services import disk_cache as dc

    def down(*a, **k):
        raise pyodbc.OperationalError("08001", "server not found")

    monkeypatch.setattr(pyodbc, "connect", down)
    monkeypatch.setattr(discovery_features, "signature", lambda: "f")
    monkeypatch.setattr(dc, "_FINGERPRINT", None)
    first = dc.data_fingerprint()
    assert first.startswith("unavailable:")
    stamped_at = dc._FINGERPRINT[0]
    monkeypatch.setattr(dc, "_FINGERPRINT", None)
    assert dc.data_fingerprint() != first
    import time

    assert time.time() - stamped_at >= dc.FINGERPRINT_TTL - dc.FINGERPRINT_RETRY - 1


def test_a_baseline_store_that_cannot_be_reached_is_not_no_baseline(monkeypatch):
    from sdg.analyzer.services import baseline

    def down():
        raise pyodbc.OperationalError("08001", "server not found")

    # Reading opens a connection and never creates the table.
    monkeypatch.setattr(baseline, "_conn", down)
    with pytest.raises(pyodbc.OperationalError):
        baseline.get_all()
    with pytest.raises(pyodbc.OperationalError):
        baseline.get("members", "ALL")
    with pytest.raises(pyodbc.OperationalError):
        baseline.get_thresholds()


def test_damaged_stored_thresholds_fall_back_to_fixed_shares(monkeypatch):
    from sdg.analyzer.services import baseline

    class Cursor:
        def execute(self, *a):
            return self

        def fetchone(self):
            return ("{not json",)

    class Conn(_Conn):
        def cursor(self):
            return Cursor()

    monkeypatch.setattr(baseline, "ensure_table", lambda: None)
    monkeypatch.setattr(baseline, "_conn", lambda: Conn())
    assert baseline.get_thresholds() is None


# ── Batch 15: reports and stories ────────────────────────────────────────────

def test_a_failed_read_of_the_chosen_features_is_not_kept(monkeypatch):
    from sdg.analyzer.services import discovery_features as df

    entity = packs.active().primary

    def down():
        raise pyodbc.OperationalError("08001", "server not found")

    monkeypatch.setattr(df, "_CHOSEN", {})
    monkeypatch.setattr(df, "_known", lambda e: {k: {} for k in df.DEFAULTS[e]})
    monkeypatch.setattr(df, "ensure_table", down)
    assert df.chosen(entity) == list(df.DEFAULTS[entity])
    assert entity not in df._CHOSEN, "one failed read replaced the saved choice"


def test_provider_features_are_empty_only_when_their_tables_are_missing(monkeypatch):
    from sdg.analyzer import entity_loader
    from sdg.analyzer import provider_features as pf

    monkeypatch.setattr(entity_loader, "connect", lambda: _Conn())

    def missing(*a, **k):
        raise _missing_table_error()

    monkeypatch.setattr(pf, "read_sql", missing)
    assert pf._read("SELECT 1") is None

    def refused(*a, **k):
        raise pyodbc.ProgrammingError("42000", "[42000] SELECT permission was denied")

    monkeypatch.setattr(pf, "read_sql", refused)
    with pytest.raises(pyodbc.ProgrammingError):
        pf._read("SELECT 1")


def test_report_fields_that_cannot_be_read_are_not_the_builtin_ones(monkeypatch):
    from sdg.analyzer.services import report_fields as rf

    def down():
        raise pyodbc.OperationalError("08001", "server not found")

    monkeypatch.setattr(rf, "ensure_table", down)
    with pytest.raises(pyodbc.OperationalError):
        rf._stored(packs.active().primary)


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize("filename", ["stories.docx", "stories.xlsx"])
def test_an_upload_that_unpacks_past_the_limit_is_refused(monkeypatch, filename):
    from sdg.analyzer.services import story_service as svc

    monkeypatch.setattr(svc, "MAX_UNPACKED_BYTES", 10_000)
    raw = _zip({"word/document.xml": b"\0" * 50_000})
    assert len(raw) < 10_000, "the test zip should be small compressed"
    with pytest.raises(ValueError, match="unpacks to more than"):
        svc.parse_upload(filename, raw)


@pytest.mark.parametrize("filename", ["stories.docx", "stories.xlsx"])
def test_a_damaged_office_upload_is_a_value_error(filename):
    from sdg.analyzer.services import story_service as svc

    with pytest.raises(ValueError, match="damaged"):
        svc.parse_upload(filename, b"not a zip at all")


def test_a_zip_that_is_not_a_word_document_is_a_value_error():
    from sdg.analyzer.services import story_service as svc

    with pytest.raises(ValueError):
        svc.parse_upload("stories.docx", _zip({"readme.txt": b"hello"}))

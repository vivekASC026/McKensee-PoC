"""The API group's fixes from the standards audit (AUDIT.md batches 1, 2, 3, 16).

Pure Python: nothing here reaches a database. Each failure that used to be
swallowed is now either said to the caller or kept with a log line, and each
upload is refused before more than its limit is read.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sdg.config import MssqlSettings, odbc_value


def _settings(**overrides) -> MssqlSettings:
    base = dict(server="sql.example", port=1433, user="sdm", password="secret",
                driver="ODBC Driver 18 for SQL Server", trust_cert=True)
    return MssqlSettings(**{**base, **overrides})


class _Upload:
    """Enough of an UploadFile for a route called directly."""

    def __init__(self, data: bytes, filename: str = "stories.txt"):
        self.filename = filename
        self._data = data
        self.asked: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.asked.append(size)
        return self._data if size < 0 else self._data[:size]


# -- Batch 2 #1, #2: connection-string values --------------------------------

def test_plain_values_are_left_as_they_are():
    assert odbc_value("CUSTOMER_DENORM") == "CUSTOMER_DENORM"
    built = _settings().connection_string("X")
    assert "DATABASE=X;" in built and "PWD=secret;" in built


def test_a_password_with_delimiters_is_braced_and_reads_back_whole():
    built = _settings(password="a;b}c").connection_string("X")
    assert "PWD={a;b}}c};" in built
    assert "TrustServerCertificate=yes" in built


def test_redacted_hides_the_whole_password_however_it_is_quoted():
    redacted = _settings(password="pa;ss}word").redacted("X")
    assert "PWD=***;" in redacted
    assert "pa;ss" not in redacted and "word" not in redacted


def test_a_database_name_cannot_add_connection_attributes():
    built = _settings().connection_string("x;Server=other")
    assert "DATABASE={x;Server=other};" in built
    assert ";Server=other;" not in built


def test_values_edged_with_spaces_are_braced():
    assert odbc_value(" pw ") == "{ pw }"


# -- Batch 1 #3: the SNIP file check ------------------------------------------

def test_snip_check_refuses_a_binary_file():
    from sdg.api.app import app

    client = TestClient(app)
    response = client.post("/api/edi/snip/check-file",
                           files={"file": ("x.bin", b"ISA*00\x00\x01\x02", "application/octet-stream")})
    assert response.status_code == 422, response.text
    assert "not a text file" in response.json()["detail"]


def test_snip_check_refuses_a_file_over_the_limit(monkeypatch):
    from sdg.api import app as app_module
    from sdg.api import limits

    monkeypatch.setattr(app_module, "MAX_SAMPLE_BYTES", 16)
    monkeypatch.setattr(limits, "MAX_SAMPLE_BYTES", 16)
    client = TestClient(app_module.app)
    response = client.post("/api/edi/snip/check-file",
                           files={"file": ("big.x12", b"ISA*" * 20, "text/plain")})
    assert response.status_code == 413, response.text


def test_an_unexpected_error_is_logged_with_its_traceback(caplog):
    from sdg.api.app import _guard

    def boom():
        raise RuntimeError("the cause")

    with caplog.at_level(logging.ERROR, logger="sdg.api.app"):
        with pytest.raises(HTTPException) as raised:
            _guard(boom)
    assert raised.value.status_code == 500
    assert any(r.exc_info for r in caplog.records), "no traceback was logged"


# -- Batch 3: service ---------------------------------------------------------

def test_plan_descriptions_that_fail_are_not_held_for_the_process(caplog):
    from sdg.api.service import Service

    class Failing:
        calls = 0

        def query(self, *_args, **_kwargs):
            Failing.calls += 1
            raise RuntimeError("server gone")

    service = Service.__new__(Service)
    service._coverage_plans = None
    service.registry = Failing()
    with caplog.at_level(logging.WARNING, logger="sdg.api.service"):
        assert service.coverage_plans() == {}
    assert service._coverage_plans is None
    assert caplog.records, "the failure was not logged"
    service.coverage_plans()
    assert Failing.calls == 2, "the failure was cached as if it were a result"


def test_the_target_connection_file_is_written_for_its_owner(tmp_path):
    from sdg.api.service import _write_private

    path = tmp_path / "target-connection.json"
    path.write_text("old", encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o644)
    _write_private(path, '{"password": "x"}')
    assert path.read_text(encoding="utf-8") == '{"password": "x"}'
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


# -- Batch 16: routers --------------------------------------------------------

def test_a_hierarchy_failure_is_said_not_shown_as_no_rows(monkeypatch):
    from sdg.api.routers import hierarchy

    def down(*_args):
        raise RuntimeError("login failed")

    monkeypatch.setattr(hierarchy.service, "groups", down)
    with pytest.raises(HTTPException) as raised:
        hierarchy.groups()
    assert raised.value.status_code == 500
    assert "could not be read" in raised.value.detail


def test_claims_available_says_an_outage(monkeypatch):
    from sdg.api.routers import claims

    class Loader:
        @staticmethod
        def claims_available():
            raise RuntimeError("server gone")

    monkeypatch.setattr(claims, "_loader", lambda: Loader)
    with pytest.raises(HTTPException) as raised:
        claims.available()
    assert raised.value.status_code == 503


def test_claims_available_still_answers_no(monkeypatch):
    from sdg.api.routers import claims

    class Loader:
        @staticmethod
        def claims_available():
            return False

    monkeypatch.setattr(claims, "_loader", lambda: Loader)
    assert claims.available() == {"available": False}


def test_rules_whose_usage_cannot_be_read_are_not_listed_as_unused(monkeypatch):
    from sdg.api.routers import rules

    def down():
        raise RuntimeError("timeout")

    monkeypatch.setattr(rules.ls, "get_all_rules", lambda: [{"id": 1, "active": True}])
    monkeypatch.setattr(rules.ls, "rule_usage", down)
    monkeypatch.setattr(rules.ls, "aliases_by_rule", lambda: {})
    with pytest.raises(HTTPException) as raised:
        rules.list_rules()
    assert raised.value.status_code == 500


def test_a_layout_whose_rules_cannot_be_read_is_not_shown_as_rules_missing(monkeypatch):
    from sdg.api.routers import layouts

    def down():
        raise RuntimeError("timeout")

    monkeypatch.setattr(layouts.ls, "get_layout",
                        lambda _id: {"fields": [{"rule_id": 7, "name": "A"}]})
    monkeypatch.setattr(layouts.ls, "get_rules", down)
    with pytest.raises(HTTPException) as raised:
        layouts.one(1)
    assert raised.value.status_code == 500


def test_a_sample_is_not_assigned_blank_rules_when_rules_cannot_be_read(monkeypatch):
    from sdg.api.routers import layouts

    def down():
        raise RuntimeError("timeout")

    monkeypatch.setattr(layouts.ls, "parse_sample",
                        lambda _name, _content: {"fields": [{"name": "A"}]})
    monkeypatch.setattr(layouts.ls, "get_rules", down)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(layouts.parse(_Upload(b"A\n1\n", "s.csv")))
    assert raised.value.status_code == 500


def test_a_story_upload_is_not_read_past_its_limit():
    from sdg.api.routers import story

    upload = _Upload(b"x" * (story.MAX_UPLOAD_BYTES + 10))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(story.parse(upload))
    assert raised.value.status_code == 413
    assert upload.asked == [story.MAX_UPLOAD_BYTES + 1]


def test_a_story_file_the_reader_cannot_open_is_a_422(monkeypatch):
    from sdg.api.routers import story

    def broken(_name, _raw):
        raise KeyError("sheet")

    monkeypatch.setattr(story.svc, "parse_upload", broken)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(story.parse(_Upload(b"data", "s.xlsx")))
    assert raised.value.status_code == 422


def test_a_partial_baseline_walk_is_recorded_as_partial(monkeypatch):
    from sdg.analyzer import scope_walk
    from sdg.analyzer.services import analysis
    from sdg.api.routers import baseline

    class Found:
        entity = "members"

        @staticmethod
        def analyze(combo, _k):
            if combo.get("bad"):
                raise RuntimeError("clustering failed")
            return {"patterns": [{"size": 5}], "n_patterns": 1}

        @staticmethod
        def subset(_result):
            return 5

        @staticmethod
        def size(pattern):
            return pattern["size"]

    stored: list[tuple] = []
    monkeypatch.setattr(analysis, "for_entity", lambda _e: Found)
    monkeypatch.setattr(baseline, "_combinations", lambda *_a: [{}, {"bad": True}])
    monkeypatch.setattr(scope_walk, "first_filter", lambda _e: None)
    monkeypatch.setattr(baseline.baseline, "upsert",
                        lambda *args, **kwargs: stored.append((args, kwargs)))

    result = baseline._compute(baseline.ComputeRequest(entity="members"))
    assert result["combinations_failed"] == 1
    (args, kwargs), = stored
    assert args[1] == "ALL"
    assert kwargs["detail"]["complete"] is False
    assert kwargs["detail"]["capped"] is False

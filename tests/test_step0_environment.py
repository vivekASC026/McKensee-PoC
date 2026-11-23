"""Step 0 acceptance: the environment is configured and both databases answer."""

from __future__ import annotations

import pytest

from sdg.config import get_settings
from sdg.discovery.connections import ConnectionRegistry


def test_settings_load_from_env():
    settings = get_settings()
    assert settings.source_db
    assert settings.ref_db
    assert settings.mssql.driver


def test_connection_string_redacts_password():
    settings = get_settings()
    redacted = settings.mssql.redacted("FACETS_QA")
    assert "PWD=***" in redacted
    if settings.mssql.password:
        assert settings.mssql.password not in redacted


def test_connection_string_includes_port_only_when_non_default():
    settings = get_settings()
    conn = settings.mssql.connection_string("X")
    assert "DATABASE=X;" in conn
    if settings.mssql.port == 1433:
        assert f"SERVER={settings.mssql.server};" in conn
    else:
        assert f"SERVER={settings.mssql.server},{settings.mssql.port};" in conn


@pytest.mark.db
@pytest.mark.parametrize("name", ["source", "ref"])
def test_database_is_reachable(name):
    registry = ConnectionRegistry()
    result = registry.check(name)
    assert result["ok"], result.get("error")
    assert result["db"] == registry.database_for(name)

"""The pattern store's own connection and schema.

On a customer's side the denorm database is theirs: another server, a login of
its own, and one schema the tool may write. Each part is a setting, and each
defaults to the configured server's, so nothing changes where they are unset.
"""

from __future__ import annotations

from sdg.config import get_settings
from sdg.discovery.connections import ConnectionRegistry
from seed.bootstrap import in_schema


def test_the_store_falls_back_to_the_server_settings(monkeypatch):
    for name in ("SERVER", "PORT", "USER", "PASSWORD", "DRIVER", "TRUSTED", "TRUST_CERT",
                 "ENCRYPT", "SCHEMA"):
        monkeypatch.delenv(f"SDG_DENORM_{name}", raising=False)
    monkeypatch.delenv("SDG_ANALYZER_SCHEMA", raising=False)
    settings = get_settings()
    assert settings.store == settings.mssql
    assert settings.denorm_schema == "sdm"


def test_the_store_has_a_connection_of_its_own(monkeypatch):
    monkeypatch.setenv("SDG_DENORM_SERVER", "customer-sql.example")
    monkeypatch.setenv("SDG_DENORM_PORT", "14330")
    monkeypatch.setenv("SDG_DENORM_USER", "sdm_reader")
    monkeypatch.setenv("SDG_DENORM_PASSWORD", "secret")
    monkeypatch.setenv("SDG_DENORM_SCHEMA", "sdm_platform")
    monkeypatch.setenv("SDG_DB_DENORM", "CUSTOMER_DENORM")
    settings = get_settings()
    assert settings.store.server == "customer-sql.example" and settings.store.port == 14330
    assert settings.denorm_schema == "sdm_platform"

    registry = ConnectionRegistry(settings)
    built = registry.connection_string("denorm")
    assert "SERVER=customer-sql.example,14330" in built and "DATABASE=CUSTOMER_DENORM" in built
    assert "UID=sdm_reader" in built
    # Every other role keeps the configured server.
    assert "customer-sql.example" not in registry.connection_string("source")


def test_encrypt_is_set_apart_from_trusting_the_certificate(monkeypatch):
    for name in ("SDG_MSSQL_ENCRYPT", "SDG_DENORM_ENCRYPT", "SDG_DENORM_TRUST_CERT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SDG_MSSQL_TRUST_CERT", "no")
    built = get_settings().mssql.connection_string("X")
    assert "Encrypt=no" in built and "TrustServerCertificate" not in built  # as before
    monkeypatch.setenv("SDG_MSSQL_ENCRYPT", "yes")
    settings = get_settings()
    built = settings.mssql.connection_string("X")
    assert "Encrypt=yes" in built and "TrustServerCertificate" not in built
    assert "Encrypt=yes" in settings.store.connection_string("X")
    monkeypatch.setenv("SDG_MSSQL_TRUST_CERT", "yes")
    monkeypatch.delenv("SDG_MSSQL_ENCRYPT")
    assert "TrustServerCertificate=yes;Encrypt=yes" in get_settings().mssql.connection_string("X")


def test_store_scripts_are_written_into_the_configured_schema():
    script = ("IF SCHEMA_ID('sdm') IS NULL EXEC('CREATE SCHEMA sdm');\n"
              "CREATE TABLE sdm.patterns (id int);\nSELECT * FROM sdm.member_denorm;\n"
              "-- msdm.x is not the schema\n")
    rendered = in_schema(script, "sdm_platform")
    assert "sdm_platform.patterns" in rendered and "sdm_platform.member_denorm" in rendered
    assert "SCHEMA_ID('sdm_platform')" in rendered and "CREATE SCHEMA [sdm_platform]" in rendered
    assert "msdm.x" in rendered, "a name merely ending in sdm was rewritten"
    assert in_schema(script, "sdm") == script

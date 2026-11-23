"""A new store starts with its business pack's generation rules, and no other.

The seven default rules (synthetic names, a street, a ZIP, a phone) moved from
SDM's own tables (seed/ddl_sdm.sql) into the health pack's SQL
(sdg/packs/health/sql/default_rules.sql). A fresh health store made by
customer-tables holds exactly those seven, in the order they always had, and
running it again adds none; a fresh banking store holds none. Only databases
named SDMT_RULES_* are touched; both are dropped afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pyodbc
import pytest

pytestmark = [pytest.mark.db, pytest.mark.slow]

ROOT = Path(__file__).resolve().parent.parent

# As seed/ddl_sdm.sql inserted them before the move (f959483), in that order.
PHONE = r"\([2-9]\d{2}\) [2-9]\d{2}-\d{4}"
HEALTH_RULES = [
    ("MEME_FIRST_NAME", "lookup", "first_names", ""),
    ("MEME_LAST_NAME", "lookup", "last_names", ""),
    ("SBSB_FIRST_NAME", "lookup", "first_names", ""),
    ("SBSB_LAST_NAME", "lookup", "last_names", ""),
    ("STREET_1", "lookup", "street_names", ""),
    ("ZIP_CODE", "lookup", "zip_city_state", ""),
    ("PHONE", "pattern", "", PHONE),
]


def _master():
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _drop(database: str) -> None:
    assert database.startswith("SDMT_RULES_")
    with _master() as m:
        m.execute(f"IF DB_ID(N'{database}') IS NOT NULL BEGIN ALTER DATABASE [{database}] "
                  f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{database}]; END")


def _fresh_store(pack: str, times: int = 1) -> list[tuple]:
    """customer-tables run `times` on a new, empty database; the rules it holds."""
    from sdg.config import get_settings

    database = f"SDMT_RULES_{pack.upper()}"
    _drop(database)
    with _master() as m:
        m.execute(f"CREATE DATABASE [{database}]")
    env = dict(os.environ, SDG_PACK=pack, SDG_DB_DENORM=database, SDG_DENORM_SCHEMA="sdm")
    try:
        for _ in range(times):
            done = subprocess.run(
                [sys.executable, "-m", "sdg.cli.main", "customer-tables", "--no-indexes"],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=900)
            assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
        with pyodbc.connect(get_settings().mssql.connection_string(database)) as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT field_name, gen_method, lookup_source, format_pattern "
                "FROM sdm.generation_rules "
                "ORDER BY id").fetchall()]
    finally:
        _drop(database)


def test_a_new_health_store_has_the_seven_default_rules_in_order_once():
    assert _fresh_store("health", times=2) == HEALTH_RULES


def test_a_new_banking_store_has_no_health_rule():
    assert _fresh_store("banking") == []

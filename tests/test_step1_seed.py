"""Step 1 acceptance: the seed rebuilds and every planted pattern holds."""

from __future__ import annotations

import pyodbc
import pytest

from sdg.config import get_settings
from seed.npi import is_valid_npi, luhn_check_digit, make_npi
from seed.reference_data import zip_triples
from seed.verify import run_checks


def test_zip_triples_are_functional():
    triples = zip_triples()
    assert len(triples) >= 500
    by_zip: dict[str, tuple[str, str]] = {}
    for code, city, state in triples:
        assert len(code) == 5 and code.isdigit()
        assert by_zip.setdefault(code, (city, state)) == (city, state)


def test_npi_round_trip():
    for base in (1, 999_999_999, 123_456_789, 7919):
        npi = make_npi(base)
        assert len(npi) == 10 and npi.isdigit()
        assert is_valid_npi(npi)
        # Flipping a digit must break the check.
        broken = npi[:8] + str((int(npi[8]) + 1) % 10) + npi[9]
        assert not is_valid_npi(broken)


def test_luhn_matches_a_known_value():
    # The standard Luhn example: 7992739871 takes check digit 3.
    assert luhn_check_digit("7992739871") == 3
    # The NPI published as a worked example is 1234567893.
    assert make_npi(123_456_789) == "1234567893"


@pytest.fixture(scope="module")
def seed_checks():
    settings = get_settings()
    conn = pyodbc.connect(settings.mssql.connection_string(settings.source_db))
    ref_conn = pyodbc.connect(settings.mssql.connection_string(settings.ref_db))
    try:
        yield run_checks(conn.cursor(), ref_conn.cursor())
    finally:
        conn.close()
        ref_conn.close()


@pytest.mark.db
def test_every_planted_pattern_holds(seed_checks):
    failed = [f"{c.name}: {c.detail}" for c in seed_checks if not c.passed]
    assert not failed, "\n".join(failed)


@pytest.mark.db
def test_seed_covers_the_whole_slice(seed_checks):
    counts = next(c.measured for c in seed_checks if c.name == "row counts")
    for table in ("CMC_SBSB_SUBSC", "CMC_MEME_MEMBER", "CMC_PRPR_PROV",
                  "CMC_PDPD_PRODUCT", "CMC_CLCL_CLAIM", "CMC_CDML_CL_LINE"):
        assert counts[table] > 0, f"{table} is empty"

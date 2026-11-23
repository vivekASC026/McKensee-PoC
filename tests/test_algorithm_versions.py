"""Every version of every algorithm makes the values it always made.

A file written under a request can be written again because its request names
the version of each algorithm that made it, and that version never changes
what it makes. This holds those values: the first values each version made,
for fixed cases, in tests/fixtures/algorithm-golden.json.

When a test here fails, the algorithm's output changed. Do not edit the
recorded values: keep the old function registered under its version and
register the changed one under the next (`register(name, version=2)`,
`register_algorithm(name, 2, fn)`). Then record the new version's values:

    set SDM_RECORD_ALGORITHMS=1 && pytest tests/test_algorithm_versions.py

which adds the versions not recorded yet and never rewrites one that is.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pytest

from sdg.engine.generators import RowContext, base
from sdg.engine.seed import value_seed
from sdg.writers.samples import engine as sample_engine  # noqa: F401  (registers occurrences)
import sdg.engine.rules  # noqa: F401  (registers the rule reading)
import sdg.writers.elements  # noqa: F401  (registers claims per member)

GOLDEN = Path(__file__).parent / "fixtures" / "algorithm-golden.json"
ROWS = range(12)
SEED = "golden-seed"


def _context(field: str, params: dict[str, Any], index: int, **more: Any) -> RowContext:
    return RowContext(seed=SEED, scenario="golden", table="golden", field_name=field,
                      params=params, row_index=index, **more)


def _generator(field: str, params: dict[str, Any], **more: Any) -> Callable[[Callable], list]:
    return lambda fn: [fn(_context(field, params, row, **more)) for row in ROWS]


ADDRESSES = [
    {"ADDRESS_ZIP": "28203", "ADDRESS_CITY": "Charlotte", "ADDRESS_STATE": "NC"},
    {"ADDRESS_ZIP": "68104", "ADDRESS_CITY": "Omaha", "ADDRESS_STATE": "NE"},
    {"ADDRESS_ZIP": "30040", "ADDRESS_CITY": "Cumming", "ADDRESS_STATE": "GA"},
]

# Rules a family is made from, none of them read from a database.
FAMILY_RULES = {
    "RELATIONSHIP_CODE": {"gen_method": "lookup", "values": ["M", "S", "D"],
                          "weights": [45, 20, 35]},
    "MARITAL_STATUS": {"gen_method": "lookup", "values": ["S", "M", "D"],
                       "weights": [60, 30, 10]},
    "SUBSCRIBER_ID": {"gen_method": "pattern", "format_pattern": r"[A-Z]{3}\d{6}"},
    "MEMBER_ID": {"gen_method": "pattern", "format_pattern": r"[A-Z]{3}\d{8}"},
    "LAST_NAME": {"gen_method": "lookup", "values": ["Rivera", "Okafor", "Lund", "Sato"]},
    "FIRST_NAME": {"gen_method": "lookup", "values": ["Ana", "Luis", "Kai", "Noor", "Eli"]},
    "DOB": {"gen_method": "date", "format_pattern": "2026-06-30", "start_value": 70 * 365},
    "COVERAGE_PLAN_ID": {"gen_method": "lookup", "values": ["PLN1", "PLN2"]},
}

# A remittance's claims: a paid claim of two lines, a denied one, and one
# whose copay is more than it allows.
CLAIM_LINES = [
    [{"CLAIM_ID": "C1", "CLAIM_STATUS": "02", "CHARGE_AMOUNT": "120.00"},
     {"CLAIM_ID": "C1", "CLAIM_STATUS": "02", "CHARGE_AMOUNT": "45.50"}],
    [{"CLAIM_ID": "C2", "CLAIM_STATUS": "04", "CHARGE_AMOUNT": "300.00"}],
    [{"CLAIM_ID": "C3", "CLAIM_STATUS": "11", "CHARGE_AMOUNT": "10.00"}],
]

# Whether a claim shares the loop of the one before: (before, this, keys).
LOOP_PAIRS = [
    (None, {"MEMBER_ID": "M1"}, ["MEMBER_ID"]),
    ({"MEMBER_ID": "M1"}, {"MEMBER_ID": "M1"}, ["MEMBER_ID"]),
    ({"MEMBER_ID": "M1"}, {"MEMBER_ID": "M2"}, ["MEMBER_ID"]),
    ({}, {}, ["MEMBER_ID"]),
    ({"SUBSCRIBER_MEMBER_ID": "S1", "MEMBER_ID": "M1"},
     {"SUBSCRIBER_MEMBER_ID": "S1", "MEMBER_ID": "M2"}, ["SUBSCRIBER_MEMBER_ID"]),
    ({"MEMBER_ID": "S1"}, {"SUBSCRIBER_MEMBER_ID": "S1", "MEMBER_ID": "M2"},
     ["SUBSCRIBER_MEMBER_ID", "MEMBER_ID"]),
]

# For each algorithm: named cases, each a function of the version's function.
CASES: dict[str, dict[str, Callable[[Callable], list]]] = {
    "constant": {"value": _generator("PLAN", {"value": "HMO01"})},
    "skip": {"skipped": lambda fn: [repr(fn(_context("X", {}, row))) for row in ROWS]},
    "sequence": {
        "plain": _generator("ID", {"start": 1000}),
        "decorated": _generator("ID", {"start": 7, "width": 6, "prefix": "M", "suffix": "Z",
                                       "step": 3}),
    },
    "pattern": {
        "member id": _generator("MEMBER_ID", {"regex": r"[A-Z]{3}\d{8}"}),
        "digits": _generator("SSN", {"regex": r"\d{9}"}),
    },
    "number": {
        "uniform": _generator("CHARGE", {"distribution": "uniform", "min": 10, "max": 500,
                                         "decimals": 2}),
        "integer": _generator("UNITS", {"distribution": "uniform", "min": 1, "max": 9,
                                        "decimals": 0}),
        "normal": _generator("CHARGE", {"distribution": "normal", "min": 0, "max": 1000,
                                        "mean": 150, "stddev": 40, "decimals": 2}),
        "bands": _generator("AGE", {"distribution": "uniform", "min": 0, "max": 90,
                                    "decimals": 0, "bands": [[0, 17, 1], [18, 64, 3],
                                                             [65, 90, 1]]}),
    },
    "date_relative": {
        "window": _generator("DOB", {"anchor": "2026-06-30", "min_days": -16425,
                                     "max_days": -10950}),
        "bands": _generator("SERVICE_DATE", {"anchor": "2026-06-30", "min_days": -365,
                                             "max_days": 0,
                                             "bands": [[-365, -181, 1], [-180, 0, 2]]}),
    },
    "lookup": {
        "plain": _generator("SEX", {"values": ["F", "M"]}),
        "weighted": _generator("STATUS", {"values": ["A", "T", "C"], "weights": [8, 1, 1]}),
        "records": _generator("ADDRESS_CITY", {"values": ADDRESSES, "field": "ADDRESS_CITY",
                                               "record_key": "pattern_address"}),
    },
    "street_address": {
        "streets": _generator("ADDRESS_LINE", {"streets": ["MAIN ST", "OAK AVE", "UNION ST"]}),
    },
    "template": {
        "email": _generator("EMAIL", {"template": "{FIRST_NAME}.{LAST_NAME}@example.com",
                                      "lower": True},
                            row={"FIRST_NAME": "Dana", "LAST_NAME": "Okafor"}),
    },
    "expression": {
        "arithmetic": _generator("TOTAL", {"expression": "AMOUNT * 2 + 1", "decimals": 2},
                                 row={"AMOUNT": Decimal("12.25")}),
    },
    "reference": {
        "hash": _generator("MEME_CK", {"parent_table": "dbo.MEMBER", "parent_column": "MEME_CK"},
                           parent_keys={"dbo.MEMBER": [101, 202, 303, 404, 505]}),
        "stated": _generator("DX", {"parent_table": "dbo.DX", "parent_column": "CODE",
                                    "keys": ["E119", "I10", "J45"], "weights": [5, 3, 1]}),
    },
    "rule reading": {
        "methods": lambda fn: [fn(rule, field, None) for field, rule in [
            ("PLAN", {"gen_method": "constant", "domain": "HMO01"}),
            ("ID", {"gen_method": "sequential", "start_value": 5, "length": 8, "prefix": "M"}),
            ("NUM", {"gen_method": "random", "length": 7}),
            ("SEX", {"gen_method": "lookup", "domain": "F, M", "weights": "3, 1"}),
            ("CHARGE", {"gen_method": "range", "min_value": "10", "max_value": "99.5"}),
            ("UNITS", {"gen_method": "range", "min_value": 1, "max_value": 4,
                       "data_type": "int"}),
            ("CODE", {"gen_method": "pattern", "format_pattern": r"[A-Z]\d{3}"}),
            ("TEXT", {"gen_method": "pattern", "length": 5}),
            ("DOB", {"gen_method": "date", "min_value": -100, "max_value": 0,
                     "format_pattern": "2026-01-01"}),
            ("EMAIL", {"gen_method": "formatted", "domain": "example.com"}),
            ("TOTAL", {"gen_method": "formatted", "format_pattern": "sum:charge"}),
            ("NOTHING", {"gen_method": "default"}),
        ]],
    },
    "rule finish": {
        "decorated": lambda fn: [fn(rule, value) for rule, value in [
            ({"prefix": "M", "postfix": "-1"}, "12345"),
            ({"length": 4}, "ABCDEFG"),
            ({"length": 4, "data_type": "int"}, "123456"),
            ({"data_type": "varchar(3)"}, "WXYZ"),
            ({}, ""),
        ]],
    },
    "claims per member": {
        "a share": lambda fn: [fn(SEED, place, 60.0, 1, 3) for place in range(1, 13)],
        "all": lambda fn: [fn(SEED, place, 100.0, 2, 2) for place in range(1, 13)],
        "a mix": (lambda fn: [fn(SEED, place, 80.0, 1, 1, ((1, 60.0), (2, 30.0), (4, 10.0)))
                               for place in range(1, 13)], 2),
    },
    "lines per claim": {
        "exactly": lambda fn: [fn(SEED, claim, 3, 3) for claim in range(1, 13)],
        "range": lambda fn: [fn(SEED, claim, 1, 5) for claim in range(1, 13)],
        "a mix": lambda fn: [fn(SEED, claim, 1, 1, ((1, 20.0), (2, 50.0), (6, 30.0)))
                             for claim in range(1, 13)],
    },
    "adjudication": {
        "paid and denied": lambda fn: [
            fn(lines, None, SEED) for lines in CLAIM_LINES],
        "own terms": lambda fn: [
            fn(lines, {"denied_statuses": ["11"], "copays": [10]}, SEED) for lines in CLAIM_LINES],
    },
    "shared loops": {
        "pairs": lambda fn: [fn(previous, record, keys) for previous, record, keys in LOOP_PAIRS],
    },
    "members": {
        "families": lambda fn: fn(12, FAMILY_RULES, SEED, "2026-06-30"),
        "no relationship rule": lambda fn: fn(3, {}, SEED, "2026-06-30"),
    },
    "occurrences": {
        "a mix": (lambda fn: [
            fn(SEED, sample_engine.Vary(at="NK1", present=75, mix=((0, 10.0), (1, 50.0), (3, 40.0))),
               row, 0, 1) for row in ROWS], 2),
        "present and range": lambda fn: [
            fn(SEED, sample_engine.Vary(at="Patient telecom[*]", present=70, low=1, high=3),
               row, 0, 2) for row in ROWS],
        "as the sample": lambda fn: [
            fn(SEED, sample_engine.Vary(at="Patient address[*]", present=80, low=None,
                                        high=None), row, 1, 2) for row in ROWS],
    },
}


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return f"Decimal:{value}"
    if isinstance(value, (datetime, date)):
        return f"{type(value).__name__}:{value.isoformat()}"
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


# A case only a later version takes (a mix, from version 2), as (make, since).

def _made(name: str, version: int) -> dict[str, Any]:
    fn = base._VERSIONS[name][version]
    out = {}
    for case, make in CASES[name].items():
        make, since = make if isinstance(make, tuple) else (make, 1)
        if version >= since:
            out[case] = _plain(make(fn))
    return out


def _recorded() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


def _every_version() -> list[tuple[str, int]]:
    return [(name, version) for name, held in sorted(base._VERSIONS.items())
            for version in sorted(held)]


def test_the_seed_engine_never_changes():
    """Every value of every version is drawn from this; it has one version, forever."""
    recorded = _recorded().get("seed engine")
    made = [value_seed("demo-seed-001", "file", field, row)
            for field in ("MEMBER_ID", "DOB") for row in range(5)]
    if recorded is None:
        pytest.skip("not recorded yet")
    assert made == recorded


def test_every_algorithm_has_cases():
    assert sorted(set(base._VERSIONS) - set(CASES)) == [], "add cases for the new algorithm"


@pytest.mark.parametrize("name,version", _every_version(),
                         ids=[f"{n} v{v}" for n, v in _every_version()])
def test_a_version_makes_what_it_always_made(name, version):
    recorded = _recorded().get(name, {}).get(str(version))
    if recorded is None:
        pytest.fail(f"{name} version {version} has no recorded values: run with "
                    "SDM_RECORD_ALGORITHMS=1 to record them (it adds, never rewrites)")
    assert _made(name, version) == recorded, (
        f"{name} version {version} no longer makes what it made. Keep it as it was and "
        f"register the change as version {max(base._VERSIONS[name]) + 1}.")


def test_a_file_written_again_uses_the_version_it_was_made_with():
    """Pinned to version 1, an algorithm with a version 2 still makes version 1's values."""
    calls: list[int] = []
    base.register_algorithm("golden probe", 1, lambda: calls.append(1) or "one")
    base.register_algorithm("golden probe", 2, lambda: calls.append(2) or "two")
    try:
        assert base.algorithm("golden probe")() == "two"
        with base.pinned({"golden probe": 1}):
            assert base.algorithm("golden probe")() == "one"
            assert base.algorithm("pattern") is base._VERSIONS["pattern"][1]
        with base.pinned({"golden probe": 3}), pytest.raises(base.GenerationError,
                                                               match="newer SDM"):
            base.algorithm("golden probe")
        assert base.algorithm_versions()["golden probe"] == 2
    finally:
        del base._VERSIONS["golden probe"]


if os.environ.get("SDM_RECORD_ALGORITHMS") == "1":  # pragma: no cover - a maintainer's step
    held = _recorded()
    held.setdefault("seed engine", [value_seed("demo-seed-001", "file", field, row)
                                    for field in ("MEMBER_ID", "DOB") for row in range(5)])
    for _name, _version in _every_version():
        held.setdefault(_name, {}).setdefault(str(_version), _made(_name, _version))
    GOLDEN.write_text(json.dumps(held, indent=1, sort_keys=True) + "\n", encoding="utf-8")

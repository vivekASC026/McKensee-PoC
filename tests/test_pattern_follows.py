"""Generation follows the pattern: the same plans, products and groups in the
same shares, ages and charges spread as the pattern's are.

From customer feedback: SDM is expected to create data based on the pattern's
parameters (same plan, product, group, demographics). A pattern carries its
enrollment mix (group, plan and product as whole records with shares) and the
deciles of each number; generation draws from those, in files and in database
plans alike.
"""

from __future__ import annotations

import collections
import csv
import io
from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sdg.analyzer.bridge import _slices, to_pattern
from sdg.api.app import app
from sdg.engine.generators import RowContext, get_generator

client = TestClient(app)


# ---------------------------------------------------------------------------
# The pieces, without a database
# ---------------------------------------------------------------------------

def test_deciles_become_ten_slices_of_equal_share():
    stats = {"min": 10, "max": 60, "deciles": [10, 20, 22, 24, 26, 28, 30, 35, 40, 50, 60]}
    slices = _slices(stats, 10, 60)
    assert len(slices) == 10 and slices[0] == [10, 20, 0.1] and slices[-1] == [50, 60, 0.1]
    assert _slices({"min": 1, "max": 2}, 1, 2) is None, "an old pattern keeps its band"
    found = to_pattern({"continuous": {"_age": stats}})
    assert found.parameters[0].bands == slices


def test_enrollment_crosses_as_whole_records_with_shares():
    found = to_pattern({"enrollment": [
        {"GROUP_KEY": "1", "COVERAGE_PLAN_ID": "P1", "PRODUCT_CODE": "X1", "share": 0.75},
        {"GROUP_KEY": "1", "COVERAGE_PLAN_ID": "P2", "PRODUCT_CODE": "X2", "share": 0.25},
    ]})
    by_field = {p.record_field: p for p in found.parameters}
    assert set(by_field) == {"GROUP_KEY", "COVERAGE_PLAN_ID", "PRODUCT_CODE"}
    assert all(p.record_group == "enrollment" and p.weights == [0.75, 0.25]
               for p in by_field.values())


def _draws(generator: str, params: dict, n: int = 4000) -> list:
    make = get_generator(generator)
    return [make(RowContext(seed="t", scenario="s", table="t", field_name="f",
                            params=params, row_index=i, row={})) for i in range(n)]


def test_a_number_is_drawn_slice_by_slice():
    bands = [[0, 10, 0.8], [100, 200, 0.2]]
    values = [float(v) for v in _draws("number", {"distribution": "uniform", "min": 0,
                                                  "max": 200, "decimals": 2,
                                                  "bands": bands})]
    low = sum(v <= 10 for v in values) / len(values)
    assert all(v <= 10 or 100 <= v <= 200 for v in values), "nothing between the slices"
    assert low == pytest.approx(0.8, abs=0.03)


def test_a_date_is_drawn_slice_by_slice_inside_its_window():
    params = {"anchor": "2026-06-30", "min_days": -3650, "max_days": 0,
              "bands": [[-3650, -3000, 0.5], [-100, 0, 0.5]]}
    days = [(d - date(2026, 6, 30)).days for d in _draws("date_relative", params)]
    assert all(-3650 <= g <= -3000 or -100 <= g <= 0 for g in days)
    assert sum(g >= -100 for g in days) / len(days) == pytest.approx(0.5, abs=0.03)


# ---------------------------------------------------------------------------
# Against the store: a file and a database plan from a real pattern
# ---------------------------------------------------------------------------

def _pattern():
    analysis = client.post("/api/patterns/analyze",
                           json={"filters": {"grgr_ck": [1]}, "k": 3}).json()
    return max(analysis["patterns"], key=lambda p: p["size"])


@pytest.mark.db
def test_members_written_from_a_pattern_have_its_ages_plans_and_products():
    pattern = _pattern()
    assert pattern["enrollment"] and pattern["continuous"]["_age"]["deciles"]
    response = client.post("/api/export", json={
        "format": "csv", "subject": "members", "members": 3000,
        "pattern": {"profile": pattern, "domain": "members", "filters": {"grgr_ck": [1]},
                    "k": 3, "cluster_id": pattern["cluster_id"]}})
    assert response.status_code == 200, response.text[:500]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 3000

    # Ages spread as the pattern's: its deciles, within a year (the analysis
    # measures age on 2026-01-01, a file is written as of 2026-06-30).
    ages = [(date(2026, 6, 30) - date.fromisoformat(r["DOB"][:10])).days / 365.25
            for r in rows]
    made = np.quantile(ages, [i / 10 for i in range(11)])
    for want, got in zip(pattern["continuous"]["_age"]["deciles"], made):
        assert abs(want - got) <= 1.0, (want, got)

    # Plan and product drawn together, in the pattern's shares.
    pairs = {(e["COVERAGE_PLAN_ID"], e["PRODUCT_CODE"]): e["share"]
             for e in pattern["enrollment"]}
    seen = collections.Counter((r["COVERAGE_PLAN_ID"], r["PRODUCT_CODE"]) for r in rows)
    assert set(seen) <= set(pairs), "never a plan with another plan's product"
    for pair, share in pairs.items():
        assert seen[pair] / len(rows) == pytest.approx(share, abs=0.025), pair


@pytest.mark.db
def test_a_database_plan_from_a_pattern_carries_its_spread_and_enrollment():
    response = client.post("/api/patterns/to-plan", json={
        "pattern": _pattern(), "kind": "member", "as_of": "2026-06-30"})
    assert response.status_code == 200, response.text
    targets = {t["column"]: t for t in response.json()["resolution"]["targets"]}
    birth = targets["MEME_BIRTH_DT"]["params"]
    assert len(birth["bands"]) == 10
    assert all(birth["min_days"] - 400 <= b[0] <= b[1] <= birth["max_days"] + 400
               for b in birth["bands"])
    # Enrollment is the member's family's: held on the subscriber above it, and
    # never drawn again on the claims below it.
    tables = {t["table"] for t in response.json()["resolution"]["targets"]}
    assert tables == {"dbo.CMC_MEME_MEMBER", "dbo.CMC_SBSB_SUBSC"}, tables


@pytest.mark.db
def test_claims_written_from_a_claims_pattern_have_its_charges():
    analysis = client.post("/api/claims/analyze", json={"grgr_ck": [1], "k": 2}).json()
    pattern = max(analysis["patterns"], key=lambda p: p["size"])
    deciles = pattern["continuous"]["charge_amount"]["deciles"]
    response = client.post("/api/export", json={
        "format": "csv", "subject": "claims", "members": 300,
        "pattern": {"profile": pattern, "domain": "claims", "filters": {"grgr_ck": [1]},
                    "k": 2, "cluster_id": pattern["cluster_id"]}})
    assert response.status_code == 200, response.text[:500]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    column = next(c for c in rows[0] if c.upper() in ("CHARGE_AMOUNT", "LINE_CHARGE_AMOUNT"))
    charges = [float(r[column]) for r in rows if r[column]]
    assert len(charges) > 200
    made = np.quantile(charges, [0.1, 0.5, 0.9])
    for want, got in zip((deciles[1], deciles[5], deciles[9]), made):
        assert abs(want - got) <= max(0.15 * want, 5.0), (want, got)

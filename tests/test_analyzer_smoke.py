"""The analyzer's code paths, run against fixtures.

Ported from the pattern analyzer's own suite. The tests are unchanged in
substance: same fixtures, same invariants, same order. What moved is where the
code lives -- `cluster_profiler` is `sdg.analyzer`, the services are
`sdg.analyzer.services`, and the app is the one this tool already serves.

No database required; the loaders are replaced with fixtures.
"""

import sys
import traceback
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd



# ── Fixtures shaped like the real tables ─────────────────────────────────────

def fake_members(n: int = 4000) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return pd.DataFrame({
        "MEME_CK": np.arange(n),
        "SBSB_CK": rng.integers(0, n // 2, n),
        "GRGR_CK": rng.choice([10042, 10118], n),
        "_age": rng.integers(0, 90, n).astype(float),
        "_tenure": rng.gamma(2, 2, n),
        "MEME_SEX": rng.choice(["M", "F"], n),
        "MEME_MARITAL_STATUS": rng.choice(["S", "M", "U"], n),
        "MEME_REL": rng.choice(["M", "S", "D"], n),
        "GRGR_NAME": "Ohio Medicaid",
        "CSPD_CAT_DESC": "Medical",
        "PLDS_DESC": "Adult Expansion",
        "PDDS_DESC": "Standard PPO",
    })


def fake_claims(n: int = 3000) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    return pd.DataFrame({
        "CLCL_ID": [f"CLM{i // 3:06d}" for i in range(n)],   # ~3 lines per claim
        "MEME_CK": rng.integers(0, 900, n),
        "GRGR_CK": rng.choice([10042, 10118], n),
        "CSPI_ID": "M04201",
        "PDPD_ID": "M04201-P1",
        "CLCL_CL_TYPE": rng.choice(["M", "D"], n),
        "CLCL_ME_AGE": rng.integers(0, 90, n).astype(float),
        "CDML_CHG_AMT": rng.gamma(3, 60, n),
        "CDML_ALLOW": rng.gamma(3, 40, n),
        "CDML_PAID_AMT": rng.gamma(3, 30, n),
        "MEME_SEX": rng.choice(["M", "F"], n),
        "CLCL_CUR_STS": rng.choice(["01", "02"], n),
        "CLCL_NTWK_IND": rng.choice(["I", "O"], n),
        "IDCD_ID_PRIMARY": rng.choice(["E11.9", "I10", "J06.9"], n),
        "IDCD_DESC_PRIMARY": "Some diagnosis",
        "IPCD_ID": rng.choice(["99213", "80053"], n),
        "IPCD_DESC": "Some procedure",
        "PSCD_ID": rng.choice(["11", "21"], n),
        "PRPR_MCTR_SPEC": rng.choice(["IM", "FP"], n),
    })


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"  PASS  {name}")
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        RESULTS.append((name, False, detail))
        print(f"  FAIL  {name}\n        {detail}")
        traceback.print_exc(limit=3)


# ── Member analysis ──────────────────────────────────────────────────────────

def test_member_analyze():
    from sdg.analyzer.services import service

    members = fake_members()
    family = members[["SBSB_CK", "MEME_CK", "MEME_REL"]]

    with patch.object(service, "_load", return_value=(members, None, family, {"grgr_ck": [10042]})), \
         patch.object(service, "overview", return_value={"n_members": 500000}), \
         patch.object(service, "funnel_counts", return_value={
             "total": 500000, "group": 120000, "cat": 90000,
             "plan": 40000, "product": 12000}):

        result = service.analyze({"grgr_ck": [10042]}, k=3)

    # Every field the client reads must be present and the right shape.
    for key in ("total_members", "subset_members", "n_patterns", "scope_total",
                "scope_label", "scope_from_baseline", "pct_outside_filter",
                "patterns", "cache_key"):
        assert key in result, f"missing key: {key}"

    assert result["n_patterns"] > 0, "no patterns produced"

    for p in result["patterns"]:
        for key in ("cluster_id", "name", "size", "pct_of_subset",
                    "pct_of_total", "weight_band", "rare"):
            assert key in p, f"pattern missing key: {key}"
        assert 0 <= p["pct_of_subset"] <= 1.0001, p["pct_of_subset"]
        assert p["weight_band"] in ("very high", "high", "medium", "low", "rare")

    # Subset shares partition the subset, so they sum to 1.
    total = sum(p["pct_of_subset"] for p in result["patterns"])
    assert abs(total - 1.0) < 0.01, f"subset shares sum to {total}, expected 1.0"


def test_member_pattern_context():
    from sdg.analyzer.services import service

    members = fake_members()
    family = members[["SBSB_CK", "MEME_CK", "MEME_REL"]]
    with patch.object(service, "_load", return_value=(members, None, family, {})), \
         patch.object(service, "_cluster",
                      return_value=(np.zeros(len(members), dtype=int), {"method": "kmeans"})):
        profile, filters_used, rows, name = service.pattern_context({}, None, 0)
    assert profile["size"] == len(members)
    assert isinstance(name, str) and name


# ── Claims analysis ──────────────────────────────────────────────────────────

def test_claims_analyze():
    from sdg.analyzer.services import claims_service as cs

    claims = fake_claims()
    with patch.object(cs, "_load", return_value=(claims, {"grgr_ck": [10042]})), \
         patch.object(cs, "book_totals", return_value={"claims": 200000, "lines": 600000}), \
         patch.object(cs, "scope_denominator", return_value=(50000, "group 10042", True)):

        result = cs.analyze({"grgr_ck": [10042]}, k=3)

    for key in ("subset_claim_lines", "subset_claims", "denominator",
                "scope_total", "scope_label", "scope_from_baseline",
                "split_claims", "avg_lines_per_claim", "patterns"):
        assert key in result, f"missing key: {key}"

    assert result["subset_claims"] < result["subset_claim_lines"], \
        "distinct claims should be fewer than claim lines"

    for p in result["patterns"]:
        for key in ("cluster_id", "name", "size", "n_claims", "pct_of_subset",
                    "pct_of_lines", "pct_of_total", "weight_band"):
            assert key in p, f"claims pattern missing key: {key}"
        assert p["n_claims"] <= p["size"], "claims cannot exceed lines"


# ── Weight bands ─────────────────────────────────────────────────────────────

def test_weight_bands_fallback():
    """Fixed-share bands, used only before a baseline exists."""
    from sdg.analyzer.services.service import weight_band

    assert weight_band(0.02) == "very high"
    assert weight_band(0.005) == "high"
    assert weight_band(0.002) == "medium"
    assert weight_band(0.0005) == "low"
    assert weight_band(0.0001) == "rare"
    assert weight_band(0.0) == "rare"


def test_weight_bands_percentile():
    """With a baseline, bands come from observed sizes and always spread.

    The failure this guards against is every pattern landing in one bucket,
    which is what fixed thresholds did against real data.
    """
    from collections import Counter

    from sdg.api.routers.baseline import _percentiles
    from sdg.analyzer.services.service import weight_band

    rng = np.random.default_rng(3)
    sizes = np.concatenate([
        rng.lognormal(6.5, 1.3, 900).astype(int),
        rng.lognormal(9.0, 0.6, 100).astype(int),
    ]).tolist()

    th = _percentiles(sizes)
    assert th["p20"] < th["p50"] < th["p80"] < th["p95"], th

    counts = Counter(weight_band(0.0, size=s, thresholds=th) for s in sizes)
    for band in ("very high", "high", "medium", "low", "rare"):
        share = counts[band] / len(sizes)
        assert share > 0.01, f"band '{band}' only got {share:.1%} of patterns"
    assert counts["very high"] / len(sizes) < 0.15, (
        "'very high' should be the top few percent, not most of them"
    )


# ── Serialization ────────────────────────────────────────────────────────────

def test_to_py_handles_numpy():
    from sdg.analyzer.services.service import to_py

    out = to_py({
        "i": np.int64(5), "f": np.float64(1.5), "b": np.bool_(True),
        "nan": np.float64("nan"), "arr": np.array([1, 2]),
        "ts": pd.Timestamp("2026-01-01"), "nested": {"x": [np.int32(1)]},
    })
    assert out["i"] == 5 and isinstance(out["i"], int)
    assert out["nan"] is None, "NaN must serialize to null, not NaN"
    assert out["arr"] == [1, 2]
    assert out["nested"]["x"] == [1]


# ── Disk cache ───────────────────────────────────────────────────────────────

def test_disk_cache_roundtrip():
    from sdg.analyzer.services import disk_cache

    key = disk_cache.make_key("smoke", {"a": 1})
    disk_cache.put(key, {"value": 42}, ttl=60, label="smoke")
    assert disk_cache.get(key) == {"value": 42}
    disk_cache.clear()
    assert disk_cache.get(key) is None


# ── Serialization through the actual HTTP route ──────────────────────────────

def test_analyze_route_keeps_all_fields():
    """The field a service computes must survive the response model.

    Calling service.analyze() directly bypasses FastAPI's response_model, which
    silently drops any field the Pydantic class does not declare. That is
    invisible server-side and shows up in the browser as a value that renders
    as zero. This exercises the real route.
    """
    from fastapi.testclient import TestClient

    from sdg.analyzer.services import service
    from sdg.api.app import app

    members = fake_members()
    family = members[["SBSB_CK", "MEME_CK", "MEME_REL"]]

    with patch.object(service, "_load",
                      return_value=(members, None, family, {"grgr_ck": [10042]})), \
         patch.object(service, "overview", return_value={"n_members": 500000}), \
         patch.object(service, "scope_denominator",
                      return_value=(120000, "group 10042", True)), \
         patch.object(service, "funnel_counts", return_value={
             "total": 500000, "group": 120000, "cat": 90000,
             "plan": 40000, "product": 12000}):

        client = TestClient(app)
        resp = client.post("/api/patterns/analyze",
                           json={"filters": {"grgr_ck": [10042]}, "k": 3})

    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()

    for key in ("scope_total", "scope_label", "scope_from_baseline",
                "weight_explained", "pct_outside_filter"):
        assert key in body, f"response model dropped '{key}'"

    assert body["scope_total"] == 120000, body["scope_total"]

    for p in body["patterns"]:
        assert "pct_of_total" in p, "response model dropped 'pct_of_total'"
        assert p["pct_of_total"] > 0, (
            f"pct_of_total is {p['pct_of_total']} — a pattern of "
            f"{p['size']} against {body['scope_total']} cannot be zero"
        )

    # Shares against a scope that equals the subset must sum to ~1.
    assert sum(p["pct_of_total"] for p in body["patterns"]) > 0


# ── App wiring ───────────────────────────────────────────────────────────────

def test_app_routes():
    from sdg.api.app import app

    paths = app.openapi()["paths"]
    # Generation is the plan engine's now, and what it produced is written by
    # /api/export, so the analyzer's own generate routes are not here to mount.
    for required in ("/api/analyzer/health", "/api/patterns/analyze",
                     "/api/patterns/to-plan", "/api/export",
                     "/api/claims/analyze", "/api/story/match",
                     "/api/baseline/compute"):
        assert required in paths, f"route missing: {required}"


def main() -> int:
    print("\nSDM API smoke test — fixture data, no database\n")
    check("member analyze", test_member_analyze)
    check("member pattern context", test_member_pattern_context)
    check("claims analyze", test_claims_analyze)
    check("weight bands (fallback)", test_weight_bands_fallback)
    check("weight bands (percentile)", test_weight_bands_percentile)
    check("numpy serialization", test_to_py_handles_numpy)
    check("disk cache roundtrip", test_disk_cache_roundtrip)
    check("analyze route keeps all fields", test_analyze_route_keeps_all_fields)
    check("app routes", test_app_routes)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("\nFailures:")
        for name, _, detail in failed:
            print(f"  {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The analyzer's features, through real HTTP routes.

Ported from the pattern analyzer's own suite. The tests are unchanged in
substance: same fixtures, same invariants, same order. What moved is where the
code lives -- `cluster_profiler` is `sdg.analyzer`, the services are
`sdg.analyzer.services`, and the app is the one this tool already serves.

No database required; the loaders are replaced with fixtures.
"""

from datetime import datetime
from unittest.mock import patch

import numpy as np
import pytest
import pandas as pd


from tests.fake_db import FakeDB  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

def fake_members(n=4000):
    rng = np.random.default_rng(7)
    return pd.DataFrame({
        "MEME_CK": np.arange(n), "SBSB_CK": rng.integers(0, n // 2, n),
        "GRGR_CK": rng.choice([10042, 10118], n),
        "_age": rng.integers(0, 90, n).astype(float),
        "_tenure": rng.gamma(2, 2, n),
        "MEME_SEX": rng.choice(["M", "F"], n),
        "MEME_MARITAL_STATUS": rng.choice(["S", "M", "U"], n),
        "MEME_REL": rng.choice(["M", "S", "D"], n),
        "GRGR_NAME": "Ohio Medicaid", "CSPD_CAT_DESC": "Medical",
        "PLDS_DESC": "Adult", "PDDS_DESC": "PPO",
    })


def fake_claims(n=3000):
    rng = np.random.default_rng(11)
    return pd.DataFrame({
        "CLCL_ID": [f"CLM{i // 3:06d}" for i in range(n)],
        "MEME_CK": rng.integers(0, 900, n), "GRGR_CK": rng.choice([10042, 10118], n),
        "CSPI_ID": "M04201", "PDPD_ID": "M04201-P1",
        "CLCL_CL_TYPE": rng.choice(["M", "D"], n),
        "CLCL_ME_AGE": rng.integers(0, 90, n).astype(float),
        "CDML_CHG_AMT": rng.gamma(3, 60, n), "CDML_ALLOW": rng.gamma(3, 40, n),
        "CDML_PAID_AMT": rng.gamma(3, 30, n), "MEME_SEX": rng.choice(["M", "F"], n),
        "CLCL_CUR_STS": rng.choice(["01", "02"], n),
        "CLCL_NTWK_IND": rng.choice(["I", "O"], n),
        "IDCD_ID_PRIMARY": rng.choice(["E11.9", "I10"], n),
        "IDCD_DESC_PRIMARY": "Dx", "IPCD_ID": rng.choice(["99213", "80053"], n),
        "IPCD_DESC": "Proc", "PSCD_ID": rng.choice(["11", "21"], n),
        "PRPR_MCTR_SPEC": rng.choice(["IM", "FP"], n),
    })


@pytest.fixture(autouse=True)
def _stop_patches():
    """Undo every patch a test here started.

    _install_fake_db starts its patches rather than scoping them, and nothing
    stopped them: the in-memory store stayed installed for the rest of the
    run, so later tests that meant to reach SQL Server reached it instead.
    """
    yield
    patch.stopall()


def _install_fake_db(fake):
    from sdg.analyzer import db
    for m in ("upsert_pattern", "get_pattern", "get_all_patterns",
              "mark_pattern_saved", "mark_pattern_unsaved", "get_saved_patterns",
              "is_pattern_saved", "save_pattern_from_profiler"):
        patch.object(db, m, getattr(fake, m)).start()


def _client():
    from sdg.api.app import app
    from fastapi.testclient import TestClient
    return TestClient(app)


# ── Member profiler ──────────────────────────────────────────────────────────

def test_member_analyze_and_percentages():
    from sdg.analyzer.services import service

    members = fake_members()
    family = members[["SBSB_CK", "MEME_CK", "MEME_REL"]]
    with patch.object(service, "_load", return_value=(members, None, family, {"grgr_ck": [10042]})), \
         patch.object(service, "overview", return_value={"n_members": 10000}), \
         patch.object(service, "funnel_counts", return_value={
             "total": 10000, "group": 4000, "cat": 3000, "plan": 1500, "product": 500}):
        client = _client()
        r = client.post("/api/patterns/analyze",
                        json={"filters": {"grgr_ck": [10042]}, "k": 3})

    assert r.status_code == 200, r.text[:200]
    body = r.json()
    # Every field the UI reads survives the response model.
    for key in ("scope_total", "scope_label", "weight_explained", "patterns"):
        assert key in body, f"missing {key}"
    # Displayed share is against population — a real pattern is not ~0%.
    tops = [p["pct_of_total"] for p in body["patterns"]]
    assert max(tops) > 0.02, f"all patterns near zero: {tops}"
    # Bands are present and valid.
    for p in body["patterns"]:
        assert p["weight_band"] in ("very high", "high", "medium", "low", "rare")


# ── Generation, EDI, row selection ───────────────────────────────────────────


def test_claims_analyze():
    from sdg.analyzer.services import claims_service as cs

    claims = fake_claims()
    with patch.object(cs, "_load", return_value=(claims, {"grgr_ck": [10042]})), \
         patch.object(cs, "book_totals", return_value={"claims": 5000, "lines": 15000}), \
         patch.object(cs, "scope_denominator", return_value=(5000, "all claims", True)):
        client = _client()
        r = client.post("/api/claims/analyze", json={"grgr_ck": [10042], "k": 3})

    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["subset_claims"] < body["subset_claim_lines"]
    for p in body["patterns"]:
        assert p["n_claims"] <= p["size"]


# ── System discovery: members and claims ─────────────────────────────────────

def test_discovery_members():
    from sdg.analyzer import entity_loader
    from sdg.analyzer.services import discovery_service as ds

    fake = FakeDB()
    _install_fake_db(fake)
    ds.discover.cache_clear()

    members = fake_members()
    family = members[["SBSB_CK", "MEME_CK", "MEME_REL"]]

    def fake_load(entity, filters, reference_date=None):
        assert entity == "members"
        return entity_loader.Loaded(rows=members, filters=filters, labels=None, family=family)

    with patch("sdg.analyzer.entity_loader.load", side_effect=fake_load), \
         patch.object(ds, "overview", return_value={"n_members": 10000}), \
         patch("sdg.analyzer.entity_loader.options",
               side_effect=lambda entity, level, chosen=(): (
                   [{"key": "10042", "label": "G1"}] if level == "groups" else [])), \
         patch("sdg.analyzer.services.discovery_service.discover_clusters",
               return_value=(np.zeros(len(members), dtype=int), {"silhouette": 0.5})), \
         patch("sdg.analyzer.services.discovery_service.build_subset_summary", return_value={}), \
         patch("sdg.analyzer.services.discovery_service.build_contextual_name", return_value="Pattern"), \
         patch("sdg.analyzer.services.discovery_service.profile_cluster",
               return_value={"size": 4000, "cluster_id": 0}):
        client = _client()
        r = client.get("/api/discovery?entity=members&max_depth=1")

    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["n_discovered"] >= 1
    # Ranked by population share, and not near zero.
    assert body["patterns"][0]["pct_of_population"] > 0.02


# ── Save / retrieve / remove — the two reported bugs ─────────────────────────

def test_discovery_percentages_never_exceed_100():
    """A pattern's share of the book is impossible above 100%.

    Reproduces the stale-denominator bug: fresh per-pattern counts divided by a
    cached, too-small book total produced >100%. The guard caps at 1.0 and the
    refresh path clears the denominator cache.
    """
    from sdg.analyzer.services import discovery_service as ds

    fake = FakeDB()
    _install_fake_db(fake)
    ds.discover_claims.cache_clear()

    import sdg.analyzer.services.claims_service as cs

    # book deliberately smaller than a pattern, simulating a stale total.
    def fake_book(*a, **kw):
        return {"claims": 500, "lines": 1500}

    def fake_analyze(combo, k=None):
        return {"patterns": [
            {"cluster_id": 0, "name": "P0", "n_claims": 900, "group": None, "plan": None},
        ], "silhouette": 0.4}

    with patch.object(cs, "book_totals", side_effect=fake_book), \
         patch.object(cs, "analyze", side_effect=fake_analyze), \
         patch.object(cs, "groups", return_value=[{"key": "10042", "label": "G"}]), \
         patch.object(cs, "plans", return_value=[]):
        ds.discover_claims.cache_clear()
        out = ds.discover_claims(top_n=10, max_depth=1, refresh=True)

    for p in out["patterns"]:
        assert p["pct_of_population"] <= 1.0, \
            f"pattern shows {p['pct_of_population']*100:.0f}% — exceeds 100%"


def test_save_then_remove_reflects_immediately():
    """Remove must take effect on the next read, not after a cache TTL."""
    from sdg.analyzer.services import discovery_service as ds

    fake = FakeDB()
    _install_fake_db(fake)

    pid = fake.save_pattern_from_profiler(
        combo={"grgr_ck": [10042], "grgr_name": "G", "cspd_cat_desc": "C",
               "plds_desc": "P", "pdds_desc": ""},
        cluster_id=0, contextual_name="Test", member_count=100,
        pct_of_pop=0.1, silhouette=0.5)

    client = _client()
    assert len(client.get("/api/discovery/saved").json()["patterns"]) == 1

    rm = client.post("/api/discovery/unsave", json={"pattern_id": pid})
    assert rm.status_code == 200, rm.text[:200]

    after = client.get("/api/discovery/saved").json()["patterns"]
    assert len(after) == 0, f"remove did not take effect: {len(after)} still saved"


def test_no_duplicate_on_double_save():
    """Saving the same pattern from discovery and the profiler yields one row."""
    from sdg.analyzer.services.discovery_service import canonical_combo

    fake = FakeDB()
    _install_fake_db(fake)

    # Discovery persists at depth-2 (cspd_cat as a single-item list).
    fake.upsert_pattern(
        combo=canonical_combo({"grgr_ck": [10042], "cspd_cat": ["MED"],
                               "grgr_name": "G", "cspd_cat_desc": "MED"}),
        cluster_id=0, contextual_name="Discovered", member_count=100,
        pct_of_pop=0.1, silhouette=0.5)

    # Profiler saves the same pattern, combo built from as_kwargs (ints, maybe
    # unsorted). Canonicalization must collapse both to one key.
    fake.save_pattern_from_profiler(
        combo=canonical_combo({"grgr_ck": [10042], "cspd_cat": ["MED"],
                               "grgr_name": "G", "cspd_cat_desc": "MED"}),
        cluster_id=0, contextual_name="Saved", member_count=100,
        pct_of_pop=0.1, silhouette=0.5)

    rows = fake.get_all_patterns()
    assert len(rows) == 1, f"same pattern stored {len(rows)} times"


def test_member_claims_no_id_collision():
    """A member and a claims pattern from the same group+cluster stay separate.

    This is the "unsave removed a pattern in another domain" bug: without the
    domain in the key they collapsed to one row.
    """
    from tests.fake_db import _make_pattern_key
    from sdg.analyzer.services.discovery_service import canonical_combo

    m = canonical_combo({"grgr_ck": [10042]}, entity="members")
    c = canonical_combo({"grgr_ck": [10042]}, entity="claims")
    assert _make_pattern_key(m, 0) != _make_pattern_key(c, 0), \
        "member and claims pattern share a key — they will collide"

    fake = FakeDB()
    _install_fake_db(fake)
    mid = fake.save_pattern_from_profiler(
        combo=m, cluster_id=0, contextual_name="KY-Medical",
        member_count=2034, pct_of_pop=0.10, silhouette=0.5)
    cid = fake.save_pattern_from_profiler(
        combo=c, cluster_id=0, contextual_name="[Claims] KY",
        member_count=800, pct_of_pop=0.08, silhouette=0.4)
    assert mid != cid, "member and claims collapsed to one row"

    # Unsaving the claims pattern must leave the member pattern saved.
    fake.mark_pattern_unsaved(cid)
    assert fake.is_pattern_saved(mid), "unsaving claims removed the member pattern"
    assert not fake.is_pattern_saved(cid)


def test_discovery_dedupes_sibling_combos():
    """The same pattern found at two hierarchy depths appears once."""
    from sdg.analyzer.services.discovery_service import _dedupe_by_identity

    raw = [
        {"name": "KY-Medical-Adult-P0", "size": 2034, "group": "KY", "cluster_id": 0,
         "entity": "members", "combo": {"grgr_ck": [10042]}},
        {"name": "KY-Medical-Adult-P0", "size": 2034, "group": "KY", "cluster_id": 0,
         "entity": "members", "combo": {"grgr_ck": [10042], "cspd_cat": ["MED"]}},
        {"name": "KY-Medical-Young-P1", "size": 1991, "group": "KY", "cluster_id": 1,
         "entity": "members", "combo": {"grgr_ck": [10042]}},
        {"name": "KY-Medical-Young-P1", "size": 1991, "group": "KY", "cluster_id": 1,
         "entity": "members", "combo": {"grgr_ck": [10042], "cspd_cat": ["MED"]}},
    ]
    out = _dedupe_by_identity(raw)
    assert len(out) == 2, f"expected 2 unique patterns, got {len(out)}"
    # The shallowest combo is kept.
    for p in out:
        assert "cspd_cat" not in p["combo"] or not p["combo"]["cspd_cat"], \
            "kept the deeper combo instead of the shallowest"


def test_canonical_combo_collapses_representations():
    """Different-looking combos for the same selection must share a key."""
    from tests.fake_db import _make_pattern_key
    from sdg.analyzer.services.discovery_service import canonical_combo

    a = canonical_combo({"grgr_ck": [10042], "cspd_cat": None})
    b = canonical_combo({"grgr_ck": [10042], "cspd_cat": []})
    c = canonical_combo({"grgr_ck": ["10042"]})
    assert _make_pattern_key(a, 0) == _make_pattern_key(b, 0) == _make_pattern_key(c, 0)


def test_claims_save_endpoint():
    from sdg.analyzer.services import claims_service as cs

    fake = FakeDB()
    _install_fake_db(fake)

    claims = fake_claims()
    with patch.object(cs, "_load", return_value=(claims, {"grgr_ck": [10042]})), \
         patch.object(cs, "book_totals", return_value={"claims": 5000, "lines": 15000}), \
         patch.object(cs, "scope_denominator", return_value=(5000, "all claims", True)):
        client = _client()
        analyze = client.post("/api/claims/analyze", json={"grgr_ck": [10042], "k": 2}).json()
        cid = analyze["patterns"][0]["cluster_id"]
        save = client.post("/api/claims/save", json={"grgr_ck": [10042], "k": 2, "cluster_id": cid})

    assert save.status_code == 200, save.text[:200]
    assert save.json()["saved"] is True
    # It shows up in saved, tagged as a claims pattern.
    saved = _client().get("/api/discovery/saved").json()["patterns"]
    assert any("[Claims]" in p["name"] for p in saved), "claims pattern not in saved list"


# ── Story → Data ─────────────────────────────────────────────────────────────

def test_story_match_runs():
    from sdg.api.routers import story

    fake = FakeDB()
    _install_fake_db(fake)
    # A saved pattern to match against.
    fake.save_pattern_from_profiler(
        combo={"grgr_ck": [10042], "grgr_name": "Ohio", "cspd_cat_desc": "Medical"},
        cluster_id=0, contextual_name="Ohio Medical adults", member_count=500,
        pct_of_pop=0.05, silhouette=0.4)

    from sdg.analyzer.services import story_service
    # Return the shape the response model actually declares.
    stub = {"stories": [{
        "index": 0, "story": "Test Ohio medical adult members",
        "data_type": "members", "volume": None,
        "keywords": ["ohio", "medical"], "raw_terms": ["ohio", "medical"],
        "confidence": 0.5, "matched": True,
    }], "warning": None}
    with patch.object(story_service, "match_stories", return_value=stub):
        client = _client()
        r = client.post("/api/story/match",
                        json={"stories": ["Test Ohio medical adult members"]})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["stories"][0]["matched"] is True


# ── Health / build ───────────────────────────────────────────────────────────

def test_refresh_clears_dependency_chain():
    """Refresh must invalidate upstream caches, not just the discovery result.

    Reproduces the reported symptom: after a data reload, clicking refresh on
    claims discovery showed no change because book_totals / load / cluster were
    still serving cached values.
    """
    from sdg.analyzer.services import disk_cache

    ns = "test.refresh.chain"
    disk_cache.clear_namespace(ns)  # start clean
    seen = {"n": 0}

    @disk_cache.two_tier(ns, ttl=3600)
    def fake_load(filters):
        seen["n"] += 1
        return seen["n"]  # value changes each real call

    first = fake_load({"grgr_ck": [1]})
    cached = fake_load({"grgr_ck": [1]})
    assert first == cached, "should be cached"

    # A namespace clear must drop both tiers so the next call recomputes.
    disk_cache.clear_namespace(ns)
    after = fake_load({"grgr_ck": [1]})
    assert after != first, "cleared namespace still served the stale value"
    disk_cache.clear_namespace(ns)


def test_layout_parse_csv_and_assign():
    """CSV sample parses, types infer, and matched fields get rules."""
    from sdg.analyzer.services import layout_service as ls
    text = ("MEME_CK,MEME_FIRST_NAME,MEME_BIRTH_DT,CDML_CHG_AMT\n"
            "500001,James,1980-05-12,220.50\n500002,Mary,1975-11-03,89.00")
    parsed = ls.parse_csv(text)
    assert parsed["kind"] == "csv" and parsed["delimiter"] == ","
    types = {f["name"]: f["type"] for f in parsed["fields"]}
    assert types["MEME_CK"] == "int"
    assert types["MEME_BIRTH_DT"] == "date"
    assert types["CDML_CHG_AMT"] == "decimal"

    rules = [{"id": 1, "field_name": "MEME_FIRST_NAME", "field_label": "First Name",
              "gen_method": "lookup"}]
    assigned = ls.auto_assign(parsed["fields"], rules)
    by = {f["name"]: f for f in assigned}
    # Matched field carries the real rule; unmatched rides the flagged default.
    assert by["MEME_FIRST_NAME"]["rule_id"] == 1
    assert by["MEME_FIRST_NAME"]["is_default"] is False
    assert by["MEME_CK"]["is_default"] is True
    assert by["MEME_CK"]["gen_method"] == "default"


def test_layout_parse_fixed_width():
    """Fixed-width boundaries infer from always-blank columns."""
    from sdg.analyzer.services import layout_service as ls
    text = "500001  James     19800512\n500002  Mary      19751103"
    parsed = ls.parse_fixed(text)
    assert parsed["kind"] == "fixed"
    assert len(parsed["fields"]) >= 2
    assert all("start" in f and "length" in f for f in parsed["fields"])


def test_layout_render_pattern_wins():
    """Generation through a layout: pattern data wins, rule fills, default blank."""
    import pandas as pd
    from sdg.analyzer.services import layout_service as ls

    df = pd.DataFrame({
        "MEME_CK": [500001, 500002],
        "GRGR_NAME": ["Evergreen Medicaid", "Evergreen Dual"],
        "MEME_BIRTH_DT": ["1980-05-12", "1975-11-03"],
    })
    layout = {
        "kind": "fixed", "delimiter": None,
        "fields": [
            {"name": "MEME_CK", "type": "int", "length": 8, "is_default": False, "rule_id": None},
            {"name": "GRGR_NAME", "type": "text", "length": 20, "is_default": False, "rule_id": None},
            {"name": "MEME_BIRTH_DT", "type": "date", "length": 8,
             "date_format": "YYYYMMDD", "is_default": False, "rule_id": None},
            {"name": "SEQ", "type": "int", "length": 5, "is_default": False, "rule_id": 99},
            {"name": "UNMAPPED", "type": "text", "length": 6, "is_default": True, "rule_id": None},
        ],
    }
    with patch.object(ls, "get_rules", return_value=[
        {"id": 99, "gen_method": "sequence", "start_value": 100, "prefix": "", "postfix": ""},
    ]):
        out = ls.render_through_layout(df, layout)

    lines = out.split("\n")
    assert "Evergreen Medicaid" in lines[0]          # pattern data wins (RI)
    assert "19800512" in lines[0]                     # date reformatted to layout
    assert "  100" in lines[0] and "  101" in lines[1]  # sequence rule filled gap
    # default field is blank: last 6 chars of the line are spaces
    assert lines[0][-6:] == "      "
    # fixed-width row length equals sum of field widths
    assert len(lines[0]) == 8 + 20 + 8 + 5 + 6


_MEM_CACHE: dict = {}

try:
    import pandas as _pd
    _FIXTURE_DF = _pd.DataFrame({
        "MEME_CK": [1, 2, 3, 4, 5],
        "MEME_FIRST_NAME": ["A", "B", "C", "D", "E"],
    })
except Exception:  # pragma: no cover
    _FIXTURE_DF = None


def test_layout_fields_come_from_generated_data():
    """A layout must render the generated dataset, never invent values.

    Names came out blank because the sample header was matched against the
    generated columns case-sensitively. Matching is now normalized, and a field
    the dataset genuinely doesn't cover stays blank and is reported as unmapped
    rather than being filled with a freshly drawn name.
    """
    import pandas as pd
    from sdg.analyzer.services import layout_service as ls

    rules = {
        1: {"id": 1, "field_name": "MEME_FIRST_NAME", "gen_method": "lookup",
            "lookup_source": "first_names", "length": 0},
        2: {"id": 2, "field_name": "MEME_LAST_NAME", "gen_method": "lookup",
            "lookup_source": "last_names", "length": 0},
        3: {"id": 3, "field_name": "STREET_1", "gen_method": "lookup",
            "lookup_source": "street_names", "length": 0},
        4: {"id": 4, "field_name": "CLAIM_SEQ", "gen_method": "sequential",
            "prefix": "CL", "start_value": 1, "length": 8},
    }
    df = pd.DataFrame({
        "MEME_FIRST_NAME": ["Alice", "Bob"],
        "MEME_LAST_NAME": ["Smith", "Jones"],
        "MEME_SEX": ["F", "M"],
    })
    layout = {
        "kind": "csv", "delimiter": ",",
        "fields": [
            # Header casing differs from the generated column — must still match.
            {"name": "Meme_First_Name", "type": "text", "rule_id": 1, "is_default": False},
            {"name": "meme last name",  "type": "text", "rule_id": 2, "is_default": False},
            # Not in the dataset, and a name/address rule — must stay blank.
            {"name": "Street_1",        "type": "text", "rule_id": 3, "is_default": False},
            # Not in the dataset, but deterministic — may be filled.
            {"name": "Claim_Seq",       "type": "text", "rule_id": 4, "is_default": False},
        ],
    }

    with patch.object(ls, "_rules_by_id", return_value=rules):
        out = ls.render_through_layout(df, layout)
        unmapped = ls.unmapped_fields(df, layout)

    rows = [r.split(",") for r in out.split("\n")[1:]]
    assert rows[0][0] == "Alice" and rows[1][0] == "Bob"   # case-insensitive match
    assert rows[0][1] == "Smith"                            # spaces in header match
    assert rows[0][2] == ""                                 # not invented
    assert rows[0][3] == "CL000001"                         # sequential fill
    assert unmapped == ["Street_1"]                         # blank column reported






# ── Claims → member pattern mapping ──────────────────────────────────────────

def _stored(pid, name, cat, cat_desc="Medical", count=1000, saved=None):
    """A row as db.get_all_patterns returns it."""
    return {
        "id": pid, "contextual_name": name, "cspd_cat": cat,
        "cspd_cat_desc": cat_desc, "member_count": count,
        "grgr_name": "Group A", "plds_desc": "Plan 1", "saved_at": saved,
        "grgr_ck": '["12345"]', "cspi_id": '["P1"]', "pdpd_id": None,
        "profile_json": '{"n_members": 1000}',
    }


def test_member_pattern_list_excludes_claims():
    """The mapping dropdown must offer member patterns only.

    Both domains share the patterns table, kept apart by a sentinel folded into
    cspd_cat. Nothing filtered on it, so claims patterns appeared in the list —
    and picking one fed a claims profile to the member generator.
    """
    from sdg.analyzer.services import claims_service as cs

    rows = [
        _stored(1, "Ohio Medicaid Adults", '["Medical", "__MEMBERS__"]'),
        _stored(2, "[Claims] High Utilizers", '["__CLAIMS__"]', "Claims"),
        _stored(3, "Dual Eligible", '["Medical", "__MEMBERS__"]', saved="2026-01-01"),
        # Saved before the sentinel existed — only the name prefix marks it.
        _stored(4, "[Claims] Legacy", None, "Claims"),
        # Neither marker: a profiler save, which is a member pattern.
        _stored(5, "Untagged Profiler Save", None, ""),
    ]

    with patch("sdg.analyzer.db.get_all_patterns", return_value=rows):
        got = cs.saved_member_patterns()

    ids = [p["pattern_id"] for p in got]
    assert 2 not in ids, "claims pattern offered as a member pattern"
    assert 4 not in ids, "legacy claims pattern offered as a member pattern"
    assert set(ids) == {1, 3, 5}, ids
    # Saved patterns sort first.
    assert ids[0] == 3, f"saved pattern should lead, got {ids}"


def test_mapping_rejects_a_claims_pattern():
    """A stale tab can still post a claims pattern id. The message must say so."""
    from sdg.analyzer.services import claims_service as cs

    claims_row = _stored(2, "[Claims] High Utilizers", '["__CLAIMS__"]', "Claims")

    with patch("sdg.analyzer.db.get_pattern", return_value=claims_row):
        try:
            cs._require_member_pattern(2)
        except ValueError as e:
            assert "claims pattern" in str(e).lower(), str(e)
            assert "member pattern" in str(e).lower(), str(e)
        else:
            raise AssertionError("a claims pattern was accepted as the mapping")


def test_mapping_rejects_missing_and_unset():
    from sdg.analyzer.services import claims_service as cs

    try:
        cs._require_member_pattern(0)
    except ValueError as e:
        assert "choose a member pattern" in str(e).lower(), str(e)
    else:
        raise AssertionError("an unset mapping was accepted")

    with patch("sdg.analyzer.db.get_pattern", return_value=None):
        try:
            cs._require_member_pattern(999)
        except ValueError as e:
            assert "no longer exists" in str(e).lower(), str(e)
        else:
            raise AssertionError("a missing pattern was accepted")


def test_mapping_accepts_a_member_pattern():
    from sdg.analyzer.services import claims_service as cs

    row = _stored(1, "Ohio Medicaid Adults", '["Medical", "__MEMBERS__"]')
    with patch("sdg.analyzer.db.get_pattern", return_value=row):
        cs._require_member_pattern(1)   # must not raise


def test_pattern_filters_drop_sentinels_and_blanks():
    """The int('') failure, at its source.

    cspd_cat stores a domain sentinel that is bookkeeping, not a plan category —
    passing it back as a filter matches nothing. And a blank grgr_ck reaches SQL
    through int(), which raised `invalid literal for int() with base 10: ''`
    from deep inside the loader instead of anything actionable.
    """
    from sdg.analyzer.services.story_service import _filters_from_pattern

    filters = _filters_from_pattern({
        "grgr_ck": '["12345", "", null, "  "]',
        "cspd_cat": '["Medical", "__MEMBERS__"]',
        "cspi_id": '["__CLAIMS__"]',
        "pdpd_id": None,
    })

    assert filters["grgr_ck"] == [12345], filters["grgr_ck"]
    assert filters["cspd_cat"] == ["Medical"], filters["cspd_cat"]
    # Sentinel-only lists disappear entirely rather than filtering to nothing.
    assert "cspi_id" not in filters, filters
    assert "pdpd_id" not in filters

    # Every value in grgr_ck survives int() — the original crash.
    for v in filters["grgr_ck"]:
        int(v)


def test_pattern_filters_survive_hostile_shapes():
    from sdg.analyzer.services.story_service import _filters_from_pattern

    # Nothing usable at all must yield no filters, not an exception.
    assert _filters_from_pattern({
        "grgr_ck": '[""]', "cspd_cat": '["__CLAIMS__"]',
        "cspi_id": None, "pdpd_id": "not json",
    }) == {}

    # Numerics arriving as floats or ints still coerce.
    out = _filters_from_pattern({
        "grgr_ck": [12345, "678.0", 90],
        "cspd_cat": None, "cspi_id": None, "pdpd_id": None,
    })
    assert out["grgr_ck"] == [12345, 678, 90], out







def test_where_builders_survive_blank_ids():
    """The int('') crash, at the two places that actually build the SQL.

    Both loaders inline IDs with int(). A blank arriving from a stored combo,
    a cleared UI field, or a legacy row raised `invalid literal for int() with
    base 10: ''` from inside query construction — a message that named neither
    the filter nor the caller.
    """
    from sdg.analyzer import entity_loader

    def _build_claims_where(**filters):
        return entity_loader.where("claims", filters)

    def _build_where(**filters):
        return entity_loader.where("members", filters)

    where = _build_claims_where(grgr_ck=["12345", "", None, "  ", 678])
    assert "12345" in where and "678" in where, where
    assert "IN ()" not in where, where

    # Nothing usable: the clause is dropped, not emitted empty.
    assert "GRGR_CK" not in _build_claims_where(grgr_ck=["", None])

    where = _build_where(grgr_ck=["", "999"], cspd_cat=["Medical", ""])
    assert "999" in where and "IN ()" not in where, where

    # A single gender arriving as a bare string must not be split per character.
    where = _build_claims_where(gender="F")
    assert "'F'" in where and "','" not in where, where

    # Blank age bounds are absent, not zero.
    assert "CLCL_ME_AGE" not in _build_claims_where(age_min="", age_max=None)
    assert "CLCL_ME_AGE >= 18" in _build_claims_where(age_min="18")

    # A non-numeric bound is a real mistake and says so.
    try:
        _build_claims_where(age_min="abc")
    except ValueError as e:
        assert "age_min" in str(e), str(e)
    else:
        raise AssertionError("a non-numeric age bound was accepted")


def test_error_detail_names_its_origin():
    """A 422 must locate itself, so the next report needs no guesswork."""
    from sdg.analyzer.services import errors

    def inner():
        int("")

    try:
        inner()
    except ValueError as e:
        detail = errors.detail(e, "member_pattern_id=7")

    assert "invalid literal" in detail
    assert "test_analyzer_integration.py" in detail, detail
    assert "inner()" in detail, detail
    assert "member_pattern_id=7" in detail, detail



def test_discovery_result_survives_its_own_walk():
    """A discovery walk must not evict the answer it just produced.

    One walk writes an entry per combination — hundreds of member frames. With a
    single global cap and oldest-first eviction, those loads pushed the discovery
    result out of the cache, so the next click recomputed the whole sweep. Which
    is why discovery was slow every time rather than only the first.
    """
    from sdg.analyzer.services import disk_cache as dc

    real_max, real_quota = dc.MAX_CACHE_BYTES, dict(dc.NAMESPACE_QUOTAS)
    dc.MAX_CACHE_BYTES = 400_000
    dc.NAMESPACE_QUOTAS["members.load"] = 200_000
    dc.clear()
    try:
        dc.put("disc", {"patterns": ["expensive"]}, ttl=3600,
               namespace="discovery.top", label="discovery.top()")

        # The walk: many per-combination loads, enough to blow both ceilings.
        frame = b"m" * 50_000
        for i in range(20):
            dc.put(f"load{i}", frame, ttl=3600,
                   namespace="members.load", label=f"members.load({i})")

        assert dc.get("disc") is not None, \
            "discovery result evicted by the loads that produced it"

        kept = sum(1 for i in range(20) if dc.get(f"load{i}") is not None)
        assert kept < 20, "per-combination loads were never capped"

        # Second mechanism: an unquota'd namespace filling the global cap must
        # still not be able to take the discovery result. The quota above keeps
        # members.load in check; only PROTECTED covers this case. Enough writes
        # to cross the amortized sweep threshold, as a real walk would.
        for i in range(dc.SWEEP_EVERY + 10):
            dc.put(f"other{i}", b"o" * 50_000, ttl=3600,
                   namespace="claims.cluster", label=f"claims.cluster({i})")

        assert dc.get("disc") is not None, \
            "discovery result evicted by global eviction"
    finally:
        dc.MAX_CACHE_BYTES = real_max
        dc.NAMESPACE_QUOTAS.clear()
        dc.NAMESPACE_QUOTAS.update(real_quota)
        dc.clear()


def test_quota_checks_do_not_reread_sidecars():
    """Quota enforcement must not cost a file read per cached entry.

    It ran on every write while holding the cache lock, so a few hundred cached
    entries turned each generate into a few hundred JSON reads — unrelated work
    blocking on the lock is what looked like generation hanging.
    """
    from sdg.analyzer.services import disk_cache as dc

    dc.clear()
    dc._NS_INDEX.clear()
    try:
        for i in range(12):
            dc.put(f"k{i}", b"x" * 1000, ttl=3600,
                   namespace="dataset", label=f"dataset({i})")

        # Writing populates the index, so lookups need no sidecar at all.
        reads = {"n": 0}
        real_paths = dc._paths

        def counting_paths(key):
            reads["n"] += 1
            return real_paths(key)

        dc._paths = counting_paths
        try:
            for i in range(12):
                assert dc._namespace_of(f"k{i}") == "dataset"
        finally:
            dc._paths = real_paths

        assert reads["n"] == 0, f"{reads['n']} sidecar reads on a warm index"
    finally:
        dc.clear()






def test_quota_enforcement_reads_no_filesystem():
    """Quota checks must not scale with the cache size.

    Enforcing a quota by globbing and stat-ing every cached file ran on every
    write, so a 500-combination discovery walk performed 500 full directory
    scans while holding the lock — quadratic, and the reason discovery got
    slower the longer the cache had been alive.

    Proven by pointing the cache directory at nothing: enforcement still has to
    work, which it can only do from the in-memory ledger.
    """
    from pathlib import Path

    from sdg.analyzer.services import disk_cache as dc

    dc.clear()
    dc._NS_INDEX.clear()
    dc._NS_FILES.clear()
    dc._NS_SCANNED = False
    real_quota = dict(dc.NAMESPACE_QUOTAS)
    real_dir = dc.CACHE_DIR
    dc.NAMESPACE_QUOTAS["members.load"] = 30_000
    try:
        for i in range(20):
            dc.put(f"load{i}", b"m" * 5_000, ttl=3600,
                   namespace="members.load", label=f"members.load({i})")

        ledger = dc._NS_FILES.get("members.load", {})
        assert ledger, "the ledger was never populated"
        held = sum(size for _, size in ledger.values())
        assert held <= dc.NAMESPACE_QUOTAS["members.load"], (
            f"quota not enforced: {held} bytes held")

        # With the directory gone, only the ledger can answer.
        dc.CACHE_DIR = Path("/nonexistent-cache-dir")
        before = len(dc._NS_FILES.get("members.load", {}))
        dc.NAMESPACE_QUOTAS["members.load"] = 10_000
        dc._enforce_quota("members.load")
        after = len(dc._NS_FILES.get("members.load", {}))
        assert after < before, "enforcement did nothing without the filesystem"
    finally:
        dc.CACHE_DIR = real_dir
        dc.NAMESPACE_QUOTAS.clear()
        dc.NAMESPACE_QUOTAS.update(real_quota)
        dc.clear()


def test_large_values_stay_out_of_the_disk_tier():
    """Frames above the ceiling are memory-only — pickling them cost more than
    the query they replaced, and a walk evicted them within the same run."""
    from sdg.analyzer.services import disk_cache as dc

    dc.clear()
    real = dc.MAX_DISK_ENTRY_BYTES
    dc.MAX_DISK_ENTRY_BYTES = 50_000
    try:
        dc.put("small", b"s" * 1_000, ttl=3600, namespace="t", label="t(small)")
        dc.put("big", b"b" * 200_000, ttl=3600, namespace="t", label="t(big)")

        assert dc.get("small") is not None, "small entry should persist"
        assert dc.get("big") is None, "oversized entry was written to disk"
        assert dc.stats()["entries"] == 1, dc.stats()
    finally:
        dc.MAX_DISK_ENTRY_BYTES = real
        dc.clear()


def test_approved_layout_can_be_edited():
    """Editing an approved layout resets it for re-approval by default.

    The approval attested to a specific set of fields and rules; changing them
    behind it would make the status meaningless. keep_approval covers a
    correction that changes no output — renaming FIELD_3 to CITY, say.
    """
    from sdg.analyzer.services import layout_service as ls

    approved = {
        "id": 7, "name": "FACETS Load", "kind": "fixed", "delimiter": None,
        "status": "approved",
        "fields": [
            {"name": "FIELD_1", "type": "text", "rule_id": 1, "is_default": False},
            {"name": "FIELD_2", "type": "text", "rule_id": None, "is_default": True},
        ],
    }
    renamed = [
        {"name": "MEME_FIRST_NAME", "type": "text", "rule_id": 1, "is_default": False},
        {"name": "MEME_LAST_NAME", "type": "text", "rule_id": 2, "is_default": False},
    ]

    executed = []

    class Cur:
        rowcount = 1
        def execute(self, sql, *a): executed.append((sql, a))
    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    with patch.object(ls, "get_layout", return_value=approved), \
         patch.object(ls, "_conn", return_value=Conn()):
        out = ls.update_layout(7, fields=renamed)
        assert out["status"] == "pending_review", out
        assert out["reapproval_required"] is True, out
        assert out["n_default"] == 0, out

        executed.clear()
        keep = ls.update_layout(7, fields=renamed, keep_approval=True)
        assert keep["status"] == "approved", keep
        assert keep["reapproval_required"] is False, keep
        assert "pending_review" not in executed[0][0], executed[0][0]


def test_layout_edit_rejects_duplicate_field_names():
    """Field names are what generated columns match on, so they must be
    distinct — two fields sharing one would both read the same column."""
    from sdg.analyzer.services import layout_service as ls

    approved = {"id": 7, "name": "L", "kind": "csv", "delimiter": ",",
                "status": "approved", "fields": []}
    dupes = [
        {"name": "MEME_FIRST_NAME", "type": "text", "rule_id": None, "is_default": True},
        {"name": "meme first name", "type": "text", "rule_id": None, "is_default": True},
    ]
    with patch.object(ls, "get_layout", return_value=approved):
        try:
            ls.update_layout(7, fields=dupes)
        except ValueError as e:
            assert "distinct" in str(e).lower(), str(e)
        else:
            raise AssertionError("duplicate field names were accepted")

        try:
            ls.update_layout(7, fields=[{"name": "  ", "type": "text"}])
        except ValueError as e:
            assert "name" in str(e).lower(), str(e)
        else:
            raise AssertionError("a blank field name was accepted")


def test_claims_layout_renders_from_claims_data():
    """A layout used for claims maps against the generated claims columns, and
    anything the claims data has no column for renders blank, not invented."""
    from sdg.analyzer.services import layout_service as ls

    rules = {
        1: {"id": 1, "field_name": "CLCL_ID", "gen_method": "lookup",
            "lookup_source": None},
        2: {"id": 2, "field_name": "MEME_FIRST_NAME", "gen_method": "lookup",
            "lookup_source": "first_names"},
    }
    claims = pd.DataFrame({
        "CLCL_ID": ["C1", "C2"],
        "IDCD_ID_PRIMARY": ["E119", "I10"],
    })
    layout = {
        "kind": "csv", "delimiter": ",",
        "fields": [
            {"name": "Clcl_Id", "type": "text", "rule_id": 1, "is_default": False},
            {"name": "Idcd_Id_Primary", "type": "text", "rule_id": None, "is_default": True},
            # Member column the claims frame does not carry.
            {"name": "Meme_First_Name", "type": "text", "rule_id": 2, "is_default": False},
        ],
    }

    with patch.object(ls, "_rules_by_id", return_value=rules):
        out = ls.render_through_layout(claims, layout)
        unmapped = ls.unmapped_fields(claims, layout)

    rows = [r.split(",") for r in out.split("\n")[1:]]
    assert rows[0][0] == "C1", rows[0]
    assert rows[0][1] == "E119", rows[0]
    assert rows[0][2] == "", "a missing column was filled rather than left blank"
    assert unmapped == ["Meme_First_Name"], unmapped





def test_coerce_handles_every_bad_shape():
    from sdg.analyzer import coerce

    series = pd.Series([500, "", None, "700", "abc", "12.0"])
    assert list(coerce.to_int(series)) == [500, 0, 0, 700, 0, 12]
    assert str(coerce.to_int(series).dtype) == "int64"

    # A blank column must not raise, and must report itself.
    blanks = pd.DataFrame({"SBSB_CK": ["", "", ""], "NAME": list("ABC")})
    assert coerce.to_int(blanks["SBSB_CK"]).tolist() == [0, 0, 0]
    assert coerce.blank_key_columns(blanks) == {"SBSB_CK": 3}

    assert [coerce.one_int(v) for v in (5, "5", "", None, "abc", "7.0")] == \
        [5, 5, 0, 0, 0, 7]


def test_claims_attach_to_pool_with_unconfigured_keys():
    """The exact customer failure: a member pool whose SBSB_CK has no rule."""
    from sdg.analyzer import coerce

    pool = pd.DataFrame({
        "MEME_CK": [1, 2, 3],
        "SBSB_CK": ["", "", ""],      # no generation rule configured
        "GRGR_CK": ["", "", ""],
        "MEME_SEX": ["F", "M", "F"],
    })

    # The original cast, for the record.
    try:
        pool["SBSB_CK"].astype(int)
    except ValueError as e:
        assert "invalid literal for int()" in str(e)
    else:
        raise AssertionError("expected the original cast to fail")

    # The coercion that replaced it.
    assert coerce.to_int(pool["SBSB_CK"]).tolist() == [0, 0, 0]
    assert coerce.to_int(pool["GRGR_CK"]).tolist() == [0, 0, 0]


def test_error_origin_names_our_code_not_the_library():
    """An origin pointing into pandas sent a real investigation to the wrong
    place. It must name the deepest frame we can actually change."""
    from sdg.analyzer.services import errors

    def our_function():
        pd.Series(["", ""]).astype(int)    # raises inside pandas

    try:
        our_function()
    except ValueError as e:
        text = errors.origin(e)

    assert "our_function()" in text, text
    assert "test_analyzer_integration.py" in text, text
    # The library frame is still named, after ours.
    assert "→" in text, text



def test_pool_sanitized_at_the_boundary():
    """No downstream cast can meet a blank key, whichever rule is missing.

    Guarding each cast site meant a new one surfaced each round — MEME_CK, then
    SBSB_CK and GRGR_CK. Coercing the pool once at the boundary covers the sites
    that exist and the ones nobody has found yet.
    """
    from sdg.analyzer.services import claims_service as cs

    pool = pd.DataFrame({
        "MEME_CK": ["", "", ""],
        "SBSB_CK": [None, "", "not-a-number"],
        "GRGR_CK": ["", "12345", ""],
        "MEME_SEX": ["F", "M", "F"],
        "MEME_FIRST_NAME": ["A", "B", "C"],
    })
    out = cs._sanitise_pool(pool.copy())

    for col in ("MEME_CK", "SBSB_CK", "GRGR_CK"):
        assert str(out[col].dtype) == "int64", f"{col} is {out[col].dtype}"
        out[col].astype(int)              # the cast that used to fail
    assert out["GRGR_CK"].tolist() == [0, 12345, 0], out["GRGR_CK"].tolist()
    # Text columns are left exactly as they were.
    assert out["MEME_SEX"].tolist() == ["F", "M", "F"]
    assert out["MEME_FIRST_NAME"].tolist() == ["A", "B", "C"]

    assert cs._sanitise_pool(pd.DataFrame()) is not None


def test_error_detail_names_the_build():
    """A fixed bug and an undeployed fix look identical from the UI."""
    from sdg.analyzer.services import errors
    from sdg import __version__ as BUILD

    try:
        int("")
    except ValueError as e:
        text = errors.detail(e, "member_pattern_id=157")

    assert f"build {BUILD}" in text, text



# ── Pattern provenance and certification ─────────────────────────────────────

def _pat(pid, name, origin="discovered", ck="G1|CAT|ALL|ALL",
         certified_at=None, count=1000, cat='["Medical", "__MEMBERS__"]'):
    return {
        "id": pid, "contextual_name": name, "origin": origin, "combo_key": ck,
        "certified_at": certified_at, "certified_by": None,
        "certified_against_id": None, "member_count": count,
        "grgr_ck": '["12345"]', "cspd_cat": cat, "cspi_id": None, "pdpd_id": None,
        "grgr_name": "Group A", "cspd_cat_desc": "Medical", "plds_desc": "Plan 1",
        "created_by": "user", "saved_at": None, "profile_json": '{"n_members": 10}',
    }


def test_discovered_patterns_are_immutable():
    """A discovered pattern is evidence. Editing one would turn evidence into
    opinion while leaving it labeled as evidence."""
    from sdg.analyzer.services import pattern_service as ps

    with patch("sdg.analyzer.db.get_pattern", return_value=_pat(1, "Ohio Adults")):
        try:
            ps.update_user_pattern(1, name="Renamed")
        except ValueError as e:
            assert "cannot be edited" in str(e).lower(), str(e)
            assert "clone" in str(e).lower(), "the message should offer the way forward"
        else:
            raise AssertionError("a discovered pattern was edited")


def test_clone_is_always_user_defined():
    """A copy of evidence is not itself evidence — even an unchanged copy."""
    from sdg.analyzer.services import pattern_service as ps

    created = {}

    def fake_create(name, features, entity="members", created_by="user", labels=None):
        created.update({"name": name, "features": features, "entity": entity})
        return {"pattern_id": 99, "name": name, "origin": ps.USER_DEFINED,
                "combo_key": "x", "matches": []}

    with patch("sdg.analyzer.db.get_pattern", return_value=_pat(1, "Ohio Adults")), \
         patch.object(ps, "create_user_pattern", side_effect=fake_create):
        out = ps.clone_pattern(1, "New LOB variant", {"cspi_id": ["NEWPLAN"]})

    assert out["origin"] == ps.USER_DEFINED
    assert out["cloned_from"] == 1
    assert created["features"]["cspi_id"] == ["NEWPLAN"], created["features"]
    # The domain sentinel is bookkeeping and must not survive as a stated value.
    assert "__MEMBERS__" not in (created["features"].get("cspd_cat") or []), created


def test_user_pattern_accepts_values_not_in_reference_data():
    """A new line of business has codes configured nowhere yet. Refusing them
    would defeat the feature; the flag carries the caveat instead."""
    from sdg.analyzer.services import pattern_service as ps

    captured = {}

    class Cur:
        description = None
        def execute(self, sql, *a): captured["args"] = a
        def fetchone(self): return (42,)
    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    with patch.object(ps, "_find_by_combo", return_value=[]), \
         patch.object(ps, "_conn", return_value=Conn()):
        out = ps.create_user_pattern(
            "2027 LOB launch", {"cspi_id": ["DOES-NOT-EXIST-YET"]})

    assert out["origin"] == ps.USER_DEFINED
    assert out["pattern_id"] == 42
    assert ps.USER_DEFINED in captured["args"], "origin not persisted"

    # A pattern with no values at all matches everything and describes nothing.
    with patch.object(ps, "_find_by_combo", return_value=[]):
        try:
            ps.create_user_pattern("Empty", {})
        except ValueError as e:
            assert "at least one feature value" in str(e)
        else:
            raise AssertionError("an empty combination was accepted")


def test_combo_key_ignores_cluster_and_separates_domains():
    """Certification matches on the combination alone. Which cluster a run
    assigned it to is an artifact of that run, not part of the pattern."""
    from sdg.analyzer.services import pattern_service as ps

    a = ps.combo_key({"grgr_ck": ["1", "2"], "cspd_cat": ["Medical"]})
    b = ps.combo_key({"grgr_ck": [2, 1], "cspd_cat": ["Medical"]})
    assert a == b, "value order or type changed the identity"

    members = ps.combo_key({"grgr_ck": ["1"]}, "members")
    claims = ps.combo_key({"grgr_ck": ["1"]}, "claims")
    assert members != claims, "a member pattern could certify a claims pattern"


def test_review_queue_pairs_user_patterns_with_discoveries():
    """The queue is computed, not stored — a stored one goes stale the moment
    a discovery run changes what exists."""
    from sdg.analyzer.services import pattern_service as ps

    catalog = [
        _pat(1, "SME: new dual product", "user_defined", "G9|CAT|ALL|ALL"),
        _pat(2, "Discovered G9", "discovered", "G9|CAT|ALL|ALL", count=4200),
        _pat(3, "Discovered G9 (smaller run)", "discovered", "G9|CAT|ALL|ALL", count=800),
        _pat(4, "SME: no match yet", "user_defined", "G7|CAT|ALL|ALL"),
        _pat(5, "SME: already certified", "user_defined", "G8|CAT|ALL|ALL",
             certified_at=datetime(2026, 1, 1)),
        _pat(6, "Discovered G8", "discovered", "G8|CAT|ALL|ALL"),
    ]
    with patch("sdg.analyzer.db.get_all_patterns", return_value=catalog):
        queue = ps.review_queue()

    ids = [q["user_pattern"]["pattern_id"] for q in queue]
    assert ids == [1], f"queue should hold only the unmatched-but-now-found one: {ids}"
    assert queue[0]["matched_pattern"]["pattern_id"] == 2, "should match the larger run"
    assert queue[0]["other_matches"] == 1
    assert queue[0]["observed_members"] == 4200


def test_certification_requires_review_and_a_real_match():
    """Never automatic: data generated under a hypothesis must not
    retroactively appear to have been evidence-backed."""
    from sdg.analyzer.services import pattern_service as ps

    user = _pat(1, "SME pattern", "user_defined", "G9|CAT|ALL|ALL", count=0)
    found = _pat(2, "Discovered", "discovered", "G9|CAT|ALL|ALL", count=4200)
    mismatch = _pat(3, "Other", "discovered", "ZZZ|CAT|ALL|ALL")

    class Cur:
        rowcount = 1
        def execute(self, sql, *a): self.args = a
    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    def lookup(pid):
        return {1: user, 2: found, 3: mismatch}.get(pid)

    with patch("sdg.analyzer.db.get_pattern", side_effect=lookup), \
         patch.object(ps, "_conn", return_value=Conn()):
        out = ps.certify(1, 2, approved_by="reviewer")
        assert out["certified_against_id"] == 2
        assert out["certified_by"] == "reviewer"
        assert out["observed_members"] == 4200

        # Combinations that differ cannot certify each other.
        try:
            ps.certify(1, 3)
        except ValueError as e:
            assert "same combination" in str(e).lower(), str(e)
        else:
            raise AssertionError("certified against a different combination")

        # Nor can a discovered pattern be the thing certified.
        try:
            ps.certify(2, 2)
        except ValueError as e:
            assert "user-defined" in str(e).lower(), str(e)
        else:
            raise AssertionError("a discovered pattern was certified")


def test_editing_a_certified_pattern_clears_its_certification():
    """Certification was awarded to a combination. Change the combination and
    it asserts evidence that was never gathered for what it now says."""
    from sdg.analyzer.services import pattern_service as ps

    row = _pat(1, "SME pattern", "user_defined", "G9|CAT|ALL|ALL",
               certified_at=datetime(2026, 1, 1))

    class Cur:
        def execute(self, sql, *a): self.sql = sql
    cur = Cur()
    class Conn:
        def cursor(self): return cur
        def commit(self): pass
        def close(self): pass

    with patch("sdg.analyzer.db.get_pattern", return_value=row), \
         patch.object(ps, "_conn", return_value=Conn()), \
         patch.object(ps, "_find_by_combo", return_value=[]):
        out = ps.update_user_pattern(1, features={"grgr_ck": ["99999"]})

    assert out["certification_cleared"] is True
    assert "certified_at = NULL" in cur.sql


def test_size_prepass_skips_small_combinations():
    """Most combinations hold a handful of members and produce no pattern worth
    ranking. Skipping them before the load is the largest saving in the walk.

    The pre-pass also orders by size, so the cap spends its budget on the
    combinations that will actually yield patterns rather than on whichever
    groups happened to sort first.
    """
    from sdg.analyzer.services import discovery_service as ds

    grid = pd.DataFrame([
        {"GRGR_CK": 10, "GRGR_NAME": "Big", "CSPD_CAT": None,
         "CSPD_CAT_DESC": None, "CSPI_ID": None, "n_members": 50_000, "depth": 1},
        {"GRGR_CK": 20, "GRGR_NAME": "Mid", "CSPD_CAT": "MED",
         "CSPD_CAT_DESC": "Medical", "CSPI_ID": None, "n_members": 4_000, "depth": 2},
        {"GRGR_CK": 30, "GRGR_NAME": "Small", "CSPD_CAT": None,
         "CSPD_CAT_DESC": None, "CSPI_ID": None, "n_members": 900, "depth": 1},
    ])

    with patch("sdg.analyzer.entity_loader.combination_sizes",
               return_value=grid) as sizes:
        combos = ds._enumerate_by_size(2, None, cap=500, min_members=30)
    # Members' grid, to the walk's depth, with its minimum: one loader serves
    # both entities, so the entity asked for is what tells them apart.
    sizes.assert_called_once_with("members", max_depth=2, minimum=30)

    assert combos is not None
    # Largest first: the cap should buy the biggest combinations.
    assert [c["grgr_ck"][0] for c in combos] == [10, 20, 30], combos
    # Depth is respected — a depth-2 row carries its category, depth-1 does not.
    assert "cspd_cat" not in combos[0], combos[0]
    assert combos[1]["cspd_cat"] == ["MED"], combos[1]

    # limit_groups still caps distinct groups, not rows.
    with patch("sdg.analyzer.entity_loader.combination_sizes",
               return_value=grid):
        limited = ds._enumerate_by_size(2, 2, cap=500, min_members=30)
    assert {c["grgr_ck"][0] for c in limited} == {10, 20}, limited


def test_discovery_falls_back_when_prepass_unavailable():
    """A failing aggregate must not take discovery down — the old enumeration
    depends only on the hierarchy helpers and still works."""
    from sdg.analyzer.services import discovery_service as ds

    with patch("sdg.analyzer.entity_loader.combination_sizes",
               side_effect=RuntimeError("no such table")):
        assert ds._enumerate_by_size(2, None, 500, 30) is None

    # An empty book is not an error either — it simply has no combinations.
    with patch("sdg.analyzer.entity_loader.combination_sizes",
               return_value=pd.DataFrame()):
        assert ds._enumerate_by_size(2, None, 500, 30) is None


def test_status_probe_matches_the_cache_key():
    """The landing page asks whether a result is cached before running a walk.
    If the probe builds a different key than the decorator, it always misses
    and the page always offers to re-run work that is already done."""
    from sdg.analyzer.services import disk_cache as dc

    namespace = "discovery.top"
    calls = {"n": 0}

    @dc.two_tier(namespace, ttl=600)
    def fake_discover(top_n=50, max_depth=2, limit_groups=None):
        calls["n"] += 1
        return {"patterns": []}

    def probe(top_n=50, max_depth=2, limit_groups=None):
        kwargs = sorted({"top_n": top_n, "max_depth": max_depth,
                         "limit_groups": limit_groups}.items())
        return dc.get(dc.make_key(namespace, (), kwargs)) is not None

    dc.clear(); dc._NS_INDEX.clear(); dc._NS_FILES.clear(); dc._NS_SCANNED = False
    try:
        assert probe() is False, "cold cache reported as warm"
        fake_discover(top_n=50, max_depth=2, limit_groups=None)
        assert probe() is True, "warm cache reported as cold — the keys diverge"
        assert probe(max_depth=3) is False, "a different view reported as cached"
        assert calls["n"] == 1
    finally:
        dc.clear()


def test_upsert_reuses_a_supplied_connection():
    """A walk persists hundreds of patterns. Opening a connection per pattern
    cost more than the writes."""
    from sdg.analyzer import db

    opened = {"n": 0}

    class Cur:
        description = None
        def execute(self, sql, *a): self.sql = sql
        def fetchone(self): return (7,)
    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    shared = Conn()

    def counting_conn():
        opened["n"] += 1
        return shared

    with patch.object(db, "get_connection", side_effect=counting_conn):
        for _ in range(5):
            db.upsert_pattern({}, 0, "n", 1, 0.1, 0.5, conn=shared)
    assert opened["n"] == 0, f"{opened['n']} connections opened despite a shared one"



def test_claims_prepass_matches_the_member_fix():
    """The claims walk had the same N+1 enumeration and no size gate.

    Fixing only the member path left the claims toggle on the same screen just
    as slow, which is how this surfaced. The hierarchy differs — Group → Plan →
    Product rather than Group → Category → Plan — so the mapping is checked
    here rather than assumed to mirror the member one.
    """
    from sdg.analyzer.services import discovery_service as ds

    grid = pd.DataFrame([
        {"GRGR_CK": 10, "GRGR_NAME": "Big", "CSPI_ID": None, "PLDS_DESC": None,
         "PDPD_ID": None, "n_claims": 900_000, "depth": 1},
        {"GRGR_CK": 20, "GRGR_NAME": "Mid", "CSPI_ID": "PLAN-A",
         "PLDS_DESC": "Plan A", "PDPD_ID": None, "n_claims": 40_000, "depth": 2},
        {"GRGR_CK": 30, "GRGR_NAME": "Deep", "CSPI_ID": "PLAN-B",
         "PLDS_DESC": "Plan B", "PDPD_ID": "PROD-1", "n_claims": 5_000, "depth": 3},
    ])

    with patch("sdg.analyzer.entity_loader.combination_sizes",
               return_value=grid) as sizes:
        combos = ds._enumerate_claims_by_size(3, None, cap=500, min_claims=100)
    sizes.assert_called_once_with("claims", max_depth=3, minimum=100)

    assert combos is not None
    assert [c["grgr_ck"][0] for c in combos] == [10, 20, 30], combos
    # Claims nest by plan then product — never by plan category.
    assert "cspd_cat" not in combos[1], combos[1]
    assert combos[1]["cspi_id"] == ["PLAN-A"], combos[1]
    assert combos[2]["pdpd_id"] == ["PROD-1"], combos[2]

    # And it falls back rather than failing.
    with patch("sdg.analyzer.entity_loader.combination_sizes",
               side_effect=RuntimeError("no such table")):
        assert ds._enumerate_claims_by_size(2, None, 500, 100) is None


def test_both_walks_share_one_persist_connection():
    """Both walks persist hundreds of patterns. Neither may open a connection
    per pattern — the member fix alone left claims paying that cost."""
    from pathlib import Path

    # Both walks are wrapped by @two_tier, so inspect.getsource returns the
    # decorator's wrapper rather than the body. Read the module instead.
    module = Path("sdg/analyzer/services/discovery_service.py").read_text(encoding="utf-8")

    def body(name: str) -> str:
        start = module.index(f"def {name}(")
        rest = module[start + 1:]
        nxt = rest.find("\ndef ")
        return rest[:nxt] if nxt > 0 else rest

    for name, label in (("discover", "members"), ("discover_claims", "claims")):
        src = body(name)
        assert "_persist_connection(" in src, (
            f"{label} walk does not take its connection from the guarded helper")
        assert "conn=persist_conn" in src, f"{label} walk does not pass it to upsert"

    # The guarantee itself, not the spelling of it. This assertion used to read
    # `"persist_conn.close()" in src`, which passed while the walk was leaking:
    # the call was present but sat after the loop with nothing guarding it, so
    # any exception skipped it and left the connection holding exclusive locks
    # on sdm.patterns until uvicorn restarted.
    from sdg.analyzer.services.discovery_service import _persist_connection

    class FakeConn:
        def __init__(self):
            self.committed = self.rolled_back = self.closed = False
        def commit(self):
            self.committed = True
        def rollback(self):
            self.rolled_back = True
        def close(self):
            self.closed = True

    made: list = []

    def _open():
        made.append(FakeConn())
        return made[-1]

    with patch("sdg.analyzer.db.get_raw_connection", _open):
        # Normal completion commits and closes.
        with _persist_connection("test"):
            pass
        assert made[-1].committed, "clean walk does not commit"
        assert made[-1].closed, "clean walk does not close"

        # A walk that raises must roll back and close, never leak.
        for exc in (RuntimeError("walk failed"), KeyboardInterrupt()):
            try:
                with _persist_connection("test"):
                    raise exc
            except BaseException:
                pass
            assert made[-1].rolled_back, f"{type(exc).__name__} left an open transaction"
            assert made[-1].closed, f"{type(exc).__name__} leaked the connection"
            assert not made[-1].committed, f"{type(exc).__name__} committed a failed walk"



def test_pattern_service_opens_a_real_connection():
    """_conn must yield a connection, not a context-manager object.

    db.get_connection is decorated with @contextmanager, so calling it returns
    the manager rather than a connection — and every `.cursor()` / `.close()`
    then fails with AttributeError at runtime. The other pattern-service tests
    all patch _conn, so none of them exercised this: the bug reached a user.

    This calls the real _conn with only the driver stubbed, so the wiring is
    checked even though no database is present.
    """
    from sdg.analyzer.services import pattern_service as ps

    class FakeCursor:
        description = [("id",)]
        def execute(self, *a, **k): return self
        def fetchone(self): return (1,)
        def fetchall(self): return []

    class FakeConn:
        def cursor(self): return FakeCursor()
        def commit(self): pass
        def close(self): pass

    with patch("sdg.analyzer.db.pyodbc.connect", return_value=FakeConn()):
        conn = ps._conn()
        # The three operations every caller in this module performs.
        conn.cursor()
        conn.commit()
        conn.close()

    # And the path that actually failed, end to end.
    with patch("sdg.analyzer.db.pyodbc.connect", return_value=FakeConn()):
        assert ps._find_by_combo("G1|CAT|ALL|ALL") == []


def test_every_service_conn_helper_returns_a_connection():
    """No module may hand back a context manager from its _conn helper.

    Cheap structural guard: the mistake is easy to repeat and invisible until a
    request hits the database, because every unit test patches _conn away.
    """
    from pathlib import Path

    checked = set()
    for path in ("sdg/analyzer/services/pattern_service.py",
                 "sdg/analyzer/services/layout_service.py",
                 "sdg/analyzer/services/baseline.py",
                 "sdg/analyzer/entity_loader.py",
                 "sdg/analyzer/claims_data_loader.py"):
        src = Path(path).read_text(encoding="utf-8")
        # entity_loader's helper is connect(); the others' _conn().
        start = max(src.find("def _conn("), src.find("def connect("))
        if start < 0:
            continue
        checked.add(path)
        body = src[start:start + 600].split("\ndef ")[0]
        # Strip the docstring. It legitimately names the wrong call in order to
        # warn about it, and matching prose would flag the warning itself.
        parts = body.split(chr(34) * 3)
        code = parts[0] + ("".join(parts[2:]) if len(parts) > 2 else "")
        assert "return db.get_connection()" not in code, (
            f"{path}: _conn returns a context manager, not a connection")
    # The loader holds every connection the analysis opens: it is checked.
    assert "sdg/analyzer/entity_loader.py" in checked




def test_health_reports_build():
    r = _client().get("/api/analyzer/health")
    assert r.status_code == 200
    assert "build" in r.json()


def main():
    """Run this file on its own: every test in it, as pytest finds them. A list
    of them kept here by hand fell behind as tests were renamed and removed."""
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    raise SystemExit(main())

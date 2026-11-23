"""What is generated from a pattern holds the pattern's values.

Every field a pattern measures is checked against what comes out, by every
route out of the tool: a file, a database load, scenarios of a pattern, and a
pattern somebody defined. A value the pattern holds at 54% has to come out
near 54%; values the pattern holds together (a plan with its own product and
group, a marital status with a relationship, a claim type with its diagnosis,
procedure and network) have to come out together, never re-paired.

Database loads are generated in memory from a plan built with the pattern
applied; nothing is written to the database. Patterns kept here are deleted by
the ids the keep returned, only when it created them.
"""

from __future__ import annotations

import collections
import csv
import io
import json
from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient

import sdg.api.app as api
from sdg.analyzer.services.scenarios import AGE_BANDS
from tests.saved_cleanup import unrestore

client = TestClient(api.app)
pytestmark = pytest.mark.db

AS_OF = date(2026, 6, 30)  # the date files and loads are written as of
SCOPE = {"grgr_ck": [1]}
SHARE = 0.03               # how far a share may stray in a few thousand rows
MEMBER = "dbo.CMC_MEME_MEMBER"
SUBSCRIBER = "dbo.CMC_SBSB_SUBSC"
CLAIM = "dbo.CMC_CLCL_CLAIM"
LINE = "dbo.CMC_CDML_CL_LINE"


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def _shares(values) -> dict[str, float]:
    counted = collections.Counter("" if v is None else str(v).strip() for v in values)
    total = sum(counted.values()) or 1
    return {k: n / total for k, n in counted.items()}


def _same_shares(name: str, want: dict, got: dict, tolerance: float = SHARE) -> None:
    want = {str(k).strip(): float(v) for k, v in want.items()}
    total = sum(want.values()) or 1
    want = {k: v / total for k, v in want.items()}
    off = {k: (round(want.get(k, 0), 3), round(got.get(k, 0), 3))
           for k in set(want) | set(got)
           if abs(want.get(k, 0) - got.get(k, 0)) > tolerance}
    assert not off, f"{name}: (pattern, generated) {off}"


def _same_spread(name: str, deciles: list[float], values: list[float],
                 tolerance: float) -> None:
    made = np.quantile(values, [i / 10 for i in range(1, 10)])
    off = [(round(w, 1), round(float(g), 1)) for w, g in zip(deciles[1:10], made)
           if abs(w - g) > tolerance]
    assert not off, f"{name}: deciles (pattern, generated) {off}"


def _years(day) -> float:
    day = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return (AS_OF - day).days / 365.25


def _months(day) -> float:
    return _years(day) * 12


def _band(age: float) -> str | None:
    whole = max(int(age // 1), 0)
    return next((label for low, high, label in AGE_BANDS if low <= whole <= high), None)


def _key(record: dict, fields: list[str]) -> str:
    return "/".join("" if record.get(f) is None else str(record.get(f)).strip()
                    for f in fields)


# ---------------------------------------------------------------------------
# Patterns, found once for the module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def member_pattern():
    found = client.post("/api/patterns/analyze", json={"filters": SCOPE, "k": 3}).json()
    return max(found["patterns"], key=lambda p: p["size"])


@pytest.fixture(scope="module")
def claim_pattern():
    found = client.post("/api/claims/analyze", json={**SCOPE, "k": 2}).json()
    return max(found["patterns"], key=lambda p: p["size"])


@pytest.fixture(scope="module")
def plan_products():
    """Which product each plan carries, as the source holds it."""
    return {(str(r["CSPI_ID"]).strip(), str(r["PDPD_ID"]).strip())
            for r in api.service.registry.query(
                "source", "SELECT DISTINCT CSPI_ID, PDPD_ID FROM dbo.CMC_CSPI_CS_PLAN")}


def _file(subject: str, body: dict, extra: list[str]) -> list[dict]:
    """A CSV from the export route, with the extra elements it would not
    otherwise carry, so every field the pattern measures can be read back."""
    columns = api.DEFAULT_ELEMENTS[subject]
    api.DEFAULT_ELEMENTS[subject] = [*columns, *[e for e in extra if e not in columns]]
    try:
        response = client.post("/api/export", json={"format": "csv", "subject": subject,
                                                   **body})
    finally:
        api.DEFAULT_ELEMENTS[subject] = columns
    assert response.status_code == 200, response.text[:500]
    return list(csv.DictReader(io.StringIO(response.text)))


def _load(resolutions, monkeypatch, *, as_cohort: bool = False, limit: int = 3000):
    """A database load with the patterns applied, generated in memory."""
    from sdg.engine.execute import run_plan
    from sdg.plan.model import Plan
    from sdg.plan.patterns import apply_to_decisions

    service = api.service
    document, _ = apply_to_decisions(resolutions, service.read_decisions(),
                                     as_cohort=as_cohort)
    with monkeypatch.context() as patch:
        patch.setattr(service, "read_decisions", lambda: document)
        patch.setattr(Plan, "save", lambda self, path: None)
        plan, _graph, _info = service.build()
        patch.setattr(service, "require_plan", lambda: plan)
        sets = service.reference_sets_for_run()
    return plan, run_plan(plan, limit=limit, reference_sets=sets)


def _unseen_combinations(plan, result) -> list[str]:
    """Combinations written that the store never holds, read as validation
    reads them: each table's linked fields with its parents' of the same key."""
    from sdg.plan.build import parent_links
    from sdg.writers.validate import _StoreCombinations

    store = _StoreCombinations(api.service.registry)
    by_name = {t.name: t for t in plan.tables}
    problems = []
    for table in plan.tables:
        if table.name not in result.tables:
            continue
        keys = {f.params.get("record_key") for f in table.fields
                if str(f.params.get("record_key") or "").startswith(("combo:", "pattern_combo:"))}
        for key in sorted(keys):
            columns = [(f.params["field"], None, f.name) for f in table.fields
                       if f.params.get("record_key") == key]
            parents = {}
            for parent_name, child_columns, parent_columns in parent_links(table, api.service.model):
                parent = by_name.get(parent_name)
                theirs = [f for f in (parent.fields if parent else [])
                          if f.params.get("record_key") == key]
                if theirs:
                    parents[parent_name] = (
                        {tuple(row[c] for c in parent_columns): row
                         for row in result.tables[parent_name].rows}, child_columns)
                    columns += [(f.params["field"], parent_name, f.name) for f in theirs]
            if len(columns) < 2:
                continue
            found = store.held([e for e, _p, _c in columns])
            if found is None:
                continue
            written = set()
            for row in result.tables[table.name].rows:
                values = []
                for _element, parent_name, column in columns:
                    source = row if parent_name is None else parents[parent_name][0].get(
                        tuple(row[c] for c in parents[parent_name][1]), {})
                    value = source.get(column)
                    values.append("" if value is None else str(value).strip())
                if all(values):
                    written.add(tuple(values))
            unseen = written - found[1]
            if unseen:
                problems.append(f"{table.name} {[c for _e, _p, c in columns]}: "
                                f"{len(unseen)} of {len(written)}, e.g. {sorted(unseen)[:2]}")
    return problems


def _resolve(profile: dict, kind: str, name: str = "fidelity"):
    from sdg.analyzer.bridge import to_pattern
    from sdg.plan.patterns import resolve_pattern

    service = api.service
    return resolve_pattern(
        to_pattern(profile, kind=kind, as_of=AS_OF.isoformat(), pattern_id=name),
        catalog=service.catalog, mappings=service.require_mappings(),
        plan=service.require_plan(), model=service.model)


# ---------------------------------------------------------------------------
# A member pattern
# ---------------------------------------------------------------------------

def test_a_member_file_holds_the_patterns_values(member_pattern):
    pattern = member_pattern
    rows = _file("members", {
        "members": 4000,
        "pattern": {"profile": pattern, "domain": "members", "filters": SCOPE, "k": 3,
                    "cluster_id": pattern["cluster_id"]},
    }, ["RELATIONSHIP_CODE", "GROUP_KEY", "ORIGINAL_EFFECTIVE_DATE"])
    assert len(rows) == 4000
    categorical = pattern["categorical"]
    _same_shares("sex", categorical["MEME_SEX"]["pct"], _shares(r["SEX"] for r in rows))
    _same_shares("marital status", categorical["MEME_MARITAL_STATUS"]["pct"],
                 _shares(r["MARITAL_STATUS"] for r in rows))
    for combination in pattern["combinations"]:
        fields = combination["elements"]
        _same_shares(" + ".join(fields),
                     {_key(c, fields): c["share"] for c in combination["records"]},
                     _shares(_key(r, fields) for r in rows))
    enrollment = ["GROUP_KEY", "COVERAGE_PLAN_ID", "PRODUCT_CODE"]
    _same_shares("group, plan and product",
                 {_key(e, enrollment): e["share"] for e in pattern["enrollment"]},
                 _shares(_key(r, enrollment) for r in rows))
    _same_spread("age", pattern["continuous"]["_age"]["deciles"],
                 [_years(r["DOB"]) for r in rows], 1.0)
    _same_spread("months enrolled", pattern["continuous"]["_tenure"]["deciles"],
                 [_months(r["ORIGINAL_EFFECTIVE_DATE"]) for r in rows], 2.0)


def test_a_member_load_holds_the_patterns_values(member_pattern, plan_products, monkeypatch):
    pattern = member_pattern
    resolution = _resolve(pattern, "member")
    plan, result = _load([resolution], monkeypatch)
    assert not _unseen_combinations(plan, result)
    members = result.tables[MEMBER].rows
    subscribers = {r["SBSB_CK"]: r for r in result.tables[SUBSCRIBER].rows}
    assert len(members) > 2000

    categorical = pattern["categorical"]
    _same_shares("sex", categorical["MEME_SEX"]["pct"], _shares(r["MEME_SEX"] for r in members))
    _same_shares("marital status + relationship",
                 {f'{c["MARITAL_STATUS"]}/{c["RELATIONSHIP_CODE"]}': c["share"]
                  for c in pattern["combinations"][0]["records"]},
                 _shares(f'{r["MEME_MARITAL_STATUS"]}/{r["MEME_REL"]}' for r in members))
    plans = collections.Counter()
    for e in pattern["enrollment"]:
        plans[f'{e["GROUP_KEY"]}/{e["COVERAGE_PLAN_ID"]}'] += e["share"]
    _same_shares("group and plan", plans, _shares(
        f'{subscribers[r["SBSB_CK"]]["GRGR_CK"]}/{str(subscribers[r["SBSB_CK"]]["CSPI_ID"]).strip()}'
        for r in members))
    _same_spread("age", pattern["continuous"]["_age"]["deciles"],
                 [_years(r["MEME_BIRTH_DT"]) for r in members], 1.0)
    _same_spread("months enrolled", pattern["continuous"]["_tenure"]["deciles"],
                 [_months(r["MEME_ORIG_EFF_DT"]) for r in members], 2.0)

    # The claims under them carry the product of their member's plan.
    by_member = {r["MEME_CK"]: r for r in members}
    claims = result.tables[CLAIM].rows
    stray = [r for r in claims
             if (str(subscribers[by_member[r["MEME_CK"]]["SBSB_CK"]]["CSPI_ID"]).strip(),
                 str(r["PDPD_ID"]).strip()) not in plan_products]
    assert claims and not stray, f"{len(stray)} of {len(claims)} claims name another product"


# ---------------------------------------------------------------------------
# A claims pattern
# ---------------------------------------------------------------------------

def test_each_claims_pattern_holds_one_claim_type():
    """Professional, institutional and dental claims are patterned apart, so
    each keeps its own charges, codes and blanks, and is named for its type."""
    found = client.post("/api/claims/analyze", json={**SCOPE, "k": 2}).json()
    names = {"P": "Professional", "I": "Institutional", "D": "Dental"}
    types = set()
    for pattern in found["patterns"]:
        mix = pattern["categorical"]["claim_type"]
        assert len(mix) == 1, f"{pattern['name']}: {mix}"
        code = next(iter(mix))
        types.add(code)
        assert all(r["CLAIM_TYPE"] == code
                   for c in pattern["combinations"] if "CLAIM_TYPE" in c["elements"]
                   for r in c["records"]), pattern["name"]
        assert pattern["name"].startswith(names.get(code, code)), pattern["name"]
    assert types == {"P", "I", "D"}
    assert sum(p["n_claims"] for p in found["patterns"]) - found["subset_claims"] \
        == found["split_claims"]


def test_a_claims_file_holds_the_patterns_values(claim_pattern):
    pattern = claim_pattern
    lines = _file("claims", {
        "members": 300,
        "pattern": {"profile": pattern, "domain": "claims", "filters": SCOPE, "k": 2,
                    "cluster_id": pattern["cluster_id"]},
    }, ["CLAIM_TYPE", "NETWORK_INDICATOR", "NETWORK_ID", "PROVIDER_ID", "SEX",
        "GROUP_KEY", "COVERAGE_PLAN_ID", "PRODUCT_CODE"])
    assert len(lines) > 1000
    # The pattern measures these over its lines.
    for label, element in [("claim_type", "CLAIM_TYPE"), ("network", "NETWORK_INDICATOR"),
                           ("claim_status", "CLAIM_STATUS"), ("gender", "SEX")]:
        _same_shares(label, pattern["categorical"][label],
                     _shares(r[element] for r in lines), 0.05)
    _same_spread("charge", pattern["continuous"]["charge_amount"]["deciles"],
                 [float(r["CHARGE_AMOUNT"]) for r in lines if r["CHARGE_AMOUNT"]], 10.0)
    combination = pattern["combinations"][0]
    fields = combination["elements"]
    held = {_key(c, fields) for c in combination["records"]}
    outside = sum(1 for r in lines if _key(r, fields) not in held)
    assert not outside, f"{outside} lines pair {' + '.join(fields)} as the pattern never does"
    # Claims belong to the pattern's members, on their own plan and product.
    pairs = collections.Counter((r["COVERAGE_PLAN_ID"], r["PRODUCT_CODE"]) for r in lines)
    assert all(product.startswith("PD") for _plan, product in pairs), pairs.most_common(3)


def test_a_claims_load_holds_the_patterns_values(claim_pattern, plan_products, monkeypatch):
    pattern = claim_pattern
    resolution = _resolve(pattern, "claim")
    plan, result = _load([resolution], monkeypatch)
    # Every claim and line pairs its type, diagnosis, procedure, provider and
    # network as the store does -- what validation checks after a load.
    assert not _unseen_combinations(plan, result)
    claims = result.tables[CLAIM].rows
    lines = result.tables[LINE].rows
    assert len(claims) > 1000
    # Generated claim by claim, so compared claim by claim; the pattern's
    # shares are of its lines, close to its claims' for these fields.
    for label, column in [("claim_type", "CLCL_CL_TYPE"), ("network", "CLCL_NTWK_IND"),
                          ("claim_status", "CLCL_CUR_STS")]:
        _same_shares(label, pattern["categorical"][label],
                     _shares(r[column] for r in claims), 0.05)
    _same_spread("charge", pattern["continuous"]["charge_amount"]["deciles"],
                 [float(r["CDML_CHG_AMT"]) for r in lines if r.get("CDML_CHG_AMT") is not None],
                 10.0)

    members = {r["MEME_CK"]: r for r in result.tables[MEMBER].rows}
    subscribers = {r["SBSB_CK"]: r for r in result.tables[SUBSCRIBER].rows}
    stray = [r for r in claims
             if (str(subscribers[members[r["MEME_CK"]]["SBSB_CK"]]["CSPI_ID"]).strip(),
                 str(r["PDPD_ID"]).strip()) not in plan_products]
    assert not stray, f"{len(stray)} of {len(claims)} claims name another product"
    # A claim is made by a member of the pattern's ages: the member's birth
    # date is the pattern's, though the member is generated before the claim.
    ages = pattern["continuous"]["member_age"]
    old = [round(_years(members[r["MEME_CK"]]["MEME_BIRTH_DT"]), 1) for r in claims
           if not ages["min"] - 1 <= _years(members[r["MEME_CK"]]["MEME_BIRTH_DT"])
           <= ages["max"] + 1]
    assert not old, f"{len(old)} of {len(claims)} claims by members aged {sorted(old)[:5]}"


def test_a_claims_load_over_a_share_draws_its_members_in_whole(claim_pattern, monkeypatch):
    """The pattern over its share of the load: drawn over members, so a claim
    in the share is made by a member of the pattern's ages, and every claim of
    a member drawn in is in it. Outside it, the members are as measured."""
    pattern = {**claim_pattern, "pct_of_total": 0.3}
    resolution = _resolve(pattern, "claim")
    assert resolution.share_of == MEMBER, resolution.rejections
    plan, result = _load([resolution], monkeypatch, as_cohort=True)
    assert [c.subject for c in plan.cohorts] == [MEMBER]

    members = result.tables[MEMBER]
    inside = {r["MEME_CK"] for r, tag in zip(members.rows, members.tags) if tag}
    by_key = {r["MEME_CK"]: r for r in members.rows}
    claims = result.tables[CLAIM]
    in_share = [r for r, tag in zip(claims.rows, claims.tags) if tag]
    assert in_share, "the share got no claims"
    assert {r["MEME_CK"] for r in in_share} <= inside
    assert all(not tag for r, tag in zip(claims.rows, claims.tags) if r["MEME_CK"] not in inside)
    ages = pattern["continuous"]["member_age"]
    old = [r for r in in_share
           if not ages["min"] - 1 <= _years(by_key[r["MEME_CK"]]["MEME_BIRTH_DT"])
           <= ages["max"] + 1]
    assert not old, f"{len(old)} of {len(in_share)} claims in the share by members outside its ages"
    outside = [_years(r["MEME_BIRTH_DT"]) for r in members.rows if r["MEME_CK"] not in inside]
    assert min(outside) < ages["min"] - 1 or max(outside) > ages["max"] + 1, (
        "members outside the share should keep the measured ages")


# ---------------------------------------------------------------------------
# Scenarios of a pattern
# ---------------------------------------------------------------------------

def _forget(pattern_ids: list[int]) -> None:
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as connection:
        cursor = connection.cursor()
        for pattern_id in pattern_ids:
            cursor.execute(f"DELETE FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,))
        connection.commit()


def _scenarios(pattern: dict, dimensions: list[str], take: int):
    address = {"domain": "members", "filters": SCOPE, "k": 3,
               "cluster_id": pattern["cluster_id"], "claim_type": None}
    listed = client.post("/api/patterns/scenarios",
                         json={**address, "dimensions": dimensions}).json()["scenarios"]
    listed = [s for s in listed if "(none)" not in s["values"].values()][:take]
    kept = client.post("/api/patterns/scenarios/keep", json={
        **address, "dimensions": dimensions, "name": "fidelity probe",
        "scenarios": [s["values"] for s in listed]}).json()["kept"]
    return kept


def _scenario_holds(scenario: dict, sex: str, age: float, plan: str | None) -> bool:
    values = scenario["scenario"]
    return ((values.get("gender") in (None, sex))
            and (values.get("age_band") in (None, _band(age)))
            and (values.get("plan") in (None, plan)))


@pytest.mark.parametrize("dimensions", [["gender", "age_band"], ["plan"]])
def test_scenarios_in_one_file_each_hold_their_values(member_pattern, dimensions):
    kept = _scenarios(member_pattern, dimensions, 3)
    try:
        assert len(kept) == 3
        rows = _file("members", {
            "members": 900,
            "scenarios": [{"pattern_id": k["pattern_id"], "label": k["label"], "count": 300}
                          for k in kept],
        }, [])
        by_label = {k["label"]: k for k in kept}
        assert collections.Counter(r["SCENARIO"] for r in rows) == {k["label"]: 300 for k in kept}
        wrong = [r for r in rows if not _scenario_holds(
            by_label[r["SCENARIO"]], r["SEX"], _years(r["DOB"]), r["COVERAGE_PLAN_ID"])]
        assert len(wrong) <= len(rows) * 0.01, (len(wrong), wrong[:3])
    finally:
        _forget([k["pattern_id"] for k in kept if k.get("created")])
        unrestore(kept)


@pytest.mark.parametrize("dimensions", [["gender", "age_band"], ["plan"]])
def test_scenarios_in_one_load_each_hold_their_values(member_pattern, monkeypatch, dimensions):
    """Each scenario a segment of one load, each segment holding its own
    values: its plan on its families' subscriber, its gender and age band on
    its members -- and no family in two segments."""
    from sdg.analyzer import db

    kept = _scenarios(member_pattern, dimensions, 2)
    try:
        resolutions, named = [], {}
        for part in kept:
            profile = json.loads(db.get_pattern(part["pattern_id"])["profile_json"])
            profile["pct_of_total"] = 0.3
            name = f"fidelity-{part['pattern_id']}"
            resolutions.append(_resolve(profile, "member", name))
            named[name] = part
        plan, result = _load(resolutions, monkeypatch, as_cohort=True)
        bits = {c.name: 1 << i for i, c in enumerate(plan.cohorts)}
        members = result.tables[MEMBER]
        subscribers = {r["SBSB_CK"]: r for r in result.tables[SUBSCRIBER].rows}
        assert not any(tag & (tag - 1) for tag in members.tags), "a member in two segments"
        for name, part in named.items():
            inside = [r for r, tag in zip(members.rows, members.tags) if tag & bits[name]]
            assert len(inside) > 100, f"{part['label']} got {len(inside)} members"
            wrong = [r for r in inside if not _scenario_holds(
                part, r["MEME_SEX"], _years(r["MEME_BIRTH_DT"]),
                str(subscribers[r["SBSB_CK"]]["CSPI_ID"]).strip())]
            assert len(wrong) <= len(inside) * 0.01, (part["label"], len(wrong), len(inside))
    finally:
        _forget([k["pattern_id"] for k in kept if k.get("created")])
        unrestore(kept)


# ---------------------------------------------------------------------------
# A pattern somebody defined
# ---------------------------------------------------------------------------

def test_a_defined_pattern_writes_only_what_its_rules_allow():
    scope = {**SCOPE, "conditions": {"gender": {"in": ["F"]}, "age": {"min": 30, "max": 45}}}
    kept = client.post("/api/patterns/keep", json={
        "domain": "members", "filters": scope, "k": 1, "cluster_id": 0,
        "name": "fidelity: women 30 to 45", "origin": "user_defined"}).json()
    try:
        rows = _file("members", {"pattern_id": kept["pattern_id"], "members": 500}, [])
        assert len(rows) == 500
        assert {r["SEX"] for r in rows} == {"F"}
        ages = [_years(r["DOB"]) for r in rows]
        # Measured on 2026-01-01 and written as of 2026-06-30: half a year's slack.
        assert 29.5 <= min(ages) and max(ages) < 46.5, (min(ages), max(ages))
    finally:
        _forget([kept["pattern_id"]] if kept.get("created") else [])
        unrestore([kept])

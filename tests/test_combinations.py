"""Features that only occur in certain combinations, generated only as those.

From the demo: the diagnosis and procedure example is one case of many. A plan
belongs to one product, a provider has one specialty, a child is not married,
some claim types carry only some diagnoses. The pattern store is measured for
every pair of coded features; dependent ones are grouped and drawn as the
combinations the store holds; a run is checked for any it does not.
"""

from __future__ import annotations

import pandas as pd
import pytest

from sdg.plan import combinations as combos


def _rows(pairs: dict[tuple[str, str], int]) -> list[dict]:
    return [{"a": a, "b": b, "n": n} for (a, b), n in pairs.items()]


def test_a_pair_that_never_mixes_is_dependent_and_one_that_does_is_not():
    # Plans each belong to one product: drawn apart, half the rows would pair
    # a plan with the other plan's product.
    tied = _rows({("P1", "X"): 500, ("P2", "Y"): 500})
    assert combos._unseen(tied) == pytest.approx(0.5)
    # Sex and status mix freely.
    free = _rows({("F", "A"): 250, ("F", "T"): 250, ("M", "A"): 250, ("M", "T"): 250})
    assert combos._unseen(free) == 0.0
    # A pair too rare to be missing by rule is not counted as missing.
    sparse = _rows({("a", "x"): 2, ("b", "y"): 2})
    assert combos._unseen(sparse) == 0.0


def test_a_pattern_keeps_its_own_combinations_but_not_its_hierarchy():
    group = combos.Group(
        store="member_denorm",
        elements=["MARITAL_STATUS", "RELATIONSHIP_CODE", "COVERAGE_PLAN_ID"],
        columns=["MEME_MARITAL_STATUS", "MEME_REL", "CSPI_ID"],
    )
    frame = pd.DataFrame({
        "MEME_MARITAL_STATUS": ["M", "M", "S", "S"],
        "MEME_REL": ["18", "18", "19", "19"],
        "CSPI_ID": ["A", "B", "A", "B"],
    })
    [kept] = combos.of_rows(frame, "member_denorm", [group])
    assert kept["elements"] == ["MARITAL_STATUS", "RELATIONSHIP_CODE"]
    assert {(r["MARITAL_STATUS"], r["RELATIONSHIP_CODE"]) for r in kept["records"]} == {
        ("M", "18"), ("S", "19")}
    assert sum(r["share"] for r in kept["records"]) == pytest.approx(1.0)


@pytest.mark.db
def test_the_store_is_measured_into_groups_of_dependent_features():
    from sdg.api.app import service

    groups = {tuple(sorted(g.elements)): g for g in service.combinations(refresh=True)}
    elements = {e for key in groups for e in key}
    together = lambda a, b: any(a in key and b in key for key in groups)  # noqa: E731
    assert together("DIAGNOSIS_CODE", "PROCEDURE_CODE"), "always a group"
    assert together("COVERAGE_PLAN_ID", "PRODUCT_CODE"), "a plan belongs to one product"
    assert together("MARITAL_STATUS", "RELATIONSHIP_CODE")
    assert "SEX" not in elements or not together("SEX", "MARITAL_STATUS"), \
        "independent features are left apart"
    for group in groups.values():
        assert group.combinations and all(len(v) == len(group.elements)
                                          for v, _ in group.combinations)


@pytest.mark.db
def test_a_plain_load_writes_only_combinations_the_store_holds():
    from sdg.api.app import service
    from sdg.engine.execute import run_plan

    plan = service.plan if service.plan and not service.plan_stale() else service.build()[0]
    result = run_plan(plan, limit=300, reference_sets=service.reference_sets_for_run())

    def held(store: str, elements: list[str]) -> set[tuple[str, ...]]:
        column = {e: c for c, e in combos.STORE_ELEMENTS[store].items()}
        return combos.held_combinations(service.registry, store, [column[e] for e in elements])

    # Within one table: marital status with relationship.
    members = result.rows("dbo.CMC_MEME_MEMBER")
    known = held("member_denorm", ["MARITAL_STATUS", "RELATIONSHIP_CODE"])
    assert members and all(
        (str(m["MEME_MARITAL_STATUS"]).strip(), str(m["MEME_REL"]).strip()) in known
        for m in members)

    # Across a claim and its lines: what the claim carries with each procedure.
    fields = {(t.name, f.name): f for t in plan.tables for f in t.fields}
    key = fields[("dbo.CMC_CDML_CL_LINE", "CDML_SVC_CD")].params["record_key"]
    on_claim = [(f.name, f.params["field"]) for (t, _), f in fields.items()
                if t == "dbo.CMC_CLCL_CLAIM" and f.params.get("record_key") == key]
    elements = [e for _, e in on_claim] + ["PROCEDURE_CODE"]
    known = held("claims_denorm", elements)
    claims = {c["CLCL_ID"]: c for c in result.rows("dbo.CMC_CLCL_CLAIM")}
    lines = result.rows("dbo.CMC_CDML_CL_LINE")
    assert lines
    for line in lines:
        claim = claims[line["CLCL_ID"]]
        # No network (out of network) is held as empty.
        combo = (tuple("" if claim[c] is None else str(claim[c]).strip() for c, _ in on_claim)
                 + (str(line["CDML_SVC_CD"]).strip(),))
        assert combo in known, f"{elements} = {combo} is not a combination the store holds"

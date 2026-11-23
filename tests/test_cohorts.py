"""A pattern applied to a share of the population rather than to every row.

Until this existed a discovered pattern could only be generated whole: ask for
a cohort covering three per cent of members and you got a population where
every member looked like it, which is not the population the analyzer saw.

Three things have to hold together, and each is useless alone:

    the share       lands near the figure asked for, drawn from the seed so the
                    same plan puts the same rows in the cohort every time
    the inheritance a member in the cohort has all of their claims in it, or a
                    pattern spanning two tables says nothing
    the remainder   everyone outside is generated as measured, untouched
"""

from __future__ import annotations

import time

import pytest

from sdg.engine.execute import run_plan
from sdg.plan.model import Cohort, FieldOverride, Plan, PlanField, PlanTable, new_meta
from sdg.plan.patterns import measure_cohorts

MEMBER = "dbo.CMC_MEME_MEMBER"
LINE = "dbo.CMC_CDML_CL_LINE"


def _plan(share: float = 0.25, cohorts: int = 1) -> Plan:
    """A root table and a child, with a cohort over the root."""
    return Plan(
        meta=new_meta("cohort-test", "seed-one"),
        cohorts=[
            Cohort(name=f"C{i}", subject="dbo.PARENT", share=share)
            for i in range(cohorts)
        ],
        tables=[
            PlanTable(
                name="dbo.PARENT", mode="generate", count=400, level=0,
                fields=[
                    PlanField(
                        name="KIND", bucket="free", generator="constant",
                        params={"value": "base"}, provenance="learned",
                        overrides=[
                            FieldOverride(
                                cohort=f"C{i}", generator="constant",
                                params={"value": f"cohort{i}"},
                            )
                            for i in range(cohorts)
                        ],
                    ),
                    PlanField(
                        name="PK", bucket="business_key", generator="sequence",
                        params={"start": 1}, provenance="structural",
                    ),
                ],
            ),
            PlanTable(
                name="dbo.CHILD", mode="generate", level=1,
                driving_parent="dbo.PARENT",
                ratio={"parent": "dbo.PARENT", "kind": "fixed", "value": 3},
                fields=[
                    PlanField(
                        name="MARK", bucket="free", generator="constant",
                        params={"value": "base"}, provenance="learned",
                        overrides=[
                            FieldOverride(
                                cohort="C0", generator="constant",
                                params={"value": "inherited"},
                            )
                        ],
                    ),
                ],
            ),
        ],
        generation_order=["dbo.PARENT", "dbo.CHILD"],
    )


def _tagged(result, table):
    rows = result.tables[table]
    return [row for row, tag in zip(rows.rows, rows.tags) if tag]


def _untagged(result, table):
    rows = result.tables[table]
    return [row for row, tag in zip(rows.rows, rows.tags) if not tag]


# ---------------------------------------------------------------------------
# The share
# ---------------------------------------------------------------------------

def test_the_cohort_gets_about_the_share_it_asked_for():
    result = run_plan(_plan(share=0.25))
    rows = result.tables["dbo.PARENT"]
    inside = sum(1 for tag in rows.tags if tag)
    assert 0.18 <= inside / len(rows.rows) <= 0.32, (
        f"{inside} of {len(rows.rows)} is not near a quarter"
    )


def test_the_same_seed_puts_the_same_rows_in_the_cohort():
    """Drawn from the seed, so nothing has to be stored between runs."""
    first = run_plan(_plan())
    second = run_plan(_plan())
    assert first.tables["dbo.PARENT"].tags == second.tables["dbo.PARENT"].tags


def test_a_different_seed_draws_a_different_cohort():
    plan = _plan()
    first = run_plan(plan)
    plan.meta.seed = "seed-two"
    second = run_plan(plan)
    assert first.tables["dbo.PARENT"].tags != second.tables["dbo.PARENT"].tags


def test_rows_inside_the_cohort_take_its_rule_and_the_rest_do_not():
    result = run_plan(_plan())
    assert {r["KIND"] for r in _tagged(result, "dbo.PARENT")} == {"cohort0"}
    assert {r["KIND"] for r in _untagged(result, "dbo.PARENT")} == {"base"}


# ---------------------------------------------------------------------------
# The inheritance
# ---------------------------------------------------------------------------

def test_a_cohort_members_children_are_in_it_too():
    """Without this a pattern spanning two tables says nothing: the members
    would be young and their claims would belong to someone else."""
    result = run_plan(_plan())
    assert {r["MARK"] for r in _tagged(result, "dbo.CHILD")} == {"inherited"}
    assert {r["MARK"] for r in _untagged(result, "dbo.CHILD")} == {"base"}


def test_the_inherited_share_matches_the_parents():
    result = run_plan(_plan(share=0.25))
    parents = result.tables["dbo.PARENT"]
    children = result.tables["dbo.CHILD"]
    parent_share = sum(1 for t in parents.tags if t) / len(parents.tags)
    child_share = sum(1 for t in children.tags if t) / len(children.tags)
    # Every parent has the same number of children here, so the shares match.
    assert abs(parent_share - child_share) < 0.01


# ---------------------------------------------------------------------------
# More than one
# ---------------------------------------------------------------------------

def test_two_cohorts_are_tracked_separately():
    result = run_plan(_plan(share=0.3, cohorts=2))
    tags = result.tables["dbo.PARENT"].tags
    assert any(t & 1 for t in tags), "the first cohort got nobody"
    assert any(t & 2 for t in tags), "the second cohort got nobody"
    # The first override listed wins where a row is in both, which is the
    # plan's own order and the only rule that does not depend on dict order.
    both = [i for i, t in enumerate(tags) if t & 1 and t & 2]
    if both:
        assert result.tables["dbo.PARENT"].rows[both[0]]["KIND"] == "cohort0"


# ---------------------------------------------------------------------------
# What it costs
# ---------------------------------------------------------------------------

def test_a_plan_without_cohorts_pays_nothing_for_them():
    """The machinery has to be free when unused, or every plan pays for a
    feature most of them never ask for."""
    plan = _plan()
    plan.cohorts = []
    for table in plan.tables:
        for field in table.fields:
            field.overrides = None

    result = run_plan(plan)
    assert result.tables["dbo.PARENT"].tags == [], "tags were built with no cohorts"


def test_tagging_does_not_change_what_untagged_rows_generate():
    """The gap this guards: a cohort quietly shifting the rest of the run,
    which would make the remainder no longer the measured population."""
    plain = _plan()
    plain.cohorts = []
    for table in plain.tables:
        for field in table.fields:
            field.overrides = None
    baseline = run_plan(plain)

    tagged = run_plan(_plan())
    untouched = _untagged(tagged, "dbo.PARENT")
    # Same seed, same positions: a row outside the cohort is the row it would
    # have been without the cohort existing at all.
    by_key = {r["PK"]: r for r in baseline.tables["dbo.PARENT"].rows}
    for row in untouched:
        assert row["KIND"] == by_key[row["PK"]]["KIND"]


# ---------------------------------------------------------------------------
# Reporting it
# ---------------------------------------------------------------------------

def test_what_the_cohort_actually_got_is_measurable():
    plan = _plan(share=0.25)
    result = run_plan(plan)
    measured = measure_cohorts(plan, result)
    assert len(measured) == 1
    row = measured[0]
    assert row["cohort"] == "C0"
    assert row["asked"] == 0.25
    assert row["rows"] == sum(1 for t in result.tables["dbo.PARENT"].tags if t)
    assert abs(row["actual"] - 0.25) < 0.08


def test_a_cohort_whose_subject_was_not_generated_says_so():
    plan = _plan()
    plan.cohorts = [Cohort(name="C0", subject="dbo.CHILD", share=0.2)]
    result = run_plan(plan)
    result.tables["dbo.CHILD"].tags = []
    note = measure_cohorts(plan, result)[0]
    assert note["actual"] is None
    assert "not generated" in note["note"]


# ---------------------------------------------------------------------------
# Against the real plan
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_a_cohort_on_the_real_plan_leaves_the_rest_as_measured():
    from sdg.api.service import Service

    service = Service()
    plan = service.require_plan()
    sets = service.reference_sets_for_run()

    plan.cohorts = [Cohort(name="P-test", subject=MEMBER, share=0.05)]
    member = plan.table(MEMBER)
    member.field("MEME_SEX").overrides = [
        FieldOverride(cohort="P-test", generator="constant", params={"value": "F"})
    ]

    result = run_plan(plan, limit=300, reference_sets=sets)
    rows = result.tables[MEMBER]
    inside = [r for r, t in zip(rows.rows, rows.tags) if t]
    outside = [r for r, t in zip(rows.rows, rows.tags) if not t]

    assert inside, "the cohort got nobody"
    assert all(r["MEME_SEX"] == "F" for r in inside)
    assert len({r["MEME_SEX"] for r in outside}) > 1, (
        "everyone outside the cohort should still be generated as measured"
    )


@pytest.mark.db
def test_a_cohort_costs_little_on_a_real_run():
    """Measured rather than assumed. The tag is one integer per row and one
    `and` per overridden field, which should not be visible at this scale."""
    from sdg.api.service import Service

    service = Service()
    sets = service.reference_sets_for_run()

    plain = service.require_plan()
    plain.cohorts = []
    started = time.perf_counter()
    run_plan(plain, limit=600, reference_sets=sets)
    without = time.perf_counter() - started

    tagged = service.require_plan()
    tagged.cohorts = [Cohort(name="P-test", subject=MEMBER, share=0.05)]
    tagged.table(MEMBER).field("MEME_SEX").overrides = [
        FieldOverride(cohort="P-test", generator="constant", params={"value": "F"})
    ]
    started = time.perf_counter()
    run_plan(tagged, limit=600, reference_sets=sets)
    with_cohort = time.perf_counter() - started

    assert with_cohort < without * 1.5 + 0.5, (
        f"a cohort cost {with_cohort:.2f}s against {without:.2f}s without one"
    )

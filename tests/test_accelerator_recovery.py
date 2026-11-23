"""Recovery, measured: how much of each demo source's hand-defined hierarchy
the proposer gives back with no hint (docs/day0-accelerator-design.md §6).

Scored per source and role, from the recorded fixtures (no database). A level
is matched by its id, TABLE.COLUMN, never by its column name alone:
    found_top6    expected levels (walk and narrow) among the first six candidates
    in_order      expected walk levels that are selected and in the expected
                  relative order (the longest run of them, in order, among the
                  selected walk levels)
    primary_top3  the expected primary table among the top three candidates
    child_top3    the expected child table among the top three candidates

The scores kept in tests/fixtures/accelerator/recovery.json may only improve,
and never fall below FLOORS, written here so an edit of the JSON cannot lower
them. A rise fails too, until it is kept:
    set SDM_RECORD_BASELINES=1 && pytest tests/test_accelerator_recovery.py

On these two fixtures the kept scores are the ceiling: every expected level is
in the top six, every expected walk level selected in order, and each record
table in the top three (test_the_floors_are_the_ceiling_on_these_fixtures).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from sdg.accelerator import plan
from sdg.accelerator.landscape import Landscape
from sdg.discovery.model import PhysicalModel
from sdg.profiling.model import ProfileReport

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
KEPT = FIXTURES / "recovery.json"
RECORDING = os.getenv("SDM_RECORD_BASELINES") == "1"
SOURCES = ["facets_qa", "bank"]
ROLES = ["primary", "child"]
TOP_LEVELS = 6
TOP_RECORDS = 3
SCORE_NAMES = {role: ("found_top6", "in_order", f"{role}_top3") for role in ROLES}

# The least each score may be, whatever recovery.json says.
FLOORS = {
    "facets_qa": {"primary": {"found_top6": 4, "in_order": 4, "primary_top3": 1},
                  "child": {"found_top6": 4, "in_order": 3, "child_top3": 1}},
    "bank": {"primary": {"found_top6": 1, "in_order": 1, "primary_top3": 1},
             "child": {"found_top6": 3, "in_order": 2, "child_top3": 1}},
}


def landscape_of(source: str) -> Landscape:
    return Landscape(name=source.upper(),
                     model=PhysicalModel.load(FIXTURES / f"{source}-model.json"),
                     profile=ProfileReport.load(FIXTURES / f"{source}-profile.json"),
                     summary={})


def _in_order(chosen: list[str], wanted: list[str]) -> int:
    """The longest run of `wanted`, in its order, inside `chosen` (an LCS)."""
    best = [[0] * (len(wanted) + 1) for _ in range(len(chosen) + 1)]
    for i, have in enumerate(chosen, start=1):
        for j, want in enumerate(wanted, start=1):
            best[i][j] = (best[i - 1][j - 1] + 1 if have == want
                          else max(best[i - 1][j], best[i][j - 1]))
    return best[-1][-1]


def scores(held: plan.SourcePlan, expected: dict,
           table: Callable[[str], str] = lambda t: t,
           level: Callable[[str], str] = lambda i: i) -> dict[str, dict[str, int]]:
    """The four scores of a plan against one source's expected hierarchy,
    levels matched by id. `table` and `level` translate the expected table
    names and level ids (for a renamed copy)."""
    out: dict[str, dict[str, int]] = {}
    for role in ROLES:
        wanted = expected[role]
        levels = sorted(held.levels(role), key=lambda lv: lv.order)
        first = {lv.id for lv in levels[:TOP_LEVELS]}
        walk = [level(i) for i in wanted["walk"]]
        every = walk + [level(i) for i in wanted["narrow"]]
        chosen = [lv.id for lv in levels if lv.selected and lv.kind == "walk"]
        top = [c.table for c in held.candidates.get(role, [])[:TOP_RECORDS]]
        out[role] = {
            "found_top6": sum(1 for c in every if c in first),
            "in_order": _in_order(chosen, walk),
            f"{role}_top3": int(table(wanted["table"]) in top),
        }
    return out


def measured() -> dict[str, dict[str, dict[str, int]]]:
    return {source: scores(plan.propose(landscape_of(source)), EXPECTED[source])
            for source in SOURCES}


def _shape(scored: dict) -> dict[str, dict[str, set[str]]]:
    return {source: {role: set(names) for role, names in roles.items()}
            for source, roles in scored.items()}


EXPECTED_SHAPE = {source: {role: set(SCORE_NAMES[role]) for role in ROLES} for source in SOURCES}


def test_recovery_may_only_improve():
    now = measured()
    assert _shape(now) == EXPECTED_SHAPE
    # Only the first recording starts from nothing; otherwise a missing file fails.
    first = RECORDING and not KEPT.exists()
    kept = {} if first else json.loads(KEPT.read_text(encoding="utf-8"))
    if not first:
        # Exactly the scores measured: none missing, none extra.
        assert _shape(kept) == EXPECTED_SHAPE, f"recovery.json holds {_shape(kept)}"
    below = {f"{source} {role} {name}": f"{value} < {FLOORS[source][role][name]}"
             for source in SOURCES for role in ROLES
             for name, value in now[source][role].items()
             if value < FLOORS[source][role][name]}
    fallen = {f"{source} {role} {name}": f"{kept[source][role][name]} -> {value}"
              for source in SOURCES for role in ROLES
              for name, value in now[source][role].items()
              if not first and value < kept[source][role][name]}
    risen = {f"{source} {role} {name}": f"{kept[source][role][name]} -> {value}"
             for source in SOURCES for role in ROLES
             for name, value in now[source][role].items()
             if not first and value > kept[source][role][name]}
    # Checked before anything is kept: recording keeps a rise, never a fall.
    assert not below, f"recovery fell below its floor: {below}"
    assert not fallen, f"recovery fell: {fallen}"
    if RECORDING:
        KEPT.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    else:
        assert not risen, f"improved: record it with SDM_RECORD_BASELINES=1: {risen}"


def test_the_floors_are_the_ceiling_on_these_fixtures():
    """Every floor is the most its score can be against expected.json, so on
    these fixtures the baseline cannot rise: the ratchet guards a fall."""
    for source in SOURCES:
        for role in ROLES:
            wanted = EXPECTED[source][role]
            assert FLOORS[source][role] == {
                "found_top6": len(wanted["walk"]) + len(wanted["narrow"]),
                "in_order": len(wanted["walk"]),
                f"{role}_top3": 1,
            }


def test_the_scores_are_measured_as_described():
    """Each score at its bounds, so a kept score means what the docstring says."""
    for source in SOURCES:
        held = plan.propose(landscape_of(source))
        got = scores(held, EXPECTED[source])
        for role in ROLES:
            wanted = EXPECTED[source][role]
            assert 0 <= got[role]["found_top6"] <= len(wanted["walk"]) + len(wanted["narrow"])
            assert 0 <= got[role]["in_order"] <= len(wanted["walk"])
            assert got[role][f"{role}_top3"] in (0, 1)
        # An expected hierarchy nothing in the source carries scores nothing.
        nothing = {r: {"table": "dbo.NOT_THERE", "walk": ["dbo.NOT_THERE.NOT_A"],
                       "narrow": ["dbo.NOT_THERE.NOT_B"]} for r in ROLES}
        assert all(v == 0 for r in ROLES for v in scores(held, nothing)[r].values())
        # A level is matched by its table too: the right column on another
        # table scores nothing.
        elsewhere = {r: {"table": "dbo.NOT_THERE",
                         "walk": [f"dbo.NOT_THERE.{i.rpartition('.')[2]}"
                                  for i in EXPECTED[source][r]["walk"]],
                         "narrow": [f"dbo.NOT_THERE.{i.rpartition('.')[2]}"
                                    for i in EXPECTED[source][r]["narrow"]]} for r in ROLES}
        assert all(v == 0 for r in ROLES for v in scores(held, elsewhere)[r].values())
    assert _in_order(["A", "X", "C", "B"], ["A", "B", "C"]) == 2
    assert _in_order(["A", "B", "C"], ["A", "B", "C"]) == 3

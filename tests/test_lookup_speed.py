"""The faster lookup draws exactly what the slower one drew.

A lookup used to pass over every record, and sum every weight, on each row;
it now works those out once per list and keeps them. Replay promises the same
bytes, so the draw must not move. tests/fixtures/lookup_before_speedup.py is
the lookup as it was, kept word for word, and every case here is drawn by both.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from sdg.engine.generators import base
from sdg.engine.generators import lookup as lookup_module
from sdg.engine.generators.base import GenerationError, RowContext
from sdg.engine.seed import ValueRandom, running_totals

BEFORE = Path(__file__).parent / "fixtures" / "lookup_before_speedup.py"


def _load_before() -> types.ModuleType:
    """The old lookup as a module of its own, not registered over the new one."""
    module = types.ModuleType("lookup_before_speedup")
    real = base.register
    base.register = lambda name, version=1: (lambda fn: fn)
    try:
        exec(compile(BEFORE.read_text(encoding="utf-8"), str(BEFORE), "exec"), module.__dict__)
    finally:
        base.register = real
    return module


_OLD = _load_before()


@pytest.fixture(autouse=True)
def _cached_as_in_a_run():
    """Drawn with the caches kept, as a plan's run keeps them, so a case drawn
    twice is answered from them the second time."""
    with lookup_module.caching():
        yield


def _drawn(lookup, ctx: RowContext) -> Any:
    """What a lookup gives, or the error it refuses with."""
    try:
        return lookup(ctx)
    except (GenerationError, ValueError) as error:
        return (type(error).__name__, str(error))


def _same(ctx: RowContext, label: Any) -> None:
    expected = _drawn(_OLD.lookup, ctx)
    assert _drawn(lookup_module.lookup, ctx) == expected, label
    assert _drawn(lookup_module.lookup, ctx) == expected, (label, "from the caches")


# -- the weighted draw ------------------------------------------------------

def test_running_totals_pick_what_weighted_index_picks():
    picker = ValueRandom(7)
    for case in range(400):
        size = 1 + picker.below(40)
        # Zeros, ties and fractions, where an off-by-one would show.
        weights = [[0, 1, 2.5, 0.1, 3][picker.below(5)] for _ in range(size)]
        if not any(weights):
            weights[-1] = 1
        running = running_totals(weights)
        for row in range(25):
            one = ValueRandom.for_value("seed", "scope", f"f{case}", row)
            two = ValueRandom.for_value("seed", "scope", f"f{case}", row)
            assert two.cumulative_index(running) == one.weighted_index(weights)
            # And the stream is left where weighted_index leaves it.
            assert two.next_u64() == one.next_u64()


class _Fixed(ValueRandom):
    """A stream whose next number is given, to land a draw on a boundary."""

    def __init__(self, number: float) -> None:
        super().__init__(0)
        self.number = number

    def random(self) -> float:
        return self.number


@pytest.mark.parametrize("weights, number", [
    ([1, 1], 0.5),          # exactly on the first total: the second is picked
    ([1, 0, 1], 0.5),       # on a total a zero weight repeats: skip past it
    ([0, 2, 0], 0.0),       # the very first number: the first weighted one
    ([3, 0, 0], 0.999999),  # the end: never past the last
    ([1, 2, 3], 0.5),
])
def test_a_draw_on_a_boundary_picks_what_weighted_index_picks(weights, number):
    assert (_Fixed(number).cumulative_index(running_totals(weights))
            == _Fixed(number).weighted_index(weights))


def test_running_totals_refuse_what_weighted_index_refuses():
    with pytest.raises(ValueError, match="negative"):
        running_totals([1, -1])
    with pytest.raises(ValueError, match="sum to zero"):
        running_totals([0, 0])


# -- the lookup, against the one it replaced --------------------------------

def _records(rnd: ValueRandom, count: int) -> list[dict[str, Any]]:
    return [{"DX": f"D{rnd.below(6)}", "PLAN": f"P{rnd.below(3)}",
             "PROC": f"X{rnd.below(9)}", "GROUP": f"G{rnd.below(2)}",
             # Some records point at no diagnosis: never outside a table's keys.
             "CODE": None if rnd.below(7) == 0 else f"C{rnd.below(12)}"}
            for _ in range(count)]


def _weights(rnd: ValueRandom, count: int) -> list[float]:
    return [[0, 0.1, 0.25, 1, 2.5, 7][rnd.below(6)] or 0.0 for _ in range(count)]


GIVEN = {
    "plain": None,
    "same_row": {"DX": "DX"},
    "across_tables": {"DX": "dbo.CLAIM.DX"},
    # A claim's values and its member's: when none agree with both, the least
    # telling field is let go of, then the nearest row comes first.
    "two_rows_above": {"DX": "dbo.CLAIM.DX", "PLAN": "dbo.MEMBER.PLAN",
                       "GROUP": "dbo.MEMBER.GROUP"},
}


def _context(params: dict[str, Any], row: int, **extra: Any) -> RowContext:
    held = ValueRandom.for_value("held", "s", "x", row)
    claim = {"DX": f"D{held.below(8)}"}  # D6 and D7 agree with nothing
    member = {"PLAN": f"P{held.below(4)}", "GROUP": f"G{held.below(3)}"}
    return RowContext(seed="seed", scenario="scope", table="dbo.LINE", field_name="PROC",
                      params=params, row_index=row, row={"DX": claim["DX"]},
                      parents={"dbo.CLAIM": claim, "dbo.MEMBER": member},
                      driving_parent="dbo.CLAIM", **extra)


@pytest.mark.parametrize("weighted", [True, False])
@pytest.mark.parametrize("shape", list(GIVEN))
def test_the_lookup_draws_what_it_drew_before(weighted, shape):
    maker = ValueRandom(11)
    values = _records(maker, 60)
    params: dict[str, Any] = {"values": values, "field": "PROC"}
    if weighted:
        params["weights"] = _weights(maker, len(values))
        params["weights"][0] = 1  # never all zero
    if GIVEN[shape]:
        params |= {"record_key": "combo", "given": GIVEN[shape]}
    for row in range(500):
        _same(_context(params, row), (shape, row))


@pytest.mark.parametrize("weighted", [True, False])
@pytest.mark.parametrize("filtered", [False, True])
def test_a_lookup_kept_within_a_tables_keys_draws_what_it_drew_before(weighted, filtered):
    maker = ValueRandom(13)
    values = _records(maker, 80)
    params: dict[str, Any] = {"values": values, "field": "CODE", "within": "dbo.DIAG",
                              "record_key": "combo", "given": {"DX": "dbo.CLAIM.DX"}}
    if weighted:
        params["weights"] = [1 + maker.below(4) for _ in values]
    keys = [f"C{i}" for i in range(0, 12, 2)]
    attributes = {k: {"CHAPTER": "A" if int(k[1:]) < 6 else "B"} for k in keys}
    if filtered:
        params |= {"filter_attribute": "CHAPTER", "filter_values": ["A"]}
    for row in range(300):
        _same(_context(params, row, parent_keys={"dbo.DIAG": keys},
                       parent_attributes={"dbo.DIAG": attributes}), (filtered, row))


@pytest.mark.parametrize("case", ["no_values", "length_mismatch", "zero_weights",
                                  "negative_weight", "not_a_record", "no_such_field",
                                  "none_within"])
def test_a_lookup_refuses_what_it_refused_before(case):
    records = [{"DX": "D1", "PROC": "X1", "CODE": "C1"}, {"DX": "D2", "PROC": "X2", "CODE": "C2"}]
    params: dict[str, Any] = {
        "no_values": {"values": [], "field": "PROC"},
        "length_mismatch": {"values": records, "weights": [1], "field": "PROC"},
        "zero_weights": {"values": records, "weights": [0, 0], "field": "PROC"},
        "negative_weight": {"values": records, "weights": [1, -1], "field": "PROC"},
        "not_a_record": {"values": ["a", "b"], "field": "PROC"},
        "no_such_field": {"values": records, "field": "NOPE"},
        "none_within": {"values": records, "field": "CODE", "within": "dbo.DIAG"},
    }[case]
    ctx = _context(params, 0, parent_keys={"dbo.DIAG": ["C9"]})
    drawn = _drawn(lookup_module.lookup, ctx)
    assert isinstance(drawn, tuple), drawn  # refused, by both
    assert drawn == _drawn(_OLD.lookup, ctx)


def test_a_run_after_another_draws_what_a_fresh_one_draws():
    """The server keeps one process for many runs: a run after another, of the
    same plan with its weights since changed in place, draws what a process of
    its own draws -- each run empties the caches as it starts."""
    from sdg.engine.execute import run_plan
    from tests.test_clinical_pairs import Plan, PlanField, PlanTable, new_meta

    maker = ValueRandom(17)
    params = {"values": _records(maker, 50), "weights": _weights(maker, 50), "field": "PROC",
              "record_key": "combo", "given": {"DX": "DX"}}
    params["weights"][0] = 1
    plan = Plan(meta=new_meta("runs", "seed-runs"), tables=[
        PlanTable(name="dbo.LINE", mode="generate", count=300, level=0, fields=[
            PlanField(name="DX", bucket="code", generator="lookup",
                      params={"values": params["values"], "field": "DX"},
                      provenance="learned"),
            PlanField(name="PROC", bucket="code", generator="lookup", params=params,
                      provenance="learned"),
        ]),
    ], generation_order=["dbo.LINE"])

    def drawn() -> list[Any]:
        return [r["PROC"] for r in run_plan(plan).rows("dbo.LINE")]

    before = drawn()
    params["weights"][:] = [w or 3 for w in reversed(params["weights"])]
    after = drawn()
    assert after != before  # the change in place is seen ...
    lookup_module.forget()
    assert after == drawn()  # ... as a fresh run sees it


def test_a_new_list_is_never_answered_from_an_old_lists_entry():
    old = [{"DX": "D1", "PROC": "OLD"}]
    params = {"values": old, "field": "PROC", "record_key": "k", "given": {"DX": "DX"}}
    ctx = RowContext(seed="s", scenario="c", table="t", field_name="PROC",
                     params=params, row_index=0, row={"DX": "D1"})
    assert lookup_module.lookup(ctx) == "OLD"
    new = [{"DX": "D1", "PROC": "NEW"}]
    ctx.params = {**params, "values": new}
    assert lookup_module.lookup(ctx) == "NEW"


def _cache_sizes() -> list[int]:
    return [len(c) for c in (lookup_module._WITHIN, lookup_module._SPREAD,
                             lookup_module._RUNNING, lookup_module._AGREEING)]


def test_nothing_is_kept_outside_a_run_or_after_one():
    """A rule's values are made one at a time, from lists built for each: they
    are worked out afresh, and a run lets its lists go when it ends."""
    from sdg.engine.execute import run_plan
    from sdg.engine.rules import run as rule_value
    from tests.test_clinical_pairs import Plan, PlanField, PlanTable, new_meta

    token = lookup_module._CACHING.set(False)  # as outside any run
    try:
        rule = {"gen_method": "lookup", "domain": "A, B, C", "weights": "1, 2, 3"}
        drawn = {rule_value(rule, seed="s", row_index=i, field="F") for i in range(100)}
        assert drawn <= {"A", "B", "C"} and len(drawn) == 3
        assert _cache_sizes() == [0, 0, 0, 0]

        records = _records(ValueRandom(5), 30)
        plan = Plan(meta=new_meta("kept", "seed-kept"), tables=[
            PlanTable(name="dbo.LINE", mode="generate", count=50, level=0, fields=[
                PlanField(name="DX", bucket="code", generator="lookup",
                          params={"values": records, "field": "DX"}, provenance="learned"),
                PlanField(name="PROC", bucket="code", generator="lookup",
                          params={"values": records, "field": "PROC", "weights": [1] * 30,
                                  "record_key": "c", "given": {"DX": "DX"}},
                          provenance="learned"),
            ]),
        ], generation_order=["dbo.LINE"])
        assert len(run_plan(plan).rows("dbo.LINE")) == 50
        assert _cache_sizes() == [0, 0, 0, 0]
    finally:
        lookup_module._CACHING.reset(token)

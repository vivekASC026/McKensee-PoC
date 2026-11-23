"""Step 3 acceptance: the seed engine, the generator library, and the dry run.

Determinism and referential integrity are property-based, as the build brief
requires. The pattern generator is checked against the real `re` module.
"""

from __future__ import annotations

import copy
import json
import random
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from sdg.config import REPO_ROOT
from sdg.engine.dryrun import dry_run
from sdg.engine.execute import field_dependencies, ordered_fields, run_plan
from sdg.engine.pattern import PatternError, compile_pattern, generate_many, pattern_space
from sdg.engine.seed import ValueRandom, value_seed
from sdg.plan.model import Plan, PlanTable
from sdg.writers.csvfile import write_run

SAMPLE_PATH = REPO_ROOT / "examples" / "plan-sample.json"

TEN_PATTERNS = [
    "[A-Z]{3}[0-9]{6}",
    "[A-Z]{3}[0-9]{8}",
    "[0-9]{12}",
    "[A-Z][0-9]{4}",
    "PD[0-9]{6}",
    "[A-Z]{2}[0-9]{4}",
    "[0-9]{3}-[0-9]{2}-[0-9]{4}",
    "(AB|CD|EF)[0-9]{3}",
    "[A-Z][a-z]{2,5}",
    "[0-9]{10}",
]


@pytest.fixture(scope="module")
def sample_plan() -> Plan:
    return Plan.load(SAMPLE_PATH)


# ---------------------------------------------------------------------------
# Seed engine
# ---------------------------------------------------------------------------

@given(
    seed=st.text(min_size=1, max_size=20),
    scope=st.text(min_size=1, max_size=20),
    field=st.text(min_size=1, max_size=20),
    row=st.integers(min_value=-1, max_value=10**9),
)
def test_value_seed_is_stable_and_in_range(seed, scope, field, row):
    first = value_seed(seed, scope, field, row)
    second = value_seed(seed, scope, field, row)
    assert first == second
    assert 0 <= first < 2**64


@given(
    seed=st.text(min_size=1, max_size=10),
    scope=st.text(min_size=1, max_size=10),
    field=st.text(min_size=1, max_size=10),
)
def test_value_seed_separates_neighboring_rows(seed, scope, field):
    values = {value_seed(seed, scope, field, row) for row in range(32)}
    # Collisions in 32 draws from a 64-bit space would mean the hash is broken.
    assert len(values) == 32


def test_value_seed_is_pinned():
    # Pinned so a change to the hashing shows up as a failing test, not as data
    # that quietly stops matching a previous run.
    assert value_seed("demo-seed-001", "facets-slice-sample", "MEME_ID", 0) == 1238109456519918223


@given(state=st.integers(min_value=0, max_value=2**64 - 1))
def test_value_random_bounds(state):
    rnd = ValueRandom(state)
    assert 0.0 <= rnd.random() < 1.0
    assert 0 <= rnd.below(7) < 7
    assert 3 <= rnd.randint(3, 9) <= 9
    assert rnd.weighted_index([1.0, 0.0, 2.0]) in (0, 2)


def test_value_random_weights_follow_their_proportions():
    counts = [0, 0, 0]
    for row in range(6000):
        counts[ValueRandom(value_seed("s", "sc", "f", row)).weighted_index([0.5, 0.3, 0.2])] += 1
    shares = [c / 6000 for c in counts]
    assert shares[0] == pytest.approx(0.5, abs=0.02)
    assert shares[1] == pytest.approx(0.3, abs=0.02)
    assert shares[2] == pytest.approx(0.2, abs=0.02)


def test_weighted_index_rejects_zero_weights():
    with pytest.raises(ValueError):
        ValueRandom(1).weighted_index([0, 0])


# ---------------------------------------------------------------------------
# Pattern generator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("regex", TEN_PATTERNS)
def test_pattern_generates_only_matching_values(regex):
    """10,000 samples per pattern, checked against the real regex engine."""
    compiled = re.compile(f"^(?:{regex})$")
    values = generate_many(regex, 10_000, unique=False, offset=0)
    bad = [v for v in values if not compiled.match(v)]
    assert not bad, f"{regex}: {bad[:5]}"


@pytest.mark.parametrize("regex", TEN_PATTERNS)
def test_unique_pattern_values_are_distinct(regex):
    count = min(10_000, pattern_space(regex))
    values = generate_many(regex, count, unique=True, offset=4242)
    assert len(set(values)) == count


@pytest.mark.parametrize("regex", TEN_PATTERNS)
def test_unique_values_are_not_a_counter(regex):
    """Consecutive rows must not land a constant distance apart."""
    values = generate_many(regex, 40, unique=True, offset=99)
    digits = ["".join(c for c in v if c.isdigit()) for v in values]
    if not all(digits):
        return
    gaps = {int(b) - int(a) for a, b in zip(digits, digits[1:])}
    assert len(gaps) > 1, f"{regex} produced evenly spaced values"


def test_pattern_space_is_reported():
    assert pattern_space("[A-Z]{3}[0-9]{6}") == 26**3 * 10**6
    assert pattern_space("[0-9]{12}") == 10**12
    assert pattern_space("(AB|CD)[0-9]{2}") == 200


def test_pattern_exhausts_a_small_space_without_collisions():
    values = generate_many("(AB|CD)[0-9]{2}", 200, unique=True, offset=7)
    assert len(set(values)) == 200


@pytest.mark.parametrize("bad", ["[Z-A]", "[", "a{", "*x", "a{2,1}"])
def test_bad_patterns_are_rejected(bad):
    with pytest.raises(PatternError):
        compile_pattern(bad)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def _shuffle_fields(document: dict, rng: random.Random) -> dict:
    shuffled = copy.deepcopy(document)
    for table in shuffled["tables"]:
        rng.shuffle(table["fields"])
    return shuffled


def _snapshot(result, plan: Plan) -> dict:
    """Rows keyed by table, with columns in a fixed order."""
    out = {}
    for table in plan.tables_in_order():
        columns = sorted(f.name for f in table.fields if f.generator != "skip")
        out[table.name] = [
            tuple(str(row.get(c)) for c in columns) for row in result.rows(table.name)
        ]
    return out


def test_two_runs_of_the_same_plan_are_identical(sample_plan):
    first = run_plan(sample_plan, limit=20)
    second = run_plan(sample_plan, limit=20)
    assert _snapshot(first, sample_plan) == _snapshot(second, sample_plan)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(min_value=0, max_value=10_000))
def test_shuffling_field_order_does_not_change_the_data(seed):
    document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    baseline_plan = Plan.from_dict(document)
    baseline = _snapshot(run_plan(baseline_plan, limit=8), baseline_plan)

    shuffled_plan = Plan.from_dict(_shuffle_fields(document, random.Random(seed)))
    shuffled = _snapshot(run_plan(shuffled_plan, limit=8), shuffled_plan)

    assert shuffled == baseline


@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed_text=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12))
def test_a_different_seed_changes_the_data(seed_text):
    document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    baseline = _snapshot(run_plan(Plan.from_dict(document), limit=6), Plan.from_dict(document))

    document["meta"]["seed"] = seed_text
    changed_plan = Plan.from_dict(document)
    changed = _snapshot(run_plan(changed_plan, limit=6), changed_plan)

    if seed_text != "demo-seed-001":
        assert changed != baseline


def test_field_order_is_dependency_order_not_declaration_order(sample_plan):
    claims = sample_plan.table("dbo.CMC_CLCL_CLAIM")
    order = [f.name for f in ordered_fields(claims)]
    # Declaration order puts the paid date last of the three; dependency order
    # must also put it last, because each anchors on the one before it.
    assert order.index("CLCL_SVC_DT") < order.index("CLCL_RECD_DT")
    assert order.index("CLCL_RECD_DT") < order.index("CLCL_PAID_DT")


def test_driving_parent_needs_no_ordering_edge(sample_plan):
    """The driving parent's row is in place before any field runs.

    CLCL_SVC_DT anchors on a member field, and the member is the driving parent,
    so it does not have to wait for the MEME_CK reference to be evaluated.
    """
    claims = sample_plan.table("dbo.CMC_CLCL_CLAIM")
    assert field_dependencies(claims)["CLCL_SVC_DT"] == set()


def test_reference_to_a_non_driving_parent_is_ordered_first():
    """A parent reached through a plain reference must be resolved first."""
    table = PlanTable.model_validate({
        "name": "dbo.CHILD",
        "mode": "generate",
        "driving_parent": "dbo.DRIVER",
        "fields": [
            {
                "name": "WHEN", "bucket": "free", "generator": "date_relative",
                "params": {"anchor": "dbo.SIDE.SIDE_DT", "min_days": 0, "max_days": 5},
                "provenance": "learned",
            },
            {
                "name": "SIDE_ID", "bucket": "reference", "generator": "reference",
                "params": {"parent_table": "dbo.SIDE", "parent_column": "SIDE_ID", "pick": "hash"},
                "provenance": "structural",
            },
        ],
    })
    assert field_dependencies(table)["WHEN"] == {"SIDE_ID"}
    assert [f.name for f in ordered_fields(table)] == ["SIDE_ID", "WHEN"]


# ---------------------------------------------------------------------------
# Referential integrity
# ---------------------------------------------------------------------------

@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(root_count=st.integers(min_value=1, max_value=25))
def test_every_reference_resolves_to_a_generated_parent(root_count):
    document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    for table in document["tables"]:
        if table.get("count") is not None:
            table["count"] = root_count
    plan = Plan.from_dict(document)
    result = run_plan(plan)

    for table in plan.tables_in_order():
        for plan_field in table.fields:
            if plan_field.generator != "reference":
                continue
            parent = plan_field.params["parent_table"]
            column = plan_field.params["parent_column"]
            parent_keys = {row[column] for row in result.rows(parent)}
            for row in result.rows(table.name):
                assert row[plan_field.name] in parent_keys, (
                    f"{table.name}.{plan_field.name} points at {row[plan_field.name]!r}, "
                    f"which is not a key of {parent}"
                )


def test_composite_key_sequence_restarts_inside_each_parent(sample_plan):
    result = run_plan(sample_plan, limit=6)
    seen: set[tuple] = set()
    by_claim: dict[str, list[int]] = {}
    for row in result.rows("dbo.CMC_CDML_CL_LINE"):
        key = (row["CLCL_ID"], row["CDML_SEQ_NO"])
        assert key not in seen, f"duplicate composite key {key}"
        seen.add(key)
        by_claim.setdefault(row["CLCL_ID"], []).append(row["CDML_SEQ_NO"])
    assert by_claim
    for claim_id, sequence in by_claim.items():
        assert sequence == list(range(1, len(sequence) + 1)), claim_id


def test_unique_business_keys_are_unique_across_the_run(sample_plan):
    result = run_plan(sample_plan, limit=30)
    for table in sample_plan.tables_in_order():
        for plan_field in table.fields:
            if plan_field.generator == "pattern" and plan_field.params.get("unique"):
                values = [r[plan_field.name] for r in result.rows(table.name)]
                assert len(set(values)) == len(values), f"{table.name}.{plan_field.name}"


# ---------------------------------------------------------------------------
# Generators in context
# ---------------------------------------------------------------------------

def test_dates_hold_their_planted_ordering(sample_plan):
    result = run_plan(sample_plan, limit=10)
    for row in result.rows("dbo.CMC_CLCL_CLAIM"):
        assert row["CLCL_SVC_DT"] <= row["CLCL_RECD_DT"]
        assert (row["CLCL_RECD_DT"] - row["CLCL_SVC_DT"]).days <= 30
        if row["CLCL_PAID_DT"] is not None:
            gap = (row["CLCL_PAID_DT"] - row["CLCL_RECD_DT"]).days
            assert 5 <= gap <= 60


def test_null_rate_is_honored(sample_plan):
    result = run_plan(sample_plan, limit=400)
    members = result.rows("dbo.CMC_MEME_MEMBER")
    null_share = sum(1 for r in members if r["MEME_TERM_DT"] is None) / len(members)
    assert null_share == pytest.approx(0.85, abs=0.06)


def test_dependent_columns_draw_from_one_record(sample_plan):
    """ZIP, city, and state share a record_key, so they must stay consistent."""
    result = run_plan(sample_plan, limit=200)
    pairs: dict[str, tuple[str, str]] = {}
    for row in result.rows("dbo.CMC_MEME_MEMBER"):
        key = row["MEME_ZIP"]
        value = (row["MEME_CITY"], row["MEME_STATE"])
        assert pairs.setdefault(key, value) == value, f"ZIP {key} mapped two ways"
    assert len(pairs) > 1


def test_claim_total_equals_its_line_sum(sample_plan):
    result = run_plan(sample_plan, limit=20)
    totals: dict[str, object] = {}
    for row in result.rows("dbo.CMC_CDML_CL_LINE"):
        totals[row["CLCL_ID"]] = totals.get(row["CLCL_ID"], 0) + row["CDML_CHG_AMT"]
    claims = result.rows("dbo.CMC_CLCL_CLAIM")
    assert claims
    for claim in claims:
        assert claim["CLCL_TOT_CHG"] == totals.get(claim["CLCL_ID"], 0)


def test_skip_columns_are_absent_from_the_rows(sample_plan):
    result = run_plan(sample_plan, limit=5)
    for row in result.rows("dbo.CMC_CLCL_CLAIM"):
        assert "CLCL_CREATED_TS" not in row
        assert "CLCL_ROW_HASH" not in row


# ---------------------------------------------------------------------------
# Dry run and CSV
# ---------------------------------------------------------------------------

def test_dry_run_returns_frames_with_the_plan_column_order(sample_plan):
    run = dry_run(sample_plan, rows=5)
    frame = run.frames["dbo.CMC_MEME_MEMBER"]
    assert list(frame.columns)[:3] == ["MEME_CK", "SBSB_CK", "MEME_ID"]
    assert len(run.preview(5)["dbo.CMC_SBSB_SUBSC"]) == 5


def test_sample_plan_runs_end_to_end_to_csv(sample_plan, tmp_path: Path):
    result = run_plan(sample_plan, limit=25)
    written = write_run(result, sample_plan, tmp_path)
    assert set(written) == set(sample_plan.generation_order)
    for name, path in written.items():
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(result.rows(name)) + 1, name
        header = lines[0].split(",")
        assert "CLCL_ROW_HASH" not in header


def test_the_same_seed_writes_byte_identical_csv(sample_plan, tmp_path: Path):
    first = write_run(run_plan(sample_plan, limit=25), sample_plan, tmp_path / "a")
    second = write_run(run_plan(sample_plan, limit=25), sample_plan, tmp_path / "b")
    for name in first:
        assert first[name].read_bytes() == second[name].read_bytes(), name

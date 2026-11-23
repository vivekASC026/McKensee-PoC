"""Mapping an analyzer pattern onto the fields it governs.

The analyzer hands over labeled values -- "Proc Code: A0000", "Age: 24 yrs" --
and the tool has to decide which column each label is about. These tests cover
the three things that can go wrong quietly: a label bound to the wrong column, a
value that cannot survive the column it was bound to, and a parameter dropped
without anyone being told.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from sdg.catalog.loader import load_catalog
from sdg.catalog.model import Catalog, Element
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.discovery.run import discover
from sdg.mapping.scorer import ColumnMapping, MappingStore, map_columns
from sdg.plan.build import BuildInputs, build_plan, default_reference_sets
from sdg.plan.detect import detect_sum_expressions
from sdg.plan.model import Plan, PlanField, PlanTable, new_meta
from sdg.plan.patterns import (
    AnalyzerPattern,
    PatternDocument,
    PatternParameter,
    apply_to_decisions,
    canonical_tokens,
    clear_from_decisions,
    element_keys,
    load_document,
    match_derivation,
    match_element,
    resolve_document,
    resolve_pattern,
)
from sdg.plan.proposer import propose
from sdg.profiling.run import profile
from sdg.review.queue import load_decisions

AS_OF = "2026-09-30"
ROOT_COUNT = 5_000

MEMBER = "dbo.CMC_MEME_MEMBER"
CLAIM = "dbo.CMC_CLCL_CLAIM"
LINE = "dbo.CMC_CDML_CL_LINE"


# ---------------------------------------------------------------------------
# A plan built the way the tool builds one
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def world():
    """The real model, mappings, and plan. Patterns are resolved against these."""
    registry = ConnectionRegistry()
    model = discover(registry, "source").model
    report = profile(registry, model)
    catalog = load_catalog()
    mappings = map_columns(model, catalog, report)
    inputs = BuildInputs(
        model=model, catalog=catalog, mappings=mappings, report=report,
        proposals=propose(
            model, report, detect_sum_expressions(registry, model), mappings, catalog
        ),
        modes={
            "dbo.CMC_PRPR_PROV": "reuse",
            "dbo.CMC_PDPD_PRODUCT": "reuse",
            "dbo.CMC_GRGR_GROUP": "reuse",
            "dbo.CMC_SGSG_SUBGROUP": "reuse",
            "dbo.CMC_CSPD_CAT_DESC": "reuse",
            "dbo.CMC_CSPI_CS_PLAN": "reuse",
            "dbo.CMC_PLDS_PLAN_DESC": "reuse",
            "dbo.CMC_PDDS_PROD_DESC": "reuse",
            "dbo.CMC_IDCD_DIAG_CODE": "reuse",
            "dbo.CMC_IPCD_PROC_CODE": "reuse",
            "dbo.CMC_NWNW_NETWORK": "reuse",
            "dbo.CMC_NWPR_RELATION": "reuse",
            "dbo.CMC_NWST_NET_SET": "reuse",
            # Who a member's PCP is comes from the store; assigning PCPs
            # to new members is not generated yet.
            "dbo.CMC_MEPR_PRIM_PROV": "exclude",
            "dbo.CMC_CLCL_AUDIT": "exclude",
        },
        root_counts={"dbo.CMC_SBSB_SUBSC": ROOT_COUNT},
    )
    inputs.reference_sets = default_reference_sets(registry, inputs)
    plan, _graph = build_plan(inputs)
    return {"model": model, "catalog": catalog, "mappings": mappings, "plan": plan}


def _resolve(world, kind, parameters, *, as_of=AS_OF, subject=None):
    pattern = AnalyzerPattern(
        id="T-1", kind=kind, subject=subject,
        parameters=[PatternParameter(**p) for p in parameters],
    )
    return resolve_pattern(
        pattern,
        catalog=world["catalog"],
        mappings=world["mappings"],
        plan=world["plan"],
        model=world["model"],
        as_of=as_of,
    )


def _target(resolution, table, column):
    return next(
        (t for t in resolution.targets if t.table == table and t.column == column), None
    )


def _reason(resolution, starts_with):
    return next(
        (r for r in resolution.rejections if r.parameter.startswith(starts_with)), None
    )


# ---------------------------------------------------------------------------
# A label the analyzer wrote, an element the catalog owns
# ---------------------------------------------------------------------------

def test_prose_labels_reach_the_elements_the_database_is_mapped_to():
    """The gap this guards: the analyzer writes English, the catalog writes shorthand."""
    index = element_keys(load_catalog())
    for label, expected in [
        ("Proc Code", "PROCEDURE_CODE"),
        ("Procedure Code", "PROCEDURE_CODE"),
        ("HCPCS", "PROCEDURE_CODE"),
        ("Claim Status", "CLAIM_STATUS"),
        ("Date of Birth", "DOB"),
        ("DOB", "DOB"),
        ("State", "ADDRESS_STATE"),
        ("Zip Code", "ADDRESS_ZIP"),
        ("Charge Amount", "CHARGE_AMOUNT"),
        ("Service Units", "SERVICE_UNITS"),
        ("Relationship Code", "RELATIONSHIP_CODE"),
    ]:
        assert match_element(label, index)[0] == expected, label


def test_a_label_nothing_answers_to_is_reported_with_its_nearest_elements():
    index = element_keys(load_catalog())
    element, candidates = match_element("Widget Cd", index)
    assert element is None
    # Not bound, but not silent either: the report names where to look.
    assert candidates


def test_a_label_two_elements_answer_to_equally_is_never_guessed():
    """One of them would be wrong, and nothing here can tell which."""
    catalog = Catalog(elements=[
        Element(name="ONE", generator="constant", params={"value": "x"}, aliases=["SHARED_CODE"]),
        Element(name="TWO", generator="constant", params={"value": "y"}, aliases=["SHARED_CODE"]),
    ])
    element, candidates = match_element("Shared Code", element_keys(catalog))
    assert element is None
    assert candidates == ["ONE", "TWO"]


def test_age_is_recognized_as_derived_rather_than_looked_for_as_a_column():
    assert (match_derivation("Age") or ("", {}))[0] == "age"
    assert (match_derivation("Member Age") or ("", {}))[0] == "age"
    assert (match_derivation("Days to Pay") or ("", {}))[0] == "days_to_pay"
    # A stored value must not be mistaken for a derived one.
    assert match_derivation("Paid Date") is None
    assert match_derivation("Proc Code") is None


def test_the_abbreviations_expand_the_same_way_on_both_sides():
    assert canonical_tokens("Proc Code") == canonical_tokens("PROCEDURE_CODE")
    assert canonical_tokens("DOB") == canonical_tokens("Birth Date")
    assert canonical_tokens("Days to Pay") == frozenset({"DAY", "PAY"})


# ---------------------------------------------------------------------------
# The kind names the subject
# ---------------------------------------------------------------------------

def test_a_member_pattern_is_about_the_member_table(world):
    assert _resolve(world, "member", [{"label": "State", "value": "NJ"}]).subject == MEMBER


def test_a_claim_pattern_is_about_the_claim_table(world):
    """CLAIM_ID is on the line too, as a foreign key. The business key settles it."""
    resolution = _resolve(world, "claim", [{"label": "Claim Status", "value": "02"}])
    assert resolution.subject == CLAIM


def test_a_kind_nobody_declared_is_refused_by_name(world):
    resolution = _resolve(world, "household", [{"label": "State", "value": "NJ"}])
    assert resolution.subject is None
    assert not resolution.targets
    assert "no pattern kind called 'household'" in resolution.rejections[0].reason


# ---------------------------------------------------------------------------
# Where a parameter lands
# ---------------------------------------------------------------------------

def test_a_parameter_on_the_subject_lands_on_the_subjects_own_column(world):
    resolution = _resolve(world, "claim", [{"label": "Claim Status", "value": "02"}])
    target = _target(resolution, CLAIM, "CLCL_CUR_STS")
    assert target is not None
    assert target.relation == "subject" and target.depth == 0
    assert target.generator == "constant" and target.params == {"value": "02"}


def test_a_parameter_below_the_subject_lands_there_and_says_how_far(world):
    """A member pattern naming a procedure code is talking about its claim lines."""
    resolution = _resolve(world, "member", [{"label": "Proc Code", "value": "A0000"}])
    target = _target(resolution, LINE, "CDML_SVC_CD")
    assert target is not None
    assert target.relation == "descendant"
    assert target.depth == 2  # member -> claim -> line
    # A co-occurrence finding means one line carried the code, not every line.
    assert target.apply == "at_least_one"


def test_the_same_parameter_lands_one_level_down_for_a_claim_pattern(world):
    resolution = _resolve(world, "claim", [{"label": "Proc Code", "value": "A0000"}])
    assert _target(resolution, LINE, "CDML_SVC_CD").depth == 1


def test_a_parameter_about_something_above_the_subject_lands_there(world):
    """A claims pattern's member ages are said on the member, generated first,
    and its share is drawn over members, so its claims' members have them."""
    resolution = _resolve(world, "claim", [{"label": "Age", "min": 44, "max": 50,
                                            "unit": "years"}])
    assert _reason(resolution, "Age") is None, resolution.rejections
    target = _target(resolution, MEMBER, "MEME_BIRTH_DT")
    assert target is not None
    assert target.relation == "ancestor" and target.depth == 1
    assert target.apply == "all"
    # Aged 44 to 50: born 51 years less a day before the reference date at the
    # earliest, 44 years before it at the latest.
    assert -51 * 366 <= target.params["min_days"] <= -51 * 365 + 1
    assert -44 * 366 <= target.params["max_days"] <= -44 * 365
    assert resolution.share_of == MEMBER


def test_a_member_pattern_still_does_not_state_its_subscribers_attributes(world):
    """Only the primary record is said from below it. An employee ID is the
    subscriber's alone; stated from a member pattern every member of the
    family would share it, so it is refused, and the share stays on members."""
    resolution = _resolve(world, "member", [{"label": "Employee Id", "value": "E1"},
                                            {"label": "Age", "min": 44, "max": 50,
                                             "unit": "years"}])
    assert "generated before the subject" in _reason(resolution, "Employee Id").reason
    assert not [t for t in resolution.targets if t.table == "dbo.CMC_SBSB_SUBSC"]
    assert resolution.share_of is None


def test_a_claims_pattern_never_states_a_key_above_it(world):
    """The subscriber's ID is a key: a claims pattern naming it would
    rewrite who the claim's family is."""
    resolution = _resolve(world, "claim", [{"label": "Subscriber Id", "value": "S1"}])
    assert not [t for t in resolution.targets if t.element == "SUBSCRIBER_ID"]
    assert _reason(resolution, "Subscriber Id") is not None


@pytest.mark.parametrize("order", [1, -1])
def test_where_the_share_is_drawn_does_not_depend_on_the_order_stated(world, order):
    parameters = [{"label": "Claim Status", "value": "02"},
                  {"label": "Age", "min": 44, "max": 50, "unit": "years"}][::order]
    assert _resolve(world, "claim", parameters).share_of == MEMBER


def test_the_share_is_drawn_over_the_highest_table_a_target_is_on(world):
    """One target on the claim and one on its member: the share is drawn over
    members, whose claims inherit their place in it -- never over claims,
    which could not reach back to their member."""
    resolution = _resolve(world, "claim", [
        {"label": "Claim Status", "value": "02"},
        {"label": "Age", "min": 44, "max": 50, "unit": "years"},
    ])
    assert _target(resolution, CLAIM, "CLCL_CUR_STS") is not None
    assert _target(resolution, MEMBER, "MEME_BIRTH_DT") is not None
    assert resolution.share_of == MEMBER
    # A claim pattern with nothing above the claim keeps its share on claims.
    alone = _resolve(world, "claim", [{"label": "Claim Status", "value": "02"}])
    assert alone.share_of is None


def test_an_element_mapped_on_two_tables_is_settled_by_the_subject(world):
    """EFFECTIVE_DATE is on the subscriber and on the member. Both are real."""
    member = _resolve(world, "member", [{"label": "Effective Date", "value": "2024-01-01"}])
    assert _target(member, MEMBER, "MEME_EFF_DT") is not None
    subscriber = _resolve(
        world, "subscriber", [{"label": "Effective Date", "value": "2024-01-01"}]
    )
    assert _target(subscriber, "dbo.CMC_SBSB_SUBSC", "SBSB_EFF_DT") is not None


# ---------------------------------------------------------------------------
# Columns a pattern may not state
# ---------------------------------------------------------------------------

def test_a_calculated_column_is_never_stated_but_its_input_is(world):
    """CLCL_TOT_CHG sums its lines, so pinning it would desync the claim from
    them. The amount that was measured lives one level down, and binding there
    keeps the sum true instead of refusing something the plan can express."""
    resolution = _resolve(world, "claim", [{"label": "Charge Amount", "value": 120}])

    assert _target(resolution, CLAIM, "CLCL_TOT_CHG") is None, (
        "the calculated column must not be stated"
    )
    line = _target(resolution, LINE, "CDML_CHG_AMT")
    assert line is not None, "the value should land on the column it is summed from"
    assert line.relation == "descendant"


def test_a_column_on_a_reused_table_is_refused_with_what_it_would_take(world):
    """Providers are read from the real database, so there is no value to state."""
    resolution = _resolve(
        world, "claim", [{"label": "Provider Specialty", "value": "CARD"}]
    )
    rejection = _reason(resolution, "Provider Specialty")
    assert rejection is not None
    assert "reused, not generated" in rejection.reason


def test_a_key_is_refused_because_every_row_needs_its_own(world):
    resolution = _resolve(world, "member", [{"label": "Member ID", "value": "ABC12345678"}])
    rejection = _reason(resolution, "Member ID")
    assert rejection is not None
    assert "every row needs its own value" in rejection.reason


# ---------------------------------------------------------------------------
# Values that cannot survive the column they were bound to
# ---------------------------------------------------------------------------

def test_a_code_the_column_has_never_held_is_refused_as_a_translation_problem(world):
    """The analyzer says DENY; the column holds 04. Better said here than after a run."""
    resolution = _resolve(world, "claim", [{"label": "Claim Status", "value": "DENY"}])
    rejection = _reason(resolution, "Claim Status")
    assert rejection is not None
    assert "not one of the values this column holds" in rejection.reason


def test_a_value_the_columns_format_rejects_is_refused(world):
    resolution = _resolve(world, "claim", [{"label": "Proc Code", "value": "12345"}])
    rejection = _reason(resolution, "Proc Code")
    assert rejection is not None
    assert "does not match the format" in rejection.reason


def test_a_number_outside_what_was_measured_is_refused(world):
    resolution = _resolve(world, "claim", [{"label": "Service Units", "value": 9_999}])
    rejection = _reason(resolution, "Service Units")
    assert rejection is not None
    assert "above the" in rejection.reason


def test_a_value_wider_than_its_column_is_refused_where_it_is_stated():
    """The defect this exists for: a load that stops after rows are committed."""
    plan = Plan(
        meta=new_meta("tiny", "seed"),
        tables=[
            PlanTable(
                name="dbo.T", mode="generate", count=10, level=0,
                fields=[
                    PlanField(
                        name="C", bucket="free", generator="constant",
                        params={"value": "short"}, provenance="learned",
                    )
                ],
            )
        ],
        generation_order=["dbo.T"],
    )
    mappings = MappingStore(mappings=[
        ColumnMapping(
            table="dbo.T", column="C", bucket="free", element="LAST_NAME",
            confidence="high", accepted=True,
        )
    ])
    model = PhysicalModel(tables=[
        Table(
            name="dbo.T", schema_name="dbo", table_name="T",
            columns=[Column(name="C", ordinal=1, type_name="varchar", max_length=5)],
        )
    ])
    pattern = AnalyzerPattern(
        id="T-1", kind="member", subject="dbo.T",
        parameters=[PatternParameter(label="Last Name", value="Winterbottom-Fairweather")],
    )
    resolution = resolve_pattern(
        pattern, catalog=load_catalog(), mappings=mappings, plan=plan, model=model,
    )
    assert not resolution.targets
    assert "holds 5 characters" in resolution.rejections[0].reason


# ---------------------------------------------------------------------------
# An age is not a column
# ---------------------------------------------------------------------------

def test_an_age_becomes_a_birth_date_window_that_produces_that_age(world):
    resolution = _resolve(world, "member", [{"label": "Age", "value": 24, "unit": "years"}])
    target = _target(resolution, MEMBER, "MEME_BIRTH_DT")
    assert target is not None and target.generator == "date_relative"

    anchor = date.fromisoformat(target.params["anchor"])
    assert anchor == date.fromisoformat(AS_OF)
    for offset in (target.params["min_days"], target.params["max_days"]):
        born = anchor + timedelta(days=offset)
        age = anchor.year - born.year - ((anchor.month, anchor.day) < (born.month, born.day))
        assert age == 24, f"a birth date of {born} is not 24 years old on {anchor}"


def test_an_age_with_no_reference_date_is_refused_rather_than_read_from_the_clock(world):
    """Nothing here may read the clock; a run has to reproduce on any day."""
    resolution = _resolve(
        world, "member", [{"label": "Age", "value": 24, "unit": "years"}], as_of=None
    )
    rejection = _reason(resolution, "Age")
    assert rejection is not None
    assert "needs a reference date" in rejection.reason


def test_a_turnaround_in_days_becomes_a_gap_between_two_stored_dates(world):
    resolution = _resolve(world, "claim", [{"label": "Days to Pay", "value": 45}])
    target = _target(resolution, CLAIM, "CLCL_PAID_DT")
    assert target is not None
    assert target.params["anchor"] == "CLCL_RECD_DT"
    assert target.params["min_days"] == target.params["max_days"] == 45


# ---------------------------------------------------------------------------
# Columns that are drawn together
# ---------------------------------------------------------------------------

def test_pinning_one_of_three_columns_drawn_together_narrows_all_three(world):
    """Pinning state alone would give NJ with a Massachusetts ZIP."""
    resolution = _resolve(world, "member", [{"label": "State", "value": "NJ"}])
    touched = {t.column for t in resolution.targets}
    assert touched == {"MEME_ZIP", "MEME_CITY", "MEME_STATE"}

    for column in touched:
        target = _target(resolution, MEMBER, column)
        assert target.generator == "lookup"
        assert target.params["record_key"] == "address"
        records = target.params["values"]
        assert records, column
        assert all(record["state"] == "NJ" for record in records), column

    # All three draw the same index, so all three must see the same records.
    lists = [_target(resolution, MEMBER, c).params["values"] for c in touched]
    assert all(entry == lists[0] for entry in lists)


def test_a_value_no_record_carries_is_refused_rather_than_breaking_the_group(world):
    resolution = _resolve(world, "member", [{"label": "State", "value": "ZZ"}])
    rejection = _reason(resolution, "State")
    assert rejection is not None
    assert "would break the columns drawn with it" in rejection.reason
    assert not resolution.targets


# ---------------------------------------------------------------------------
# The whole thing, and twice
# ---------------------------------------------------------------------------

def test_a_pattern_the_analyzer_would_send_binds_what_it_can_and_says_what_it_cannot(world):
    resolution = _resolve(world, "member", [
        {"label": "Age", "value": 24, "unit": "years"},
        {"label": "State", "value": "NJ"},
        {"label": "Proc Code", "value": "A0000"},
        {"label": "Favorite Color", "value": "blue"},
    ])
    assert resolution.bound() == 3
    assert {t.table for t in resolution.targets} == {MEMBER, LINE}
    unbound = _reason(resolution, "Favorite Color")
    assert unbound is not None and "no element answers" in unbound.reason


def test_resolving_the_same_pattern_twice_gives_the_same_answer(world):
    parameters = [
        {"label": "Age", "value": 24, "unit": "years"},
        {"label": "State", "value": "NJ"},
        {"label": "Proc Code", "value": "A0000"},
    ]
    first = _resolve(world, "member", parameters)
    second = _resolve(world, "member", parameters)
    assert first.model_dump() == second.model_dump()


def test_the_sample_document_resolves_against_the_live_plan(world):
    from sdg.config import REPO_ROOT

    document = load_document(REPO_ROOT / "examples" / "patterns-sample.json")
    resolutions = resolve_document(
        document, catalog=world["catalog"], mappings=world["mappings"],
        plan=world["plan"], model=world["model"],
    )
    assert [r.pattern for r in resolutions] == ["P-1043", "P-2210"]
    assert all(r.is_usable() for r in resolutions)


def test_one_pattern_on_its_own_is_accepted_as_well_as_a_document(tmp_path):
    """Selecting a single pattern in the analyzer produces exactly this."""
    single = tmp_path / "one.json"
    single.write_text(
        '{"id": "P-9", "kind": "member", "parameters": [{"label": "State", "value": "NJ"}]}',
        encoding="utf-8",
    )
    document = load_document(single)
    assert [p.id for p in document.patterns] == ["P-9"]


def test_a_key_the_analyzer_adds_later_does_not_stop_the_file_being_read(tmp_path):
    """The analyzer is another tool; it is free to grow its output."""
    path = tmp_path / "future.json"
    path.write_text(
        '{"patterns": [{"id": "P-9", "kind": "member", "lift": 4.2, '
        '"parameters": [{"label": "State", "value": "NJ", "p_value": 0.01}]}]}',
        encoding="utf-8",
    )
    assert load_document(path).patterns[0].id == "P-9"


# ---------------------------------------------------------------------------
# Applying, and taking it back out
# ---------------------------------------------------------------------------

def _resolution_with_two_targets(world):
    return _resolve(world, "claim", [
        {"label": "Claim Status", "value": "02"},
        {"label": "Days to Pay", "value": 45},
    ])


def test_applying_writes_one_approved_override_per_target(world):
    resolution = _resolution_with_two_targets(world)
    document, written = apply_to_decisions([resolution], {"columns": [], "tables": {}})
    assert len(written) == len(resolution.targets)
    for entry in document["columns"]:
        # `sdg plan` only honors a decision that is approved and names a rule.
        assert entry["approved"] is True
        assert entry["generator"] and isinstance(entry["params"], dict)
        assert entry["_pattern"] == resolution.pattern


def test_an_applied_override_is_keyed_the_way_the_plan_reads_it(world):
    from sdg.plan.resolver import decisions_index

    resolution = _resolution_with_two_targets(world)
    document, _ = apply_to_decisions([resolution], {"columns": [], "tables": {}})
    index = decisions_index(document)
    for target in resolution.targets:
        assert index[target.qualified]["params"] == target.params


def test_a_decision_made_by_hand_is_never_overwritten(world):
    """Someone sat and decided this column. A pattern does not get to undo that."""
    resolution = _resolve(world, "claim", [{"label": "Claim Status", "value": "02"}])
    mine = {
        "table": CLAIM, "column": "CLCL_CUR_STS", "approved": True,
        "generator": "constant", "params": {"value": "99"},
    }
    document, written = apply_to_decisions([resolution], {"columns": [mine], "tables": {}})
    assert written == []
    assert document["columns"][0]["params"] == {"value": "99"}


def test_an_unapproved_queue_entry_is_replaced_rather_than_duplicated(world):
    resolution = _resolve(world, "claim", [{"label": "Claim Status", "value": "02"}])
    queued = {
        "table": CLAIM, "column": "CLCL_CUR_STS", "approved": False,
        "generator": "lookup", "params": {"values": ["02"]},
    }
    document, _ = apply_to_decisions([resolution], {"columns": [queued], "tables": {}})
    entries = [c for c in document["columns"] if c["column"] == "CLCL_CUR_STS"]
    assert len(entries) == 1
    assert entries[0]["approved"] is True


def test_clearing_removes_what_a_pattern_wrote_and_leaves_the_rest(world):
    resolution = _resolution_with_two_targets(world)
    mine = {"table": "dbo.X", "column": "Y", "approved": False, "generator": "constant"}
    document, _ = apply_to_decisions([resolution], {"columns": [mine], "tables": {}})
    cleared, removed = clear_from_decisions(document)
    assert removed == len(resolution.targets)
    assert cleared["columns"] == [mine]


def test_applying_and_clearing_returns_the_file_to_where_it_started(world):
    """The defect this exists for: applying over a queued column, then clearing
    it, used to delete the queue entry and quietly shorten the review queue."""
    resolution = _resolve(world, "claim", [{"label": "Claim Status", "value": "02"}])
    queued = {
        "table": CLAIM, "column": "CLCL_CUR_STS", "approved": False,
        "element": "CLAIM_STATUS", "generator": "lookup",
        "params": {"values": ["02", "04"]},
        "_why": "the proposed rule is not certain",
    }
    start = {"columns": [queued], "tables": {"dbo.T": {"mode": "reuse"}}}

    applied, _ = apply_to_decisions([resolution], start)
    assert applied["columns"][0]["approved"] is True

    cleared, removed = clear_from_decisions(applied)
    assert removed == 1
    assert cleared["columns"] == [queued]
    assert cleared["tables"] == start["tables"]


def test_applying_the_same_pattern_twice_still_remembers_what_it_displaced(world):
    resolution = _resolve(world, "claim", [{"label": "Claim Status", "value": "02"}])
    queued = {
        "table": CLAIM, "column": "CLCL_CUR_STS", "approved": False, "generator": "lookup",
    }
    once, _ = apply_to_decisions([resolution], {"columns": [queued], "tables": {}})
    twice, _ = apply_to_decisions([resolution], once)
    cleared, _ = clear_from_decisions(twice)
    assert cleared["columns"] == [queued]


def test_the_decisions_file_this_repository_carries_is_still_readable():
    """Applying merges into a real file; this is the shape it has to merge into."""
    from sdg.config import get_settings

    path = get_settings().work_dir / "review-decisions.json"
    if not path.exists():
        pytest.skip("no decisions file in this working directory")
    document = load_decisions(path)
    assert isinstance(document.get("columns"), list)

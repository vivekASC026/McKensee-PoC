"""Step 8 acceptance: hierarchy, effective counts, resolution, and validation."""

from __future__ import annotations

import pytest

from sdg.catalog.loader import load_catalog
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import ForeignKey, PhysicalModel, Table
from sdg.discovery.run import discover
from sdg.mapping.scorer import map_columns
from sdg.plan import hierarchy as hierarchy_module
from sdg.plan.build import BuildInputs, build_plan, default_reference_sets
from sdg.plan.detect import detect_sum_expressions
from sdg.plan.hierarchy import HierarchyError, compute_counts
from sdg.plan.model import Plan, PlanField, PlanTable
from sdg.plan.proposer import propose
from sdg.plan.refsets import load_reference_set
from sdg.plan.validator import blocking, validate
from sdg.profiling.run import profile

ROOT_COUNT = 5_000


@pytest.fixture(scope="module")
def inputs():
    registry = ConnectionRegistry()
    model = discover(registry, "source").model
    report = profile(registry, model)
    catalog = load_catalog()
    mappings = map_columns(model, catalog, report)
    sums = detect_sum_expressions(registry, model)
    proposals = propose(model, report, sums, mappings, catalog)
    built = BuildInputs(
        model=model, catalog=catalog, mappings=mappings, proposals=proposals,
        report=report,
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
    built.reference_sets = default_reference_sets(registry, built)
    return built


@pytest.fixture(scope="module")
def built(inputs):
    plan, graph = build_plan(inputs)
    return plan, graph


# ---------------------------------------------------------------------------
# Hierarchy, on a graph built by hand
# ---------------------------------------------------------------------------

def _model(*specs: tuple[str, list[tuple[str, str]]]) -> PhysicalModel:
    tables = []
    for name, keys in specs:
        tables.append(
            Table(
                name=name, schema_name="dbo", table_name=name.split(".")[-1],
                foreign_keys=[
                    ForeignKey(columns=[column], parent_table=parent, parent_columns=["ID"])
                    for column, parent in keys
                ],
            )
        )
    return PhysicalModel(tables=tables)


def test_roots_levels_and_driving_parents():
    model = _model(
        ("dbo.A", []), ("dbo.B", []),
        ("dbo.C", [("A_ID", "dbo.A")]),
        ("dbo.D", [("B_ID", "dbo.B"), ("C_ID", "dbo.C")]),
    )
    graph = hierarchy_module.build(model, {n.name: "generate" for n in model.tables})

    assert [n.name for n in graph.roots()] == ["dbo.A", "dbo.B"]
    assert graph.nodes["dbo.C"].level == 1
    assert graph.nodes["dbo.D"].level == 2
    # The deepest parent drives, because it is the most specific.
    assert graph.nodes["dbo.D"].driving_parent == "dbo.C"
    assert graph.order == ["dbo.A", "dbo.B", "dbo.C", "dbo.D"]


def test_a_reuse_table_is_not_a_parent():
    """A table set to reuse holds rows already; it is not something to fill."""
    model = _model(("dbo.A", []), ("dbo.B", [("A_ID", "dbo.A")]))
    graph = hierarchy_module.build(model, {"dbo.A": "reuse", "dbo.B": "generate"})
    assert "dbo.A" not in graph.nodes
    assert graph.nodes["dbo.B"].is_root


def test_the_driving_parent_can_be_overridden():
    model = _model(
        ("dbo.A", []), ("dbo.B", []),
        ("dbo.C", [("A_ID", "dbo.A")]),
        ("dbo.D", [("B_ID", "dbo.B"), ("C_ID", "dbo.C")]),
    )
    modes = {t.name: "generate" for t in model.tables}
    graph = hierarchy_module.build(model, modes, driving_parents={"dbo.D": "dbo.B"})
    assert graph.nodes["dbo.D"].driving_parent == "dbo.B"

    with pytest.raises(HierarchyError, match="named as the driving parent"):
        hierarchy_module.build(model, modes, driving_parents={"dbo.D": "dbo.A"})


def test_a_cycle_is_an_error_with_a_way_out():
    model = _model(
        ("dbo.A", [("B_ID", "dbo.B")]),
        ("dbo.B", [("A_ID", "dbo.A")]),
    )
    with pytest.raises(HierarchyError, match="cycle"):
        hierarchy_module.build(model, {t.name: "generate" for t in model.tables})


def test_effective_counts_multiply_down_the_tree():
    model = _model(("dbo.A", []), ("dbo.B", [("A_ID", "dbo.A")]), ("dbo.C", [("B_ID", "dbo.B")]))
    graph = hierarchy_module.build(model, {t.name: "generate" for t in model.tables})
    compute_counts(graph, {"dbo.A": 100}, {"dbo.B": 2.5, "dbo.C": 3.0})
    assert graph.nodes["dbo.A"].effective_count == 100
    assert graph.nodes["dbo.B"].effective_count == 250
    assert graph.nodes["dbo.C"].effective_count == 750
    assert graph.total_rows() == 1100


def test_a_root_without_a_count_is_an_error():
    model = _model(("dbo.A", []))
    graph = hierarchy_module.build(model, {"dbo.A": "generate"})
    with pytest.raises(HierarchyError, match="no row count for root table"):
        compute_counts(graph, {}, {})


def test_a_child_without_a_ratio_is_an_error():
    model = _model(("dbo.A", []), ("dbo.B", [("A_ID", "dbo.A")]))
    graph = hierarchy_module.build(model, {t.name: "generate" for t in model.tables})
    with pytest.raises(HierarchyError, match="no ratio for dbo.B"):
        compute_counts(graph, {"dbo.A": 10}, {})


# ---------------------------------------------------------------------------
# Effective counts against the real slice
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_effective_counts_match_a_hand_calculation(built, inputs):
    _plan, graph = built
    report = inputs.report

    members = report.ratio_for("dbo.CMC_MEME_MEMBER", "dbo.CMC_SBSB_SUBSC").mean
    claims = report.ratio_for("dbo.CMC_CLCL_CLAIM", "dbo.CMC_MEME_MEMBER").mean
    lines = report.ratio_for("dbo.CMC_CDML_CL_LINE", "dbo.CMC_CLCL_CLAIM").mean

    expected_members = round(ROOT_COUNT * members)
    expected_claims = round(expected_members * claims)
    expected_lines = round(expected_claims * lines)

    assert graph.nodes["dbo.CMC_SBSB_SUBSC"].effective_count == ROOT_COUNT
    assert graph.nodes["dbo.CMC_MEME_MEMBER"].effective_count == expected_members
    assert graph.nodes["dbo.CMC_CLCL_CLAIM"].effective_count == expected_claims
    assert graph.nodes["dbo.CMC_CDML_CL_LINE"].effective_count == expected_lines

    # And because the root count matches the seeded volume, the effective
    # counts should land on what the source actually holds.
    assert graph.nodes["dbo.CMC_MEME_MEMBER"].effective_count == pytest.approx(11_451, rel=0.01)
    assert graph.nodes["dbo.CMC_CLCL_CLAIM"].effective_count == pytest.approx(52_980, rel=0.01)


@pytest.mark.db
def test_every_generated_table_gets_a_count(built):
    plan, graph = built
    for table in plan.tables:
        if table.mode != "generate":
            continue
        assert table.effective_count is not None and table.effective_count > 0, table.name
        assert table.name in graph.nodes


@pytest.mark.db
def test_the_claims_table_is_driven_by_members_not_by_products(built):
    plan, _graph = built
    claims = plan.table("dbo.CMC_CLCL_CLAIM")
    assert claims.driving_parent == "dbo.CMC_MEME_MEMBER"
    assert claims.ratio.parent == "dbo.CMC_MEME_MEMBER"
    assert claims.ratio.kind == "distribution"
    assert claims.ratio.zero_share == pytest.approx(0.30, abs=0.02)


@pytest.mark.db
def test_the_driving_parents_key_is_taken_from_the_parent_row(built):
    """Three claims per member must mean three claims pointing at that member."""
    plan, _graph = built
    claims = plan.table("dbo.CMC_CLCL_CLAIM")
    assert claims.field("MEME_CK").params["pick"] == "driving"
    # Every other reference is picked from the parent's key set.
    assert claims.field("PRPR_ID").params["pick"] == "hash"
    assert claims.field("PDPD_ID").params["pick"] == "hash"
    assert plan.table("dbo.CMC_CDML_CL_LINE").field("CLCL_ID").params["pick"] == "driving"


# ---------------------------------------------------------------------------
# Resolution and precedence
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_structural_rules_are_never_overridden(built):
    plan, _graph = built
    lines = plan.table("dbo.CMC_CDML_CL_LINE")
    counter = lines.field("CDML_SEQ_NO")
    assert counter.provenance == "structural"
    assert counter.generator == "sequence" and counter.params["scope"] == "parent"

    for name in ("MEME_CK", "PRPR_ID", "PDPD_ID"):
        assert plan.table("dbo.CMC_CLCL_CLAIM").field(name).provenance == "structural"


@pytest.mark.db
def test_the_catalog_governs_formats_and_value_sets(built):
    plan, _graph = built
    member_id = plan.table("dbo.CMC_MEME_MEMBER").field("MEME_ID")
    assert member_id.provenance == "catalog"
    assert member_id.element == "MEMBER_ID"
    assert member_id.params["regex"] == r"[A-Z]{3}\d{8}"

    first_name = plan.table("dbo.CMC_MEME_MEMBER").field("MEME_FIRST_NAME")
    assert first_name.provenance == "catalog"
    assert first_name.generator == "lookup"


@pytest.mark.db
def test_a_learned_dependency_beats_a_catalog_default(built):
    """The catalog cannot say a service date sits inside member coverage."""
    plan, _graph = built
    service = plan.table("dbo.CMC_CLCL_CLAIM").field("CLCL_SVC_DT")
    assert service.provenance == "learned"
    assert service.params["anchor"] == "dbo.CMC_MEME_MEMBER.MEME_EFF_DT"
    assert service.element == "SERVICE_DATE"
    overrode = {o.provenance for o in (service.overrode or [])}
    assert "catalog" in overrode


@pytest.mark.db
def test_the_claim_total_keeps_its_expression(built):
    plan, _graph = built
    total = plan.table("dbo.CMC_CLCL_CLAIM").field("CLCL_TOT_CHG")
    assert total.generator == "expression"
    assert total.params["expression"] == "sum(dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT)"
    assert any(o.provenance == "catalog" for o in (total.overrode or []))


@pytest.mark.db
def test_a_referenced_identity_key_is_generated_not_skipped(built):
    """The child needs the key while the row is built, so the run assigns it."""
    plan, _graph = built
    for table_name, column in (
        ("dbo.CMC_SBSB_SUBSC", "SBSB_CK"),
        ("dbo.CMC_MEME_MEMBER", "MEME_CK"),
    ):
        field_ = plan.table(table_name).field(column)
        assert field_.generator == "sequence", column
        assert field_.bucket == "system"
        # It starts after whatever is already in the table.
        assert field_.params["start"] > 1
        assert any(e.kind == "identity_referenced" for e in (field_.evidence or []))


@pytest.mark.db
def test_an_unreferenced_identity_key_is_still_left_to_the_database(inputs):
    """The audit table's key is not referenced, so nothing generates it."""
    audit = next(
        p for p in inputs.proposals
        if p.table == "dbo.CMC_CLCL_AUDIT" and p.column == "AUDIT_ID"
    )
    assert audit.generator == "skip"


@pytest.mark.db
def test_every_field_records_where_its_rule_came_from(built):
    plan, _graph = built
    for table in plan.tables:
        for field_ in table.fields:
            assert field_.provenance in {
                "structural", "catalog", "learned", "user", "physical"
            }
            assert field_.evidence, f"{table.name}.{field_.name}"


# ---------------------------------------------------------------------------
# Reference sets
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_the_plan_code_reference_set_excludes_inactive_codes(inputs):
    reference = inputs.reference_sets["element:PLAN_CODE"]
    assert reference.filter == "ACTIVE_FLAG = 'Y'"
    assert reference.size == 37
    assert reference.available == 40
    assert "leaving out 3" in reference.describe()


@pytest.mark.db
def test_reference_sets_are_recorded_in_the_plan(built, inputs):
    plan, _graph = built
    records = {r.table: r for r in plan.meta.reference_sets}
    # Provider count moves with the fixture scale; the product list does not.
    assert records["dbo.CMC_PRPR_PROV"].size == inputs.reference_sets[
        "dbo.CMC_PRPR_PROV"
    ].size > 0
    assert records["dbo.CMC_PDPD_PRODUCT"].size == 25
    assert records["element:PLAN_CODE"].filter == "ACTIVE_FLAG = 'Y'"
    for record in records.values():
        assert record.strategy in {"full", "sample", "snapshot"}


@pytest.mark.db
def test_a_sampled_reference_set_is_reproducible():
    registry = ConnectionRegistry()
    kwargs = dict(
        table="dbo.CMC_PRPR_PROV", connection="source", source_table="CMC_PRPR_PROV",
        key_column="PRPR_ID", strategy="sample", sample_size=20, seed="pin",
    )
    first = load_reference_set(registry, **kwargs)
    second = load_reference_set(registry, **kwargs)
    assert first.size == 20
    assert first.keys == second.keys
    assert first.sample_seed == "pin"

    different = load_reference_set(registry, **{**kwargs, "seed": "other"})
    assert set(different.keys) != set(first.keys)


@pytest.mark.db
def test_a_reference_set_can_carry_attributes():
    reference = load_reference_set(
        ConnectionRegistry(),
        table="dbo.CMC_PDPD_PRODUCT", connection="source",
        source_table="CMC_PDPD_PRODUCT", key_column="PDPD_ID",
        attributes=["PDPD_TYPE"],
    )
    assert reference.size == 25
    assert set(reference.attributes[reference.keys[0]]) == {"PDPD_TYPE"}
    assert {a["PDPD_TYPE"] for a in reference.attributes.values()} == {"M", "D"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_the_resolved_plan_has_nothing_blocking(built):
    plan, _graph = built
    stoppers = blocking(plan.warnings)
    assert not stoppers, [w.message for w in stoppers]


def _minimal_plan(**table_kwargs) -> Plan:
    from sdg.plan.model import new_meta

    table = PlanTable(name="dbo.CHILD", mode="generate", **table_kwargs)
    parent = PlanTable(
        name="dbo.PARENT", mode="generate", count=10,
        fields=[PlanField(name="ID", bucket="business_key", generator="sequence",
                          params={"start": 1}, provenance="structural")],
    )
    return Plan(
        meta=new_meta("t", "s"),
        tables=[parent, table],
        generation_order=["dbo.PARENT", "dbo.CHILD"],
    )


def test_a_child_without_a_ratio_fails_validation_with_a_clear_message():
    plan = _minimal_plan(driving_parent="dbo.PARENT", fields=[])
    stoppers = blocking(validate(plan))
    codes = {w.code for w in stoppers}
    assert "child_without_ratio" in codes
    message = next(w.message for w in stoppers if w.code == "child_without_ratio")
    assert "dbo.CHILD" in message and "dbo.PARENT" in message and "ratio" in message


def test_a_root_without_a_count_fails_validation():
    plan = _minimal_plan(fields=[])
    codes = {w.code for w in blocking(validate(plan))}
    assert "root_without_count" in codes


def test_a_character_business_key_without_a_format_blocks():
    plan = _minimal_plan(
        count=100,
        fields=[PlanField(
            name="K", bucket="business_key", generator="constant",
            params={"value": "x"}, provenance="physical", data_type="char(10)",
        )],
    )
    codes = {w.code for w in blocking(validate(plan))}
    assert "business_key_without_format" in codes


def test_an_integer_business_key_is_covered_by_a_sequence():
    plan = _minimal_plan(
        count=100,
        fields=[PlanField(
            name="K", bucket="business_key", generator="sequence",
            params={"start": 1}, provenance="structural", data_type="int",
        )],
    )
    codes = {w.code for w in blocking(validate(plan))}
    assert "business_key_without_format" not in codes


def test_a_derivable_column_without_an_expression_blocks():
    plan = _minimal_plan(
        count=100,
        fields=[PlanField(
            name="TOTAL", bucket="derivable", generator="number",
            params={"distribution": "uniform", "min": 0, "max": 1},
            provenance="learned", data_type="decimal(12,2)",
        )],
    )
    codes = {w.code for w in blocking(validate(plan))}
    assert "derivable_without_expression" in codes


def test_a_pattern_that_cannot_fill_the_rows_blocks():
    plan = _minimal_plan(
        count=100_000,
        fields=[PlanField(
            name="K", bucket="business_key", generator="pattern",
            params={"regex": r"[A-Z]\d{3}", "unique": True},
            provenance="learned", data_type="char(4)",
        )],
    )
    stoppers = blocking(validate(plan))
    warning = next(w for w in stoppers if w.code == "pattern_space_too_small")
    assert "26,000" in warning.message and "100,000" in warning.message


def test_a_reuse_table_without_a_source_is_rejected_by_the_model():
    """Caught at construction, before the validator ever sees it."""
    from sdg.plan.model import PlanError, new_meta

    with pytest.raises((PlanError, ValueError), match="reuse but has no source"):
        Plan(
            meta=new_meta("t", "s"),
            tables=[PlanTable(name="dbo.R", mode="reuse", source=None)],
            generation_order=[],
        )


def test_the_validator_also_reports_a_reuse_table_without_a_source():
    """A plan assembled table by table still gets the warning."""
    from sdg.plan.model import new_meta

    plan = Plan(meta=new_meta("t", "s"), tables=[], generation_order=[])
    # Bypass the model check the way a partially built plan would.
    plan.tables.append(PlanTable.model_construct(
        name="dbo.R", mode="reuse", source=None, fields=[],
    ))
    codes = {w.code for w in blocking(validate(plan))}
    assert "reuse_without_source" in codes


@pytest.mark.db
def test_the_plan_round_trips_through_its_schema(built, tmp_path):
    plan, _graph = built
    path = plan.save(tmp_path / "plan.json")
    reloaded = Plan.load(path)
    assert reloaded.to_dict() == plan.to_dict()
    assert reloaded.generation_order == plan.generation_order


# ---------------------------------------------------------------------------
# Warnings that depend on the resolved field, not on the plan's shape
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_providers_are_reused_even_when_a_plan_asks_to_generate_them(inputs):
    """Providers are real: a plan may hold Generate for them (to model their
    shape and choose a driving parent), but every run refuses it, so no
    invented NPI (whose check digit a pattern cannot reproduce) is ever
    written (user decision 2026-11-10, option 1)."""
    from sdg.engine.execute import NeverGenerated, refuse_never_generated
    from sdg.plan.build import BuildInputs as Inputs

    generating = Inputs(
        model=inputs.model, catalog=inputs.catalog, mappings=inputs.mappings,
        proposals=inputs.proposals, report=inputs.report,
        modes={"dbo.CMC_PRPR_PROV": "generate", "dbo.CMC_PDPD_PRODUCT": "reuse",
               "dbo.CMC_CLCL_AUDIT": "exclude"},
        root_counts={"dbo.CMC_SBSB_SUBSC": 500, "dbo.CMC_PRPR_PROV": 100},
    )
    generating.reference_sets = default_reference_sets(ConnectionRegistry(), generating)
    plan, _graph = build_plan(generating)

    assert plan.table("dbo.CMC_PRPR_PROV").mode == "generate"
    with pytest.raises(NeverGenerated, match="CMC_PRPR_PROV is never generated"):
        refuse_never_generated(plan)

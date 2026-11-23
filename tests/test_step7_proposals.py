"""Step 7 acceptance: proposed rules, the review queue, and the review page."""

from __future__ import annotations

import pytest

from sdg.catalog.loader import load_catalog
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.run import discover
from sdg.mapping.scorer import map_columns
from sdg.plan.detect import detect_sum_expressions
from sdg.plan.proposer import propose
from sdg.profiling.run import profile
from sdg.review.page import build_context, summarize_params, write_page
from sdg.review.queue import build_queue, decisions_template, load_decisions, summarize

# Four network and PCP tables brought twenty-odd columns; the queue grew with them.
REVIEW_QUEUE_TARGET = 40


@pytest.fixture(scope="module")
def pipeline():
    registry = ConnectionRegistry()
    model = discover(registry, "source").model
    report = profile(registry, model)
    catalog = load_catalog()
    mappings = map_columns(model, catalog, report)
    sums = detect_sum_expressions(registry, model)
    proposals = propose(model, report, sums, mappings, catalog)
    queue = build_queue(model, mappings, proposals)
    return {
        "model": model, "report": report, "catalog": catalog, "mappings": mappings,
        "sums": sums, "proposals": proposals, "queue": queue,
        "by_column": {p.qualified: p for p in proposals},
    }


# ---------------------------------------------------------------------------
# The three proposals the brief names
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_claim_total_is_proposed_as_the_sum_of_its_lines(pipeline):
    proposal = pipeline["by_column"]["dbo.CMC_CLCL_CLAIM.CLCL_TOT_CHG"]
    assert proposal.generator == "expression"
    assert proposal.params["expression"] == "sum(dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT)"
    assert proposal.confidence == "high"
    assert proposal.coverage == 1.0
    assert any(e["kind"] == "expression_match" for e in proposal.evidence)


@pytest.mark.db
def test_claim_status_is_proposed_as_a_weighted_lookup(pipeline):
    proposal = pipeline["by_column"]["dbo.CMC_CLCL_CLAIM.CLCL_CUR_STS"]
    assert proposal.generator == "lookup"
    values = [str(v).strip() for v in proposal.params["values"]]
    weights = proposal.params["weights"]
    assert len(values) == 6
    assert weights[values.index("02")] == pytest.approx(0.70, abs=0.02)
    assert sum(weights) == pytest.approx(1.0, abs=0.01)


@pytest.mark.db
def test_received_date_is_proposed_relative_to_the_service_date(pipeline):
    proposal = pipeline["by_column"]["dbo.CMC_CLCL_CLAIM.CLCL_RECD_DT"]
    assert proposal.generator == "date_relative"
    assert proposal.params["anchor"] == "CLCL_SVC_DT"
    assert proposal.params["min_days"] == 0
    assert proposal.params["max_days"] == 30
    assert proposal.confidence == "high"


# ---------------------------------------------------------------------------
# Expression detection
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_expression_detection_finds_only_the_real_one(pipeline):
    found = {
        (s.parent_table, s.parent_column, s.child_table, s.child_column)
        for s in pipeline["sums"]
    }
    assert found == {
        ("dbo.CMC_CLCL_CLAIM", "CLCL_TOT_CHG",
         "dbo.CMC_CDML_CL_LINE", "CDML_CHG_AMT"),
    }
    assert pipeline["sums"][0].match_rate == 1.0


# ---------------------------------------------------------------------------
# The structural rules that the profile must not override
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_a_composite_key_counter_is_a_per_parent_sequence(pipeline):
    """Five distinct values look like a code list; a lookup would duplicate keys."""
    proposal = pipeline["by_column"]["dbo.CMC_CDML_CL_LINE.CDML_SEQ_NO"]
    assert proposal.generator == "sequence"
    assert proposal.params["scope"] == "parent"
    assert proposal.provenance == "structural"


@pytest.mark.db
def test_identity_and_computed_columns_are_skipped(pipeline):
    for qualified in (
        "dbo.CMC_MEME_MEMBER.MEME_CK",
        "dbo.CMC_SBSB_SUBSC.SBSB_CK",
        "dbo.CMC_CLCL_CLAIM.CLCL_ROW_HASH",
        "dbo.CMC_CLCL_CLAIM.CLCL_CREATED_TS",
    ):
        proposal = pipeline["by_column"][qualified]
        assert proposal.generator == "skip", qualified
        assert proposal.provenance == "structural"


@pytest.mark.db
def test_foreign_keys_are_proposed_as_references(pipeline):
    for qualified, parent in (
        ("dbo.CMC_MEME_MEMBER.SBSB_CK", "dbo.CMC_SBSB_SUBSC"),
        ("dbo.CMC_CLCL_CLAIM.MEME_CK", "dbo.CMC_MEME_MEMBER"),
        ("dbo.CMC_CDML_CL_LINE.CLCL_ID", "dbo.CMC_CLCL_CLAIM"),
    ):
        proposal = pipeline["by_column"][qualified]
        assert proposal.generator == "reference", qualified
        assert proposal.params["parent_table"] == parent


@pytest.mark.db
def test_a_check_constraint_supplies_the_values_and_the_data_supplies_the_weights(pipeline):
    proposal = pipeline["by_column"]["dbo.CMC_MEME_MEMBER.MEME_REL"]
    assert proposal.generator == "lookup"
    assert sorted(proposal.params["values"]) == ["D", "M", "P", "S"]
    assert "weights" in proposal.params
    kinds = {e["kind"] for e in proposal.evidence}
    assert {"check_constraint", "top_values"} <= kinds


# ---------------------------------------------------------------------------
# The rest of the decision table
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_unique_string_keys_are_proposed_as_unique_patterns(pipeline):
    for qualified, regex in (
        ("dbo.CMC_MEME_MEMBER.MEME_ID", r"[A-Z]{3}\d{8}"),
        ("dbo.CMC_CLCL_CLAIM.CLCL_ID", r"\d{12}"),
        ("dbo.CMC_SBSB_SUBSC.SBSB_ID", r"[A-Z]{3}\d{6}"),
    ):
        proposal = pipeline["by_column"][qualified]
        assert proposal.generator == "pattern", qualified
        assert proposal.params["regex"] == regex
        assert proposal.params.get("unique") is True


@pytest.mark.db
def test_the_npi_proposal_admits_it_cannot_reproduce_the_check_digit(pipeline):
    proposal = pipeline["by_column"]["dbo.CMC_PRPR_PROV.PRPR_NPI"]
    assert proposal.generator == "pattern"
    assert any(e["kind"] == "check_digit" for e in proposal.evidence)
    assert "check digit" in proposal.note


@pytest.mark.db
def test_name_columns_draw_from_their_element_dictionary(pipeline):
    """A pattern fitted to surnames makes letter soup; the element makes names."""
    for qualified in (
        "dbo.CMC_MEME_MEMBER.MEME_FIRST_NAME",
        "dbo.CMC_MEME_MEMBER.MEME_LAST_NAME",
        "dbo.CMC_SBSB_SUBSC.SBSB_LAST_NAME",
    ):
        proposal = pipeline["by_column"][qualified]
        assert proposal.generator == "lookup", qualified
        assert proposal.provenance == "catalog"
        assert len(proposal.params["values"]) >= 40


@pytest.mark.db
def test_address_columns_keep_their_shared_record_key(pipeline):
    keys = {
        pipeline["by_column"][f"dbo.CMC_MEME_MEMBER.{column}"].params.get("record_key")
        for column in ("MEME_ZIP", "MEME_CITY", "MEME_STATE")
    }
    assert keys == {"address"}


@pytest.mark.db
def test_the_service_date_is_anchored_inside_member_coverage(pipeline):
    proposal = pipeline["by_column"]["dbo.CMC_CLCL_CLAIM.CLCL_SVC_DT"]
    assert proposal.generator == "date_relative"
    assert proposal.params["anchor"] == "dbo.CMC_MEME_MEMBER.MEME_EFF_DT"
    # The coverage end date bounds it from above.
    assert proposal.params.get("upper_anchor") == "dbo.CMC_MEME_MEMBER.MEME_TERM_DT"


@pytest.mark.db
def test_charge_amounts_are_proposed_as_a_fitted_distribution(pipeline):
    proposal = pipeline["by_column"]["dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT"]
    assert proposal.generator == "number"
    assert proposal.params["distribution"] == "lognormal"
    assert proposal.params["decimals"] == 2


@pytest.mark.db
def test_every_proposal_carries_evidence(pipeline):
    for proposal in pipeline["proposals"]:
        assert proposal.evidence, proposal.qualified
        assert proposal.confidence in {"high", "medium", "low"}
        for item in proposal.evidence:
            assert item["kind"] and item["detail"]


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_review_queue_is_small_enough_to_work_through(pipeline):
    assert len(pipeline["queue"]) < REVIEW_QUEUE_TARGET, summarize(pipeline["queue"])


@pytest.mark.db
def test_the_service_code_is_not_queued_as_uncertain(pipeline):
    """Its Facets name is a recorded alias of the procedure."""
    assert not [q for q in pipeline["queue"]
                if q.column == "CDML_SVC_CD" and "mapping_uncertain" in q.reasons]


@pytest.mark.db
def test_nothing_accepted_at_high_confidence_is_queued(pipeline):
    queued = {q.qualified for q in pipeline["queue"]}
    for proposal in pipeline["proposals"]:
        mapping = pipeline["mappings"].for_column(proposal.table, proposal.column)
        if (
            proposal.confidence == "high"
            and mapping is not None
            and mapping.accepted
            and proposal.bucket not in {"business_key", "derivable"}
        ):
            assert proposal.qualified not in queued, proposal.qualified


@pytest.mark.db
def test_system_columns_never_reach_the_queue(pipeline):
    assert not [q for q in pipeline["queue"] if q.bucket == "system"]


@pytest.mark.db
def test_decisions_template_lists_every_queued_column(pipeline, tmp_path):
    from sdg.review.queue import save_decisions

    template = decisions_template(pipeline["queue"])
    assert len(template["columns"]) == len(pipeline["queue"])
    assert all(entry["approved"] is False for entry in template["columns"])
    assert all(entry["_why"] for entry in template["columns"])

    path = save_decisions(template, tmp_path / "review-decisions.json")
    loaded = load_decisions(path)
    assert len(loaded["columns"]) == len(pipeline["queue"])
    assert loaded["tables"] == {}


def test_loading_a_missing_decisions_file_is_not_an_error(tmp_path):
    assert load_decisions(tmp_path / "nothing.json") == {"columns": [], "tables": {}}


# ---------------------------------------------------------------------------
# Review page
# ---------------------------------------------------------------------------

def test_long_value_lists_are_cut_down():
    text = summarize_params({"values": [f"V{i}" for i in range(600)]})
    assert "more" in text
    assert len(text) < 400
    assert "\n" not in text.strip("\n")


def test_params_render_one_line_per_key():
    text = summarize_params({"regex": r"\d{12}", "unique": True})
    assert text.splitlines() == [r'regex: "\\d{12}"', "unique: true"]


def test_a_ceiling_that_loops_back_is_rejected():
    """A received date anchors on service, so service cannot be bounded by it."""
    from sdg.plan.proposer import anchor_map, best_ceiling
    from sdg.profiling.model import DateOrdering

    orderings = [
        DateOrdering(kind="row", table="T", earlier="SVC", later="RECD",
                     rows_compared=100, share_in_order=1.0, gap_min=0, gap_max=30),
        DateOrdering(kind="join", table="T", earlier="SVC", later="P.TERM",
                     rows_compared=100, share_in_order=1.0, gap_min=0, gap_max=1700),
    ]
    anchors = anchor_map(orderings)
    assert anchors["RECD"] == "SVC"
    ceiling = best_ceiling("SVC", orderings, anchors)
    assert ceiling is not None and ceiling.later == "P.TERM"


@pytest.mark.db
def test_the_page_shows_every_table_in_the_slice(pipeline, tmp_path):
    context = build_context(
        pipeline["model"], pipeline["report"], pipeline["mappings"],
        pipeline["proposals"], pipeline["queue"], pipeline["catalog"],
    )
    path = write_page(context, tmp_path / "review.html")
    html = path.read_text(encoding="utf-8")

    for table in pipeline["model"].tables:
        assert table.name.upper() in html.upper(), table.name
    # One card per column, and no template left unrendered.
    assert html.count('class="panel card"') == sum(
        len(t.columns) for t in pipeline["model"].tables
    )
    assert "{{" not in html and "{%" not in html
    assert "Review queue" in html and "Hierarchy and volumes" in html
    assert "median (with children)" in html


@pytest.mark.db
def test_the_page_reports_the_success_measures(pipeline, tmp_path):
    context = build_context(
        pipeline["model"], pipeline["report"], pipeline["mappings"],
        pipeline["proposals"], pipeline["queue"], pipeline["catalog"],
    )
    labels = {stat["label"]: stat["value"] for stat in context["stats"]}
    assert labels["in the review queue"] == str(len(pipeline["queue"]))
    assert labels["auto-mapped"].endswith("%")
    assert int(labels["auto-mapped"].rstrip("%")) >= 60

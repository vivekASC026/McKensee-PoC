"""Step 2 acceptance: the schema validates the sample, rejects broken variants,
and load/save round-trips without losing anything."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sdg.config import REPO_ROOT
from sdg.plan.model import Plan, PlanError, new_meta, plan_schema, validate_document

SAMPLE_PATH = REPO_ROOT / "examples" / "plan-sample.json"


@pytest.fixture(scope="module")
def sample_document() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def test_schema_is_well_formed():
    schema = plan_schema()
    assert schema["title"] == "SDG generation plan"
    for section in ("meta", "sources", "tables", "generation_order", "warnings"):
        assert section in schema["properties"]


def test_sample_plan_validates(sample_document):
    validate_document(sample_document)
    plan = Plan.from_dict(sample_document)
    assert plan.meta.name == "facets-slice-sample"
    assert len(plan.tables) == 6
    assert plan.generation_order[0] == "dbo.CMC_PRPR_PROV"


def test_sample_plan_covers_the_whole_slice(sample_document):
    plan = Plan.from_dict(sample_document)
    names = {t.name for t in plan.tables}
    assert names == {
        "dbo.CMC_SBSB_SUBSC", "dbo.CMC_MEME_MEMBER", "dbo.CMC_PRPR_PROV",
        "dbo.CMC_PDPD_PRODUCT", "dbo.CMC_CLCL_CLAIM", "dbo.CMC_CDML_CL_LINE",
    }
    # Every table set to generate is either a root with a count or a child with a ratio.
    for table in plan.tables:
        if table.mode != "generate":
            continue
        assert (table.count is not None) ^ (table.ratio is not None), table.name


# ---------------------------------------------------------------------------
# Deliberately broken variants
# ---------------------------------------------------------------------------

def _without_meta_seed(doc):
    del doc["meta"]["seed"]
    return doc


def _unknown_generator(doc):
    doc["tables"][0]["fields"][0]["generator"] = "markov_chain"
    return doc


def _bad_table_mode(doc):
    doc["tables"][0]["mode"] = "maybe"
    return doc


def _field_without_bucket(doc):
    del doc["tables"][0]["fields"][0]["bucket"]
    return doc


def _bad_confidence(doc):
    doc["tables"][0]["fields"][0]["confidence"] = "very high"
    return doc


def _coverage_out_of_range(doc):
    doc["tables"][0]["fields"][0]["coverage"] = 1.4
    return doc


def _unknown_key_on_field(doc):
    doc["tables"][0]["fields"][0]["magic"] = True
    return doc


def _fixed_ratio_without_value(doc):
    doc["tables"][3]["ratio"] = {"parent": "dbo.CMC_SBSB_SUBSC", "kind": "fixed"}
    return doc


BROKEN = [
    ("meta without a seed", _without_meta_seed),
    ("unknown generator", _unknown_generator),
    ("table mode outside the enum", _bad_table_mode),
    ("field without a bucket", _field_without_bucket),
    ("confidence outside the enum", _bad_confidence),
    ("coverage above one", _coverage_out_of_range),
    ("unknown key on a field", _unknown_key_on_field),
    ("fixed ratio with no value", _fixed_ratio_without_value),
]


@pytest.mark.parametrize("label,break_it", BROKEN, ids=[b[0] for b in BROKEN])
def test_schema_rejects_broken_variants(sample_document, label, break_it):
    document = break_it(copy.deepcopy(sample_document))
    with pytest.raises(PlanError):
        Plan.from_dict(document)


# ---------------------------------------------------------------------------
# Consistency the JSON Schema cannot express
# ---------------------------------------------------------------------------

def test_reference_to_an_unknown_table_is_rejected(sample_document):
    document = copy.deepcopy(sample_document)
    claims = next(t for t in document["tables"] if t["name"] == "dbo.CMC_CLCL_CLAIM")
    field = next(f for f in claims["fields"] if f["name"] == "PRPR_ID")
    field["params"]["parent_table"] = "dbo.NOT_A_TABLE"
    with pytest.raises(PlanError, match="unknown table"):
        Plan.from_dict(document)


def test_parent_generated_after_its_child_is_rejected(sample_document):
    document = copy.deepcopy(sample_document)
    order = document["generation_order"]
    i, j = order.index("dbo.CMC_SBSB_SUBSC"), order.index("dbo.CMC_MEME_MEMBER")
    order[i], order[j] = order[j], order[i]
    with pytest.raises(PlanError, match="generated after its child"):
        Plan.from_dict(document)


def test_generation_order_must_cover_every_generate_table(sample_document):
    document = copy.deepcopy(sample_document)
    document["generation_order"].remove("dbo.CMC_CDML_CL_LINE")
    with pytest.raises(PlanError, match="missing tables set to generate"):
        Plan.from_dict(document)


def test_generator_without_its_required_params_is_rejected(sample_document):
    document = copy.deepcopy(sample_document)
    subs = next(t for t in document["tables"] if t["name"] == "dbo.CMC_SBSB_SUBSC")
    field = next(f for f in subs["fields"] if f["name"] == "SBSB_ID")
    field["params"] = {"unique": True}
    with pytest.raises(PlanError):
        Plan.from_dict(document)


def test_lookup_weights_must_match_values(sample_document):
    document = copy.deepcopy(sample_document)
    products = next(t for t in document["tables"] if t["name"] == "dbo.CMC_PDPD_PRODUCT")
    field = next(f for f in products["fields"] if f["name"] == "PDPD_TYPE")
    field["params"]["weights"] = [0.5, 0.3, 0.2]
    with pytest.raises(PlanError, match="weights"):
        Plan.from_dict(document)


def test_reuse_table_needs_a_source(sample_document):
    document = copy.deepcopy(sample_document)
    products = next(t for t in document["tables"] if t["name"] == "dbo.CMC_PDPD_PRODUCT")
    products["mode"] = "reuse"
    document["generation_order"].remove("dbo.CMC_PDPD_PRODUCT")
    with pytest.raises(PlanError, match="no source"):
        Plan.from_dict(document)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_round_trip_is_lossless(sample_document, tmp_path: Path):
    plan = Plan.from_dict(sample_document)
    assert plan.to_dict() == sample_document

    path = plan.save(tmp_path / "plan.json")
    reloaded = Plan.load(path)
    assert reloaded.to_dict() == sample_document
    assert reloaded == plan


def test_saved_plan_is_valid_json_with_a_trailing_newline(tmp_path: Path):
    plan = Plan.from_dict(json.loads(SAMPLE_PATH.read_text(encoding="utf-8")))
    path = plan.save(tmp_path / "plan.json")
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == plan.to_dict()


def test_load_reports_bad_json_clearly(tmp_path: Path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlanError, match="not valid JSON"):
        Plan.load(path)


def test_new_meta_stamps_a_timestamp():
    meta = new_meta("demo", "seed-1")
    assert meta.name == "demo" and meta.seed == "seed-1"
    assert meta.created.startswith("20")


def test_ratio_mean_children():
    plan = Plan.from_dict(json.loads(SAMPLE_PATH.read_text(encoding="utf-8")))
    members = plan.table("dbo.CMC_MEME_MEMBER")
    assert members.ratio is not None
    assert members.ratio.mean_children() == pytest.approx(2.31, abs=0.01)

    claims = plan.table("dbo.CMC_CLCL_CLAIM")
    # 30 percent of members get no claims, so the mean sits below the median of the tail.
    assert 4.0 < claims.ratio.mean_children() < 6.0

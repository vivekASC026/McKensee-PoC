"""Step 4 acceptance: metadata, classification, and foreign-key inference.

The expected bucket for every column in the slice is written out below as a
fixture, so a change in the classifier shows up as a named failure.
"""

from __future__ import annotations

import pytest

from sdg.discovery.classify import classify_column, tokenize
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.inference import candidates
from sdg.discovery.metadata import parse_check_values
from sdg.discovery.model import PhysicalModel
from sdg.discovery.run import discover

# Every column in the Facets slice and the bucket it must land in.
EXPECTED_BUCKETS = {
    "dbo.CMC_CDML_CL_LINE": {
        "CLCL_ID": "reference",
        "CDML_SEQ_NO": "business_key",
        "CDML_SVC_CD": "free",
        "CDML_CHG_AMT": "free",
        "CDML_UNITS": "free",
    },
    "dbo.CMC_CLCL_AUDIT": {
        "AUDIT_ID": "system",
        "CLCL_ID": "free",
        "AUDIT_ACTION": "free",
        "AUDIT_TS": "system",
    },
    "dbo.CMC_CLCL_CLAIM": {
        "CLCL_ID": "business_key",
        "MEME_CK": "reference",
        "PRPR_ID": "reference",
        "PDPD_ID": "code",
        "CLCL_CUR_STS": "free",
        "CLCL_CL_TYPE": "code",
        "CLCL_NTWK_IND": "code",
        "NWNW_ID": "code",
        "CLCL_IDCD_ID": "code",
        "CLCL_SVC_DT": "free",
        "CLCL_RECD_DT": "free",
        "CLCL_PAID_DT": "free",
        "CLCL_TOT_CHG": "derivable",
        "CLCL_CREATED_TS": "system",
        "CLCL_ROW_HASH": "system",
    },
    "dbo.CMC_CSPD_CAT_DESC": {
        "CSPD_CAT": "business_key",
        "CSPD_CAT_DESC": "free",
        "CSPD_TYPE": "free",
    },
    "dbo.CMC_CSPI_CS_PLAN": {
        "CSPI_ID": "business_key",
        "CSPD_CAT": "code",
        "PDPD_ID": "code",
        "CSPI_EFF_DT": "free",
        "CSPI_TERM_DT": "free",
        "CSPI_SEL_IND": "free",
        "NWST_PFX": "free",
    },
    "dbo.CMC_GRGR_GROUP": {
        "GRGR_CK": "system",
        "GRGR_ID": "business_key",
        "GRGR_NAME": "free",
        "GRGR_STATE": "free",
        "GRGR_COUNTY": "free",
        "GRGR_STS": "code",
        "GRGR_MCTR_TYPE": "free",
        "GRGR_ORIG_EFF_DT": "free",
        "GRGR_TERM_DT": "free",
    },
    "dbo.CMC_IDCD_DIAG_CODE": {
        "IDCD_ID": "business_key",
        "IDCD_DESC": "free",
        "IDCD_CHAPTER": "free",
    },
    "dbo.CMC_IPCD_PROC_CODE": {
        "IPCD_ID": "business_key",
        "IPCD_DESC": "free",
    },
    "dbo.CMC_MEME_MEMBER": {
        "MEME_CK": "system",
        "SBSB_CK": "reference",
        "MEME_ID": "business_key",
        "MEME_FIRST_NAME": "free",
        "MEME_LAST_NAME": "free",
        "MEME_MID_INIT": "free",
        "MEME_BIRTH_DT": "free",
        "MEME_SEX": "code",
        "MEME_MARITAL_STATUS": "code",
        "MEME_SSN": "free",
        "MEME_MCTR_STS": "free",
        "MEME_REL": "code",
        "MEME_SFX": "free",
        "MEME_EFF_DT": "free",
        "MEME_ORIG_EFF_DT": "free",
        "MEME_TERM_DT": "free",
        "MEME_ZIP": "free",
        "MEME_CITY": "free",
        "MEME_STATE": "free",
    },
    "dbo.CMC_PDDS_PROD_DESC": {
        "PDPD_ID": "code",
        "PDDS_DESC": "free",
    },
    "dbo.CMC_PDPD_PRODUCT": {
        "PDPD_ID": "business_key",
        "PDPD_TYPE": "code",
    },
    "dbo.CMC_PLDS_PLAN_DESC": {
        "CSPI_ID": "code",
        "PLDS_DESC": "free",
    },
    "dbo.CMC_PRPR_PROV": {
        "PRPR_ID": "business_key",
        "PRPR_NPI": "business_key",
        "PRPR_SPECIALTY": "free",
        "PRPR_STATE": "free",
        "PRPR_ENTITY": "code",
    },
    "dbo.CMC_SBSB_SUBSC": {
        "SBSB_CK": "system",
        "SBSB_ID": "business_key",
        "SBSB_LAST_NAME": "free",
        "SBSB_FIRST_NAME": "free",
        "SBSB_EFF_DT": "free",
        "SBSB_ORIG_EFF_DT": "free",
        "SBSB_MCTR_STS": "free",
        "SBSB_EMPLOY_ID": "free",
        "GRGR_CK": "code",
        "SGSG_CK": "code",
        "CSPI_ID": "code",
    },
    "dbo.CMC_SGSG_SUBGROUP": {
        "SGSG_CK": "system",
        "GRGR_CK": "code",
        "SGSG_ID": "business_key",
        "SGSG_NAME": "free",
        "CSCS_ID": "free",
        "SGSG_STATE": "free",
        "SGSG_STS": "free",
        "SGSG_ORIG_EFF_DT": "free",
        "SGSG_TERM_DT": "free",
    },
}

# The undeclared foreign keys the seed contains. The description tables hold
# exactly one row per parent, so inclusion holds in both directions and the
# inferred key can point either way; the declared constraint settles which is
# which -- a product's own key is never inferred to point at its description,
# which declares the key the other way.
EXPECTED_INFERENCES = {
    ("dbo.CMC_CLCL_CLAIM", "MEME_CK", "dbo.CMC_MEME_MEMBER", "MEME_CK"),
    ("dbo.CMC_CLCL_CLAIM", "PDPD_ID", "dbo.CMC_PDDS_PROD_DESC", "PDPD_ID"),
    ("dbo.CMC_CSPI_CS_PLAN", "PDPD_ID", "dbo.CMC_PDDS_PROD_DESC", "PDPD_ID"),
    ("dbo.CMC_SBSB_SUBSC", "CSPI_ID", "dbo.CMC_PLDS_PLAN_DESC", "CSPI_ID"),
    ("dbo.CMC_CLCL_AUDIT", "CLCL_ID", "dbo.CMC_CLCL_CLAIM", "CLCL_ID"),
}


@pytest.fixture(scope="module")
def discovery():
    return discover(ConnectionRegistry(), "source")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_tokenize_splits_names():
    assert tokenize("MEME_BIRTH_DT") == ["MEME", "BIRTH", "DT"]
    assert tokenize("CLCL_TOT_CHG") == ["CLCL", "TOT", "CHG"]
    assert tokenize("memberId") == ["MEMBER", "ID"]


@pytest.mark.parametrize("definition,expected", [
    ("([MEME_REL]='53' OR [MEME_REL]='01' OR [MEME_REL]='19' OR [MEME_REL]='18')",
     ("MEME_REL", ["01", "18", "19", "53"])),
    ("([PDPD_TYPE]='D' OR [PDPD_TYPE]='M')", ("PDPD_TYPE", ["D", "M"])),
    ("([STATUS] IN ('A','B','C'))", ("STATUS", ["A", "B", "C"])),
    # A range is not a value list.
    ("([AMT]>=(0) AND [AMT]<=(100))", (None, None)),
    ("([A]='x' AND [A]<>'y')", ("A", None)),
])
def test_parse_check_values(definition, expected):
    assert parse_check_values(definition) == expected


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_structural_flags_are_read_correctly(discovery):
    model = discovery.model
    claims = model.table("dbo.CMC_CLCL_CLAIM")
    assert claims is not None

    assert model.table("dbo.CMC_MEME_MEMBER").column("MEME_CK").is_identity
    assert model.table("dbo.CMC_SBSB_SUBSC").column("SBSB_CK").is_identity

    row_hash = claims.column("CLCL_ROW_HASH")
    assert row_hash.is_computed and row_hash.computed_definition

    created = claims.column("CLCL_CREATED_TS")
    assert not created.is_computed
    assert "getdate" in (created.default_definition or "").lower()

    # Nothing else in the slice is identity, computed, or defaulted.
    flagged = {
        f"{t.table_name}.{c.name}"
        for t, c in model.columns()
        if c.is_identity or c.is_computed or c.default_definition
    }
    assert flagged == {
        "CMC_CLCL_AUDIT.AUDIT_ID",
        "CMC_CLCL_AUDIT.AUDIT_TS",
        "CMC_CLCL_CLAIM.CLCL_CREATED_TS",
        "CMC_CLCL_CLAIM.CLCL_ROW_HASH",
        "CMC_GRGR_GROUP.GRGR_CK",
        "CMC_MEME_MEMBER.MEME_CK",
        "CMC_SBSB_SUBSC.SBSB_CK",
        "CMC_SGSG_SUBGROUP.SGSG_CK",
    }


@pytest.mark.db
def test_keys_and_constraints_are_read(discovery):
    model = discovery.model
    lines = model.table("dbo.CMC_CDML_CL_LINE")
    assert lines.primary_key == ["CLCL_ID", "CDML_SEQ_NO"]

    members = model.table("dbo.CMC_MEME_MEMBER")
    assert members.primary_key == ["MEME_CK"]
    assert "MEME_ID" in members.unique_single_columns()
    assert members.column("MEME_REL").check_values == ["D", "M", "P", "S"]

    declared = {
        (t.name, tuple(fk.columns), fk.parent_table)
        for t in model.tables
        for fk in t.foreign_keys
        if fk.kind == "declared"
    }
    assert ("dbo.CMC_MEME_MEMBER", ("SBSB_CK",), "dbo.CMC_SBSB_SUBSC") in declared
    assert ("dbo.CMC_CDML_CL_LINE", ("CLCL_ID",), "dbo.CMC_CLCL_CLAIM") in declared
    assert ("dbo.CMC_CLCL_CLAIM", ("PRPR_ID",), "dbo.CMC_PRPR_PROV") in declared
    assert ("dbo.CMC_CLCL_CLAIM", ("PDPD_ID",), "dbo.CMC_PDPD_PRODUCT") in declared


@pytest.mark.db
def test_triggers_are_reported(discovery):
    claims = discovery.model.table("dbo.CMC_CLCL_CLAIM")
    assert claims.triggers == ["TR_CMC_CLCL_CLAIM_AUDIT"]
    assert discovery.model.table("dbo.CMC_MEME_MEMBER").triggers == []


@pytest.mark.db
def test_row_counts_come_from_catalog_statistics(discovery):
    counts = {t.name: t.row_count for t in discovery.model.tables}
    assert counts["dbo.CMC_CLCL_CLAIM"] > 0
    assert counts["dbo.CMC_CDML_CL_LINE"] > counts["dbo.CMC_CLCL_CLAIM"]
    assert counts["dbo.CMC_PDPD_PRODUCT"] == 25


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_candidate_list_stays_small(discovery):
    """Name and type narrowing must keep the query count down."""
    assert len(candidates(discovery.model)) <= 6


@pytest.mark.db
def test_inference_finds_the_planted_key_and_nothing_false(discovery):
    found = {
        (k.child_table, k.child_column, k.parent_table, k.parent_column)
        for k in discovery.inferred
    }
    assert ("dbo.CMC_CLCL_CLAIM", "MEME_CK", "dbo.CMC_MEME_MEMBER", "MEME_CK") in found
    # Every inference must be one of the relationships the seed really contains.
    assert found <= EXPECTED_INFERENCES, f"false inference: {found - EXPECTED_INFERENCES}"


@pytest.mark.db
def test_inferred_key_carries_its_evidence(discovery):
    key = next(
        k for k in discovery.inferred
        if (k.child_table, k.child_column) == ("dbo.CMC_CLCL_CLAIM", "MEME_CK")
    )
    assert key.coverage == 1.0
    assert key.shared_tokens == ["MEME"]
    assert any("distinct values exist in" in line for line in key.evidence())


@pytest.mark.db
def test_inferred_keys_are_attached_to_the_model(discovery):
    claims = discovery.model.table("dbo.CMC_CLCL_CLAIM")
    inferred = {
        fk.columns[0]: fk.parent_table
        for fk in claims.foreign_keys if fk.kind == "inferred"
    }
    # MEME_CK is the planted one. PDPD_ID is inferred as well now that the
    # product has a description table holding exactly its own identifiers.
    assert inferred["MEME_CK"] == "dbo.CMC_MEME_MEMBER"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.db
@pytest.mark.parametrize("table_name", sorted(EXPECTED_BUCKETS))
def test_every_column_lands_in_its_expected_bucket(discovery, table_name):
    table = discovery.model.table(table_name)
    assert table is not None, table_name
    actual = {c.name: c.bucket for c in table.columns}
    expected = dict(EXPECTED_BUCKETS[table_name])
    if table_name == "dbo.CMC_CLCL_AUDIT" and actual.get("CLCL_ID") == "reference":
        # Inferred as a key to claims while every audit row still resolves --
        # true of a fresh seed, not after a generation run is cleaned up.
        expected["CLCL_ID"] = "reference"
    assert actual == expected


@pytest.mark.db
def test_every_column_carries_a_reason(discovery):
    for table, column in discovery.model.columns():
        assert column.bucket, f"{table.name}.{column.name} has no bucket"
        assert column.bucket_reason, f"{table.name}.{column.name} has no reason"


@pytest.mark.db
def test_a_column_can_be_declared_derivable(discovery):
    model = discovery.model
    members = model.table("dbo.CMC_MEME_MEMBER")
    column = members.column("MEME_CITY")
    bucket, reason = classify_column(
        members, column, model, derivable_columns=["dbo.CMC_MEME_MEMBER.MEME_CITY"]
    )
    assert bucket == "derivable"
    assert reason == "declared as derivable"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_physical_model_round_trips(discovery, tmp_path):
    path = discovery.model.save(tmp_path / "physical-model.json")
    reloaded = PhysicalModel.load(path)
    assert reloaded.table_names() == discovery.model.table_names()
    assert reloaded == discovery.model


@pytest.mark.db
def test_summary_reports_what_was_found(discovery):
    summary = discovery.summary()
    # The network tables and member PCPs are the 16th to 19th.
    assert summary["tables"] == 19
    # Seven more with the network tables, the plan's network set and PCPs.
    assert summary["declared_foreign_keys"] == 20
    # The claims-to-members key is always inferred. The audit-to-claims one is
    # only inferred while every audit row still resolves, which a generation
    # run followed by a cleanup can change, so the count is a floor.
    assert summary["inferred_foreign_keys"] >= 1
    assert summary["buckets"]["system"] == 8

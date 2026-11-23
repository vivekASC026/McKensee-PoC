"""Step 6 acceptance: the catalog loads, and columns map to elements with evidence."""

from __future__ import annotations

import pytest

from sdg.catalog.loader import CatalogError, load_catalog, load_dictionary
from sdg.catalog.model import Catalog, Element
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.run import discover
from sdg.mapping.matchers import alias_match, informative, name_token_match
from sdg.mapping.scorer import MappingStore, map_columns, score_match
from sdg.mapping.matchers import Evidence, Match
from sdg.profiling.run import profile

# What each mappable column must map to. A high-confidence mapping to anything
# else is a false accept, which is the failure this fixture exists to catch.
EXPECTED = {
    # The identity columns are system-bucket, so the writer leaves them to the
    # database, but they still carry an element and mapping them is correct.
    # Every column the mapper accepts at high confidence is listed, so a new
    # column silently binding to the wrong element shows up as a failure.
    "dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT": "CHARGE_AMOUNT",
    "dbo.CMC_CDML_CL_LINE.CDML_SEQ_NO": "LINE_NUMBER",
    "dbo.CMC_CDML_CL_LINE.CDML_UNITS": "SERVICE_UNITS",
    "dbo.CMC_CDML_CL_LINE.CLCL_ID": "CLAIM_ID",
    "dbo.CMC_CLCL_AUDIT.AUDIT_ACTION": "AUDIT_ACTION",
    "dbo.CMC_CLCL_AUDIT.CLCL_ID": "CLAIM_ID",
    "dbo.CMC_CLCL_CLAIM.CLCL_CL_TYPE": "CLAIM_TYPE",
    "dbo.CMC_CLCL_CLAIM.CLCL_CUR_STS": "CLAIM_STATUS",
    "dbo.CMC_CLCL_CLAIM.CLCL_ID": "CLAIM_ID",
    "dbo.CMC_CLCL_CLAIM.CLCL_IDCD_ID": "DIAGNOSIS_CODE",
    "dbo.CMC_CLCL_CLAIM.CLCL_NTWK_IND": "NETWORK_INDICATOR",
    "dbo.CMC_CLCL_CLAIM.CLCL_PAID_DT": "PAID_DATE",
    "dbo.CMC_CLCL_CLAIM.CLCL_RECD_DT": "RECEIVED_DATE",
    "dbo.CMC_CLCL_CLAIM.CLCL_SVC_DT": "SERVICE_DATE",
    "dbo.CMC_CLCL_CLAIM.CLCL_TOT_CHG": "CHARGE_AMOUNT",
    "dbo.CMC_CLCL_CLAIM.MEME_CK": "MEMBER_KEY",
    "dbo.CMC_CLCL_CLAIM.PDPD_ID": "PRODUCT_CODE",
    "dbo.CMC_CLCL_CLAIM.PRPR_ID": "PROVIDER_ID",
    "dbo.CMC_CSPD_CAT_DESC.CSPD_CAT_DESC": "PLAN_CATEGORY_DESC",
    "dbo.CMC_CSPD_CAT_DESC.CSPD_TYPE": "PLAN_CATEGORY_TYPE",
    "dbo.CMC_CSPI_CS_PLAN.CSPI_EFF_DT": "EFFECTIVE_DATE",
    "dbo.CMC_CSPI_CS_PLAN.CSPI_ID": "COVERAGE_PLAN_ID",
    "dbo.CMC_CSPI_CS_PLAN.CSPI_TERM_DT": "TERMINATION_DATE",
    "dbo.CMC_CSPI_CS_PLAN.PDPD_ID": "PRODUCT_CODE",
    "dbo.CMC_GRGR_GROUP.GRGR_CK": "GROUP_KEY",
    "dbo.CMC_GRGR_GROUP.GRGR_COUNTY": "ADDRESS_COUNTY",
    "dbo.CMC_GRGR_GROUP.GRGR_ID": "GROUP_ID",
    "dbo.CMC_GRGR_GROUP.GRGR_MCTR_TYPE": "GROUP_TYPE",
    "dbo.CMC_GRGR_GROUP.GRGR_NAME": "GROUP_NAME",
    "dbo.CMC_GRGR_GROUP.GRGR_ORIG_EFF_DT": "ORIGINAL_EFFECTIVE_DATE",
    "dbo.CMC_GRGR_GROUP.GRGR_STATE": "ADDRESS_STATE",
    "dbo.CMC_GRGR_GROUP.GRGR_TERM_DT": "TERMINATION_DATE",
    "dbo.CMC_IDCD_DIAG_CODE.IDCD_CHAPTER": "DIAGNOSIS_CHAPTER",
    "dbo.CMC_IDCD_DIAG_CODE.IDCD_DESC": "DIAGNOSIS_DESCRIPTION",
    "dbo.CMC_IDCD_DIAG_CODE.IDCD_ID": "DIAGNOSIS_CODE",
    "dbo.CMC_IPCD_PROC_CODE.IPCD_DESC": "PROCEDURE_DESCRIPTION",
    # Provider networks: who is in which network over which dates.
    "dbo.CMC_NWNW_NETWORK.NWNW_ID": "NETWORK_ID",
    "dbo.CMC_NWPR_RELATION.NWNW_ID": "NETWORK_ID",
    "dbo.CMC_NWPR_RELATION.PRPR_ID": "PROVIDER_ID",
    "dbo.CMC_NWPR_RELATION.NWPR_EFF_DT": "EFFECTIVE_DATE",
    "dbo.CMC_NWPR_RELATION.NWPR_TERM_DT": "TERMINATION_DATE",
    "dbo.CMC_NWST_NET_SET.NWST_EFF_DT": "EFFECTIVE_DATE",
    "dbo.CMC_NWST_NET_SET.NWST_TERM_DT": "TERMINATION_DATE",
    "dbo.CMC_NWST_NET_SET.NWNW_ID": "NETWORK_ID",
    # The claim names the network it was in network through.
    "dbo.CMC_CLCL_CLAIM.NWNW_ID": "NETWORK_ID",
    # A member's primary care provider, over dates.
    "dbo.CMC_MEPR_PRIM_PROV.MEME_CK": "MEMBER_KEY",
    "dbo.CMC_MEPR_PRIM_PROV.GRGR_CK": "GROUP_KEY",
    "dbo.CMC_MEPR_PRIM_PROV.PRPR_ID": "PROVIDER_ID",
    "dbo.CMC_MEPR_PRIM_PROV.MEPR_EFF_DT": "EFFECTIVE_DATE",
    "dbo.CMC_MEPR_PRIM_PROV.MEPR_TERM_DT": "TERMINATION_DATE",
    "dbo.CMC_IPCD_PROC_CODE.IPCD_ID": "PROCEDURE_CODE",
    "dbo.CMC_CDML_CL_LINE.CDML_SVC_CD": "PROCEDURE_CODE",
    "dbo.CMC_MEME_MEMBER.MEME_BIRTH_DT": "DOB",
    "dbo.CMC_MEME_MEMBER.MEME_CITY": "ADDRESS_CITY",
    "dbo.CMC_MEME_MEMBER.MEME_CK": "MEMBER_KEY",
    "dbo.CMC_MEME_MEMBER.MEME_EFF_DT": "EFFECTIVE_DATE",
    "dbo.CMC_MEME_MEMBER.MEME_FIRST_NAME": "FIRST_NAME",
    "dbo.CMC_MEME_MEMBER.MEME_ID": "MEMBER_ID",
    "dbo.CMC_MEME_MEMBER.MEME_LAST_NAME": "LAST_NAME",
    "dbo.CMC_MEME_MEMBER.MEME_MARITAL_STATUS": "MARITAL_STATUS",
    "dbo.CMC_MEME_MEMBER.MEME_MCTR_STS": "MEMBER_STATUS",
    "dbo.CMC_MEME_MEMBER.MEME_MID_INIT": "MIDDLE_INITIAL",
    "dbo.CMC_MEME_MEMBER.MEME_ORIG_EFF_DT": "ORIGINAL_EFFECTIVE_DATE",
    "dbo.CMC_MEME_MEMBER.MEME_REL": "RELATIONSHIP_CODE",
    "dbo.CMC_MEME_MEMBER.MEME_SEX": "SEX",
    "dbo.CMC_MEME_MEMBER.MEME_SSN": "SSN",
    "dbo.CMC_MEME_MEMBER.MEME_STATE": "ADDRESS_STATE",
    "dbo.CMC_MEME_MEMBER.MEME_TERM_DT": "TERMINATION_DATE",
    "dbo.CMC_MEME_MEMBER.MEME_ZIP": "ADDRESS_ZIP",
    "dbo.CMC_MEME_MEMBER.SBSB_CK": "SUBSCRIBER_KEY",
    "dbo.CMC_PDDS_PROD_DESC.PDDS_DESC": "PRODUCT_DESCRIPTION",
    "dbo.CMC_PDDS_PROD_DESC.PDPD_ID": "PRODUCT_CODE",
    "dbo.CMC_PDPD_PRODUCT.PDPD_ID": "PRODUCT_CODE",
    "dbo.CMC_PDPD_PRODUCT.PDPD_TYPE": "PRODUCT_TYPE",
    "dbo.CMC_PLDS_PLAN_DESC.CSPI_ID": "COVERAGE_PLAN_ID",
    "dbo.CMC_PLDS_PLAN_DESC.PLDS_DESC": "PLAN_DESCRIPTION",
    "dbo.CMC_PRPR_PROV.PRPR_ID": "PROVIDER_ID",
    "dbo.CMC_PRPR_PROV.PRPR_NPI": "NPI",
    "dbo.CMC_PRPR_PROV.PRPR_SPECIALTY": "PROVIDER_SPECIALTY",
    "dbo.CMC_PRPR_PROV.PRPR_STATE": "ADDRESS_STATE",
    "dbo.CMC_SBSB_SUBSC.CSPI_ID": "COVERAGE_PLAN_ID",
    "dbo.CMC_SBSB_SUBSC.GRGR_CK": "GROUP_KEY",
    "dbo.CMC_SBSB_SUBSC.SBSB_CK": "SUBSCRIBER_KEY",
    "dbo.CMC_SBSB_SUBSC.SBSB_EFF_DT": "EFFECTIVE_DATE",
    "dbo.CMC_SBSB_SUBSC.SBSB_EMPLOY_ID": "EMPLOYEE_ID",
    "dbo.CMC_SBSB_SUBSC.SBSB_FIRST_NAME": "FIRST_NAME",
    "dbo.CMC_SBSB_SUBSC.SBSB_ID": "SUBSCRIBER_ID",
    "dbo.CMC_SBSB_SUBSC.SBSB_LAST_NAME": "LAST_NAME",
    "dbo.CMC_SBSB_SUBSC.SBSB_MCTR_STS": "MEMBER_STATUS",
    "dbo.CMC_SBSB_SUBSC.SBSB_ORIG_EFF_DT": "ORIGINAL_EFFECTIVE_DATE",
    "dbo.CMC_SBSB_SUBSC.SGSG_CK": "SUBGROUP_KEY",
    "dbo.CMC_SGSG_SUBGROUP.GRGR_CK": "GROUP_KEY",
    "dbo.CMC_SGSG_SUBGROUP.SGSG_CK": "SUBGROUP_KEY",
    "dbo.CMC_SGSG_SUBGROUP.SGSG_NAME": "SUBGROUP_NAME",
    "dbo.CMC_SGSG_SUBGROUP.SGSG_ORIG_EFF_DT": "ORIGINAL_EFFECTIVE_DATE",
    "dbo.CMC_SGSG_SUBGROUP.SGSG_STATE": "ADDRESS_STATE",
    "dbo.CMC_SGSG_SUBGROUP.SGSG_TERM_DT": "TERMINATION_DATE",
}



AUTO_MAP_TARGET = 0.60


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def store(catalog):
    registry = ConnectionRegistry()
    model = discover(registry, "source").model
    report = profile(registry, model, skip_pairwise=True)
    return map_columns(model, catalog, report)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_catalog_holds_the_elements_the_brief_names(catalog):
    required = {
        "SUBSCRIBER_ID", "MEMBER_ID", "FIRST_NAME", "LAST_NAME", "DOB",
        "ADDRESS_ZIP", "ADDRESS_CITY", "ADDRESS_STATE", "RELATIONSHIP_CODE",
        "NPI", "PROVIDER_ID", "PROVIDER_SPECIALTY", "PRODUCT_CODE", "PLAN_CODE",
        "CLAIM_ID", "PROCEDURE_CODE", "CLAIM_STATUS", "CHARGE_AMOUNT",
        "SERVICE_DATE", "SSN",
    }
    assert required <= {e.name for e in catalog.elements}
    assert 15 <= len(catalog.elements) <= 60


def test_catalog_uses_all_three_rules(catalog):
    summary = catalog.summary()
    assert summary["rules"]["generate"] > 0
    assert summary["rules"]["reuse"] >= 2
    assert summary["rules"]["copy"] >= 1


def test_reuse_elements_are_bound_to_master_data(catalog):
    for name in ("PLAN_CODE", "PRODUCT_CODE"):
        element = catalog.element(name)
        assert element.rule == "reuse"
        assert element.reuse_domain is not None
        assert element.reuse_domain.connection == "ref"
    # Only active plan codes are drawn from.
    assert catalog.element("PLAN_CODE").reuse_domain.filter == "ACTIVE_FLAG = 'Y'"


def test_catalog_holds_elements_this_application_does_not_use(catalog, store):
    """The catalog belongs to the organization, not to the project."""
    used = {m.element for m in store.mappings if m.element}
    # SSN is held on the member now that the fixture carries one, so the
    # unused pair is a plan code the application never stores and an entity
    # name no table in this slice holds.
    assert "LEGAL_ENTITY_NAME" in {e.name for e in catalog.elements}
    assert "LEGAL_ENTITY_NAME" not in used
    assert "PLAN_CODE" not in used


def test_pii_is_classified(catalog):
    assert catalog.element("SSN").pii == "sensitive"
    assert catalog.element("MEMBER_ID").pii == "identifier"
    assert catalog.element("CLAIM_STATUS").pii == "none"


def test_dictionaries_are_expanded_at_load_time(catalog):
    zip_element = catalog.element("ADDRESS_ZIP")
    assert "dictionary" not in zip_element.params
    values = zip_element.params["values"]
    assert len(values) >= 500
    assert set(values[0]) == {"zip", "city", "state"}

    first_names = catalog.element("FIRST_NAME").params["values"]
    assert len(first_names) >= 40 and isinstance(first_names[0], str)


def test_address_columns_share_a_record_key(catalog):
    """ZIP, city, and state must be drawn from one record, not three."""
    keys = {
        catalog.element(name).params.get("record_key")
        for name in ("ADDRESS_ZIP", "ADDRESS_CITY", "ADDRESS_STATE")
    }
    assert keys == {"address"}


def test_load_dictionary_reports_a_missing_file():
    with pytest.raises(CatalogError, match="not found"):
        load_dictionary("no_such_dictionary")


def test_a_generate_element_must_name_a_generator():
    with pytest.raises(ValueError, match="needs a generator"):
        Element(name="X", rule="generate")


def test_a_reuse_element_must_name_a_domain():
    with pytest.raises(ValueError, match="needs a reuse_domain"):
        Element(name="X", rule="reuse")


def test_duplicate_element_names_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        Catalog(elements=[
            Element(name="X", rule="generate", generator="constant", params={"value": 1}),
            Element(name="X", rule="generate", generator="constant", params={"value": 2}),
        ])


# ---------------------------------------------------------------------------
# Matchers
# ---------------------------------------------------------------------------

def test_informative_drops_generic_tokens():
    assert informative(["MEME", "ID"]) == {"MEME"}
    assert informative(["SEQ", "NO"]) == set()


def test_alias_matching_is_token_based():
    element = Element(
        name="FIRST_NAME", rule="generate", generator="lookup",
        params={"values": ["a"]}, aliases=["FIRST_NAME", "FNAME"],
    )
    assert alias_match("MEME_FIRST_NAME", element) is not None
    assert alias_match("FIRST_NAME", element).score == 1.0
    assert alias_match("LAST_NAME", element) is None


def test_an_alias_of_only_generic_tokens_never_matches():
    element = Element(
        name="LINE_NUMBER", rule="generate", generator="sequence",
        params={"start": 1}, aliases=["SEQ_NO"],
    )
    # SEQ and NO are both generic, so this alias must not match every sequence.
    assert alias_match("ORDER_SEQ_NO", element) is None


def test_name_token_is_only_a_weak_signal():
    element = Element(
        name="PROCEDURE_CODE", rule="generate", generator="pattern",
        params={"regex": "[A-Z]"}, aliases=["PROC_CD"],
    )
    evidence = name_token_match("CDML_PROCEDURE_ID", element)
    assert evidence is not None and evidence.score < 0.5


@pytest.mark.parametrize("kinds,expected", [
    ({"alias"}, "high"),
    ({"fk_propagation"}, "high"),
    ({"check_digit"}, "high"),
    ({"format", "name_token"}, "high"),
    ({"format"}, "medium"),
    ({"name_token"}, "low"),
])
def test_scoring_follows_the_brief(kinds, expected):
    match = Match(element="X", evidence=[Evidence(kind=k, detail="") for k in kinds])
    assert score_match(match)[0] == expected


# ---------------------------------------------------------------------------
# Against the seeded database
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_auto_map_rate_clears_the_target(store):
    stats = store.statistics()
    assert stats["auto_map_rate"] >= AUTO_MAP_TARGET, stats
    # MEME_SFX, the member's suffix, is the 87th; the network tables, the
    # plan's network set, the claim's network and member PCPs bring 114.
    assert stats["mappable_columns"] == 114
    assert stats["system_columns"] == 8


@pytest.mark.db
def test_no_column_maps_at_high_confidence_to_the_wrong_element(store):
    wrong = [
        f"{m.qualified}: got {m.element}, expected {EXPECTED.get(m.qualified)}"
        for m in store.mappings
        if m.confidence == "high" and EXPECTED.get(m.qualified) != m.element
    ]
    assert not wrong, wrong


@pytest.mark.db
def test_every_expected_mapping_is_found(store):
    for qualified, element in EXPECTED.items():
        mapping = store.for_column(*qualified.rsplit(".", 1))
        assert mapping is not None, qualified
        assert mapping.element == element, qualified


@pytest.mark.db
def test_the_service_code_is_the_procedure_by_its_facets_name(store):
    """In Facets the claim line's service code is the procedure, and the
    customer's denorm names it IPCD_ID; both are recorded aliases."""
    mapping = store.for_column("dbo.CMC_CDML_CL_LINE", "CDML_SVC_CD")
    assert mapping.element == "PROCEDURE_CODE"
    assert mapping.confidence == "high"
    assert mapping.accepted


@pytest.mark.db
def test_system_columns_are_not_counted_or_queued(store):
    for mapping in store.mappings:
        if mapping.bucket == "system":
            assert not mapping.needs_review()


@pytest.mark.db
def test_foreign_keys_inherit_their_parent_element(store):
    """A claim's provider must carry the same element the provider table does."""
    parent = store.for_column("dbo.CMC_PRPR_PROV", "PRPR_ID")
    child = store.for_column("dbo.CMC_CLCL_CLAIM", "PRPR_ID")
    assert parent.element == child.element == "PROVIDER_ID"
    assert "fk_propagation" in {e["kind"] for e in child.evidence}


@pytest.mark.db
def test_the_inferred_key_propagates_too(store):
    """A key nobody declared still carries its element to the child column.

    Claims point at members through MEME_CK and declare no constraint, so the
    key is inferred -- and the element travels along it just as it does along
    a declared one.
    """
    claims = store.for_column("dbo.CMC_CLCL_CLAIM", "MEME_CK")
    assert claims.element == "MEMBER_KEY"
    propagation = next(e for e in claims.evidence if e["kind"] == "fk_propagation")
    assert "inferred" in propagation["detail"]

    # A product's own key is a key, not a link to its description: the
    # description table declares the key the other way.
    product = store.for_column("dbo.CMC_PDPD_PRODUCT", "PDPD_ID")
    assert product.element == "PRODUCT_CODE"
    assert "fk_propagation" not in {e["kind"] for e in product.evidence}


@pytest.mark.db
def test_the_npi_is_recognized_by_its_check_digit(store):
    mapping = store.for_column("dbo.CMC_PRPR_PROV", "PRPR_NPI")
    assert mapping.element == "NPI"
    assert "check_digit" in {e["kind"] for e in mapping.evidence}


@pytest.mark.db
def test_every_mapping_carries_its_evidence(store):
    for mapping in store.mappings:
        if mapping.element:
            assert mapping.evidence, mapping.qualified
            assert mapping.reason, mapping.qualified
            for item in mapping.evidence:
                assert item["kind"] and item["detail"]


@pytest.mark.db
def test_statistics_break_the_rate_down_by_evidence(store):
    stats = store.statistics()
    by_kind = stats["high_confidence_by_evidence"]
    assert by_kind["alias"] > 0
    assert by_kind["fk_propagation"] > 0
    assert stats["high"] + stats["medium"] + stats["low"] <= stats["mappable_columns"]


@pytest.mark.db
def test_mapping_store_round_trips(store, tmp_path):
    path = store.save(tmp_path / "mappings.json")
    reloaded = MappingStore.load(path)
    assert reloaded.statistics() == store.statistics()
    assert reloaded.for_column("dbo.CMC_MEME_MEMBER", "MEME_ID").element == "MEMBER_ID"

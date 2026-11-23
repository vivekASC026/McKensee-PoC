"""The health business pack holds exactly what SDM's code held.

Every health constant was written into sdg/packs/health, and here each one
is held equal to the value kept in tests/fixtures/pack-constants.json before
it moved. The code reads them from the pack (tests/test_pack_constants.py
holds what it reads to the same values).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sdg import packs
from sdg.api.app import app
from tests.test_pack_constants import FILES, KEPT, ROOT, _plain

client = TestClient(app)

# Each kept constant, and where the health pack holds it.
PLACE = {
    "never generated": "pack:tables.never_generated",
    "table modes": "pack:tables.modes",
    "store elements": "pack:plan.store_elements",
    "always together": "pack:plan.always_together",
    "kind anchors": "pack:plan.kind_anchors",
    "sex fields": "pack:plan.sex_fields",
    "file elements": "pack:files.default_elements",
    "empty is a value": "pack:plan.empty_is_a_value",
    "combination key": "features:store.combo_key",
    "record tables": "pack:plan.record_tables",
    "scope elements": "pack:files.scope_elements",
    "record keys": "pack:files.record_keys",
    "subscriber codes": "pack:files.subscriber_codes",
    "household": "pack:files.household.family",
    "partner codes": "pack:files.household.partner_codes",
    "child years": "pack:files.household.child_years",
    "continuous features": "features:analyzer.continuous_features",
    "categorical features": "features:analyzer.categorical_features",
    "geography columns": "features:analyzer.geography_columns",
    "description columns": "features:analyzer.description_columns",
    "filter columns": "features:analyzer.filter_columns",
    "label cluster columns": "features:analyzer.label_cluster_columns",
    "numeric columns": "features:analyzer.numeric_columns",
    "key suffixes": "features:analyzer.key_suffixes",
    "enrollment columns": "features:analyzer.enrollment_columns",
    "feature labels": "features:analyzer.feature_labels",
    "seed element labels": "features:analyzer.seed_element_labels",
    "member columns": "features:store.member_columns",
    "claim columns": "features:store.claim_columns",
    "member key": "features:store.member_key",
    "report keys": "features:store.report_keys",
    "personal columns": "features:store.personal_columns",
    "personal columns (rows)": "features:store.personal_columns_rows",
    "personal suffixes (rows)": "features:store.personal_suffixes_rows",
    "claims filters": "features:store.claims_filters",
    "clinical columns": "features:store.clinical_columns",
    "claim type names": "features:store.claim_type_names",
    "defined features": "features:patterns.defined",
    "discovery defaults": "features:patterns.discovery_defaults",
    "scope levels": "features:patterns.scope_levels",
    "scenario dimensions": "features:patterns.scenario_dimensions",
    "scenario split": "features:patterns.scenario_split",
    "amount bands": "features:patterns.amount_bands",
    "report fields": "features:patterns.report_fields",
    "provider member columns": "features:joined.provider_member_columns",
    "provider claim columns": "features:joined.provider_claim_columns",
    "provider entity words": "features:joined.provider_entity_words",
    "story stop words": "story:stop_words",
    "story gender": "story:gender",
    "story marital": "story:marital",
    "story claim types": "story:claim_types",
    "story network": "story:network",
    "story age words": "story:age_words",
    "story states": "story:states",
    "story spoken for": "story:spoken_for",
    "keyword aliases": "story:keyword_aliases",
    "naming states": "story:naming_states",
}



@pytest.fixture(scope="module")
def kept() -> dict:
    return json.loads(KEPT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def health() -> packs.Pack:
    return packs.load("health")


def test_every_kept_value_has_a_place_in_the_pack(kept):
    constants = {k for k in kept if not k.startswith("file: ")}
    assert sorted(constants) == sorted(PLACE), "a kept value has no place, or a place no value"


@pytest.mark.parametrize("key", sorted(PLACE))
def test_the_pack_holds_what_the_code_held(kept, health, key):
    assert _plain(health.value(PLACE[key])) == kept[key], key


def test_the_code_reads_the_files_the_pack_holds(health):
    from sdg.analyzer import facets_names
    from sdg.catalog.loader import load_catalog

    assert ROOT / FILES["catalog"] == health.catalog_path
    assert ROOT / FILES["column names"] == health.folder / "column_names.json"
    assert load_catalog() == load_catalog(health.catalog_path)
    facets_names.names.cache_clear()
    assert facets_names.names() == health.column_names()
    assert facets_names.name("PDPD_ID") == health.column_names()["PDPD_ID"]


def test_the_health_pack_says_what_each_record_plays(health):
    assert health.primary == "members"
    assert health.child == "claims"
    assert health.with_role("parent_of_primary") == "subscribers"
    assert health.role("claims") == "child" and health.role("nothing") is None
    assert health.term("claims", "One") == "Claim"
    assert health.term("members") == "members"
    assert health.has_module("x12") and not health.has_module("nothing")
    # The plan name keys the seed hash: frozen.
    assert health.value("pack:plan_name") == "facets-slice"


@pytest.mark.parametrize("key, primary, child", [
    ("members", True, False), ("claims", False, True), ("subscribers", False, False),
    ("Claims", False, False), ("", False, False), (None, False, False),
])
def test_code_asks_the_role_where_it_tested_the_key(key, primary, child):
    # Each `entity == "members"` / `== "claims"` became one of these; for the
    # health pack they answer exactly what the key test did.
    assert packs.is_primary(key) is primary and packs.is_child(key) is child
    assert packs.is_primary(key) == (key == "members")
    assert packs.is_child(key) == (key == "claims")


def test_the_active_pack_is_health_unless_said(monkeypatch):
    monkeypatch.delenv("SDG_PACK", raising=False)
    assert packs.active().id == "health"
    monkeypatch.setenv("SDG_PACK", "nothing")
    with pytest.raises(packs.PackError, match="No business pack 'nothing'"):
        packs.active()


def test_a_pack_that_cannot_be_read_is_refused_plainly(tmp_path, monkeypatch):
    def made(body: dict) -> str:
        folder = tmp_path / body.get("id", "broken")
        folder.mkdir(exist_ok=True)
        (folder / "pack.json").write_text(json.dumps(body), encoding="utf-8")
        packs.load.cache_clear()
        return body.get("id", "broken")

    monkeypatch.setenv("SDG_PACKS_DIR", str(tmp_path))
    good = {"id": "bank", "name": "Banking", "version": 1, "entities": {
        "customers": {"role": "primary", "anchor": "CUSTOMER_ID"},
        "accounts": {"role": "child", "parent": "customers", "anchor": "ACCOUNT_ID"}}}
    loaded = packs.load(made(good))
    assert loaded.primary == "customers"
    # What a plan reads, where the pack does not say it.
    assert loaded.value("pack:plan_name") == "bank-slice"
    assert loaded.value("pack:plan.joins") == {} and loaded.value("pack:plan.root_counts") == {}
    bad_cases = {
        "no primary": {**good, "entities": {"accounts": {"role": "child", "anchor": "A"}}},
        "two primaries": {**good, "entities": {"a": {"role": "primary", "anchor": "A"},
                                               "b": {"role": "primary", "anchor": "B"}}},
        "unknown role": {**good, "entities": {"a": {"role": "boss", "anchor": "A"}}},
        "key too long": {**good, "entities": {"a" * 21: {"role": "primary", "anchor": "A"}}},
        "parent missing": {**good, "entities": {"a": {"role": "primary", "anchor": "A"},
                                                "b": {"role": "child", "parent": "z",
                                                      "anchor": "B"}}},
        "join of a role nobody plays": {**good, "plan": {"joins": {"parent_of_primary": "X"}}},
        "join of an unknown role": {**good, "plan": {"joins": {"boss": "X"}}},
        "root count not a number": {**good, "plan": {"root_counts": {"dbo.T": "many"}}},
        "empty plan name": {**good, "plan_name": ""},
    }
    try:
        for why, body in bad_cases.items():
            with pytest.raises(packs.PackError):
                packs.load(made(body))
            assert why
    finally:
        packs.load.cache_clear()


def test_the_screens_are_told_the_pack():
    body = client.get("/api/pack").json()
    assert body["id"] == "health"
    roles = {e["key"]: e["role"] for e in body["entities"]}
    assert roles == {"members": "primary", "claims": "child", "subscribers": "parent_of_primary"}
    assert body["glossary"]["claims"]["Many"] == "Claims"
    assert "edi:834" in body["formats"]["members"]
    names = client.get("/api/pack/column-names").json()
    assert names == json.loads((ROOT / FILES["column names"]).read_text(encoding="utf-8"))


def test_every_record_has_its_words():
    """The screens name records only through the glossary (ui/src/lib/pack.ts):
    every entity states each form, and the health words are today's."""
    health = packs.load("health")
    for key in health.entities:
        terms = health.glossary[key]
        assert {"one", "many", "One", "Many"} <= set(terms), key
        assert terms["One"] == terms["one"][:1].upper() + terms["one"][1:], key
        assert terms["Many"] == terms["many"][:1].upper() + terms["many"][1:], key
    assert health.glossary["members"]["many"] == "members"
    assert health.glossary["claims"]["lines"] == "claim lines"
    assert health.glossary["claims"]["Line"] == "Claim line"


def test_the_screens_name_types_as_the_analyzer_does():
    """A child record's type names on screen are the analyzer's own."""
    health = packs.load("health")
    assert health.glossary["claims"]["types"] == health.value("features:store.claim_type_names")


def test_every_phrase_and_chart_the_screens_ask_for_is_in_the_pack():
    """phrase("x") on any screen is in the glossary's phrases (a missing one
    stops the screen); the charts draw only what the glossary names."""
    import re

    health = packs.load("health")
    asked, unread = set(), []
    for path in (ROOT / "ui" / "src").rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        for call in re.finditer(r"(?<![\w.])phrase\(([^)]*)\)", text):
            if path.name == "pack.ts" and call.group(1).startswith("key"):
                continue  # its definition
            literal = re.fullmatch(r"""\s*(["'`])([\w-]+)\1\s*""", call.group(1))
            if literal:
                asked.add(literal.group(2))
            else:
                unread.append(f"{path.name}: phrase({call.group(1)})")
    assert not unread, f"a phrase asked for by a name this test cannot read: {unread}"
    assert len(asked) >= 2, f"the scan found too few phrases: {sorted(asked)}"
    phrases = {k for k in health.glossary["phrases"] if not k.startswith("_")}
    assert not asked - phrases, f"phrases the pack lacks: {sorted(asked - phrases)}"
    assert not phrases - asked, f"phrases no screen says: {sorted(phrases - asked)}"
    charts = health.glossary["charts"]
    assert list(charts["ranges"]) == ["_age", "member_age", "_tenure", "charge_amount"]
    assert all({"label", "unit"} <= set(v) for v in charts["ranges"].values())
    assert list(charts["mixes"]) == ["MEME_SEX", "gender", "MEME_MARITAL_STATUS",
                                     "claim_status", "claim_type", "network"]


def test_a_packs_read_back_settings_are_checked_when_it_loads(tmp_path, monkeypatch):
    """How a pack's people are read back (pack.json files.composed_id and
    latest_columns) goes into SQL by name: a malformed one, or one that is not
    a plain name, is refused when the pack loads, not part way through a file."""
    def made(body: dict) -> str:
        folder = tmp_path / body["id"]
        folder.mkdir(exist_ok=True)
        (folder / "pack.json").write_text(json.dumps(body), encoding="utf-8")
        packs.load.cache_clear()
        return body["id"]

    monkeypatch.setenv("SDG_PACKS_DIR", str(tmp_path))
    good = {"id": "bank", "name": "Banking", "version": 1, "entities": {
        "customers": {"role": "primary", "anchor": "CUSTOMER_ID"}}}
    loaded = packs.load(made(good))
    assert loaded.value("pack:files.composed_id") is None
    assert loaded.value("pack:files.latest_columns") == []
    bad = {
        "half an id": {**good, "files": {"composed_id": {"element": "X", "from": "A"}}},
        "a name that is not plain": {**good, "files": {"latest_columns": ["EFF; DROP TABLE x"]}},
        "a part that is not plain": {**good, "files": {"composed_id": {
            "element": "X", "from": "A]", "suffix": "B", "subscriber_suffix": "00"}}},
    }
    try:
        for why, body in bad.items():
            with pytest.raises(packs.PackError):
                packs.load(made({**body, "id": "bad"}))
            assert why
    finally:
        packs.load.cache_clear()

"""A pattern's scope, kept in sdm.patterns: written and read in the pack's words.

Under health every key and column comes out as the code before scope_json
made it (combo keys are what certification and de-duplication match on, so
they may not move); a row from before scope_json is read as it always was.
Under another pack the scope is kept in scope_json alone, and the record key
keeps two record kinds' keys apart.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import sys

from sdg.analyzer import pattern_scope as scope
from sdg.analyzer.services import pattern_service as ps
from sdg.analyzer.services.discovery_service import canonical_combo
from tests.test_pack_constants import ROOT


def test_health_keys_are_made_as_before():
    # As discovery and the profiler made them (db._make_combo_key over
    # canonical_combo), and as user-defined patterns did (pattern_service).
    from sdg.analyzer import db

    member = canonical_combo({"grgr_ck": [10042, 7], "cspd_cat": ["M"]})
    assert db._make_combo_key(member) == "['10042', '7']|['M', '__MEMBERS__']|ALL|ALL"
    claim = canonical_combo({"grgr_ck": [1], "pdpd_id": ["P1"]}, entity="claims")
    assert db._make_combo_key(claim) == "['1']|['__CLAIMS__']|ALL|['P1']"
    assert ps.combo_key({"grgr_ck": ["2", 1], "cspd_cat": ["M"]}) == (
        "['1', '2']|['M', '__MEMBERS__']|ALL|ALL")
    assert ps.combo_key({"cspi_id": ["X"]}, "claims") == "ALL|['__CLAIMS__']|['X']|ALL"


def test_health_columns_are_written_as_before_and_the_scope_beside_them():
    combo = canonical_combo({"grgr_ck": [5], "cspi_id": ["PPO"], "grgr_name": "Acme",
                             "plds_desc": "PPO plan"}, entity="claims")
    cols = scope.columns(combo["_entity"], combo, combo["_labels"], tagged=combo)
    assert cols["grgr_ck"] == '["5"]' and cols["cspd_cat"] == '["__CLAIMS__"]'
    assert cols["cspi_id"] == '["PPO"]' and cols["pdpd_id"] == "null"
    assert (cols["grgr_name"], cols["plds_desc"], cols["cspd_cat_desc"]) == ("Acme", "PPO plan", "")
    assert cols["entity"] == "claims"
    assert json.loads(cols["scope_json"]) == {
        "entity": "claims", "filters": {"grgr_ck": ["5"], "cspi_id": ["PPO"]},
        "labels": {"grgr_ck": "Acme", "cspi_id": "PPO plan"}}


# Rows as three generations of the code left them, and what the code before
# scope_json said each one was (pattern_service.entity_of at f448ad9).
OLD_ROWS = [
    ({"cspd_cat": '["M", "__MEMBERS__"]', "contextual_name": "Ohio adults"}, "members"),
    ({"cspd_cat": '["__CLAIMS__"]', "contextual_name": "Dental"}, "claims"),
    ({"cspd_cat": "null", "contextual_name": "[Claims] Older"}, "claims"),
    ({"cspd_cat": None, "contextual_name": "Copy of [Claims] Older"}, "claims"),
    ({"cspd_cat": None, "name": "[Claims] by name only"}, "claims"),
    ({"cspd_cat": None, "cspd_cat_desc": " Claims "}, "claims"),
    ({"cspd_cat": None, "contextual_name": "Claims in name, not prefix"}, "members"),
    # The entity column defaulted to members on every row before it was set.
    ({"cspd_cat": '["__CLAIMS__"]', "entity": "members"}, "claims"),
]


def test_an_older_row_is_told_apart_as_before():
    for row, was in OLD_ROWS:
        assert scope.entity_of(row) == was, row


def test_the_scope_json_says_the_record_kind_outright():
    row = {"cspd_cat": '["M", "__MEMBERS__"]',
           "scope_json": json.dumps({"entity": "claims", "filters": {}, "labels": {}})}
    assert scope.entity_of(row) == "claims"
    # One the pack does not have is not believed.
    row["scope_json"] = json.dumps({"entity": "customers"})
    assert scope.entity_of(row) == "members"


def test_an_older_rows_scope_is_read_without_the_tag():
    row = {"grgr_ck": '["10042", null]', "cspd_cat": '["M", "__MEMBERS__"]', "cspi_id": "null",
           "pdpd_id": "not json", "grgr_name": "Acme", "cspd_cat_desc": "Medical",
           "plds_desc": None}
    assert scope.filters_of(row) == {"grgr_ck": ["10042"], "cspd_cat": ["M"]}
    assert scope.labels_of(row) == {"grgr_ck": "Acme", "cspd_cat": "Medical"}
    assert scope.named_labels(scope.labels_of(row)) == {
        "group": "Acme", "plan_category": "Medical", "plan": None, "product": None}


def test_the_scope_json_wins_over_the_columns():
    row = {"grgr_ck": '["1"]', "grgr_name": "Old",
           "scope_json": json.dumps({"entity": "members", "filters": {"cspi_id": ["P"]},
                                     "labels": {"cspi_id": "Plan P"}})}
    assert scope.filters_of(row) == {"cspi_id": ["P"]}
    assert scope.labels_of(row) == {"cspi_id": "Plan P"}


def test_names_are_taken_by_level_or_by_what_the_screens_call_it():
    assert scope.labels_from_names({"group": "Acme", "cspi_id": "PPO"}) == {
        "grgr_ck": "Acme", "cspi_id": "PPO"}


def test_a_stored_patterns_filters_and_api_shape_come_from_its_scope():
    from sdg.analyzer.services.story_service import _filters_from_pattern

    row = {"id": 3, "contextual_name": "x", "grgr_ck": '["10042", ""]',
           "cspd_cat": '["__CLAIMS__"]', "grgr_name": "Acme"}
    assert _filters_from_pattern(row) == {"grgr_ck": [10042]}
    shaped = ps._row_to_pattern(row)
    assert shaped["entity"] == "claims"
    # As _row_to_pattern gave them: a blank kept, the tag not.
    assert shaped["features"] == {"grgr_ck": ["10042", ""], "cspd_cat": None,
                                  "cspi_id": None, "pdpd_id": None}
    assert shaped["labels"]["group"] == "Acme"


BANKING = r'''
import json
from sdg.analyzer import db, pattern_scope as scope
from sdg.analyzer.services import pattern_service as ps
from sdg.analyzer.services.discovery_service import canonical_combo

out = {}
c = canonical_combo({"branch_id": [3], "_labels": {"branch_id": "Louisville Main"}},
                    entity="accounts")
out["key"] = db._make_combo_key(c)
out["other"] = db._make_combo_key(canonical_combo({"branch_id": [3]}, entity="customers"))
out["ps_key"] = ps.combo_key({"branch_id": [3]}, "accounts")
out["columns"] = scope.columns("accounts", c, c["_labels"], tagged=c)
row = {"scope_json": out["columns"]["scope_json"]}
out["entity"] = scope.entity_of(row)
out["filters"] = scope.filters_of(row)
out["labels"] = scope.named_labels(scope.labels_of(row))
out["no_scope"] = scope.entity_of({"contextual_name": "[Claims] x"})
print(json.dumps(out))
'''


def test_under_banking_the_scope_is_kept_in_the_banks_words():
    import os

    done = subprocess.run([sys.executable, "-c", BANKING], cwd=ROOT, capture_output=True,
                          text=True, env=dict(os.environ, SDG_PACK="banking"), timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["key"] == "accounts|['3']|ALL"
    assert out["other"] == "customers|['3']|ALL"
    assert out["ps_key"] == "accounts|['3']|ALL"
    assert set(out["columns"]) == {"entity", "scope_json"}
    assert out["entity"] == "accounts"
    assert out["filters"] == {"branch_id": ["3"]}
    assert out["labels"] == {"branch_id": "Louisville Main", "product_code": None}
    # Health's markers mean nothing to a bank: a row without a scope is the
    # primary records'.
    assert out["no_scope"] == "customers"


def test_a_claims_analysis_saved_before_any_tag_is_told_by_its_profile():
    row = {"cspd_cat": None, "contextual_name": "Diabetes visits",
           "profile_json": json.dumps({"claims_pattern": True, "n_claims": 4})}
    assert scope.entity_of(row) == "claims"


def test_a_scope_without_names_leaves_the_names_the_row_holds():
    row = {"grgr_name": "Acme", "scope_json": json.dumps(
        {"entity": "members", "filters": {"grgr_ck": ["1"]}, "labels": {}})}
    assert scope.labels_of(row) == {"grgr_ck": "Acme"}


def test_a_name_a_pack_puts_into_sql_is_a_plain_one():
    from sdg import packs

    assert packs.plain_name("GRGR_CK", "x") == "GRGR_CK"
    for bad in ("GRGR_CK; DROP TABLE x", "a b", "1col", "", None, "x]", "[x]"):
        with pytest.raises(packs.PackError, match="not a plain table or column name"):
            packs.plain_name(bad, "x")


def test_health_keeps_a_pattern_on_a_store_without_scope_json():
    # Its own columns hold the scope: a store not yet upgraded keeps working.
    scope.require_column({"id", "pattern_key", "grgr_ck"})
    scope.require_column(set())  # a shape not read: the insert says itself


NO_COLUMN = r"""
import json
from sdg.analyzer import pattern_scope as scope
out = {}
try:
    scope.require_column({"id", "pattern_key", "entity"})
    out["refused"] = None
except scope.StoreNotUpgraded as e:
    out["refused"] = str(e)
scope.require_column({"id", "scope_json"})
print(json.dumps(out))
"""


def test_a_pack_without_columns_of_its_own_refuses_a_store_without_scope_json():
    import os

    done = subprocess.run([sys.executable, "-c", NO_COLUMN], cwd=ROOT, capture_output=True,
                          text=True, env=dict(os.environ, SDG_PACK="banking"), timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    refused = json.loads(done.stdout.strip().splitlines()[-1])["refused"]
    assert refused and "scope_json" in refused and "01_sdm_upgrade.sql" in refused


def test_the_store_refusal_reaches_the_screens_in_words():
    # A ValueError: the routes answer one with its message, as any refusal.
    assert issubclass(scope.StoreNotUpgraded, ValueError)
    # Health keeps its scope in its own columns: nothing is asked of the store.
    scope.require_store()


def test_a_record_held_twice_with_no_latest_column_is_refused(monkeypatch):
    """Where the pack names no column telling which of a record's rows is the
    latest, two rows for one record would be read by chance: refused."""
    from types import SimpleNamespace

    from sdg.writers import elements

    monkeypatch.setattr(elements, "LATEST_COLUMNS", [])
    monkeypatch.setattr(elements, "_store_columns", lambda shape, table: {"DOB": "MEME_BIRTH_DT"})
    monkeypatch.setattr(elements, "_own_columns", lambda shape, table, prefix: {})
    monkeypatch.setattr("sdg.analyzer.entity_loader.present_columns",
                        lambda table: {"MEME_CK", "MEME_BIRTH_DT"})
    asked = []

    class Registry:
        def query(self, connection, sql, params):
            asked.append(sql)
            return [{"k": 7}] if "HAVING COUNT(*) > 1" in sql else []

    with pytest.raises(ValueError, match="more than one row for MEME_CK 7"):
        elements.store_members([7, 8], SimpleNamespace(elements=[]), Registry())
    assert len(asked) == 1, "refused before any row was read"

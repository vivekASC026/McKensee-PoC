"""The API the web interface talks to.

Read-only endpoints are exercised against the artifacts already on disk. The
endpoints that change something are tested through the service with a working
directory of their own, so a test run never disturbs a demo in progress.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app
from sdg.api.service import DEFAULT_SETTINGS, Service, ServiceError

client = TestClient(app)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """A service with its own working directory."""
    return Service(work_dir=tmp_path_factory.mktemp("sdg-api"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_state_reports_every_step_in_order():
    response = client.get("/api/state")
    assert response.status_code == 200
    body = response.json()
    # Eight steps, ending in the two destinations. The plan is not one of them:
    # it is the shape of a database load and belongs inside the step that
    # performs one, not beside it.
    assert [step["id"] for step in body["steps"]] == [
        "connect", "discover", "map", "patterns", "run",
    ]
    labels = {step["id"]: step["label"] for step in body["steps"]}
    # Reading the structure and measuring it are one screen.
    assert labels["discover"] == "Discover and Profile"
    # Mapping and reviewing are one screen; so is generating, with a tab for
    # a database load and a tab for files.
    assert labels["map"] == "Map and Review"
    assert labels["run"] == "Generation"
    assert body["database"]
    assert "rootCounts" in body["settings"]


@pytest.mark.db
def test_state_names_the_database_worked_on_and_the_pattern_store():
    """Connect names the database chosen there; patterns always come from the
    pattern store, never from whichever database was read first."""
    from sdg.api.app import service

    body = client.get("/api/state").json()
    connect = next(s for s in body["steps"] if s["id"] == "connect")
    assert connect["detail"] == body["database"] == service.working_database()["database"]
    assert body["landscape"] == service.settings.denorm_db


@pytest.mark.db
def test_state_carries_the_headline_number_for_each_step():
    steps = {s["id"]: s for s in client.get("/api/state").json()["steps"]}
    if steps["discover"]["done"]:
        assert steps["discover"]["value"] == 19
        assert "tables" in steps["discover"]["detail"]
    if steps["map"]["done"]:
        assert steps["map"]["value"] >= 60
        assert "auto-mapped" in steps["map"]["detail"]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_schema_folds_four_artifacts_into_one_row_per_column():
    body = client.get("/api/schema").json()
    columns = [c for t in body["tables"] for c in t["columns"]]
    assert len(columns) == 122  # MEME_SFX, the network tables and PCPs among them

    member_id = next(
        c for c in columns
        if c["table"].endswith("CMC_MEME_MEMBER") and c["name"] == "MEME_ID"
    )
    # From the physical model.
    assert member_id["bucket"] == "business_key"
    assert member_id["type"].startswith("char")
    # From the profile.
    assert member_id["profile"]["format"]["regex"] == r"[A-Z]{3}\d{8}"
    # From the mapping.
    assert member_id["element"] == "MEMBER_ID"
    assert member_id["mappingEvidence"]
    # From the proposer.
    assert member_id["generator"] == "pattern"
    assert member_id["provenance"]


@pytest.mark.db
def test_schema_reports_the_inferred_key_and_the_trigger():
    body = client.get("/api/schema").json()
    claims = next(t for t in body["tables"] if t["name"].endswith("CMC_CLCL_CLAIM"))
    inferred = [fk for fk in claims["foreignKeys"] if fk["kind"] == "inferred"]
    # MEME_CK is the planted undeclared key. PDPD_ID is inferred as well,
    # because the product description table holds exactly its identifiers.
    assert ["MEME_CK"] in [fk["columns"] for fk in inferred]
    assert claims["triggers"] == ["TR_CMC_CLCL_CLAIM_AUDIT"]


@pytest.mark.db
def test_schema_carries_the_measured_relationships():
    body = client.get("/api/schema").json()
    dependencies = {(d["determinant"], d["dependent"]) for d in body["dependencies"]}
    assert ("MEME_ZIP", "MEME_CITY") in dependencies

    claims = next(
        r for r in body["ratios"]
        if r["child_table"].endswith("CLCL_CLAIM") and r["parent_table"].endswith("MEME_MEMBER")
    )
    assert claims["zero_share"] == pytest.approx(0.30, abs=0.02)
    assert claims["histogram"]


# ---------------------------------------------------------------------------
# Queue and catalog
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_queue_items_carry_their_question_and_its_answer():
    body = client.get("/api/queue").json()
    assert len(body["items"]) < 40
    for item in body["items"]:
        assert item["explanation"]
        assert item["reasons"]
        assert "qualified" in item


@pytest.mark.db
def test_catalog_reports_which_columns_use_each_element():
    body = client.get("/api/catalog").json()
    elements = {e["name"]: e for e in body["elements"]}
    assert elements["MEMBER_ID"]["usedBy"]
    # The catalog holds elements this application does not use.
    # Nothing in this slice holds a legal entity name, so the element is
    # carried by the catalog and used by no column.
    assert elements["LEGAL_ENTITY_NAME"]["usedBy"] == []
    assert elements["PLAN_CODE"]["rule"] == "reuse"


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_plan_reports_counts_provenance_and_warnings():
    body = client.get("/api/plan").json()
    if not body.get("built"):
        pytest.skip("no plan has been built in this working directory")
    assert body["totalRows"] > 0
    assert set(body["provenance"]) <= {
        "structural", "catalog", "learned", "user", "physical"
    }
    generated = [t for t in body["tables"] if t["mode"] == "generate"]
    for table in generated:
        assert table["effectiveCount"] is not None
        if table["drivingParent"]:
            # A reloaded plan has no graph, so the mean has to come from the
            # ratio itself; the cascade reads it either way.
            assert table["ratioMean"] is not None, table["name"]


# ---------------------------------------------------------------------------
# Settings and decisions, in a working directory of their own
# ---------------------------------------------------------------------------

def test_settings_start_from_the_defaults(sandbox):
    settings = sandbox.read_settings()
    assert settings["rootCounts"] == DEFAULT_SETTINGS["rootCounts"]
    assert settings["seed"]


def test_settings_merge_rather_than_replace(sandbox):
    sandbox.write_settings({"seed": "a-different-seed"})
    settings = sandbox.read_settings()
    assert settings["seed"] == "a-different-seed"
    # Everything not named is left alone.
    assert settings["modes"] == DEFAULT_SETTINGS["modes"]


def test_a_decision_is_recorded_and_can_be_changed(sandbox):
    sandbox.record_decision({
        "table": "dbo.T", "column": "C", "approved": True, "element": "MEMBER_ID",
    })
    document = sandbox.read_decisions()
    entry = next(c for c in document["columns"] if c["column"] == "C")
    assert entry["approved"] is True
    assert entry["element"] == "MEMBER_ID"

    # Reopening it updates the same row rather than adding another.
    sandbox.record_decision({"table": "dbo.T", "column": "C", "approved": False})
    document = sandbox.read_decisions()
    matching = [c for c in document["columns"] if c["column"] == "C"]
    assert len(matching) == 1
    assert matching[0]["approved"] is False
    assert matching[0]["element"] == "MEMBER_ID"


def test_decisions_are_written_where_the_command_line_reads_them(sandbox):
    """One file, so a run can be started in a browser and finished at a prompt."""
    from sdg.review.queue import load_decisions

    sandbox.record_decision({"table": "dbo.T", "column": "D", "approved": True})
    on_disk = load_decisions(sandbox.path("decisions"))
    assert any(c["column"] == "D" for c in on_disk["columns"])
    assert sandbox.path("decisions").name == "review-decisions.json"


# ---------------------------------------------------------------------------
# Editing a rule the tool worked out
# ---------------------------------------------------------------------------

def test_every_generator_is_offered_with_the_fields_it_needs():
    """The interface builds its form from this, so it has to be complete."""
    body = client.get("/api/generators").json()
    by_name = {g["name"]: g for g in body["generators"]}
    assert set(by_name) >= {"pattern", "lookup", "number", "date_relative", "skip"}
    assert [f["name"] for f in by_name["pattern"]["fields"]] == ["regex", "unique"]
    assert by_name["pattern"]["fields"][0]["required"] is True
    assert body["structuralWarning"]


def test_any_column_can_be_edited_not_only_the_ones_in_the_queue(sandbox):
    """The queue is where the tool was unsure. Being sure is not being right."""
    sandbox.record_decision({
        "table": "dbo.CMC_MEME_MEMBER", "column": "MEME_ID", "approved": True,
        "element": "MEMBER_ID", "generator": "pattern",
        "params": {"regex": r"[A-Z]{4}\d{9}", "unique": True},
    })
    entry = next(
        c for c in sandbox.read_decisions()["columns"] if c["column"] == "MEME_ID"
    )
    assert entry["generator"] == "pattern"
    assert entry["params"]["regex"] == r"[A-Z]{4}\d{9}"


def test_a_business_element_brings_its_rule_and_custom_brings_its_own(sandbox):
    """Map and Review: a column is given an element, and the element's rule is
    used; only a Custom column has its rule chosen by hand."""
    from sdg.catalog.loader import load_catalog
    from sdg.discovery.model import Column, Table
    from sdg.plan.proposer import Proposal
    from sdg.plan.resolver import resolve_column

    key = {"table": "dbo.T", "column": "REL"}
    # Custom first: a rule chosen by hand.
    sandbox.record_decision({**key, "approved": True, "element": None,
                             "generator": "constant", "params": {"value": "X"}})
    # Then an element: what the screen sends clears the hand-made rule.
    sandbox.record_decision({**key, "approved": True, "element": "RELATIONSHIP_CODE",
                             "generator": None, "params": None})
    decision = next(c for c in sandbox.read_decisions()["columns"] if c["column"] == "REL")
    assert decision["generator"] is None and decision["element"] == "RELATIONSHIP_CODE"

    catalog = load_catalog()
    table = Table(name="dbo.T", schema_name="dbo", table_name="T",
                  columns=[Column(name="REL", ordinal=1, type_name="char", max_length=1)])
    learned = Proposal(table="dbo.T", column="REL", bucket="free", generator="pattern",
                       params={"regex": "[A-Z]"})
    field = resolve_column(table, table.columns[0], learned, decision["element"], catalog,
                           {}, decision)
    element = catalog.element("RELATIONSHIP_CODE")
    assert field.element == "RELATIONSHIP_CODE"
    assert (field.generator, field.params) == (element.generator, element.params)


def test_an_edit_the_engine_could_not_run_is_refused_before_it_is_stored(sandbox):
    with pytest.raises(ServiceError, match="cannot be used"):
        sandbox.record_decision({
            "table": "dbo.T", "column": "BAD", "approved": True,
            "generator": "pattern", "params": {"regex": "[Z-A]"},
        })
    assert not any(c["column"] == "BAD" for c in sandbox.read_decisions()["columns"])


def test_the_refusal_reaches_the_browser_as_a_sentence(tmp_path, monkeypatch):
    import sdg.api.app as api

    monkeypatch.setattr(api, "service", Service(work_dir=tmp_path))
    response = TestClient(api.app).post("/api/decisions", json={
        "table": "dbo.T", "column": "C", "approved": True,
        "generator": "lookup", "params": {"values": ["A", "B"], "weights": [1]},
    })
    assert response.status_code == 409
    assert "2 values but 1 weights" in response.json()["detail"]


def test_what_a_form_sends_is_stored_as_what_the_engine_reads(sandbox):
    """A form hands back strings; a plan has to carry numbers."""
    sandbox.record_decision({
        "table": "dbo.T", "column": "AMOUNT", "approved": True,
        "generator": "number",
        "params": {"distribution": "uniform", "min": "0", "max": "500", "decimals": "2"},
    })
    entry = next(
        c for c in sandbox.read_decisions()["columns"] if c["column"] == "AMOUNT"
    )
    assert entry["params"] == {
        "distribution": "uniform", "min": 0.0, "max": 500.0, "decimals": 2,
    }


def test_an_override_can_be_dropped_to_get_the_tools_rule_back(sandbox):
    sandbox.record_decision({
        "table": "dbo.T", "column": "UNDO", "approved": True,
        "generator": "constant", "params": {"value": "X"},
    })
    assert any(c["column"] == "UNDO" for c in sandbox.read_decisions()["columns"])

    result = sandbox.record_decision({"table": "dbo.T", "column": "UNDO", "reset": True})
    assert result["reset"] is True
    assert not any(c["column"] == "UNDO" for c in sandbox.read_decisions()["columns"])


def test_dropping_an_override_that_was_never_made_is_harmless(sandbox):
    sandbox.record_decision({"table": "dbo.T", "column": "NEVER", "reset": True})
    assert not any(c["column"] == "NEVER" for c in sandbox.read_decisions()["columns"])


@pytest.mark.db
def test_the_schema_carries_back_what_a_person_changed():
    """The interface marks an edited column, so it has to be told which ones."""
    body = client.get("/api/schema").json()
    columns = [c for t in body["tables"] for c in t["columns"]]
    assert all("decision" in c for c in columns)


# ---------------------------------------------------------------------------
# Changing how many children each parent gets
# ---------------------------------------------------------------------------

def test_the_ratio_shapes_are_offered_with_their_fields():
    body = client.get("/api/ratio-shapes").json()
    by_name = {s["name"]: s for s in body["shapes"]}
    assert set(by_name) == {"measured", "fixed", "range"}
    assert [f["name"] for f in by_name["fixed"]["fields"]] == ["value", "zero_share"]
    assert [f["name"] for f in by_name["range"]["fields"]] == ["min", "max", "zero_share"]


def test_a_stated_ratio_is_stored_as_numbers(sandbox):
    saved = sandbox.write_settings({
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "fixed", "value": "5", "zero_share": "0.3"}},
    })
    assert saved["ratios"] == {
        "dbo.CMC_CLCL_CLAIM": {"kind": "fixed", "value": 5.0, "zero_share": 0.3},
    }


def test_going_back_to_the_measured_shape_drops_the_setting(sandbox):
    sandbox.write_settings({
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "fixed", "value": "5"}},
    })
    saved = sandbox.write_settings({"ratios": {"dbo.CMC_CLCL_CLAIM": None}})
    assert saved["ratios"] == {}

    saved = sandbox.write_settings({
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "measured"}},
    })
    assert saved["ratios"] == {}


def test_a_ratio_set_in_the_browser_is_read_at_the_prompt(sandbox):
    """One file, so a run can be started in a browser and finished at a prompt."""
    from sdg.plan.build import BuildInputs, table_ratios
    from sdg.review.queue import load_decisions

    sandbox.write_settings({
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "fixed", "value": "5", "zero_share": "0.3"}},
    })
    on_disk = load_decisions(sandbox.path("decisions"))
    assert on_disk["tables"]["dbo.CMC_CLCL_CLAIM"]["ratio"]["value"] == 5.0

    # The planner reads it from there, which is what the command line passes in.
    inputs = BuildInputs(
        model=None, catalog=None, mappings=None, proposals=[], decisions=on_disk,
    )
    assert table_ratios(inputs)["dbo.CMC_CLCL_CLAIM"]["zero_share"] == 0.3


def test_a_ratio_set_at_the_prompt_is_shown_in_the_browser(sandbox):
    sandbox.write_settings({
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "range", "min": "1", "max": "9"}},
    })
    assert sandbox.read_settings()["ratios"] == {
        "dbo.CMC_CLCL_CLAIM": {"kind": "range", "min": 1, "max": 9},
    }


def test_going_back_to_measured_leaves_the_tables_mode_alone(sandbox):
    """Modes and ratios share a home; clearing one must not clear the other."""
    document = sandbox.read_decisions()
    document.setdefault("tables", {})["dbo.CMC_PRPR_PROV"] = {"mode": "reuse"}
    from sdg.review.queue import save_decisions

    save_decisions(document, sandbox.path("decisions"))

    sandbox.write_settings({
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "fixed", "value": "5"}},
    })
    sandbox.write_settings({"ratios": {}})

    tables = sandbox.read_decisions()["tables"]
    assert tables["dbo.CMC_PRPR_PROV"] == {"mode": "reuse"}
    assert "dbo.CMC_CLCL_CLAIM" not in tables


def test_a_mode_set_in_the_browser_is_read_at_the_prompt(sandbox):
    """Without this, a table set to reuse reverts to generate at a prompt."""
    from sdg.plan.build import BuildInputs, table_modes
    from sdg.review.queue import load_decisions

    sandbox.write_settings({"modes": {"dbo.CMC_PRPR_PROV": "reuse"}})
    on_disk = load_decisions(sandbox.path("decisions"))
    assert on_disk["tables"]["dbo.CMC_PRPR_PROV"]["mode"] == "reuse"

    class _Table:
        def __init__(self, name):
            self.name = name

    # The command line passes no mode flags; the decision still applies.
    inputs = BuildInputs(
        model=type("M", (), {"tables": [_Table("dbo.CMC_PRPR_PROV")]})(),
        catalog=None, mappings=None, proposals=[], decisions=on_disk, modes={},
    )
    assert table_modes(inputs)["dbo.CMC_PRPR_PROV"] == "reuse"


def test_a_mode_typed_at_the_prompt_beats_the_stored_one(sandbox):
    """A flag is what this run asked for, so it wins over an earlier decision."""
    from sdg.plan.build import BuildInputs, table_modes

    class _Table:
        def __init__(self, name):
            self.name = name

    decisions = {"tables": {"dbo.CMC_PRPR_PROV": {"mode": "exclude"}}}
    inputs = BuildInputs(
        model=type("M", (), {"tables": [_Table("dbo.CMC_PRPR_PROV")]})(),
        catalog=None, mappings=None, proposals=[], decisions=decisions,
        modes={"dbo.CMC_PRPR_PROV": "reuse"},
    )
    assert table_modes(inputs)["dbo.CMC_PRPR_PROV"] == "reuse"


def test_the_shipped_modes_show_through_until_someone_changes_one(sandbox):
    assert sandbox.read_settings()["modes"]["dbo.CMC_CLCL_AUDIT"] == "exclude"


def test_changing_a_mode_stores_the_whole_effective_map(sandbox):
    """So a plan built at a prompt needs no mode flags at all."""
    sandbox.write_settings({"modes": {"dbo.CMC_PDPD_PRODUCT": "generate"}})
    stored = sandbox.read_decisions()["tables"]
    assert stored["dbo.CMC_PDPD_PRODUCT"]["mode"] == "generate"
    # The defaults nobody touched are written down too, not left implied.
    assert stored["dbo.CMC_CLCL_AUDIT"]["mode"] == "exclude"
    assert stored["dbo.CMC_GRGR_GROUP"]["mode"] == "reuse"


def test_providers_are_never_generated(sandbox):
    """Providers are real: a plan may hold Generate for them, so their shape can
    be modeled and a driving parent chosen, but no run that makes rows does it
    (user decision 2026-11-10, option 1)."""
    from sdg.engine.execute import NeverGenerated, refuse_never_generated
    from sdg.plan.build import BuildInputs, table_modes

    class _Table:
        name = "dbo.CMC_PRPR_PROV"

    model = type("M", (), {"tables": [_Table()]})()
    unasked = BuildInputs(model=model, catalog=None, mappings=None, proposals=[])
    assert table_modes(unasked)["dbo.CMC_PRPR_PROV"] == "reuse", "reuse unless asked"
    asked = BuildInputs(model=model, catalog=None, mappings=None, proposals=[],
                        modes={"dbo.CMC_PRPR_PROV": "generate"})
    assert table_modes(asked)["dbo.CMC_PRPR_PROV"] == "generate"

    # The setting is kept (e2e test 08b builds a plan from it and switches the
    # claim's driving parent) ...
    before = sandbox.read_settings()["modes"]
    try:
        sandbox.write_settings({"modes": {**before, "dbo.CMC_PRPR_PROV": "generate"}})
        assert sandbox.read_settings()["modes"]["dbo.CMC_PRPR_PROV"] == "generate"
    finally:
        sandbox.write_settings({"modes": {
            **before, "dbo.CMC_PRPR_PROV": before.get("dbo.CMC_PRPR_PROV", "reuse")}})

    # ... and every run that makes rows refuses a plan that holds it.
    plan = type("P", (), {"tables": [type("T", (), {"name": "dbo.CMC_PRPR_PROV",
                                                    "mode": "generate"})()]})()
    with pytest.raises(NeverGenerated, match="never generated.*Nothing was generated"):
        refuse_never_generated(plan)


def test_a_mode_that_does_not_exist_is_refused(sandbox):
    before = sandbox.read_settings()["modes"]
    with pytest.raises(ServiceError, match="no mode called recycle"):
        sandbox.write_settings({"modes": {"dbo.CMC_PRPR_PROV": "recycle"}})
    assert sandbox.read_settings()["modes"] == before


def test_modes_and_ratios_share_a_home_without_clobbering_each_other(sandbox):
    sandbox.write_settings({
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "fixed", "value": "5"}},
    })
    sandbox.write_settings({"modes": {"dbo.CMC_PRPR_PROV": "reuse"}})
    settings = sandbox.read_settings()
    assert settings["ratios"]["dbo.CMC_CLCL_CLAIM"]["value"] == 5.0
    assert settings["modes"]["dbo.CMC_PRPR_PROV"] == "reuse"

    # Clearing the ratios must leave every mode standing.
    sandbox.write_settings({"ratios": {}})
    settings = sandbox.read_settings()
    assert settings["ratios"] == {}
    assert settings["modes"]["dbo.CMC_PRPR_PROV"] == "reuse"


def test_one_bad_ratio_stops_the_whole_write(sandbox):
    """Storing the good ones and refusing the rest would be a half-saved form."""
    before = sandbox.read_settings().get("ratios")
    with pytest.raises(ServiceError, match="The fewest is above the most"):
        sandbox.write_settings({
            "ratios": {
                "dbo.A": {"kind": "fixed", "value": "3"},
                "dbo.B": {"kind": "range", "min": "9", "max": "2"},
            },
        })
    assert sandbox.read_settings().get("ratios") == before


def test_a_refused_ratio_names_the_table_it_came_from(tmp_path, monkeypatch):
    import sdg.api.app as api

    monkeypatch.setattr(api, "service", Service(work_dir=tmp_path))
    response = TestClient(api.app).put("/api/settings", json={
        "ratios": {"dbo.CMC_CLCL_CLAIM": {"kind": "fixed", "value": "5", "zero_share": "2"}},
    })
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "dbo.CMC_CLCL_CLAIM" in detail and "between 0 and 1" in detail


def test_a_word_where_a_count_belongs_is_refused_not_crashed(tmp_path, monkeypatch):
    """This reaches the browser as a sentence, not a stack trace."""
    import sdg.api.app as api

    monkeypatch.setattr(api, "service", Service(work_dir=tmp_path))
    response = TestClient(api.app).put("/api/settings", json={
        "ratios": {"dbo.T": {"kind": "fixed", "value": "lots"}},
    })
    assert response.status_code == 409
    assert response.json()["detail"] == "Children per parent needs a number. 'lots' is not."


def test_an_unreadable_generator_parameter_is_refused_not_crashed(tmp_path, monkeypatch):
    import sdg.api.app as api

    monkeypatch.setattr(api, "service", Service(work_dir=tmp_path))
    response = TestClient(api.app).post("/api/decisions", json={
        "table": "dbo.T", "column": "C", "approved": True,
        "generator": "number", "params": {"distribution": "uniform", "decimals": "two"},
    })
    assert response.status_code == 409
    assert response.json()["detail"] == "Decimal places needs a whole number. 'two' is not."


@pytest.mark.db
def test_the_plan_says_which_ratios_were_stated_rather_than_measured():
    body = client.get("/api/plan").json()
    if not body.get("built"):
        pytest.skip("no plan on disk")
    for table in body["tables"]:
        assert "ratioOverride" in table
        if table["ratio"]:
            assert table["ratioOverride"] == (
                table["ratio"].get("provenance") == "user"
            )


def test_asking_for_a_plan_before_discovery_says_what_to_do(sandbox):
    with pytest.raises(ServiceError, match="Read the database first"):
        sandbox.require_model()
    with pytest.raises(ServiceError, match="Build the plan first"):
        sandbox.require_plan()


# ---------------------------------------------------------------------------
# Errors reach the browser as sentences
# ---------------------------------------------------------------------------

def test_a_missing_prerequisite_is_a_409_with_a_readable_message(tmp_path, monkeypatch):
    import sdg.api.app as api

    monkeypatch.setattr(api, "service", Service(work_dir=tmp_path))
    response = TestClient(api.app).get("/api/schema")
    assert response.status_code == 409
    assert "Read the database first" in response.json()["detail"]


def test_generating_without_a_plan_is_refused(tmp_path, monkeypatch):
    import sdg.api.app as api

    monkeypatch.setattr(api, "service", Service(work_dir=tmp_path))
    response = TestClient(api.app).post("/api/generate")
    assert response.status_code == 409
    assert "plan" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# The built interface is served
# ---------------------------------------------------------------------------

def test_the_interface_is_built_and_served():
    from sdg.api.app import UI_DIST

    assert UI_DIST.exists(), "run npm run build in ui/"
    response = client.get("/")
    assert response.status_code == 200
    assert "<div id=\"root\">" in response.text


def test_unknown_paths_fall_through_to_the_interface():
    """The interface owns its own routing; the server should not guess."""
    response = client.get("/review")
    assert response.status_code == 200
    assert "<div id=\"root\">" in response.text


def test_the_api_contract_is_documented():
    response = client.get("/api/docs")
    assert response.status_code == 200


def test_every_endpoint_the_interface_calls_exists():
    """The client names these paths; the server has to have them."""
    from sdg.config import REPO_ROOT

    source = (REPO_ROOT / "ui" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    called = set(json.loads(json.dumps(
        [line.split('"/api')[1].split('"')[0].split("?")[0].split("`")[0]
         for line in source.splitlines() if '"/api' in line]
    )))
    # Read the published schema rather than walking app.routes: once routers
    # are mounted with include_router, the route list holds router objects as
    # well as routes, and the paths inside them are not reachable by attribute.
    paths = set(app.openapi()["paths"])
    for path in called:
        assert f"/api{path}" in paths, f"the interface calls /api{path}, which does not exist"


def test_the_pattern_store_is_never_a_target(sandbox):
    """Generated rows never sit beside the measurements the analyzer reads."""
    store = sandbox.settings.denorm_db
    with pytest.raises(ServiceError, match="is the pattern store"):
        sandbox.set_target(store)
    with pytest.raises(ServiceError, match="is the pattern store"):
        sandbox.save_target_connection({"database": store, "trusted": True})

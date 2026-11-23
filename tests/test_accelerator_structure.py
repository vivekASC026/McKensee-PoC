"""Denorm structures and privacy: proposed from the recorded fixtures.

Fast and read from tests/fixtures/accelerator/ only: no database. Names no
source column of the health demo: columns are found by what the graph says
of them (the catalog element they map to, what they serve).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdg import packs
from sdg.accelerator import criteria, plan, structure
from sdg.accelerator.hierarchy import BIRTH_ELEMENT
from sdg.accelerator.landscape import Landscape
from sdg.discovery.model import PhysicalModel
from sdg.profiling.model import ProfileReport

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
SOURCES = ["facets_qa", "bank"]
# Base catalog elements that say who a person is.
IDENTIFYING = {"FIRST_NAME", "LAST_NAME", "MIDDLE_NAME", "FULL_NAME", "NATIONAL_ID", "EMAIL",
               "PHONE", "ADDRESS_STREET", "USER_LOGIN"}

_LANDSCAPES: dict[str, Landscape] = {}
_PROPOSED: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _no_store(monkeypatch):
    """Every edit tells the store (approval.changed), which would reach the
    real one: here an edit's approval is tested by test_accelerator_approval."""
    from sdg.accelerator import approval

    monkeypatch.setattr(approval, "changed", lambda source, new_hash: None)


def _landscape(source: str) -> Landscape:
    if source not in _LANDSCAPES:
        _LANDSCAPES[source] = Landscape(
            name=source.upper(), model=PhysicalModel.load(FIXTURES / f"{source}-model.json"),
            profile=ProfileReport.load(FIXTURES / f"{source}-profile.json"), summary={})
    return _LANDSCAPES[source]


def _proposed(source: str) -> plan.SourcePlan:
    if source not in _PROPOSED:
        _PROPOSED[source] = plan.propose(_landscape(source)).to_dict()
    return plan.SourcePlan.from_dict(json.loads(json.dumps(_PROPOSED[source])))


def _graph(source: str, held: plan.SourcePlan):
    return plan.graph_for(_landscape(source), held)


def _table(held: plan.SourcePlan, role: str) -> structure.DenormTable:
    return next(t for t in held.structure if t.role == role)


def _column(held, table: str, column: str) -> structure.DenormColumn:
    found = structure.find(held.structure, table, column)
    assert found, f"{table}.{column} is in no denorm table"
    return found[0]


# --- shape ------------------------------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_one_table_per_record_kind_at_its_grain(source):
    held = _proposed(source)
    graph = _graph(source, held)
    assert [t.role for t in held.structure] == ["primary", "child"]
    for denorm in held.structure:
        kind = held.record_kinds[denorm.role]
        assert denorm.grain == ("lines" if kind["lines_table"] else "record")
        packs.plain_name(denorm.name, "test")
        assert denorm.name.endswith("_denorm")
        names = [c.name.upper() for c in denorm.columns]
        assert len(names) == len(set(names)), "a column name is used twice"
        for column in denorm.columns:
            packs.plain_name(column.name, "test")
            assert column.serves in structure.SERVES
            assert column.privacy in structure.PRIVACY and column.proposed in structure.PRIVACY
            source_type = graph.table(column.source_table).column(column.source_column)
            assert column.sql_type == (structure.SURROGATE_TYPE if column.privacy == "surrogate"
                                       else source_type.type_description())
    # The child is at the lines grain on both demo sources: its own key, the
    # primary's key it joins on, and the lines' own key.
    child = _table(held, "child")
    assert child.grain == "lines"
    serves = [c.serves for c in child.columns]
    assert serves[:3] == ["key", "key", "lines_key"]
    joins = [c for c in child.columns if c.joins]
    primary_joins = [c for c in _table(held, "primary").columns if c.joins]
    assert len(joins) == len(primary_joins) == 1
    # Each selected level gives its key; each selected feature its column or input.
    for role in ("primary", "child"):
        denorm = _table(held, role)
        held_columns = {(c.source_table, c.source_column) for c in denorm.columns}
        for level in held.levels(role):
            if level.selected:
                assert (level.table, level.column) in held_columns
                if level.label_column:
                    assert (level.label_table, level.label_column) in held_columns
        record = held.record_kinds[role]["table"]
        for feature in held.features(role):
            if feature.selected:
                column = (feature.derive or {}).get("from") or feature.column
                assert (record, column) in held_columns


def test_the_table_is_named_from_the_many_name():
    held = _proposed("bank")
    graph = _graph("bank", held)
    held = plan.name_record(held, "primary", "client", "Clients & Partners", graph=graph)
    assert _table(held, "primary").name == "clients_partners_denorm"
    held = plan.name_record(held, "child", "holding", "2026 holdings", graph=graph)
    assert _table(held, "child").name == "t_2026_holdings_denorm"
    # The same many name for both: the child's is told apart.
    held = plan.name_record(held, "child", "client", "Clients & Partners", graph=graph)
    assert {t.name for t in held.structure} == {"clients_partners_denorm",
                                                "clients_partners_child_denorm"}


# --- privacy ----------------------------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_no_personal_column_is_kept_but_by_the_approved_decision(source):
    held = _proposed(source)
    graph = _graph(source, held)
    for denorm in held.structure:
        for column in denorm.columns:
            element = graph.element_of(column.source_table, column.source_column)
            if not graph.is_personal(column.source_table, column.source_column):
                continue
            if column.privacy == "kept":
                assert (element == BIRTH_ELEMENT and column.serves == "derive_input") or \
                    element in structure.PLACE_ELEMENTS, f"{column.source_column} is kept"
                assert "decision" in column.reason


def test_a_birth_date_is_kept_only_as_the_age_input():
    held = _proposed("bank")
    graph = _graph("bank", held)
    record = held.record_kinds["primary"]["table"]
    age = next(f for f in held.features("primary") if (f.derive or {}).get("unit") == "years")
    birth = age.derive["from"]
    assert graph.element_of(record, birth) == BIRTH_ELEMENT
    column = _column(held, record, birth)
    assert (column.serves, column.privacy, column.locked) == ("derive_input", "kept", False)
    assert "age" in column.reason
    # Without the age feature, the birth date leaves the table; added by hand
    # it is withheld and locked.
    held = plan.select_feature(held, "primary", age.id, False, graph=graph)
    assert not structure.find(held.structure, record, birth)
    held = plan.add_extra_column(held, "primary", record, birth, graph=graph)
    column = _column(held, record, birth)
    assert (column.serves, column.privacy, column.locked) == ("extra", "withheld", True)
    with pytest.raises(ValueError, match="locked"):
        plan.set_privacy(held, record, birth, "kept", graph=graph)


@pytest.mark.parametrize("source", SOURCES)
def test_an_identifier_is_withheld_locked_and_can_never_be_kept(source):
    held = _proposed(source)
    graph = _graph(source, held)
    record = held.record_kinds["primary"]["table"]
    named = [c.name for c in graph.table(record).columns
             if graph.element_of(record, c.name) in IDENTIFYING]
    assert named, "the record table holds no identifying column"
    for name in named:
        held = plan.add_extra_column(held, "primary", record, name, graph=graph)
        column = _column(held, record, name)
        assert (column.privacy, column.proposed, column.locked) == ("withheld", "withheld", True)
        with pytest.raises(ValueError, match="locked"):
            plan.set_privacy(held, record, name, "kept", graph=graph)
    # Still locked after the structure is proposed again by another edit.
    held = plan.name_record(held, "primary", "person", "people", graph=graph)
    held = plan.realign(held, graph)
    for name in named:
        column = _column(held, record, name)
        assert column.privacy == "withheld" and column.locked
    # A hand edit of the saved plan cannot carry a kept choice past the lock.
    for denorm in held.structure:
        for column in denorm.columns:
            if column.locked:
                column.privacy = "kept"
    held = plan.realign(held, graph)
    assert all(c.privacy != "kept" for t in held.structure for c in t.columns if c.locked)


def test_the_join_key_is_a_surrogate_when_it_is_personal(monkeypatch):
    held = _proposed("bank")
    graph = _graph("bank", held)
    (primary_key,) = [c for c in _table(held, "primary").columns if c.joins]
    (child_key,) = [c for c in _table(held, "child").columns if c.joins]
    # Proposed a surrogate already, by its name (CUSTOMER_ID), but not locked.
    assert primary_key.privacy == child_key.privacy == "surrogate"
    assert not primary_key.locked and not child_key.locked
    personal = {(primary_key.source_table, primary_key.source_column),
                (child_key.source_table, child_key.source_column)}
    mapped = graph.element_of
    monkeypatch.setattr(graph, "element_of", lambda t, c: (
        "NATIONAL_ID" if (t, c) in personal else mapped(t, c)))
    held = plan.realign(held, graph)
    for column in (c for t in held.structure for c in t.columns if c.joins):
        assert (column.privacy, column.sql_type, column.locked) == (
            "surrogate", structure.SURROGATE_TYPE, True)
        assert "join" in column.reason
        for refused in ("kept", "withheld"):
            with pytest.raises(ValueError):
                plan.set_privacy(held, column.source_table, column.source_column, refused,
                                 graph=graph)


@pytest.mark.parametrize("side", [0, 1])
def test_one_personal_side_makes_both_sides_of_the_join_surrogates(monkeypatch, side):
    held = _proposed("bank")
    graph = _graph("bank", held)
    (pair,) = structure.join_pairs(held, graph)
    mapped = graph.element_of
    monkeypatch.setattr(graph, "element_of", lambda t, c: (
        "NATIONAL_ID" if (t, c) == pair[side] else mapped(t, c)))
    held = plan.realign(held, graph)
    for table, column in pair:
        found = _column(held, table, column)
        assert (found.privacy, found.proposed, found.locked, found.sql_type) == (
            "surrogate", "surrogate", True, structure.SURROGATE_TYPE)
    with pytest.raises(ValueError, match="locked"):
        plan.set_privacy(held, *pair[1 - side], "kept", graph=graph)


def test_a_surrogate_chosen_on_one_side_of_the_join_is_set_on_both():
    held = _proposed("bank")
    graph = _graph("bank", held)
    (pair,) = structure.join_pairs(held, graph)
    # Proposed surrogates by their name: kept on both sides once confirmed.
    held = plan.set_privacy(held, *pair[0], "kept", graph=graph, confirm_not_personal=True)
    assert {_column(held, *side).privacy for side in pair} == {"kept"}
    held = plan.set_privacy(held, *pair[1], "surrogate", graph=graph)
    assert {_column(held, *side).privacy for side in pair} == {"surrogate"}
    held = plan.name_record(held, "child", "holding", "holdings", graph=graph)
    assert {_column(held, *side).privacy for side in pair} == {"surrogate"}
    held = plan.set_privacy(held, *pair[0], "kept", graph=graph, confirm_not_personal=True)
    assert {_column(held, *side).privacy for side in pair} == {"kept"}
    # A hand edit that splits the pair is mended: surrogate wins.
    _column(held, *pair[0]).privacy = "surrogate"
    held = plan.realign(held, graph)
    assert {_column(held, *side).privacy for side in pair} == {"surrogate"}


def test_a_key_or_a_derived_features_input_is_never_withheld():
    held = _proposed("bank")
    graph = _graph("bank", held)
    for serves in ("key", "lines_key", "derive_input"):
        column = next(c for t in held.structure for c in t.columns
                      if c.serves == serves and not c.joins)
        with pytest.raises(ValueError, match="key|deselect the feature"):
            plan.set_privacy(held, column.source_table, column.source_column, "withheld",
                             graph=graph)


def test_without_a_child_nothing_joins():
    held = _proposed("bank")
    graph = _graph("bank", held)
    held.record_kinds["child"] = None
    held.hierarchies["child"] = {"levels": [], "features": []}
    held = plan._touched(held, graph)
    assert [t.role for t in held.structure] == ["primary"]
    assert not any(c.joins for c in held.structure[0].columns)
    assert structure.join_pairs(held, graph) == []


def test_the_persons_privacy_choice_survives_every_edit():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    feature = next(c for c in _table(held, "primary").columns if c.serves == "feature")
    held = plan.set_privacy(held, feature.source_table, feature.source_column, "withheld",
                            graph=graph)
    held = plan.name_record(held, "primary", "person", "people", graph=graph)
    level = next(lv for lv in held.levels("child") if lv.selected)
    held = plan.select_level(held, "child", level.id, False, graph=graph)
    column = _column(held, feature.source_table, feature.source_column)
    assert (column.privacy, column.proposed) == ("withheld", "kept")
    # Set back to what was proposed, it is no longer a choice.
    held = plan.set_privacy(held, feature.source_table, feature.source_column, "kept",
                            graph=graph)
    column = _column(held, feature.source_table, feature.source_column)
    assert column.privacy == column.proposed == "kept"
    with pytest.raises(ValueError, match="not a privacy class"):
        plan.set_privacy(held, feature.source_table, feature.source_column, "masked",
                         graph=graph)
    with pytest.raises(ValueError, match="in no denorm table"):
        plan.set_privacy(held, feature.source_table, "NOPE", "kept", graph=graph)
    date = next(c for c in _table(held, "primary").columns if c.serves == "derive_input")
    with pytest.raises(ValueError, match="a date"):
        plan.set_privacy(held, date.source_table, date.source_column, "surrogate", graph=graph)


# --- extras and clashes -------------------------------------------------------------

def test_an_extra_column_with_a_taken_name_gets_its_tables_name():
    held = _proposed("bank")
    graph = _graph("bank", held)
    child = _table(held, "child")
    taken = {c.name: c for c in child.columns}
    reads = {t.table for t in held.tables if any(s["role"] == "child" for s in t.serves)}
    clash = next((table, column.name) for table in sorted(reads)
                 for column in graph.table(table).columns
                 if column.name in taken and taken[column.name].source_table != table
                 and not structure.find([child], table, column.name))
    table, name = clash
    held = plan.add_extra_column(held, "child", table, name, graph=graph)
    added = next(c for c in _table(held, "child").columns
                 if c.source_table == table and c.source_column == name)
    assert added.serves == "extra"
    assert added.name == f"{graph.table(table).table_name}_{name}"
    assert "taken" in added.reason
    # The first keeps its name; the extra stays through another edit.
    assert next(c for c in _table(held, "child").columns if c.name == name).source_table != table
    held = plan.name_record(held, "child", "holding", "holdings", graph=graph)
    assert structure.find(held.structure, table, name)
    with pytest.raises(ValueError, match="already"):
        plan.add_extra_column(held, "child", table, name, graph=graph)
    with pytest.raises(ValueError, match="no column"):
        plan.add_extra_column(held, "child", table, "NOPE", graph=graph)
    unread = next(t.name for t in graph.model.tables if t.name not in reads)
    with pytest.raises(ValueError, match="does not read"):
        plan.add_extra_column(held, "child", unread, graph.table(unread).columns[0].name,
                              graph=graph)


@pytest.mark.parametrize("word", ["Order", "KEY", "user"])
def test_a_column_named_with_a_reserved_word_gets_its_tables_name(word):
    """The loaders write column names unbracketed: KEY, ORDER or USER as a
    denorm column would break them, so it takes its table's short name."""
    from sdg.accelerator.reserved import is_reserved

    assert is_reserved(word) and not is_reserved("CUSTOMER_ID")
    plain = _landscape("bank")
    held = plan.propose(plain)
    record = held.record_kinds["primary"]["table"]
    present = {c.source_column for c in _table(held, "primary").columns}
    model = plain.model.model_copy(deep=True)
    table = model.table(record)
    column = next(c for c in table.columns if c.name not in present and not c.is_date)
    old, column.name = column.name, word
    renamed = Landscape(name=plain.name, model=model, profile=plain.profile, summary={})
    held = plan.propose(renamed)
    graph = plan.graph_for(renamed, held)
    held = plan.add_extra_column(held, "primary", record, word, graph=graph)
    added = _column(held, record, word)
    assert added.name == f"{table.table_name}_{word}", old
    assert not is_reserved(added.name)
    assert "reserves" in added.reason
    packs.plain_name(added.name, "a denorm column")
    # Kept through another edit.
    held = plan.name_record(held, "primary", "client", "clients", graph=graph)
    assert _column(held, record, word).name == f"{table.table_name}_{word}"


def test_an_extra_that_may_identify_is_proposed_withheld_but_not_locked():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    record = held.record_kinds["primary"]["table"]
    present = {c.source_column for c in _table(held, "primary").columns}
    unique = next(c.name for c in graph.table(record).columns
                  if c.name not in present and not c.is_date
                  and not graph.is_personal(record, c.name)
                  and graph.profile_of(record, c.name).cardinality == "unique")
    held = plan.add_extra_column(held, "primary", record, unique, graph=graph)
    column = _column(held, record, unique)
    assert (column.privacy, column.locked) == ("withheld", False)
    # Kept only on the person's word that it holds nothing personal.
    with pytest.raises(ValueError, match="confirm it holds nothing personal"):
        plan.set_privacy(held, record, unique, "kept", graph=graph)
    held = plan.set_privacy(held, record, unique, "kept", graph=graph, confirm_not_personal=True)
    assert _column(held, record, unique).privacy == "kept"
    assert _column(held, record, unique).confirmed_not_personal
    # Fails closed: free text longer than a short code, and a column the
    # profile never read, are proposed withheld too.
    graph = _graph("bank", _proposed("bank"))
    held = _proposed("bank")
    child = held.record_kinds["child"]["table"]
    reads = sorted(t.table for t in held.tables if any(s["role"] == "child" for s in t.serves))
    present = {(c.source_table, c.source_column) for c in _table(held, "child").columns}
    text = next((t, c.name) for t in reads for c in graph.table(t).columns
                if (t, c.name) not in present and c.is_character
                and (c.character_length() or 0) > 10 and not graph.is_personal(t, c.name))
    held = plan.add_extra_column(held, "child", *text, graph=graph)
    (added,) = [c for c in _table(held, "child").columns
                if (c.source_table, c.source_column) == text]
    assert added.privacy == "withheld" and "free text" in added.reason
    short = next(c.name for c in graph.table(child).columns
                 if (child, c.name) not in present and not c.is_date
                 and not (c.is_character and (c.character_length() or 99) > 10)
                 and not graph.is_personal(child, c.name))
    original = graph.profile_of
    graph.profile_of = lambda t, c: None if (t, c) == (child, short) else original(t, c)
    held = plan.add_extra_column(held, "child", child, short, graph=graph)
    assert _column(held, child, short).privacy == "withheld"
    assert "not profiled" in _column(held, child, short).reason


# --- bridges ------------------------------------------------------------------------

def _dated_bridge_level(held, graph):
    """A primary level reached down into a link table with a date pair."""
    for level in held.levels("primary"):
        for join in level.path:
            if structure._down(graph, join) and criteria.best_pair(graph, join.to_table):
                return level, join.to_table
    raise AssertionError("no level is reached through a dated link")


def test_a_dated_bridge_is_picked_and_an_undated_one_grows_the_grain(monkeypatch):
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    level, bridge = _dated_bridge_level(held, graph)
    held = plan.select_level(held, "primary", level.id, True, graph=graph)
    pair = criteria.best_pair(graph, bridge)
    assert _table(held, "primary").pick == [{"table": bridge, "start": pair["start"],
                                             "end": pair["end"]}]
    # The bridge's own in-force criterion is offered too, unselected.
    assert any(c.table == bridge and c.kind == "in_force" and not c.selected
               for c in held.criteria["primary"])
    key = _column(held, level.table, level.column)
    assert "appear once per" not in key.reason

    monkeypatch.setattr(criteria, "best_pair", lambda graph, table: None)
    held = plan.realign(held, graph)
    assert _table(held, "primary").pick == []
    key = _column(held, level.table, level.column)
    assert "appear once per" in key.reason and "grain" in key.reason


# --- the plan file ------------------------------------------------------------------

def test_a_version_1_proposal_loads_and_is_filled_on_its_first_edit():
    held = _proposed("bank")
    old = held.to_dict()
    old["version"] = 1
    for key in ("criteria", "structure", "pack"):
        old.pop(key)
    back = plan.SourcePlan.from_dict(old)
    assert back.version == plan.VERSION == 2
    assert back.structure == [] and back.all_criteria() == []
    assert back.pack == {"id": None, "name": None}
    graph = _graph("bank", back)
    back = plan.realign(back, graph)
    assert back.to_dict() == held.to_dict()
    with pytest.raises(ValueError, match="version"):
        plan.SourcePlan.from_dict({**old, "version": 3})
    # A version 1 plan was never approved with criteria and structure.
    assert plan.SourcePlan.from_dict({**old, "status": "approved"}).status == "draft"
    with pytest.raises(ValueError, match="this version reads"):
        structure.DenormColumn.from_dict({**held.structure[0].columns[0].to_dict(), "x": 1})
    with pytest.raises(ValueError, match="privacy"):
        structure.DenormColumn.from_dict({**held.structure[0].columns[0].to_dict(),
                                          "privacy": "open"})


def test_the_plan_hash_leaves_out_status_and_notes():
    held = _proposed("bank")
    graph = _graph("bank", held)
    first = plan.plan_hash(held)
    assert len(first) == 64
    held.status, held.notes = "approved", held.notes + ["a note"]
    assert plan.plan_hash(held) == first
    feature = next(c for c in _table(held, "primary").columns if c.serves == "feature")
    changed = plan.set_privacy(held, feature.source_table, feature.source_column, "withheld",
                               graph=graph)
    assert plan.plan_hash(changed) != first and changed.status == "draft"


def test_naming_the_pack():
    held = _proposed("bank")
    held = plan.name_pack(held, "acme_bank", "Acme bank")
    assert held.pack == {"id": "acme_bank", "name": "Acme bank"}
    for bad in ("health", "banking", "Acme", "a", "1bank", "acme-bank", "a" * 33, "x;y"):
        with pytest.raises(ValueError):
            plan.name_pack(held, bad, "Acme bank")
    with pytest.raises(ValueError, match="plain words"):
        plan.name_pack(held, "acme", "<b>Acme</b>")


# --- the commands -------------------------------------------------------------------

def test_the_commands_edit_criteria_structure_and_pack(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sdg.accelerator import sources
    from sdg.cli.accelerate import accelerate

    for name in ("model", "profile"):
        (tmp_path / f"{name}.json").write_text(
            (FIXTURES / f"bank-{name}.json").read_text("utf-8"), encoding="utf-8")
    (tmp_path / "landscape.json").write_text('{"source": "BANK"}', encoding="utf-8")
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    runner = CliRunner()

    def run(*args: str, code: int = 0) -> str:
        done = runner.invoke(accelerate, list(args))
        assert done.exit_code == code, (args, done.output)
        return " ".join(done.output.split())

    run("propose", "BANK", "--offline")
    held = plan.load(tmp_path)
    (values,) = held.criteria["child"]
    said = run("criteria", "BANK")
    assert "no values yet" in said
    run("criterion", "BANK", values.id, "--on", code=1)
    said = run("criterion", "BANK", values.id, "--value", "A", "--on")
    assert "in A" in said
    assert plan.load(tmp_path).criteria["child"][0].selected
    record = held.record_kinds["primary"]["table"]
    identifying = next(c.name for c in _graph("bank", held).table(record).columns
                       if _graph("bank", held).element_of(record, c.name) in IDENTIFYING)
    said = run("extra", "BANK", "primary", record, identifying)
    assert "withheld" in said
    said = run("privacy", "BANK", record, identifying, "kept", code=1)
    assert "locked" in said
    run("pack-name", "BANK", "acme_bank", "Acme bank")
    said = run("structure", "BANK")
    assert "acme_bank" in said and "customer_denorm" in said
    run("pack-name", "BANK", "banking", "Bank", code=1)
    run("criterion", "BANK", values.id, code=1)
    saved = plan.load(tmp_path)
    assert saved.pack["id"] == "acme_bank" and saved.version == 2
    assert structure.find(saved.structure, record, identifying)[0].privacy == "withheld"
    # A key proposed a surrogate for its name is kept only with the person's word.
    key = held.record_kinds["child"]["key_columns"][0]
    child = held.record_kinds["child"]["table"]
    said = run("privacy", "BANK", child, key, "kept", code=1)
    assert "confirm it holds nothing personal" in said
    run("privacy", "BANK", child, key, "kept", "--confirm-not-personal")
    found = structure.find(plan.load(tmp_path).structure, child, key)[0]
    assert found.privacy == "kept" and found.confirmed_not_personal
    # No link without dates on the bank's levels: no grain change to accept.
    said = run("grain", "BANK", "primary", record, code=1)
    assert "no grain change to accept" in said


# --- keys that may say who a row is about ---------------------------------------------

@pytest.mark.parametrize("name", ["ACCT_NO", "CUST_KEY", "CARD_NBR", "EMAIL_ID"])
@pytest.mark.parametrize("serves", ["key", "lines_key"])
def test_a_key_named_as_a_person_an_account_or_a_contact_is_proposed_a_surrogate(name, serves):
    held = _proposed("bank")
    graph = _graph("bank", held)
    privacy, reason, locked = structure.privacy_for(graph, "dbo.TXN", name, serves)
    assert (privacy, locked) == ("surrogate", False)
    assert "reads as a person, an account or a contact" in reason
    assert "confirming it holds nothing personal" in reason
    # Joined on, both sides are said.
    privacy, reason, _ = structure.privacy_for(graph, "dbo.TXN", name, serves, joins=True)
    assert privacy == "surrogate" and "both sides of the join" in reason
    # The same name as a level, a label or a feature (product owner,
    # 2026-11-21): a level's key a surrogate, so it still groups; a label or a
    # feature withheld; none locked, each kept only on the person's word.
    for named, expected in (("level_key", "surrogate"), ("level_label", "withheld"),
                            ("feature", "withheld")):
        privacy, reason, locked = structure.privacy_for(graph, "dbo.TXN", name, named)
        assert (privacy, locked) == (expected, False), named
        assert "reads as a person, an account or a contact" in reason
        column = structure.DenormColumn(
            name=name, source_table="dbo.TXN", source_column=name, sql_type="int",
            serves=named, privacy=expected, proposed=expected, reason=reason, locked=False)
        assert "confirm it holds nothing personal" in structure.refusal(column, "kept", graph)
        # Confirmed, nothing personal is said (the name is made up for the
        # test, so the source's own check on the column still speaks).
        assert "personal" not in (structure.refusal(column, "kept", graph, confirmed=True) or "")
    # A derived feature's input is stored for the value derived from it.
    assert structure.privacy_for(graph, "dbo.TXN", name, "derive_input")[0] == "kept"


def test_a_key_never_profiled_is_proposed_a_surrogate(monkeypatch):
    held = _proposed("bank")
    graph = _graph("bank", held)
    lines_key = next(c for c in _table(held, "child").columns if c.serves == "lines_key")
    assert lines_key.privacy == "kept"
    where = (lines_key.source_table, lines_key.source_column)
    real = graph.profile_of
    monkeypatch.setattr(graph, "profile_of", lambda t, c: None if (t, c) == where else real(t, c))
    held = plan.realign(held, graph)
    found = _column(held, *where)
    assert (found.privacy, found.proposed, found.locked, found.sql_type) == (
        "surrogate", "surrogate", False, structure.SURROGATE_TYPE)
    assert "not profiled" in found.reason


def test_an_account_style_record_key_is_kept_only_once_confirmed_not_personal():
    held = _proposed("bank")
    graph = _graph("bank", held)
    child = held.record_kinds["child"]["table"]
    key = held.record_kinds["child"]["key_columns"][0]
    assert any(token in key for token in ("ACCOUNT", "ACCT"))
    found = _column(held, child, key)
    assert (found.privacy, found.proposed, found.locked, found.confirmed_not_personal) == (
        "surrogate", "surrogate", False, False)
    with pytest.raises(ValueError, match="confirm it holds nothing personal"):
        plan.set_privacy(held, child, key, "kept", graph=graph)
    with pytest.raises(ValueError, match="only a column being kept"):
        plan.set_privacy(held, child, key, "surrogate", graph=graph, confirm_not_personal=True)
    held = plan.set_privacy(held, child, key, "kept", graph=graph, confirm_not_personal=True)
    found = _column(held, child, key)
    assert (found.privacy, found.confirmed_not_personal) == ("kept", True)
    # The word given holds across an edit, a save and a load.
    held = plan.name_record(held, "child", "holding", "holdings", graph=graph)
    held = plan.realign(plan.SourcePlan.from_dict(json.loads(json.dumps(held.to_dict()))), graph)
    found = _column(held, child, key)
    assert (found.privacy, found.confirmed_not_personal) == ("kept", True)
    # Made a surrogate again, the word goes, and keeping it asks for it again.
    held = plan.set_privacy(held, child, key, "surrogate", graph=graph)
    assert not _column(held, child, key).confirmed_not_personal
    with pytest.raises(ValueError, match="confirm it holds nothing personal"):
        plan.set_privacy(held, child, key, "kept", graph=graph)


def test_a_join_key_named_as_a_person_is_a_surrogate_on_both_sides_until_confirmed():
    held = _proposed("bank")
    graph = _graph("bank", held)
    (pair,) = structure.join_pairs(held, graph)
    for side in pair:
        found = _column(held, *side)
        assert (found.privacy, found.proposed, found.locked, found.sql_type) == (
            "surrogate", "surrogate", False, structure.SURROGATE_TYPE), side
        assert "both sides" in found.reason
    held = plan.set_privacy(held, *pair[1], "kept", graph=graph, confirm_not_personal=True)
    assert {(_column(held, *s).privacy, _column(held, *s).confirmed_not_personal)
            for s in pair} == {("kept", True)}


def test_a_risky_key_kept_by_a_hand_edit_is_refused_unless_confirmed():
    from sdg.accelerator import builder

    held = _proposed("bank")
    graph = _graph("bank", held)
    child = held.record_kinds["child"]["table"]
    key = held.record_kinds["child"]["key_columns"][0]
    found = _column(held, child, key)
    # Edited by hand in the saved plan: kept, as if proposed so.
    found.privacy, found.proposed, found.sql_type = "kept", "kept", "bigint"
    problems = builder.privacy_problems(held, graph)
    assert any(key in p and "confirm it holds nothing personal" in p for p in problems), problems
    found.confirmed_not_personal = True
    assert not any(f".{key}" in p for p in builder.privacy_problems(held, graph))
    # A confirmation never unlocks what identifies a person.
    held = plan.add_extra_column(held, "primary", "dbo.CUSTOMER", "LAST_NAME", graph=graph)
    named = _column(held, "dbo.CUSTOMER", "LAST_NAME")
    named.privacy, named.confirmed_not_personal = "kept", True
    assert any("LAST_NAME is locked" in p for p in builder.privacy_problems(held, graph))


# --- links without dates --------------------------------------------------------------

def test_a_link_without_dates_is_refused_until_its_grain_change_is_accepted(monkeypatch):
    from sdg.accelerator import sqlgen

    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    level, bridge = _dated_bridge_level(held, graph)
    held = plan.select_level(held, "primary", level.id, True, graph=graph)
    monkeypatch.setattr(criteria, "best_pair", lambda graph, table: None)
    held = plan.realign(held, graph)
    assert structure.unpicked_links(held, "primary", graph) == [bridge]
    assert "accept the grain change" in _column(held, level.table, level.column).reason
    with pytest.raises(sqlgen.SqlRefused, match="links without dates"):
        sqlgen.block_sql(held, _table(held, "primary"), graph)
    record = held.record_kinds["primary"]["table"]
    with pytest.raises(ValueError, match="no grain change to accept"):
        plan.accept_grain(held, "primary", record, True, graph=graph)

    held = plan.accept_grain(held, "primary", bridge, True, graph=graph)
    assert held.grain_accepted == {"primary": [bridge], "child": []}
    found = graph.table(bridge)
    sql = sqlgen.block_sql(held, _table(held, "primary"), graph)
    assert f"LEFT JOIN [{found.schema_name}].[{found.table_name}] AS" in sql
    assert "sdm_pick" not in sql
    # Kept in the saved plan, and in its hash.
    again = plan.SourcePlan.from_dict(json.loads(json.dumps(held.to_dict())))
    assert again.grain_accepted == held.grain_accepted
    assert plan.plan_hash(again) == plan.plan_hash(held)
    taken_back = plan.accept_grain(plan.SourcePlan.from_dict(held.to_dict()), "primary", bridge,
                                   False, graph=graph)
    assert taken_back.grain_accepted["primary"] == []
    assert plan.plan_hash(taken_back) != plan.plan_hash(held)
    # An acceptance lasts only while the link still grows the grain.
    held = plan.select_level(held, "primary", level.id, False, graph=graph)
    assert held.grain_accepted["primary"] == []


def test_a_saved_grain_acceptance_of_another_shape_is_refused():
    held = _proposed("bank").to_dict()
    held["grain_accepted"] = {"primary": "dbo.X"}
    with pytest.raises(ValueError, match="a list of table names per role"):
        plan.SourcePlan.from_dict(held)


def test_a_hand_edit_that_changes_what_a_risky_key_serves_is_refused():
    from sdg.accelerator import builder

    held = _proposed("bank")
    graph = _graph("bank", held)
    child = held.record_kinds["child"]["table"]
    key = held.record_kinds["child"]["key_columns"][0]
    found = _column(held, child, key)
    # Made a feature by hand, so the key rule would not apply, and kept.
    found.serves, found.privacy, found.proposed = "feature", "kept", "kept"
    problems = builder.privacy_problems(held, graph)
    assert any(f".{key} is saved as serving feature, but it serves key" in p
               for p in problems), problems


def test_the_other_side_of_a_join_with_a_personal_key_kept_by_hand_is_refused(monkeypatch):
    from sdg.accelerator import builder

    held = _proposed("bank")
    graph = _graph("bank", held)
    (pair,) = structure.join_pairs(held, graph)
    mapped = graph.element_of
    monkeypatch.setattr(graph, "element_of", lambda t, c: (
        "NATIONAL_ID" if (t, c) == pair[0] else mapped(t, c)))
    held = plan.realign(held, graph)
    # The other side unlocked and kept by hand, with the word given too.
    other = _column(held, *pair[1])
    other.privacy, other.proposed, other.locked = "kept", "kept", False
    other.confirmed_not_personal = True
    problems = builder.privacy_problems(held, graph)
    assert any(f".{other.name}" in p and "locked" in p for p in problems), problems

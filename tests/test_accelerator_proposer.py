"""The proposer: record kinds, levels and the tables they need, from a landscape.

Fast and read from the recorded fixtures only (tests/fixtures/accelerator/):
no database. The hand-defined hierarchies each demo source should give back
are in expected.json, in the sources' own column names, so this file names
no source column of the health demo.

Realignment is the other half: every edit to a plan derives its tables again,
a table leaves only when nothing selected needs it, reordering changes no
table, and a swap replaces the level's path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdg.accelerator import graph as graph_module
from sdg.accelerator import hierarchy, plan, source_read, sources, tables
from sdg.accelerator.graph import KeyGraph
from sdg.accelerator.hierarchy import Join
from sdg.accelerator.landscape import Landscape
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.profiling.model import ProfileReport

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
SOURCES = ["facets_qa", "bank"]


@pytest.fixture(autouse=True)
def _no_store(monkeypatch):
    """Every edit tells the store (approval.changed), which would reach the
    real one: here an edit's approval is tested by test_accelerator_approval."""
    from sdg.accelerator import approval

    monkeypatch.setattr(approval, "changed", lambda source, new_hash: None)


def _landscape(source: str) -> Landscape:
    return Landscape(name=source.upper(),
                     model=PhysicalModel.load(FIXTURES / f"{source}-model.json"),
                     profile=ProfileReport.load(FIXTURES / f"{source}-profile.json"),
                     summary={})


_PROPOSED: dict[str, dict] = {}


def _proposed(source: str) -> plan.SourcePlan:
    """A fresh copy of the source's first proposal (made once, copied per test)."""
    if source not in _PROPOSED:
        _PROPOSED[source] = plan.propose(_landscape(source)).to_dict()
    return plan.SourcePlan.from_dict(json.loads(json.dumps(_PROPOSED[source])))


def _graph(source: str, held: plan.SourcePlan | None = None) -> KeyGraph:
    return plan.graph_for(_landscape(source), held)


def _by_column(held: plan.SourcePlan, role: str, column: str, kind: str | None = None):
    found = [lv for lv in held.levels(role) if lv.column == column
             and (kind is None or lv.kind == kind)]
    assert found, f"no {role} level on {column}"
    return found[0]


def _by_id(held: plan.SourcePlan, role: str, level_id: str, kind: str | None = None):
    found = [lv for lv in held.levels(role) if lv.id == level_id
             and (kind is None or lv.kind == kind)]
    assert found, f"no {role} {kind or ''} level {level_id}"
    return found[0]


def _without(held: plan.SourcePlan, role: str, level_id: str) -> plan.SourcePlan:
    """The plan with one level taken out of the role's candidates, as if the
    proposal had not offered it: a swap may bring in only a column that is
    not already a level."""
    held.hierarchies[role]["levels"] = [lv for lv in held.levels(role) if lv.id != level_id]
    return held


def _table_names(held: plan.SourcePlan) -> set[str]:
    return {t.table for t in held.tables}


def _needed(held: plan.SourcePlan) -> set[str]:
    """What the plan needs, read from its record kinds and selected levels
    the way a person would: record, lines, every table on a selected path,
    every label table."""
    out: set[str] = set()
    for role in tables.ROLES:
        kind = held.record_kinds.get(role)
        if not kind:
            continue
        out.add(kind["table"])
        if kind.get("lines_table"):
            out.add(kind["lines_table"])
        for level in held.levels(role):
            if level.selected:
                out.update(j.to_table for j in level.path)
                if level.label_table:
                    out.add(level.label_table)
    return out


def _normalized(held: plan.SourcePlan) -> dict:
    return {t.table: (sorted(json.dumps(s, sort_keys=True) for s in t.serves),
                      sorted(json.dumps(hierarchy.to_dict(j), sort_keys=True) for j in t.joins))
            for t in held.tables}


# --- record kinds and levels on both fixtures -----------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_the_record_kinds_are_the_hand_defined_ones(source):
    held = _proposed(source)
    expected = EXPECTED[source]
    for role in tables.ROLES:
        assert held.candidates[role], f"no {role} candidates"
        assert held.candidates[role][0].table == expected[role]["table"]
        assert held.record_kinds[role]["table"] == expected[role]["table"]
        assert held.record_kinds[role]["lines_table"] == expected[role]["lines"]
    # The person picks: several primary candidates are offered, best first.
    scores = [c.score for c in held.candidates["primary"]]
    assert len(scores) > 1 and scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("role", tables.ROLES)
def test_the_hand_defined_walk_levels_are_selected_in_order(source, role):
    held = _proposed(source)
    chosen = [lv.id for lv in sorted(held.levels(role), key=lambda lv: lv.order)
              if lv.selected and lv.kind == "walk"]
    wanted = EXPECTED[source][role]["walk"]
    assert set(wanted) <= set(chosen), f"{role}: {chosen}"
    in_chosen_order = [c for c in chosen if c in wanted]
    assert in_chosen_order == wanted
    # Each selected walk level has a path from the record and is labeled or
    # says why not.
    for level in held.levels(role):
        if level.selected and level.kind == "walk":
            assert level.path and level.path[0].from_table == held.record_kinds[role]["table"]
            assert level.path[-1].to_table == level.table
            assert level.label_column or "no label" in level.reason


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("role", tables.ROLES)
def test_the_hand_defined_narrowing_levels_are_proposed(source, role):
    held = _proposed(source)
    walk_orders = [lv.order for lv in held.levels(role) if lv.selected and lv.kind == "walk"]
    must = EXPECTED[source][role]["selected_narrow"]
    assert set(must) <= set(EXPECTED[source][role]["narrow"])
    for level_id in EXPECTED[source][role]["narrow"]:
        level = _by_id(held, role, level_id, "narrow")
        # Narrowing levels follow every selected walk level.
        if level.selected:
            assert level.order > max(walk_orders)
    for level_id in must:
        # Selected in the first proposal, and after the walk, whatever else.
        level = _by_id(held, role, level_id, "narrow")
        assert level.selected, f"{role}: {level_id} is not selected"
        assert walk_orders and level.order > max(walk_orders)


def test_the_health_childs_narrowing_level_is_held_to_selection():
    """expected.json asks the health child's kind column to be selected; the
    check above is not left to a fixture that might list nothing."""
    assert EXPECTED["facets_qa"]["child"]["selected_narrow"]


@pytest.mark.parametrize("source", SOURCES)
def test_no_level_is_personal_or_the_childs_own_parent_record(source):
    held = _proposed(source)
    graph = _graph(source)
    primary = held.record_kinds["primary"]["table"]
    for role in tables.ROLES:
        for level in held.levels(role):
            assert not graph.is_personal(level.table, level.column), level.id
            if level.label_column:
                assert not graph.is_personal(level.label_table, level.label_column), level.id
            assert level.table not in {held.record_kinds[role]["lines_table"]}
            if role == "child":
                assert level.table != primary or not level.path
        orders = [lv.order for lv in held.levels(role)]
        assert orders == list(range(1, len(orders) + 1))


def test_a_city_column_alone_does_not_make_a_table_person_like():
    graph = _graph("bank")
    assert 0 < graph.person_weight("dbo.BRANCH") < 1.0
    assert graph.person_weight("dbo.CUSTOMER") >= 3.0
    # So the branch is still a level of the customer.
    assert _by_column(_proposed("bank"), "primary", "BRANCH_ID", "walk").selected


@pytest.mark.parametrize("source", SOURCES)
def test_features_include_an_age_and_a_tenure_for_the_primary(source):
    held = _proposed(source)
    features = {f.column: f for f in held.features("primary")}
    assert features["_age"].derive["op"] == "elapsed"
    assert features["_age"].derive["unit"] == "years"
    assert features["_tenure"].derive["unit"] == "months"
    graph = _graph(source)
    record = held.record_kinds["primary"]["table"]
    for feature in held.features("primary"):
        if not feature.derive:
            assert not graph.is_personal(record, feature.column)


# --- the plan file --------------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_the_proposal_round_trips_through_save_and_load(source, tmp_path):
    held = _proposed(source)
    path = plan.save(held, tmp_path)
    assert path.name == plan.PROPOSAL_FILE
    written = json.loads(path.read_text(encoding="utf-8"))
    assert list(written) == ["version", "source", "read_at", "status", "record_kinds",
                             "candidates", "hierarchies", "tables", "notes", "joins",
                             "criteria", "structure", "pack", "grain_accepted"]
    assert written["version"] == 2 and written["status"] == "draft"
    assert set(written["criteria"]) == {"primary", "child"}
    assert written["pack"] == {"id": None, "name": None}
    assert {t["role"] for t in written["structure"]} == {"primary", "child"}
    assert set(written["record_kinds"]["primary"]) == {"table", "key_columns", "lines_table",
                                                       "name"}
    level = written["hierarchies"]["primary"]["levels"][0]
    assert {"id", "table", "column", "label_table", "label_column", "path", "distinct",
            "per_value", "kind", "confidence", "reason", "selected", "order"} <= set(level)
    assert set(written["candidates"]["primary"][0]) == {"table", "score", "reasons",
                                                        "lines_table", "lines_key"}
    assert set(written["tables"][0]) == {"table", "serves", "joins"}
    back = plan.load(tmp_path)
    assert back.to_dict() == held.to_dict()


def test_loading_a_missing_proposal_says_what_to_run(tmp_path):
    with pytest.raises(FileNotFoundError, match="propose"):
        plan.load(tmp_path)


# --- realignment ----------------------------------------------------------

def test_deselect_removes_a_table_only_when_nothing_else_needs_it():
    held = _proposed("bank")
    graph = _graph("bank", held)
    branch = _by_column(held, "child", "BRANCH_ID", "walk")
    held = plan.select_level(held, "child", branch.id, False, graph=graph)
    # The primary's own branch level still needs the table.
    assert "dbo.BRANCH" in _table_names(held)
    assert {"role": "child", "level": branch.id, "as": "level"} not in next(
        t for t in held.tables if t.table == "dbo.BRANCH").serves
    held = plan.select_level(held, "primary", branch.id, False, graph=graph)
    assert "dbo.BRANCH" not in _table_names(held)
    # The records stay whatever is deselected.
    assert {"dbo.CUSTOMER", "dbo.ACCOUNT", "dbo.TXN"} <= _table_names(held)


def test_a_narrowing_level_keeps_its_table_after_the_walk_level_goes():
    held = _proposed("bank")
    graph = _graph("bank", held)
    product = _by_column(held, "child", "PRODUCT_CODE", "walk")
    kind = _by_column(held, "child", "PRODUCT_TYPE", "narrow")
    held = plan.select_level(held, "child", kind.id, True, graph=graph)
    held = plan.select_level(held, "child", product.id, False, graph=graph)
    assert product.table in _table_names(held)
    held = plan.select_level(held, "child", kind.id, False, graph=graph)
    assert product.table not in _table_names(held)


@pytest.mark.parametrize("source", SOURCES)
def test_deselecting_level_by_level_keeps_exactly_the_tables_still_needed(source):
    held = _proposed(source)
    graph = _graph(source, held)
    for role in ("child", "primary"):
        for level in [lv for lv in held.levels(role) if lv.selected]:
            held = plan.select_level(held, role, level.id, False, graph=graph)
            assert _table_names(held) == _needed(held), level.id
    assert _table_names(held) == {held.record_kinds[r][k] for r in tables.ROLES
                                  for k in ("table", "lines_table")
                                  if held.record_kinds[r][k]}


@pytest.mark.parametrize("source", SOURCES)
def test_reorder_changes_no_table(source):
    held = _proposed(source)
    graph = _graph(source, held)
    before = _normalized(held)
    chosen = [lv.id for lv in sorted(held.levels("child"), key=lambda lv: lv.order)
              if lv.selected]
    assert len(chosen) >= 2
    held = plan.reorder_levels(held, "child", list(reversed(chosen)), graph=graph)
    after = [lv.id for lv in sorted(held.levels("child"), key=lambda lv: lv.order)
             if lv.selected]
    assert after == list(reversed(chosen))
    assert _normalized(held) == before
    with pytest.raises(ValueError, match="selected"):
        unselected = next(lv.id for lv in held.levels("child") if not lv.selected)
        plan.reorder_levels(held, "child", [unselected], graph=graph)


def test_swap_replaces_the_path():
    held = _proposed("bank")
    graph = _graph("bank", held)
    product = _by_column(held, "child", "PRODUCT_CODE", "walk")
    assert [j.to_table for j in product.path] == ["dbo.PRODUCT"]
    # The branch's state is reached through the customer. It is already a
    # candidate, and a swap brings in only a column that is not, so it is
    # taken out of the candidates first.
    held = _without(held, "child", "dbo.BRANCH.BRANCH_STATE")
    held = plan.swap_level(held, "child", product.id, "dbo.BRANCH", "BRANCH_STATE", graph=graph)
    ids = {lv.id for lv in held.levels("child")}
    assert product.id not in ids
    swapped = next(lv for lv in held.levels("child") if lv.id == "dbo.BRANCH.BRANCH_STATE")
    assert swapped.selected and swapped.order == product.order
    assert [(j.from_table, j.to_table) for j in swapped.path] == [
        ("dbo.ACCOUNT", "dbo.CUSTOMER"), ("dbo.CUSTOMER", "dbo.BRANCH")]
    # The product table served nothing else, so it left; the branch table
    # now also serves the child's swapped level.
    assert "dbo.PRODUCT" not in _table_names(held)
    branch = next(t for t in held.tables if t.table == "dbo.BRANCH")
    assert {"role": "child", "level": swapped.id, "as": "level"} in branch.serves


def test_swap_refuses_what_propose_would_not_offer():
    """One rule for both (hierarchy.level_refusal): a swap is refused for
    anything propose_levels leaves out, and for a column already a level."""
    held = _proposed("bank")
    graph = _graph("bank", held)
    product = _by_column(held, "child", "PRODUCT_CODE", "walk")
    before = held.to_dict()
    for table, column, said in (
            ("dbo.CUSTOMER", "SEGMENT", "primary record"),        # the child's parent
            ("dbo.BRANCH", "BRANCH_NAME", "neither the key"),    # a label, no code
            ("dbo.BRANCH", "BRANCH_STATE", "already a level"),
            ("dbo.ACCOUNT", "BALANCE", "not a code column")):    # on the record
        with pytest.raises(ValueError, match=said):
            plan.swap_level(held, "child", product.id, table, column, graph=graph)
    assert held.to_dict() == before
    # The primary's own people table is never one of its levels either.
    branch = _by_column(held, "primary", "BRANCH_ID", "walk")
    with pytest.raises(ValueError, match="personal"):
        plan.swap_level(held, "primary", branch.id, "dbo.CUSTOMER", "FIRST_NAME", graph=graph)
    assert held.to_dict() == before


def test_level_refusal_names_each_exclusion():
    bank = _graph("bank")
    assert "lines" in hierarchy.level_refusal(bank, "dbo.ACCOUNT", "dbo.TXN", "TXN_TYPE", [],
                                              lines="dbo.TXN")
    assert "event" in hierarchy.level_refusal(bank, "dbo.ACCOUNT", "dbo.TXN", "TXN_TYPE", [])
    path, _ = hierarchy.path_to(bank, "dbo.ACCOUNT", "dbo.CUSTOMER")
    assert "people" in hierarchy.level_refusal(bank, "dbo.ACCOUNT", "dbo.CUSTOMER",
                                               "CUSTOMER_ID", path)
    path, _ = hierarchy.path_to(bank, "dbo.ACCOUNT", "dbo.PRODUCT")
    assert hierarchy.level_refusal(bank, "dbo.ACCOUNT", "dbo.PRODUCT", "PRODUCT_CODE",
                                   path) is None
    health = _graph("facets_qa")
    extension = next(t.name for t in health.model.tables if health.owner_of(t.name))
    key = health.table(extension).primary_key[0]
    assert "only extends" in hierarchy.level_refusal(health, "dbo.NOT_THE_RECORD", extension,
                                                     key, [])
    link = next(t for t in health.model.tables if health.table_kind(t.name) == "link")
    assert "link" in hierarchy.level_refusal(health, "dbo.NOT_THE_RECORD", link.name,
                                             link.primary_key[0], [])
    empty = Table(name="dbo.EMPTY", schema_name="dbo", table_name="EMPTY", row_count=0,
                  columns=[Column(name="K", ordinal=1, type_name="int")], primary_key=["K"])
    health.model.tables.append(empty)
    assert "no rows" in hierarchy.level_refusal(health, "dbo.NOT_THE_RECORD", "dbo.EMPTY",
                                                "K", [])


def test_swap_refuses_an_unreachable_table_or_a_personal_column():
    held = _proposed("bank")
    graph = _graph("bank", held)
    branch = _by_column(held, "child", "BRANCH_ID", "walk")
    before = held.to_dict()
    with pytest.raises(ValueError, match="state the join"):
        plan.swap_level(held, "child", branch.id, "dbo.MCC", "MCC_DESC", graph=graph)
    with pytest.raises(ValueError, match="personal"):
        plan.swap_level(held, "child", branch.id, "dbo.CUSTOMER", "FIRST_NAME", graph=graph)
    assert held.to_dict() == before


def test_a_stated_join_reaches_a_table_inference_did_not():
    held = _proposed("bank")
    graph = _graph("bank", held)
    join = Join("dbo.ACCOUNT", ["PRODUCT_CODE"], "dbo.MCC", ["MCC_CODE"], "stated")
    held = plan.state_join(held, "child", join, graph=graph)
    assert held.joins["stated"][0].to_table == "dbo.MCC"
    added = _by_column(held, "child", "MCC_CODE")
    assert not added.selected and added.path[0].kind == "stated"
    # Rebuilt from the saved landscape and the plan, the join is still there.
    assert any(e.kind == "stated" for e in _graph("bank", held).edges_from("dbo.ACCOUNT"))
    with pytest.raises(ValueError):
        plan.state_join(held, "child", Join("dbo.ACCOUNT", ["NOPE"], "dbo.MCC", ["MCC_CODE"],
                                            "stated"), graph=graph)


def test_every_edit_derives_the_tables_again():
    held = _proposed("bank")
    graph = _graph("bank", held)
    branch = _by_column(held, "child", "BRANCH_ID", "walk")
    product = _by_column(held, "child", "PRODUCT_CODE", "walk")
    kind = _by_column(held, "child", "PRODUCT_TYPE", "narrow")
    feature = held.features("child")[0]
    edits = [
        lambda p: plan.select_level(p, "child", kind.id, True, graph=graph),
        lambda p: plan.select_level(p, "child", kind.id, False, graph=graph),
        lambda p: plan.reorder_levels(p, "child", [product.id, branch.id], graph=graph),
        lambda p: plan.swap_level(_without(p, "child", "dbo.BRANCH.BRANCH_STATE"), "child",
                                  product.id, "dbo.BRANCH", "BRANCH_STATE", graph=graph),
        lambda p: plan.name_record(p, "primary", "customer", "customers"),
        lambda p: plan.select_feature(p, "child", feature.id, False),
        lambda p: plan.state_join(p, "child", Join("dbo.ACCOUNT", ["PRODUCT_CODE"], "dbo.MCC",
                                                   ["MCC_CODE"], "stated"), graph=graph),
        lambda p: plan.choose_record(p, "child", "dbo.TXN", graph=graph),
    ]
    for edit in edits:
        held.tables = []
        held.status = "approved"
        held = edit(held)
        assert held.tables, "tables were not derived again"
        assert [t.to_dict() for t in held.tables] == [t.to_dict()
                                                      for t in tables.derive(held)]
        assert held.status == "draft"


def test_naming_takes_plain_words_only():
    held = _proposed("bank")
    held = plan.name_record(held, "child", " account ", "accounts")
    assert held.record_kinds["child"]["name"] == {"one": "account", "many": "accounts"}
    for bad in ("", "  ", "<b>x</b>", "x" * 61):
        with pytest.raises(ValueError):
            plan.name_record(held, "child", bad, "accounts")
    with pytest.raises(ValueError, match="role"):
        plan.name_record(held, "other", "a", "b")


def test_choosing_another_primary_proposes_again():
    held = _proposed("bank")
    graph = _graph("bank", held)
    held = plan.choose_record(held, "primary", "dbo.ACCOUNT", graph=graph)
    assert held.record_kinds["primary"]["table"] == "dbo.ACCOUNT"
    assert held.record_kinds["primary"]["lines_table"] == "dbo.TXN"
    # The old child does not key to the new primary, so it is proposed again.
    assert held.record_kinds["child"] is None or held.record_kinds["child"]["table"] != "dbo.ACCOUNT"
    assert any(c.table == "dbo.ACCOUNT" for c in held.candidates["primary"])
    with pytest.raises(ValueError):
        plan.choose_record(held, "primary", "dbo.NOT_THERE", graph=graph)
    with pytest.raises(ValueError):
        plan.choose_record(held, "child", "dbo.ACCOUNT", graph=graph)


def test_choosing_the_record_already_chosen_changes_nothing():
    held = _proposed("bank")
    graph = _graph("bank", held)
    held.status = "approved"
    before = held.to_dict()
    for role, table in (("primary", "dbo.CUSTOMER"), ("child", "dbo.ACCOUNT")):
        assert plan.choose_record(held, role, table, graph=graph).to_dict() == before


def test_a_new_primary_keeps_the_childs_name_and_choices():
    held = _proposed("bank")
    graph = _graph("bank", held)
    held = plan.name_record(held, "child", "account", "accounts")
    branch = _by_column(held, "child", "BRANCH_ID", "walk")
    held = plan.select_level(held, "child", branch.id, False, graph=graph)
    feature = held.features("child")[0]
    held = plan.select_feature(held, "child", feature.id, False)
    # ACCOUNT keys to PRODUCT too, so it stays the child of a PRODUCT primary.
    held = plan.choose_record(held, "primary", "dbo.PRODUCT", graph=graph)
    assert held.record_kinds["child"]["table"] == "dbo.ACCOUNT"
    assert held.record_kinds["child"]["name"] == {"one": "account", "many": "accounts"}
    assert not next(lv for lv in held.levels("child") if lv.id == branch.id).selected
    assert not next(f for f in held.features("child") if f.id == feature.id).selected
    assert tables.derive(held) and _table_names(held) == _needed(held)


def test_a_nesting_counted_by_the_source_is_kept_for_later_edits():
    held = _proposed("bank")
    graph = _graph("bank", held)
    branch = _by_column(held, "primary", "BRANCH_ID", "walk")
    segment = _by_column(held, "primary", "SEGMENT", "narrow")
    assert hierarchy.verdict(graph, segment, branch) is None
    assert hierarchy.verdict(graph, segment, branch, lambda *_: (2000, 4, 4)) is True
    assert segment.nesting == {branch.id: True}
    # Saved and loaded, then read with no source: the count still answers.
    again = hierarchy.LevelCandidate.from_dict(hierarchy.to_dict(segment))
    assert hierarchy.verdict(graph, again, branch) is True
    # Keys and the profile come first: a kept count never overrides them.
    product = _by_column(held, "child", "PRODUCT_CODE", "walk")
    kind = _by_column(held, "child", "PRODUCT_TYPE", "narrow")
    product.nesting[kind.id] = False
    assert hierarchy.verdict(graph, product, kind) is True


def test_a_pair_count_settles_a_nesting_the_profile_left_open():
    held = _proposed("bank")
    graph = _graph("bank", held)
    branch = _by_column(held, "primary", "BRANCH_ID", "walk")
    segment = _by_column(held, "primary", "SEGMENT", "narrow")
    assert hierarchy.nests(graph, segment, branch) is None
    asked = []

    def counts(table, a, b):
        asked.append((table, a, b))
        return (2000, 4, 4)

    assert hierarchy.nests(graph, segment, branch, counts) is True
    assert asked == [("dbo.CUSTOMER", "BRANCH_ID", "SEGMENT")]
    assert hierarchy.nests(graph, segment, branch, lambda *_: (2000, 4, 9)) is False
    # Nothing counted says nothing: never "does not nest".
    assert hierarchy.nests(graph, segment, branch, lambda *_: (0, 0, 0)) is None
    assert hierarchy.nests(graph, segment, branch, lambda *_: None) is None


def test_equal_counts_on_a_thin_sample_settle_nothing():
    """On a sampled table, values each seen about once decide their pair
    trivially: equal counts there are undecided (the profile's own rule, rows
    at least twice the distinct values). Unequal counts still say no."""
    health = _graph("facets_qa")
    sampled = next(t.name for t in health.model.tables if source_read.is_sampled(t))
    assert hierarchy._counted(health, sampled, (100, 80, 80)) is None
    assert hierarchy._counted(health, sampled, (160, 80, 80)) is True
    assert hierarchy._counted(health, sampled, (100, 80, 90)) is False
    assert hierarchy._counted(health, sampled, (0, 0, 0)) is None
    whole = _graph("bank")
    assert not source_read.is_sampled(whole.table("dbo.CUSTOMER"))
    assert hierarchy._counted(whole, "dbo.CUSTOMER", (4, 4, 4)) is True


def test_an_approximate_count_on_a_sample_is_not_taken_for_unique():
    health = _graph("facets_qa")
    for held in health.profile.columns:
        table = health.table(held.table)
        if (table is not None and source_read.is_sampled(table) and held.approximate_distinct
                and table.primary_key != [held.column]
                and held.column not in table.unique_single_columns()):
            held.null_rate, held.distinct_ratio, held.row_count = 0.0, 1.0, table.row_count
            assert not hierarchy._unique_on(health, held.table, held.column)
            held.approximate_distinct = False
            assert hierarchy._unique_on(health, held.table, held.column)
            return
    pytest.fail("the health fixture has no approximately counted column on a sampled table")


def test_unknown_nesting_is_said_as_unknown_not_crossed():
    held = _proposed("bank")
    graph = _graph("bank", held)
    product = _by_column(held, "child", "PRODUCT_CODE", "walk")
    branch = _by_column(held, "child", "BRANCH_ID", "walk")
    assert hierarchy.verdict(graph, product, branch) is None
    assert hierarchy.UNKNOWN_FLAG + "BRANCH_ID" in product.reason
    assert hierarchy.NEST_FLAG not in product.reason
    # Shown not to nest (a count kept from an earlier read): then it says so.
    product.nesting[branch.id] = False
    hierarchy.flag_nesting(held.levels("child"), graph)
    assert hierarchy.NEST_FLAG + "BRANCH_ID" in product.reason
    assert hierarchy.UNKNOWN_FLAG not in product.reason


def test_the_source_is_asked_about_neighbors_only():
    """order_levels counts pairs next to each other in the order their counts
    give, not every pair."""
    health = _graph("facets_qa")
    levels = hierarchy.propose_levels(health, "dbo.CMC_MEME_MEMBER")
    walk = [lv for lv in levels if lv.kind == "walk"]
    asked = []

    def counts(table, a, b):
        asked.append((table, a, b))
        return None

    hierarchy.order_levels(levels, health, counts)
    assert len(walk) >= 4
    # Neighbors in the first order, then neighbors once ordered (flag_nesting).
    assert len(asked) <= 2 * (len(walk) - 1) < len(walk) * (len(walk) - 1)


def test_an_estimated_count_near_the_bound_stays_a_walk_level():
    graph = _graph("bank")
    record, table = "dbo.ACCOUNT", "dbo.PRODUCT"
    path, _ = hierarchy.path_to(graph, record, table)
    bound = graph.rows(record) // hierarchy.WALK_ROWS_PER_VALUE
    held = graph.profile_of(table, "PRODUCT_CODE")
    held.distinct_count = int(bound * 1.5)
    held.approximate_distinct = True
    level = hierarchy.level_for(graph, record, table, "PRODUCT_CODE", path)
    assert level.kind == "walk" and "(estimated)" in level.reason
    assert "kept as a walk level" in level.reason
    held.approximate_distinct = False
    level = hierarchy.level_for(graph, record, table, "PRODUCT_CODE", path)
    assert level.kind == "narrow" and "estimated" not in level.reason
    held.distinct_count, held.approximate_distinct = bound * 3, True
    assert hierarchy.level_for(graph, record, table, "PRODUCT_CODE", path).kind == "narrow"


def test_a_level_left_out_for_want_of_a_profile_is_noted():
    read = _landscape("bank")
    product = read.model.table("dbo.PRODUCT")
    product.primary_key = []
    product.unique_constraints = []
    read.profile.columns = [c for c in read.profile.columns
                            if (c.table, c.column) != ("dbo.PRODUCT", "PRODUCT_CODE")]
    held = plan.propose(read)
    assert "dbo.PRODUCT.PRODUCT_CODE" not in {lv.id for lv in held.levels("child")}
    assert any("dbo.PRODUCT.PRODUCT_CODE was not profiled" in note for note in held.notes)


# --- composite keys: checked only when the source can be read -------------

def _composite_model() -> PhysicalModel:
    def column(name, ordinal, pk=None):
        return Column(name=name, ordinal=ordinal, type_name="int", is_primary_key=pk is not None,
                      primary_key_ordinal=pk)

    return PhysicalModel(tables=[
        Table(name="dbo.SPAN", schema_name="dbo", table_name="SPAN", row_count=40,
              primary_key=["OWNER_NO", "SPAN_NO"],
              columns=[column("OWNER_NO", 1, 1), column("SPAN_NO", 2, 2)]),
        Table(name="dbo.USE", schema_name="dbo", table_name="USE", row_count=400,
              primary_key=["USE_NO"],
              columns=[column("USE_NO", 1, 1), column("OWNER_NO", 2), column("SPAN_NO", 3)]),
    ])


class _Answering(ConnectionRegistry):
    def __init__(self, answer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.answer = answer
        self.sent: list[str] = []

    def query(self, name, sql, params=(), *, timeout=0):
        self.sent.append(sql)
        return [self.answer]


class _FakeSource(sources.SourceRegistry, _Answering):
    pass


def _fake(answer) -> _FakeSource:
    registry = _FakeSource(answer, reads_production=True)
    registry.register_source(*sources.source_settings("SDMT_FAKE_SRC"))
    return registry


def test_composite_candidates_are_left_unchecked_without_a_registry():
    read = Landscape("SDMT_FAKE_SRC", _composite_model(), ProfileReport(), {})
    graph = KeyGraph.from_landscape(read)
    assert [(e.child_table, e.parent_table, e.child_columns) for e in graph.unchecked] == [
        ("dbo.USE", "dbo.SPAN", ["OWNER_NO", "SPAN_NO"])]
    assert graph.edges == [] and graph.confirmed == []
    assert graph.notes and "not checked" in graph.notes[0]


def test_composite_candidates_are_checked_through_source_read():
    read = Landscape("SDMT_FAKE_SRC", _composite_model(), ProfileReport(), {})
    found = _fake({"distinct_values": 12, "missing": 0})
    graph = KeyGraph.from_landscape(read, registry=found)
    (edge,) = graph.edges
    assert (edge.child_table, edge.parent_table, edge.kind, edge.coverage) == (
        "dbo.USE", "dbo.SPAN", "inferred", 1.0)
    (sent,) = found.sent
    source_read.refuse_unless_read(sent)
    assert "[OWNER_NO]" in sent and "[SPAN_NO]" in sent and "[dbo].[SPAN]" in sent
    # A pair whose tuples are mostly missing is not a key.
    missing = _fake({"distinct_values": 12, "missing": 6})
    assert KeyGraph.from_landscape(read, registry=missing).edges == []
    # A plain registry is refused: every statement goes through the source check.
    with pytest.raises(TypeError):
        KeyGraph.from_landscape(read, registry=ConnectionRegistry())
    # Confirmed once, the key is kept in the plan and needs no second read.
    again = KeyGraph.from_landscape(read, confirmed=graph.confirmed)
    assert len(again.edges) == 1 and again.unchecked == []


class _Raising(ConnectionRegistry):
    """Raises the driver error it was given for every statement that
    reaches it (after the source registry's own check)."""

    def __init__(self, error, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error = error
        self.sent: list[str] = []

    def query(self, name, sql, params=(), *, timeout=0):
        self.sent.append(sql)
        raise self.error


class _Refusing(sources.SourceRegistry, _Raising):
    pass


def _refusing(error) -> _Refusing:
    registry = _Refusing(error, reads_production=True)
    registry.register_source(*sources.source_settings("SDMT_FAKE_SRC"))
    return registry


def test_a_check_the_source_refuses_is_noted_with_its_tables():
    import pyodbc

    read = Landscape("SDMT_FAKE_SRC", _composite_model(), ProfileReport(), {})
    refusing = _refusing(pyodbc.ProgrammingError("42000", "[42000] cannot compare these"))
    held = plan.propose(read, registry=refusing)
    assert refusing.sent, "the check was sent"
    noted = [n for n in held.notes if n.startswith("Not checked, the source refused it")]
    assert noted and "dbo.USE" in noted[0] and "dbo.SPAN" in noted[0]
    # The driver's own words can quote a source value: only its code is kept.
    assert "42000" in noted[0] and "cannot compare" not in " ".join(held.notes)
    assert held.joins["found"] == []


def test_a_lost_connection_ends_the_proposal():
    import pyodbc

    read = Landscape("SDMT_FAKE_SRC", _composite_model(), ProfileReport(), {})
    lost = _refusing(pyodbc.OperationalError("08S01", "[08S01] Communication link failure"))
    with pytest.raises(pyodbc.OperationalError):
        plan.propose(read, registry=lost)


def test_checks_past_the_cap_are_noted_and_never_sent():
    read = Landscape("SDMT_FAKE_SRC", _composite_model(), ProfileReport(), {})
    found = _fake({"distinct_values": 12, "missing": 0})
    checks = graph_module.SourceChecks(found, limit=0)
    graph = KeyGraph.from_landscape(read, registry=found, checks=checks)
    assert found.sent == [] and graph.edges == []
    assert [e.parent_table for e in graph.unchecked] == ["dbo.SPAN"]
    (note,) = checks.notes()
    assert "limit of 0" in note and "dbo.USE" in note
    assert graph_module.MAX_SOURCE_CHECKS > 0


def test_composite_sql_reads_only():
    sql = graph_module.COMPOSITE_INCLUSION_SQL.format(
        first="[A]", checked=5, child_columns="[A] AS v0", child_source="[dbo].[T]",
        not_null="[A] IS NOT NULL", parent_table="[dbo].[P]", on="p.[A] = c.v0", hint="")
    source_read.refuse_unless_read(sql)


# --- the commands ---------------------------------------------------------

def test_the_commands_propose_edit_and_show(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sdg.cli.accelerate import accelerate

    (tmp_path / "model.json").write_text((FIXTURES / "bank-model.json").read_text("utf-8"),
                                         encoding="utf-8")
    (tmp_path / "profile.json").write_text((FIXTURES / "bank-profile.json").read_text("utf-8"),
                                           encoding="utf-8")
    (tmp_path / "landscape.json").write_text('{"source": "BANK"}', encoding="utf-8")
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    monkeypatch.setattr(sources, "registry_for",
                        lambda name: pytest.fail("--offline must not open the source"))
    runner = CliRunner()

    done = runner.invoke(accelerate, ["propose", "BANK", "--offline"])
    assert done.exit_code == 0, done.output
    assert "wrote" in done.output and (tmp_path / "proposal.json").exists()
    assert "dbo.PRODUCT" in _table_names(plan.load(tmp_path))
    # A second proposal would lose the first one's choices: refused unless --force.
    first = (tmp_path / "proposal.json").read_text(encoding="utf-8")
    done = runner.invoke(accelerate, ["propose", "BANK", "--offline"])
    assert done.exit_code == 1 and "--force" in done.output
    assert (tmp_path / "proposal.json").read_text(encoding="utf-8") == first
    done = runner.invoke(accelerate, ["propose", "BANK", "--offline", "--force"])
    assert done.exit_code == 0, done.output

    done = runner.invoke(accelerate, ["deselect", "BANK", "child", "dbo.PRODUCT.PRODUCT_CODE"])
    assert done.exit_code == 0, done.output
    assert "dbo.PRODUCT" not in _table_names(plan.load(tmp_path))

    # The child's parent record is never one of its levels (hierarchy.level_refusal).
    done = runner.invoke(accelerate, ["swap", "BANK", "child", "dbo.PRODUCT.PRODUCT_CODE",
                                      "dbo.CUSTOMER", "SEGMENT"])
    assert done.exit_code == 1 and "primary record" in done.output
    for args in (["select", "BANK", "child", "dbo.PRODUCT.PRODUCT_CODE"],
                 ["reorder", "BANK", "child", "dbo.PRODUCT.PRODUCT_CODE", "dbo.BRANCH.BRANCH_ID"],
                 # A level swapped for itself: read again from the landscape.
                 ["swap", "BANK", "child", "dbo.PRODUCT.PRODUCT_TYPE", "dbo.PRODUCT",
                  "PRODUCT_TYPE"],
                 ["name", "BANK", "primary", "customer", "customers"],
                 ["choose-record", "BANK", "child", "dbo.ACCOUNT"],
                 ["show", "BANK"]):
        done = runner.invoke(accelerate, args)
        assert done.exit_code == 0, (args, done.output)

    done = runner.invoke(accelerate, ["select", "BANK", "child", "dbo.NOT.A_LEVEL"])
    assert done.exit_code == 1 and "no level" in done.output


def _bank_folder(tmp_path, monkeypatch):
    (tmp_path / "model.json").write_text((FIXTURES / "bank-model.json").read_text("utf-8"),
                                         encoding="utf-8")
    (tmp_path / "profile.json").write_text((FIXTURES / "bank-profile.json").read_text("utf-8"),
                                           encoding="utf-8")
    (tmp_path / "landscape.json").write_text('{"source": "BANK"}', encoding="utf-8")
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    return tmp_path


def test_a_proposal_older_than_the_landscape_is_refused(tmp_path, monkeypatch):
    """Re-reading the landscape leaves the proposal made from the old read:
    edits and show refuse it until it is proposed again."""
    from typer.testing import CliRunner

    from sdg.cli.accelerate import accelerate

    folder = _bank_folder(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(accelerate, ["propose", "BANK", "--offline"]).exit_code == 0
    assert runner.invoke(accelerate, ["show", "BANK"]).exit_code == 0
    profile = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    profile["profiled"] = "2099-01-01T00:00:00+00:00"
    (folder / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    kept = (folder / "proposal.json").read_text(encoding="utf-8")
    for args in (["show", "BANK"], ["deselect", "BANK", "child", "dbo.PRODUCT.PRODUCT_CODE"],
                 ["name", "BANK", "primary", "customer", "customers"]):
        done = runner.invoke(accelerate, args)
        assert done.exit_code == 1, (args, done.output)
        assert "run propose --force" in " ".join(done.output.split()), done.output
    assert (folder / "proposal.json").read_text(encoding="utf-8") == kept
    assert runner.invoke(accelerate, ["propose", "BANK", "--offline", "--force"]).exit_code == 0
    assert runner.invoke(accelerate, ["show", "BANK"]).exit_code == 0


def test_a_database_error_is_shown_by_its_codes_only(monkeypatch):
    import pyodbc
    from typer.testing import CliRunner

    from sdg.accelerator import landscape as reading
    from sdg.cli.accelerate import accelerate

    secret = "Pat Q Example"
    error = pyodbc.DataError(
        "22018", f"[22018] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Conversion "
                 f"failed when converting the varchar value '{secret}' to data type int. (245) "
                 f"(SQLExecDirectW)")

    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(reading, "read", failing)
    done = CliRunner().invoke(accelerate, ["landscape", "SDMT_FAKE_SRC"])
    assert done.exit_code == 1
    assert "22018" in done.output and "(245)" in done.output
    assert secret not in done.output and "Conversion" not in done.output
    assert source_read.error_said(error) == "DataError 22018 (245)"


def test_checks_past_the_time_limit_are_noted_and_never_sent():
    read = Landscape("SDMT_FAKE_SRC", _composite_model(), ProfileReport(), {})
    found = _fake({"distinct_values": 12, "missing": 0})
    ticks = iter([0.0, 400.0])
    checks = graph_module.SourceChecks(found, clock=lambda: next(ticks))
    graph = KeyGraph.from_landscape(read, registry=found, checks=checks)
    assert found.sent == [] and graph.edges == []
    (note,) = checks.notes()
    assert "time limit of 300 seconds" in note and "dbo.USE" in note
    assert graph_module.CHECK_SECONDS == 300
    with pytest.raises(ValueError):
        plan.propose(read, registry=found, check_seconds=0)


def test_each_check_has_its_own_time_and_lock_limits():
    seen = []

    class Recording(sources.SourceRegistry, _Answering):
        def query(self, name, sql, params=(), *, timeout=0):
            seen.append((timeout, self.session_settings(name)))
            return super().query(name, sql, params, timeout=timeout)

    registry = Recording({"distinct_values": 12, "missing": 0}, reads_production=True)
    registry.register_source(*sources.source_settings("SDMT_FAKE_SRC"))
    read = Landscape("SDMT_FAKE_SRC", _composite_model(), ProfileReport(), {})
    KeyGraph.from_landscape(read, registry=registry)
    table = _composite_model().table("dbo.USE")
    source_read.pair_counts(registry, table, "OWNER_NO", "SPAN_NO")
    lock = (f"SET LOCK_TIMEOUT {source_read.CHECK_LOCK_TIMEOUT_MS}",)
    assert seen == [(source_read.CHECK_TIMEOUT, lock)] * 2
    # Outside a check, no lock limit is set on the connection.
    assert registry.session_settings(sources.SOURCE) == ()

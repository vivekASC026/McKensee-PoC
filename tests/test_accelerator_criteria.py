"""Extraction criteria: proposed from the recorded fixtures, rendered, measured.

Fast and read from tests/fixtures/accelerator/ only: no database. A share is
measured through a real SourceChecks over a fake registry that answers every
statement, so the statement itself is checked as a read.

Names no source column of the health demo: columns are found by what the
graph says of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdg.accelerator import criteria, plan, source_read
from sdg.accelerator.criteria import Criterion
from sdg.accelerator.graph import SourceChecks, name_tokens
from sdg.accelerator.hierarchy import TENURE_TOKENS
from sdg.accelerator.landscape import END_TOKENS, Landscape
from sdg.discovery.model import Column, PhysicalModel
from sdg.profiling.model import ProfileReport

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
SOURCES = ["facets_qa", "bank"]

_LANDSCAPES: dict[str, Landscape] = {}
_PROPOSED: dict[str, dict] = {}


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


def _by_kind(held: plan.SourcePlan, role: str, kind: str) -> list[Criterion]:
    return [c for c in held.criteria[role] if c.kind == kind]


# --- proposals ------------------------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_every_proposed_criterion_names_the_model_and_kept_values_only(source):
    held = _proposed(source)
    graph = _graph(source, held)
    assert held.all_criteria(), "nothing proposed"
    for criterion in held.all_criteria():
        criteria.check(criterion, graph)
        assert criterion.reason
        assert criterion.share_kept is None
        if criterion.kind == "values":
            kept = {str(t.value) for t in
                    (graph.profile_of(criterion.table, criterion.columns[0]).top_values or [])}
            # From the scrubbed profile's kept top values only.
            assert set(criterion.values) <= kept
            assert not criterion.selected


def test_the_health_demo_proposes_in_force_values_and_a_window():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    record = held.record_kinds["primary"]["table"]
    (in_force,) = [c for c in _by_kind(held, "primary", "in_force") if c.table == record]
    assert in_force.selected
    start, end = in_force.columns
    assert graph.table(record).column(start).is_date and graph.table(record).column(end).is_date
    # The latest start is preferred over "the first" start.
    assert not name_tokens(start) & {"ORIG", "ORIGINAL"}
    assert "no end" in in_force.reason or "placeholder" in in_force.reason
    # A bridge's in-force criterion is offered, not selected.
    assert all(not c.selected for c in held.all_criteria()
               if c.kind == "in_force" and c.table != record and c.role == "primary")

    (values,) = _by_kind(held, "primary", "values")
    top = graph.profile_of(record, values.columns[0]).top_values
    assert values.values == [str(max(top, key=lambda t: t.count).value)]

    child = held.record_kinds["child"]["table"]
    (window,) = _by_kind(held, "child", "window")
    assert window.selected and window.months == criteria.WINDOW_MONTHS
    column = window.columns[0]
    assert graph.table(child).column(column).is_date
    assert not name_tokens(column) & (END_TOKENS | TENURE_TOKENS)
    assert graph.profile_of(child, column).null_rate <= criteria.WINDOW_NULL_MAX


def test_the_bank_has_no_date_pair_and_a_status_without_kept_values():
    held = _proposed("bank")
    assert not [c for c in held.all_criteria() if c.kind == "in_force"]
    # The only dated column of the child is the start of its life (its
    # tenure's input): no window.
    assert not _by_kind(held, "child", "window")
    (values,) = _by_kind(held, "child", "values")
    assert values.values == [] and not values.selected
    assert "type the values" in values.reason
    graph = _graph("bank", held)
    with pytest.raises(ValueError, match="set them first"):
        plan.select_criterion(held, values.id, True, graph=graph)
    # What the person types is taken, checked as a literal, then selected.
    held = plan.set_criterion(held, values.id, values=["A", "A", "O'K"], graph=graph)
    held = plan.select_criterion(held, values.id, True, graph=graph)
    (values,) = _by_kind(held, "child", "values")
    assert values.values == ["A", "O'K"] and values.selected
    assert "'O''K'" in criteria.where_sql(held.all_criteria(),
                                           {values.table: "t"}, graph)


def test_person_choices_survive_every_edit():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    (window,) = _by_kind(held, "child", "window")
    held = plan.set_criterion(held, window.id, months=12, graph=graph)
    held = plan.select_criterion(held, window.id, False, graph=graph)
    held = plan.name_record(held, "child", "case", "cases", graph=graph)
    level = next(lv for lv in held.levels("primary") if lv.selected)
    held = plan.select_level(held, "primary", level.id, False, graph=graph)
    (window,) = _by_kind(held, "child", "window")
    assert window.months == 12 and not window.selected
    assert held.status == "draft"


def test_set_criterion_refuses_what_does_not_fit():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    (window,) = _by_kind(held, "child", "window")
    (values,) = _by_kind(held, "primary", "values")
    with pytest.raises(ValueError, match="no criterion"):
        plan.set_criterion(held, "child:window:dbo.NOPE.X", months=3, graph=graph)
    with pytest.raises(ValueError, match="not a values"):
        plan.set_criterion(held, window.id, values=["A"], graph=graph)
    with pytest.raises(ValueError, match="not a window"):
        plan.set_criterion(held, values.id, months=3, graph=graph)
    with pytest.raises(ValueError, match="1 to"):
        plan.set_criterion(held, window.id, months=0, graph=graph)
    with pytest.raises(ValueError, match="in or not_in"):
        plan.set_criterion(held, values.id, op="like", graph=graph)
    with pytest.raises(ValueError, match="at most 60"):
        plan.set_criterion(held, values.id, values=["x" * 61], graph=graph)
    with pytest.raises(ValueError, match="control"):
        plan.set_criterion(held, values.id, values=["A\nB"], graph=graph)
    with pytest.raises(ValueError, match="nothing to set"):
        plan.set_criterion(held, values.id, graph=graph)


# --- literals and identifiers ----------------------------------------------------

def _column(name: str, type_name: str) -> Column:
    return Column(name=name, ordinal=1, type_name=type_name)


def test_literals_are_numbers_or_quoted_text():
    assert criteria.literal("42", _column("N", "int")) == "42"
    assert criteria.literal("-3.50", _column("D", "decimal")) == "-3.50"
    # A char or varchar column is compared as stored; an n-type takes N'...'.
    assert criteria.literal("O'Brien", _column("S", "varchar")) == "'O''Brien'"
    assert criteria.literal("Zoë", _column("S", "nchar")) == "N'Zoë'"
    with pytest.raises(ValueError, match="plain letters"):
        criteria.literal("Zoë", _column("S", "varchar"))
    assert criteria.literal("'; DROP TABLE t --", _column("S", "nvarchar")) == \
        "N'''; DROP TABLE t --'"
    assert criteria.literal("12", _column("S", "char")) == "'12'"
    for bad, column in (("4x", _column("N", "int")), ("1.5", _column("N", "bigint")),
                        ("1e5", _column("D", "decimal")), ("", _column("S", "varchar")),
                        ("a" * 61, _column("S", "varchar")), ("a\x00b", _column("S", "varchar")),
                        ("a\tb", _column("S", "varchar")), ("a b", _column("S", "varchar"))):
        with pytest.raises(ValueError):
            criteria.literal(bad, column)
    assert criteria.literal("a" * 60, _column("S", "nvarchar")) == "N'" + "a" * 60 + "'"


def test_where_sql_renders_each_kind_and_refuses_unknown_names():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    record = held.record_kinds["primary"]["table"]
    (in_force,) = [c for c in _by_kind(held, "primary", "in_force") if c.table == record]
    start, end = in_force.columns
    assert criteria.where_sql([in_force], {record: "p"}, graph) == (
        f"(p.[{start}] < DATEADD(day, 1, CAST(GETDATE() AS date)) AND "
        f"(p.[{end}] IS NULL OR p.[{end}] >= CAST(GETDATE() AS date)))")
    (window,) = _by_kind(held, "child", "window")
    assert criteria.where_sql([window], {window.table: "c"}, graph) == (
        f"(c.[{window.columns[0]}] >= DATEADD(month, -24, CAST(GETDATE() AS date)))")
    (values,) = _by_kind(held, "primary", "values")
    values.selected = True
    values.values = ["AC", "X'Y"]
    assert criteria.where_sql([values], {record: "p"}, graph) == (
        f"(p.[{values.columns[0]}] IN ('AC', 'X''Y'))")
    values.op = "not_in"
    assert criteria.where_sql([values], {record: "p"}, graph) == (
        f"((p.[{values.columns[0]}] IS NULL OR p.[{values.columns[0]}] NOT IN ('AC', 'X''Y')))")
    # Joined by AND; an unselected one is left out; nothing selected is empty.
    both = criteria.where_sql([in_force, values], {record: "p"}, graph)
    assert both == (criteria.where_sql([in_force], {record: "p"}, graph) + " AND "
                    + criteria.where_sql([values], {record: "p"}, graph))
    values.selected = False
    assert criteria.where_sql([values], {record: "p"}, graph) == ""
    with pytest.raises(ValueError, match="not in the statement"):
        criteria.where_sql([in_force], {}, graph)
    with pytest.raises(ValueError, match="plain alias"):
        criteria.where_sql([in_force], {record: "p; DROP"}, graph)
    # A column or a table the model does not hold is refused, whatever the
    # saved plan says.
    for wrong in (Criterion("x", "primary", record, "in_force", [start, "NOPE"], selected=True),
                  Criterion("x", "primary", "dbo.NOPE", "window", ["D"], months=3, selected=True),
                  Criterion("x", "primary", record, "window", ["[x]; DROP"], months=3,
                            selected=True)):
        with pytest.raises(ValueError, match="has no column|has no table"):
            criteria.where_sql([wrong], {wrong.table: "p"}, graph)
    # A window must read a date, a values criterion one column.
    not_date = next(c.name for c in graph.table(record).columns if not c.is_date)
    with pytest.raises(ValueError, match="date"):
        criteria.check(Criterion("x", "primary", record, "window", [not_date], months=3), graph)


# --- measuring --------------------------------------------------------------------

class _Answering:
    """A registry that answers every statement with one row."""

    def __init__(self, answer):
        self.answer = answer
        self.sent: list[tuple[str, int]] = []

    def query(self, name, sql, params=(), *, timeout=0):
        self.sent.append((sql, timeout))
        return [self.answer]


def test_measure_counts_through_the_checks_over_the_sample():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    registry = _Answering({"rows_all": 200, "rows_kept": 50})
    checks = SourceChecks(registry)
    found = held.all_criteria()
    notes = criteria.measure(found, graph, checks)
    assert checks.sent == len(registry.sent) == len(found)
    for sql, timeout in registry.sent:
        source_read.refuse_unless_read(sql)
        assert sql.startswith("SELECT COUNT_BIG(*) AS rows_all, COUNT_BIG(CASE WHEN ")
        assert timeout == source_read.CHECK_TIMEOUT
    for criterion in found:
        assert criterion.share_kept == 0.25
    # A share read from a large table's sample is said to be an estimate.
    sampled = {c.id for c in found if source_read.is_sampled(graph.table(c.table))}
    assert sampled, "no criterion on a sampled table"
    assert {n.split(": ", 1)[0].split(", ", 1)[1] for n in notes} == sampled
    assert all(n.startswith(criteria.SAMPLED) and "estimate" in n for n in notes)
    # A large table is counted on its fixed sample.
    (window,) = _by_kind(held, "child", "window")
    table = graph.table(window.table)
    if source_read.is_sampled(table):
        assert any("TABLESAMPLE" in sql and f"[{window.columns[0]}]" in sql
                   for sql, _ in registry.sent)


def test_a_check_not_run_leaves_the_share_empty_with_a_note():
    held = _proposed("bank")
    graph = _graph("bank", held)
    (values,) = _by_kind(held, "child", "values")
    values.share_kept = 0.5
    registry = _Answering({"rows_all": 10, "rows_kept": 10})
    notes = criteria.measure([values], graph, SourceChecks(registry))
    # A values criterion with no value cannot be written: nothing is sent.
    assert registry.sent == [] and values.share_kept is None
    assert notes and notes[0].startswith(criteria.NOT_MEASURED) and values.id in notes[0]

    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    found = held.all_criteria()
    checks = SourceChecks(registry, limit=1)
    notes = criteria.measure(found, graph, checks)
    assert len(registry.sent) == 1
    assert found[0].share_kept == 1.0
    assert all(c.share_kept is None for c in found[1:])
    missed = [n for n in notes if n.startswith(criteria.NOT_MEASURED)]
    assert len(missed) == len(found) - 1 and checks.skipped
    # An empty sample says so rather than dividing by nothing.
    empty = _Answering({"rows_all": 0, "rows_kept": 0})
    notes = criteria.measure(found[:1], graph, SourceChecks(empty))
    assert found[0].share_kept is None and "no rows" in notes[0]


def test_a_hand_edited_criterion_is_refused_not_crashed():
    held = _proposed("facets_qa")
    graph = _graph("facets_qa", held)
    (window,) = _by_kind(held, "child", "window")
    with pytest.raises(ValueError, match="this version reads"):
        Criterion.from_dict({**window.to_dict(), "sql": "DROP"})
    with pytest.raises(ValueError, match="true or false"):
        Criterion.from_dict({**window.to_dict(), "selected": "yes"})
    window.months = True
    with pytest.raises(ValueError, match="whole number"):
        criteria.check(window, graph)

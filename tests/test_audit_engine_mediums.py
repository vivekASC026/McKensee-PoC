"""The audit's Medium findings in the engine, plan, profiling, discovery,
landscape and pack modules. Pure Python: no database is opened."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pyodbc
import pytest


# --- expressions: resource exhaustion and raw errors -----------------------

def _evaluate(source: str):
    from sdg.engine.expressions import compile_expression

    return compile_expression(source).evaluate(lambda name: None, lambda f, t: None)


@pytest.mark.parametrize("source", [
    "((9**64)**64)**64",
    "'a' * 999999999999",
    "999999999999 * 'a'",
])
def test_an_expression_cannot_build_an_enormous_value(source):
    from sdg.engine.expressions import ExpressionError

    with pytest.raises(ExpressionError):
        _evaluate(source)


def test_an_expression_too_long_or_too_deep_is_refused():
    from sdg.engine.expressions import ExpressionError, MAX_SOURCE

    with pytest.raises(ExpressionError, match="most allowed"):
        _evaluate("1+" * MAX_SOURCE + "1")
    with pytest.raises(ExpressionError):
        _evaluate("(" * 300 + "1" + ")" * 300)


@pytest.mark.parametrize("source", ["-'x'", "'a' < 1", "1 in 5", "int(1e400)"])
def test_type_and_overflow_errors_are_expression_errors(source):
    from sdg.engine.expressions import ExpressionError

    with pytest.raises(ExpressionError, match=r"failed in"):
        _evaluate(source)


def test_ordinary_expressions_still_work():
    assert _evaluate("2 ** 10") == 1024
    assert _evaluate("'ab' * 3") == "ababab"
    assert _evaluate("(3 ** 4) ** 2") == 6561


# --- pattern: unbounded quantifiers ----------------------------------------

@pytest.mark.parametrize("regex", [
    r"\d{1000000000}",
    r"x{0,999999999}",
    r"(\d{1000}){1000}",
    r"\d{1000}" * 20,
    "(" * 400 + "a" + ")" * 400,
    "a" * 1001,
])
def test_a_format_that_would_hang_is_refused(regex):
    from sdg.engine.pattern import CompiledPattern, PatternError

    with pytest.raises(PatternError):
        CompiledPattern(regex)


def test_ordinary_formats_still_compile():
    from sdg.engine.pattern import CompiledPattern

    assert CompiledPattern(r"[A-Z]{3}\d{6}").space == 26**3 * 10**6
    assert CompiledPattern(r"\d{1000}").widest() == 1000


# --- editing: form booleans and falsy required values ----------------------

@pytest.mark.parametrize("sent, kept", [
    ("false", False), ("0", False), ("no", False), (False, False), (0, False),
    ("true", True), ("1", True), ("Yes", True), (True, True),
])
def test_a_form_boolean_reads_as_said(sent, kept):
    from sdg.plan.editing import coerce

    assert coerce("pattern", {"regex": r"\d", "unique": sent})["unique"] is kept


def test_a_form_boolean_that_is_not_one_is_refused():
    from sdg.plan.editing import coerce

    with pytest.raises(ValueError):
        coerce("pattern", {"regex": r"\d", "unique": "maybe"})


def test_a_required_zero_is_not_missing():
    from sdg.plan.editing import check

    assert not any("needed" in p for p in check("sequence", {"start": 0}))
    assert not any("needed" in p for p in check("constant", {"value": 0}))
    assert any("needed" in p for p in check("constant", {"value": ""}))


# --- build: the primary source is not added twice --------------------------

def test_the_primary_source_is_not_duplicated():
    from sdg.plan.build import _sources

    inputs = SimpleNamespace(
        model=SimpleNamespace(connection={
            "name": "source", "server": "localhost", "database": "SRC", "driver": "x"}),
        reference_sets={"t": SimpleNamespace(connection="source"),
                        "u": SimpleNamespace(connection="other")},
    )
    names = [s.name for s in _sources(inputs)]
    assert names == ["source", "other"]


# --- patterns: an unreadable band is a PatternError -----------------------

def test_an_infinite_band_is_a_pattern_error():
    from sdg.plan.patterns import PatternError, PatternParameter, _band

    with pytest.raises(PatternError):
        _band(PatternParameter(label="Age", min="inf", max="inf"))


# --- catalog and packs: names, not paths -----------------------------------

@pytest.mark.parametrize("name", ["../secrets", "..\\x", "/etc/passwd", "C:/x", ".hidden"])
def test_a_dictionary_name_cannot_be_a_path(name):
    from sdg.catalog.loader import CatalogError, load_dictionary

    with pytest.raises(CatalogError, match="not a plain file name"):
        load_dictionary(name)


@pytest.mark.parametrize("pack_id", ["../health", "..", "health/../banking", "C:\\x"])
def test_a_pack_id_cannot_be_a_path(pack_id):
    from sdg import packs

    with pytest.raises(packs.PackError, match="is not a pack id"):
        packs.folder_for(pack_id)


# --- profiling: collation names and one unreadable table -------------------

def test_a_collation_that_is_not_a_name_is_left_out():
    from sdg.discovery.model import Column
    from sdg.profiling.sample import collate_as

    value = Column(name="v", ordinal=1, type_name="varchar", collation="Latin1_General_CI_AS")
    good = Column(name="c", ordinal=1, type_name="varchar", collation="SQL_Latin1_General_CP1_CI_AS")
    bad = Column(name="c", ordinal=1, type_name="varchar", collation="x; DROP TABLE t--")
    assert collate_as(good, value) == " COLLATE SQL_Latin1_General_CP1_CI_AS"
    assert collate_as(bad, value) == ""


def test_one_unreadable_table_does_not_lose_the_profile(monkeypatch):
    from sdg.discovery.model import PhysicalModel, Table
    from sdg.profiling import run
    from sdg.profiling.run import _TableResult

    def one(registry, model, table, *args):
        if table.name == "dbo.B":
            raise pyodbc.ProgrammingError("42000", "[42000] [Microsoft][ODBC Driver 18 for "
                                          "SQL Server][SQL Server]Permission denied. (229)")
        return _TableResult(seconds=1.0)

    monkeypatch.setattr(run, "_profile_one", one)
    monkeypatch.setattr(run, "profile_ratios", lambda *a, **k: [])
    monkeypatch.setattr(run, "withhold_personal_values", lambda *a, **k: None)
    registry = SimpleNamespace(ref=lambda name: SimpleNamespace(as_dict=lambda: {"name": name}))
    model = PhysicalModel(tables=[Table(name=f"dbo.{n}", schema_name="dbo", table_name=n)
                                  for n in "ABC"])
    report = run.profile(registry, model, workers=2, max_minutes=0, catalog=object())
    assert set(report.table_seconds) == {"dbo.A", "dbo.B", "dbo.C"}
    assert [(s.table, s.analysis) for s in report.skipped] == [("dbo.B", "profile")]
    # The SQLSTATE is kept, never the driver text, which can quote a value.
    assert "42000" in report.skipped[0].reason
    assert "Permission denied" not in report.skipped[0].reason


def test_a_bug_while_profiling_is_not_hidden(monkeypatch):
    from sdg.discovery.model import PhysicalModel, Table
    from sdg.profiling import run

    def one(*args):
        raise KeyError("a bug, not an unreadable table")

    monkeypatch.setattr(run, "_profile_one", one)
    registry = SimpleNamespace(ref=lambda name: SimpleNamespace(as_dict=lambda: {"name": name}))
    model = PhysicalModel(tables=[Table(name="dbo.A", schema_name="dbo", table_name="A")])
    with pytest.raises(KeyError):
        run.profile(registry, model, workers=1, max_minutes=0, catalog=object())


# --- discovery: an empty parent is not a code table ------------------------

def test_a_child_of_an_empty_parent_is_a_reference():
    from sdg.discovery.classify import classify_column
    from sdg.discovery.model import Column, ForeignKey, PhysicalModel, Table

    column = Column(name="P_ID", ordinal=1, type_name="int")
    child = Table(name="dbo.C", schema_name="dbo", table_name="C", columns=[column],
                  foreign_keys=[ForeignKey(columns=["P_ID"], parent_table="dbo.P",
                                           parent_columns=["ID"])])
    for rows, bucket in ((0, "reference"), (7, "code"), (500, "reference")):
        parent = Table(name="dbo.P", schema_name="dbo", table_name="P", row_count=rows)
        model = PhysicalModel(tables=[child, parent])
        assert classify_column(child, column, model)[0] == bucket


# --- starts: quoted names, and an outage is not "no table" -----------------

class _Cursor:
    def __init__(self, error, seen):
        self.error, self.seen = error, seen

    def execute(self, sql):
        self.seen.append(sql)
        if self.error:
            raise self.error
        return self

    def fetchone(self):
        return (41,)


def _registry(error=None, seen=None):
    seen = [] if seen is None else seen

    @contextlib.contextmanager
    def connect(name):
        yield SimpleNamespace(cursor=lambda: _Cursor(error, seen))

    return SimpleNamespace(connect=connect)


def test_a_key_start_query_quotes_its_names():
    from sdg.engine.starts import _highest

    seen: list[str] = []
    found = _highest(_registry(seen=seen), "target", [("dbo.T", "[dbo].[T]]x]", "ID]")])
    assert found == {("dbo.T", "ID]"): 41}
    assert seen == ["SELECT MAX(TRY_CAST([ID]]] AS BIGINT)) FROM [dbo].[T]]x]"]


def test_a_missing_table_leaves_the_planned_start():
    from sdg.engine.starts import _highest

    missing = pyodbc.ProgrammingError("42S02", "Invalid object name")
    assert _highest(_registry(missing), "target", [("dbo.T", "[dbo].[T]", "ID")]) == {}


def test_an_outage_stops_the_run_rather_than_reuse_keys():
    from sdg.engine.starts import _highest

    gone = pyodbc.OperationalError("08S01", "Communication link failure")
    with pytest.raises(RuntimeError, match="stopped before writing"):
        _highest(_registry(gone), "target", [("dbo.T", "[dbo].[T]", "ID")])


def test_a_plan_table_name_is_quoted_by_part():
    from sdg.engine.starts import _qualified

    assert _qualified(SimpleNamespace(schema_name="dbo", table_name="T", name="dbo.T")) \
        == "[dbo].[T]"
    assert _qualified(SimpleNamespace(schema_name=None, table_name=None, name="dbo.T")) \
        == "[dbo].[T]"


# --- rules: the combination cache cannot hand one group's values to another -

def test_a_reused_group_id_does_not_return_stale_combinations(monkeypatch):
    import sdg.plan.combinations as combinations
    from sdg.engine import rules

    monkeypatch.setattr(rules, "_COMBINATIONS", {})
    first = SimpleNamespace(elements=["A", "B"], combinations=[(("1", "2"), 3)])
    second = SimpleNamespace(elements=["A", "B"], combinations=[(("8", "9"), 5)])
    monkeypatch.setattr(combinations, "for_patterns", lambda: [first])
    assert rules._combination("combo:A+B", "A")["values"] == [{"A": "1", "B": "2"}]
    # Same id, a different group: what an id reused after collection looks like.
    rules._COMBINATIONS[(id(second), "all")] = rules._COMBINATIONS.pop((id(first), "all"))
    monkeypatch.setattr(combinations, "for_patterns", lambda: [second])
    assert rules._combination("combo:A+B", "A")["values"] == [{"A": "8", "B": "9"}]


# --- landscape: names are quoted -------------------------------------------

def test_landscape_names_are_quoted():
    from sdg.landscape.derive import _q, _t

    assert _q("a]b") == "[a]]b]"
    assert _t("dbo.My Table") == "[dbo].[My Table]"


# --- validator: a band upside down blocks ----------------------------------

def test_reversed_bounds_block_the_plan():
    from sdg.plan.model import PlanField
    from sdg.plan.validator import _check_field

    table = SimpleNamespace(name="dbo.T")
    number = PlanField(name="AMT", bucket="free", generator="number",
                       params={"distribution": "uniform", "min": 10, "max": 1}, provenance="user")
    dates = PlanField(name="DT", bucket="free", generator="date_relative",
                      params={"anchor": "2026-01-01", "min_days": 5, "max_days": -5},
                      provenance="user")
    fine = PlanField(name="OK", bucket="free", generator="number",
                     params={"distribution": "uniform", "min": 1, "max": 10}, provenance="user")
    assert [w.code for w in _check_field(table, number, 10)] == ["bounds_reversed"]
    assert [w.code for w in _check_field(table, dates, 10)] == ["bounds_reversed"]
    assert _check_field(table, fine, 10) == []


# --- plan schema: caps and connection parts --------------------------------

def test_the_plan_schema_caps_formats_and_connection_parts():
    import re

    from sdg.plan.model import plan_schema

    schema = plan_schema()
    source = schema["$defs"]["source"]["properties"]
    assert not re.search(source["server"]["pattern"], "x;Trusted_Connection=yes")
    assert re.search(source["server"]["pattern"], "localhost,1433")
    assert not re.search(source["path"]["pattern"], "../outside.csv")
    assert re.search(source["path"]["pattern"], "data/members.csv")

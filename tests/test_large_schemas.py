"""A database of thousands of tables reads in minutes, and one awkward pair
does not stop it.

Facets has some 2,500 tables. Finding which columns might point at which keys
set every column beside every key of every other table -- minutes of work
before a single query -- and then checked each pair one after another. And
Facets keeps some text columns in a binary collation, beside others in the
database's default: SQL Server refuses to compare the two as they stand, and
that one refusal ended the whole read.
"""

from __future__ import annotations

import time

import pyodbc
import pytest

from sdg.discovery import inference
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.inference import Candidate, candidates, infer
from sdg.discovery.metadata import extract
from sdg.discovery.model import Column, PhysicalModel, Table, UniqueConstraint
from sdg.profiling import sample

BIN = "Latin1_General_BIN"
CI = "SQL_Latin1_General_CP1_CI_AS"


def _wide_model(n_tables: int) -> PhysicalModel:
    """Facets-shaped: each table keyed by its own *_CK, pointing at a few of
    the first sixty, with a spread of ordinary columns."""
    prefixes = [f"T{i:04d}" for i in range(n_tables)]
    tables = []
    for i, p in enumerate(prefixes):
        columns = [Column(name=f"{p}_CK", ordinal=1, type_name="int")]
        for hub in prefixes[(i * 7) % 60:(i * 7) % 60 + 6]:
            if hub != p:
                columns.append(Column(name=f"{hub}_CK", ordinal=len(columns) + 1, type_name="int"))
        for k in range(15):
            columns.append(Column(name=f"{p}_COL{k}", ordinal=len(columns) + 1, type_name="char",
                                  max_length=10))
        table = Table(name=f"dbo.{p}", schema_name="dbo", table_name=p, row_count=1000,
                      columns=columns, primary_key=[f"{p}_CK"])
        table.unique_constraints.append(
            UniqueConstraint(name=f"PK_{p}", columns=[f"{p}_CK"], is_primary_key=True))
        tables.append(table)
    return PhysicalModel(connection={}, discovered="now", tables=tables)


def _every_pair(model: PhysicalModel) -> list[tuple]:
    """The pairs by the plain rule: every column beside every other table's key."""
    found = []
    for child in model.tables:
        for column in child.columns:
            tokens = set(inference.tokenize(column.name))
            for parent in model.tables:
                if parent is child or parent.row_count < inference.MIN_PARENT_ROWS:
                    continue
                for key in inference._key_columns(parent):
                    shared = (tokens & set(inference.tokenize(key))) - inference.GENERIC_TOKENS
                    if shared and inference._comparable(column, parent.column(key)):
                        found.append((child.name, column.name, parent.name, key, sorted(shared)))
    return found


def test_the_pairs_worth_checking_are_found_without_comparing_every_column_to_every_key():
    model = _wide_model(300)
    assert [
        (c.child_table, c.child_column, c.parent_table, c.parent_column, c.shared_tokens)
        for c in candidates(model)
    ] == _every_pair(model)

    started = time.perf_counter()
    assert len(candidates(_wide_model(2_500))) > 10_000
    assert time.perf_counter() - started < 10


def test_text_of_two_collations_is_compared_in_the_keys_own():
    key = Column(name="K", ordinal=1, type_name="char", max_length=4, collation=BIN)
    value = Column(name="K", ordinal=1, type_name="char", max_length=4, collation=CI)
    assert sample.collate_as(key, value) == f" COLLATE {BIN}"
    assert sample.collate_as(key, key) == ""
    assert sample.collate_as(Column(name="N", ordinal=1, type_name="int"), value) == ""


@pytest.fixture(scope="module")
def two_collations():
    """In tempdb: a parent keyed in a binary collation, and a child holding the
    same keys in the default one, as Facets has them."""
    registry = ConnectionRegistry()
    registry.register("scratch", "tempdb")
    setup = f"""
        IF OBJECT_ID('dbo.SDMT_COLL_CHILD') IS NOT NULL DROP TABLE dbo.SDMT_COLL_CHILD;
        IF OBJECT_ID('dbo.SDMT_COLL_PARENT') IS NOT NULL DROP TABLE dbo.SDMT_COLL_PARENT;
        CREATE TABLE dbo.SDMT_COLL_PARENT (GRGR_ID char(8) COLLATE {BIN} NOT NULL PRIMARY KEY,
                                           TOTAL int NOT NULL);
        CREATE TABLE dbo.SDMT_COLL_CHILD (ROW_ID int NOT NULL PRIMARY KEY,
                                          GRGR_ID char(8) COLLATE {CI} NULL);
        INSERT dbo.SDMT_COLL_PARENT VALUES ('G001', 1), ('G002', 5), ('G003', 4);
        INSERT dbo.SDMT_COLL_CHILD VALUES (1, 'G001'), (2, 'G002'), (3, 'G002'), (4, 'G003');
    """
    with registry.connect("scratch", autocommit=True) as conn:
        conn.cursor().execute(setup)
    try:
        yield registry
    finally:
        with registry.connect("scratch", autocommit=True) as conn:
            conn.cursor().execute(
                "DROP TABLE dbo.SDMT_COLL_CHILD; DROP TABLE dbo.SDMT_COLL_PARENT;")


@pytest.mark.db
def test_a_key_held_in_two_collations_is_still_found(two_collations):
    model = extract(two_collations, "scratch", include_schemas=["dbo"])
    parent = model.table("dbo.SDMT_COLL_PARENT")
    child = model.table("dbo.SDMT_COLL_CHILD")
    assert parent.column("GRGR_ID").collation == BIN
    assert child.column("GRGR_ID").collation == CI

    # Only these two: tempdb may hold other people's tables.
    model.tables = [child, parent]
    found = infer(two_collations, model, "scratch")
    assert [(k.child_table, k.child_column, k.parent_table, k.coverage) for k in found] == [
        ("dbo.SDMT_COLL_CHILD", "GRGR_ID", "dbo.SDMT_COLL_PARENT", 1.0)]


@pytest.mark.db
def test_a_pair_the_database_refuses_is_listed_and_the_rest_still_checked(two_collations):
    model = extract(two_collations, "scratch", include_schemas=["dbo"])
    model.tables = [model.table("dbo.SDMT_COLL_CHILD"), model.table("dbo.SDMT_COLL_PARENT")]
    gone = Candidate(child_table="dbo.SDMT_NO_SUCH_TABLE", child_column="GRGR_ID",
                     parent_table="dbo.SDMT_COLL_PARENT", parent_column="GRGR_ID",
                     shared_tokens=["GRGR"])
    real = candidates(model)

    untested: list = []
    original = inference.candidates
    inference.candidates = lambda m: [gone, *real]
    try:
        found = infer(two_collations, model, "scratch", untested=untested)
    finally:
        inference.candidates = original
    assert [k.child_table for k in found] == ["dbo.SDMT_COLL_CHILD"]
    assert [c.child_table for c, _ in untested] == ["dbo.SDMT_NO_SUCH_TABLE"]
    with pytest.raises(Exception):
        inference.test_candidate(two_collations, "scratch", gone, model)


@pytest.mark.db
def test_the_later_steps_compare_that_key_too(two_collations):
    from sdg.discovery.inference import attach
    from sdg.plan.detect import sum_sql
    from sdg.profiling.ratio import profile_ratios

    model = extract(two_collations, "scratch", include_schemas=["dbo"])
    model.tables = [model.table("dbo.SDMT_COLL_CHILD"), model.table("dbo.SDMT_COLL_PARENT")]
    attach(model, infer(two_collations, model, "scratch"))
    ratios = profile_ratios(two_collations, model, "scratch")
    assert [(r.child_table, r.parents, r.children) for r in ratios] == [
        ("dbo.SDMT_COLL_CHILD", 3, 4)]

    child, parent = model.tables
    rows = two_collations.query("scratch", sum_sql(
        parent, "GRGR_ID", ["TOTAL"], child, "GRGR_ID", ["ROW_ID"], sample_rows=100))
    # Each parent's total is the sum of its children's ROW_ID, matched across collations.
    assert (rows[0]["rows_compared"], rows[0]["m_0_0"]) == (3, 3)


# ---------------------------------------------------------------------------
# Profiling thousands of tables
# ---------------------------------------------------------------------------

class _Counting:
    """A registry that answers nothing and counts what it was asked."""

    def __init__(self):
        self.asked: list[str] = []

    def query(self, name, sql, params=(), **_):
        self.asked.append(sql)
        return []

    def ref(self, name):
        return ConnectionRegistry().ref("source")


def _code_table(rows: int) -> Table:
    columns = [Column(name="ID", ordinal=1, type_name="int"),
               Column(name="STS", ordinal=2, type_name="char", max_length=2),
               Column(name="EFF_DT", ordinal=3, type_name="datetime")]
    return Table(name="dbo.T", schema_name="dbo", table_name="T", row_count=rows,
                 columns=columns, primary_key=["ID"])


def test_an_empty_table_is_profiled_without_asking_the_database():
    from sdg.profiling.columns import profile_table
    from sdg.profiling.run import profile

    counting = _Counting()
    profiles = profile_table(counting, _code_table(0), "source")
    assert counting.asked == []
    assert [(p.column, p.row_count, p.null_count, p.distinct_count) for p in profiles] == [
        ("ID", 0, 0, 0), ("STS", 0, 0, 0), ("EFF_DT", 0, 0, 0)]
    # As the measuring query would have said: a date with no time in it.
    assert profiles[2].has_time_component is False

    model = PhysicalModel(connection={}, discovered="now", tables=[_code_table(0)])
    report = profile(counting, model, "source", workers=1)
    assert counting.asked == [] and report.skipped == []


def test_column_measures_of_a_table_of_millions_come_from_a_sample(monkeypatch):
    from sdg.profiling.columns import profile_table

    monkeypatch.setenv("SDG_WHOLE_READ_MAX_ROWS", "50000000")
    counting = _Counting()
    profile_table(counting, _code_table(3_000_000), "source")
    assert "TABLESAMPLE" in counting.asked[0]

    counting = _Counting()
    profile_table(counting, _code_table(1_500_000), "source")
    assert "TABLESAMPLE" not in counting.asked[0]

    # 0 still means every table is read whole.
    monkeypatch.setenv("SDG_WHOLE_READ_MAX_ROWS", "0")
    counting = _Counting()
    profile_table(counting, _code_table(3_000_000), "source")
    assert "TABLESAMPLE" not in counting.asked[0]


def test_past_the_time_limit_a_table_gets_its_column_measures_only(monkeypatch):
    from sdg.config import profile_max_minutes
    from sdg.profiling import run

    monkeypatch.setenv("SDG_PROFILE_MAX_MINUTES", "")
    assert profile_max_minutes() == 60
    monkeypatch.setenv("SDG_PROFILE_MAX_MINUTES", "0")
    assert profile_max_minutes() == 0

    model = PhysicalModel(connection={}, discovered="now", tables=[_code_table(500)])
    counting = _Counting()
    report = run.profile(counting, model, "source", max_minutes=1e-9, workers=1)
    assert len(report.columns) == 3
    assert [(s.table, s.analysis) for s in report.skipped] == [("dbo.T", "pairwise")]
    assert "SDG_PROFILE_MAX_MINUTES" in report.skipped[0].reason


@pytest.fixture()
def backup_table(two_collations):
    """A table whose name starts with a digit, as a dated backup's does."""
    with two_collations.connect("scratch", autocommit=True) as conn:
        conn.cursor().execute(
            "IF OBJECT_ID('dbo.[20140208_SDMT_BKUP]') IS NOT NULL DROP TABLE dbo.[20140208_SDMT_BKUP];"
            f"CREATE TABLE dbo.[20140208_SDMT_BKUP] ([GRGR ID] char(8) COLLATE {BIN} NULL);"
            "INSERT dbo.[20140208_SDMT_BKUP] VALUES ('G001'), ('G003');")
    try:
        yield two_collations
    finally:
        with two_collations.connect("scratch", autocommit=True) as conn:
            conn.cursor().execute("DROP TABLE dbo.[20140208_SDMT_BKUP];")


@pytest.mark.db
def test_a_table_named_like_a_dated_backup_is_checked(backup_table):
    model = extract(backup_table, "scratch", include_schemas=["dbo"])
    backup, parent = model.table("dbo.20140208_SDMT_BKUP"), model.table("dbo.SDMT_COLL_PARENT")
    pair = Candidate(child_table=backup.name, child_column="GRGR ID",
                     parent_table=parent.name, parent_column="GRGR_ID", shared_tokens=["GRGR"])
    for known in (model, None):
        found = inference.test_candidate(backup_table, "scratch", pair, known)
        assert (found.distinct_values, found.missing) == (2, 0)


# ---------------------------------------------------------------------------
# Rules for tens of thousands of columns
# ---------------------------------------------------------------------------

def test_lookups_follow_their_lists_and_find_the_first_match():
    from sdg.profiling.model import ColumnProfile, ProfileReport

    report = ProfileReport(columns=[ColumnProfile(table="T", column="A", row_count=1),
                                    ColumnProfile(table="T", column="A", row_count=2)])
    assert report.column("T", "A").row_count == 1
    report.columns.append(ColumnProfile(table="T", column="B", row_count=3))
    assert report.column("T", "B").row_count == 3
    report.columns = [ColumnProfile(table="T", column="A", row_count=4)]
    assert report.column("T", "A").row_count == 4 and report.column("T", "B") is None

    model = _wide_model(3)
    assert model.table("dbo.T0001").name == "dbo.T0001"
    model.tables = model.tables[:1]
    assert model.table("dbo.T0001") is None
    # An index is kept beside a model, never in it: equal models stay equal.
    assert _wide_model(3) == _wide_model(3).model_copy() and _wide_model(2).table("dbo.T0000")


def test_rules_for_a_database_of_thousands_of_tables_take_seconds():
    from sdg.discovery.classify import classify
    from sdg.plan.proposer import propose
    from sdg.profiling.model import ColumnProfile, ProfileReport

    model = _wide_model(2_500)
    classify(model)
    report = ProfileReport(columns=[
        ColumnProfile(table=t.name, column=c.name, row_count=1000, distinct_count=10)
        for t in model.tables for c in t.columns])
    started = time.perf_counter()
    rules = propose(model, report)
    assert len(rules) == len(report.columns) > 50_000
    assert time.perf_counter() - started < 15


def test_sums_are_measured_once_per_read_even_across_restarts(tmp_path, monkeypatch):
    """Every page that shows rules or the review count asks for the rules. On
    thousands of tables, measuring which totals are sums is thousands of
    queries: done when the structure is read, kept beside it, not redone for
    each page -- nor after the tool is started again."""
    import os

    from sdg.api import service as service_module
    from sdg.api.service import Service
    from sdg.plan.detect import SumExpression

    measured = []

    def detect(registry, model):
        measured.append(model)
        return [SumExpression("dbo.H", "TOTAL", "dbo.L", "AMT", 10, 10)]

    monkeypatch.setattr(service_module, "detect_sum_expressions", detect)
    first = Service(work_dir=tmp_path)
    model = _wide_model(3)
    first.path("model").write_text(model.model_dump_json(), encoding="utf-8")

    sums = first._measured_sums(model)
    assert first._measured_sums(model) == sums
    assert Service(work_dir=tmp_path)._measured_sums(model) == sums
    assert len(measured) == 1

    # The structure read again: measured again.
    stamp = first.path("model").stat().st_mtime_ns
    os.utime(first.path("model"), ns=(stamp + 10**9, stamp + 10**9))
    assert Service(work_dir=tmp_path)._measured_sums(model) == sums
    assert len(measured) == 2


def test_the_status_counts_the_saved_rules_and_never_reads_the_source(tmp_path, monkeypatch):
    """The page asks for the status when it opens. Counting what waits for a
    decision by proposing the rules again read the source -- for hours, at a
    database of thousands of tables -- and the page stayed blank."""
    import json
    import os

    from sdg.api import service as service_module
    from sdg.api.service import Service
    from sdg.discovery.classify import classify
    from sdg.mapping.scorer import MappingStore
    from sdg.plan.proposer import Proposal

    def no_reads(*_, **__):
        raise AssertionError("the status read the source")

    monkeypatch.setattr(service_module, "detect_sum_expressions", no_reads)
    work = Service(work_dir=tmp_path)
    model = _wide_model(3)
    classify(model)
    work.path("model").write_text(model.model_dump_json(), encoding="utf-8")
    work.path("mappings").write_text(MappingStore(mappings=[]).model_dump_json(), encoding="utf-8")
    table = model.tables[0]
    rules = [Proposal(table=table.name, column=c.name, bucket=c.bucket or "attribute",
                      generator="pattern", confidence="low") for c in table.columns]
    work.path("proposals").write_text(json.dumps([r.as_dict() for r in rules]), encoding="utf-8")

    assert work.queued_count() >= 1
    # Rules older than the structure read since are not counted.
    stamp = work.path("model").stat().st_mtime_ns
    os.utime(work.path("proposals"), ns=(stamp - 10**9, stamp - 10**9))
    assert work.queued_count() is None


def test_a_slow_sum_check_is_given_up_and_the_step_has_a_time_limit():
    from sdg.discovery.model import ForeignKey
    from sdg.plan import detect

    def amount(name):
        return Column(name=name, type_name="decimal", precision=12, scale=2,
                      nullable=True, ordinal=2)

    tables = []
    for i in range(20):
        parent = Table(name=f"dbo.H{i}", schema_name="dbo", table_name=f"H{i}", row_count=1_000_000,
                       primary_key=["K"], columns=[
                           Column(name="K", type_name="int", nullable=False, ordinal=1,
                                  is_primary_key=True), amount("TOTAL")])
        child = Table(name=f"dbo.L{i}", schema_name="dbo", table_name=f"L{i}", row_count=5_000_000,
                      columns=[Column(name="K", type_name="int", nullable=False, ordinal=1),
                               amount("AMT")],
                      foreign_keys=[ForeignKey(name=None, columns=["K"], parent_table=f"dbo.H{i}",
                                               parent_columns=["K"])])
        tables += [parent, child]
    model = PhysicalModel(connection={}, discovered="now", tables=tables)

    class Slow:
        def __init__(self):
            self.timeouts = []

        def query(self, name, sql, params=(), *, timeout=0):
            self.timeouts.append(timeout)
            raise pyodbc.OperationalError("HYT00", "Query timeout expired")

    slow = Slow()
    assert detect.detect_sum_expressions(slow, model) == []
    assert slow.timeouts and set(slow.timeouts) == {detect.SUM_QUERY_SECONDS}

    spent = Slow()
    assert detect.detect_sum_expressions(spent, model, max_minutes=1e-9) == []
    assert spent.timeouts == []

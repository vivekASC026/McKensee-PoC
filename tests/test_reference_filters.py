"""A reference set's filter never runs as SQL.

Plans are files people edit and share. A filter is read as conditions on
columns: the column is quoted, every value is a query parameter, and anything
that is not a plain condition is refused -- when the plan or catalog is read,
and again before any query.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sdg.plan.filters import FilterError, parse_filter
from sdg.plan.refsets import load_reference_set

INJECTIONS = [
    "ACTIVE_FLAG = 'Y' OR 1 = 1",
    "ACTIVE_FLAG = 'Y'; DROP TABLE dbo.REF_PLAN_CODE",
    "ACTIVE_FLAG = 'Y' --",
    "ACTIVE_FLAG = (SELECT TOP 1 PLAN_CD FROM dbo.REF_PLAN_CODE)",
    "ACTIVE_FLAG = PLAN_CD",
    "1 = 1",
    "ACTIVE_FLAG = 'Y",
    "ACTIVE_FLAG = 'Y' AND",
    "[ACTIVE_FLAG] = 'Y'",
]


def test_the_shipped_filter_reads_as_one_condition():
    assert parse_filter("ACTIVE_FLAG = 'Y'") == ("[ACTIVE_FLAG] = ?", ["Y"])


def test_conditions_join_with_and_and_every_value_is_a_parameter():
    condition, params = parse_filter(
        "PLAN_CD IN ('P1', 'O''NEIL') and TERM_DT is null AND RANK >= -2.5 AND STS != N'TM' "
        "AND KIND NOT IN (1, 2) AND END_DT IS NOT NULL")
    assert condition == ("[PLAN_CD] IN (?, ?) AND [TERM_DT] IS NULL AND [RANK] >= ? "
                         "AND [STS] <> ? AND [KIND] NOT IN (?, ?) AND [END_DT] IS NOT NULL")
    assert params == ["P1", "O'NEIL", -2.5, "TM", 1, 2]


@pytest.mark.parametrize("text", INJECTIONS)
def test_anything_but_plain_conditions_is_refused(text):
    with pytest.raises(FilterError, match="filter"):
        parse_filter(text)


class Registry:
    """Records the queries; returns no rows."""

    def __init__(self):
        self.queries = []

    def query(self, connection, sql, params=()):
        self.queries.append((sql, list(params)))
        return []

    def scalar(self, connection, sql):
        self.queries.append((sql, []))
        return 0


def test_the_query_binds_the_value():
    registry = Registry()
    load_reference_set(registry, table="dbo.REF_PLAN_CODE", connection="ref",
                       source_table="REF_PLAN_CODE", key_column="PLAN_CD",
                       filter="ACTIVE_FLAG = 'Y'")
    sql, params = registry.queries[0]
    assert sql == ("SELECT [PLAN_CD] FROM [dbo].[REF_PLAN_CODE] WHERE [ACTIVE_FLAG] = ? "
                   "ORDER BY [PLAN_CD]")
    assert params == ["Y"]


def test_a_refused_filter_runs_no_query():
    registry = Registry()
    with pytest.raises(FilterError):
        load_reference_set(registry, table="dbo.REF_PLAN_CODE", connection="ref",
                           source_table="REF_PLAN_CODE", key_column="PLAN_CD",
                           filter=INJECTIONS[1])
    assert registry.queries == []


def test_a_plan_or_catalog_holding_one_is_refused_when_read():
    from sdg.catalog.model import ReuseDomain
    from sdg.plan.model import ReuseSource

    with pytest.raises(ValidationError, match="did not expect 'OR'"):
        ReuseSource(connection="ref", table="REF_PLAN_CODE", key_columns=["PLAN_CD"],
                    filter=INJECTIONS[0])
    with pytest.raises(ValidationError, match="cannot read"):
        ReuseDomain(connection="ref", table="REF_PLAN_CODE", key_column="PLAN_CD",
                    filter=INJECTIONS[1])
    kept = ReuseSource(connection="ref", table="REF_PLAN_CODE", key_columns=["PLAN_CD"],
                       filter="ACTIVE_FLAG = 'Y'")
    assert kept.filter == "ACTIVE_FLAG = 'Y'"

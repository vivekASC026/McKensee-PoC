"""The expression language must compute what users need and nothing else."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sdg.engine.expressions import ExpressionError, compile_expression


def evaluate(source: str, row: dict | None = None, aggregates: dict | None = None):
    row = row or {}
    aggregates = aggregates or {}

    def resolve(name):
        if name not in row:
            raise ExpressionError(f"no value for {name}")
        return row[name]

    def aggregate(function, target):
        return aggregates[(function, target)]

    return compile_expression(source).evaluate(resolve, aggregate)


# ---------------------------------------------------------------------------
# What it must do
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source,row,expected", [
    ("1 + 2", {}, 3),
    ("A * B", {"A": 3, "B": 4}, 12),
    ("A - B", {"A": 10, "B": 4}, 6),
    ("A / B", {"A": 10, "B": 4}, 2.5),
    ("A / 0", {"A": 10}, None),
    ("-A", {"A": 5}, -5),
    ("concat(A, '-', B)", {"A": "X", "B": 7}, "X-7"),
    ("coalesce(A, B, 'fallback')", {"A": None, "B": None}, "fallback"),
    ("upper(A)", {"A": "abc"}, "ABC"),
    ("substr(A, 0, 3)", {"A": "ABCDEF"}, "ABC"),
    ("len(A)", {"A": "ABCDE"}, 5),
    ("round(A, 2)", {"A": 3.14159}, 3.14),
    ("A if B > 2 else 0", {"A": 9, "B": 5}, 9),
    ("A if B > 2 else 0", {"A": 9, "B": 1}, 0),
    ("A in ('X', 'Y')", {"A": "X"}, True),
    ("min(A, B)", {"A": 4, "B": 9}, 4),
    ("max(A, B)", {"A": 4, "B": 9}, 9),
])
def test_supported_expressions(source, row, expected):
    assert evaluate(source, row) == expected


def test_dotted_names_reach_parent_fields():
    row = {"dbo.CMC_MEME_MEMBER.MEME_EFF_DT": dt.date(2024, 1, 1)}
    assert evaluate("dbo.CMC_MEME_MEMBER.MEME_EFF_DT", row) == dt.date(2024, 1, 1)


def test_aggregate_over_a_child_table():
    source = "sum(dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT)"
    compiled = compile_expression(source)
    assert compiled.aggregates == {("sum", "dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT")}
    assert compiled.names == set()
    value = evaluate(source, {}, {("sum", "dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT"): Decimal("120.50")})
    assert value == Decimal("120.50")


def test_aggregate_mixed_with_arithmetic():
    source = "sum(dbo.LINE.AMT) * 1.1"
    value = evaluate(source, {}, {("sum", "dbo.LINE.AMT"): 100})
    assert value == pytest.approx(110.0)


def test_decimal_and_float_compare_without_losing_precision():
    assert evaluate("A > 0.5", {"A": Decimal("0.75")}) is True
    assert evaluate("A + B", {"A": Decimal("1.10"), "B": 2.2}) == Decimal("3.30")


def test_dates_can_be_differenced():
    row = {"START": dt.date(2020, 1, 1), "END": dt.date(2024, 7, 1)}
    assert evaluate("days_between(START, END)", row) == 1643
    assert evaluate("years_between(START, END)", row) == 4


def test_null_propagates_through_arithmetic():
    assert evaluate("A + 1", {"A": None}) is None
    assert evaluate("A * B", {"A": None, "B": 3}) is None


def test_names_are_reported_for_dependency_ordering():
    compiled = compile_expression("concat(A, dbo.T.B)")
    assert compiled.names == {"A", "dbo.T.B"}


# ---------------------------------------------------------------------------
# What it must refuse
# ---------------------------------------------------------------------------

BLOCKED = [
    "__import__('os').system('echo hi')",
    "open('/etc/passwd').read()",
    "().__class__.__bases__",
    "A.__class__",
    "[x for x in range(10)]",
    "(lambda: 1)()",
    "eval('1+1')",
    "exec('x=1')",
    "globals()",
    "getattr(A, 'x')",
    "A[0]",
    "{'a': 1}",
    "A := 3",
    "print('x')",
    "compile('1','','eval')",
    "A.b.c()",
    "2 ** 9999",
]


@pytest.mark.parametrize("source", BLOCKED, ids=range(len(BLOCKED)))
def test_dangerous_expressions_are_refused(source):
    with pytest.raises(ExpressionError):
        compile_expression(source).evaluate(lambda n: 1, lambda f, t: 1)


def test_unknown_function_is_refused_at_parse_time():
    with pytest.raises(ExpressionError, match="unknown function"):
        compile_expression("frobnicate(A)")


def test_keyword_arguments_are_refused():
    with pytest.raises(ExpressionError, match="keyword arguments"):
        compile_expression("round(A, digits=2)")


def test_aggregate_needs_a_table_column():
    with pytest.raises(ExpressionError, match="table.column"):
        compile_expression("sum(1 + 2)")


def test_syntax_errors_are_reported_clearly():
    with pytest.raises(ExpressionError, match="cannot parse"):
        compile_expression("A +")

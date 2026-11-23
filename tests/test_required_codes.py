"""A code a claim's type requires is never left blank in what was written.

A professional or institutional claim names a diagnosis; a professional or
dental line names a procedure. A dental claim with no diagnosis and an
institutional line with no procedure are how real claims are billed, so they
pass. Checked against real tables, the way the validator reads a run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sdg.plan.model import PlanField, PlanTable
from sdg.writers.validate import Scope, ValidationReport, _check_required_codes

pytestmark = pytest.mark.db

CLAIMS, LINES = "#sdm_claims", "#sdm_lines"


def _field(name: str, element: str | None = None, **reference) -> PlanField:
    if reference:
        return PlanField(name=name, bucket="reference", generator="reference",
                         params=reference, provenance="user", element=element)
    return PlanField(name=name, bucket="code", generator="lookup",
                     params={"values": ["x"]}, provenance="user", element=element)


CLAIM_TABLE = PlanTable(name=CLAIMS, mode="generate", fields=[
    _field("CLCL_ID"), _field("CLCL_CL_TYPE", "CLAIM_TYPE"),
    _field("CLCL_IDCD_ID", "DIAGNOSIS_CODE")])
LINE_TABLE = PlanTable(name=LINES, mode="generate", fields=[
    _field("CLCL_ID", parent_table=CLAIMS, parent_column="CLCL_ID"),
    _field("CDML_SEQ_NO"), _field("CDML_SVC_CD", "PROCEDURE_CODE")])
PLAN = SimpleNamespace(tables=[CLAIM_TABLE, LINE_TABLE])


def _check(claims, lines):
    """The checks on both tables, for claims (id, type, diagnosis) and lines
    (claim id, number, procedure)."""
    from sdg.api.app import service

    report = ValidationReport()
    with service.registry.connect("source") as conn:
        cursor = conn.cursor()
        cursor.execute(f"CREATE TABLE {CLAIMS} (CLCL_ID int, CLCL_CL_TYPE char(1), "
                       "CLCL_IDCD_ID char(10))")
        cursor.execute(f"CREATE TABLE {LINES} (CLCL_ID int, CDML_SEQ_NO int, "
                       "CDML_SVC_CD char(10))")
        cursor.executemany(f"INSERT INTO {CLAIMS} VALUES (?, ?, ?)", claims)
        cursor.executemany(f"INSERT INTO {LINES} VALUES (?, ?, ?)", lines)
        scope = Scope(conn)
        try:
            scope.stage(CLAIMS, ["CLCL_ID"], [c[0] for c in claims])
            _check_required_codes(report, scope, PLAN, CLAIM_TABLE, None)
            scope.stage(LINES, ["CLCL_ID", "CDML_SEQ_NO"], [(l[0], l[1]) for l in lines])
            _check_required_codes(report, scope, PLAN, LINE_TABLE, None)
        finally:
            scope.drop()
            cursor.execute(f"DROP TABLE {LINES}; DROP TABLE {CLAIMS}")
    return {c.column: c for c in report.checks}


def test_blanks_where_the_claim_type_allows_them_pass():
    checks = _check(
        claims=[(1, "P", "E119000"), (2, "I", "I10X000"), (3, "D", None), (4, "D", "  ")],
        lines=[(1, 1, "99213"), (2, 1, None), (2, 2, ""), (3, 1, "D1110")])
    assert set(checks) == {"CLCL_IDCD_ID", "CDML_SVC_CD"}
    assert all(c.passed for c in checks.values()), [c.detail for c in checks.values()]
    assert checks["CLCL_IDCD_ID"].kind == "required_code"


def test_blanks_a_claim_type_requires_are_counted():
    checks = _check(
        claims=[(1, "P", None), (2, "I", " "), (3, "D", None), (4, "P", "E119000")],
        lines=[(1, 1, None), (2, 1, None), (3, 1, ""), (4, 1, "99213")])
    diagnosis, procedure = checks["CLCL_IDCD_ID"], checks["CDML_SVC_CD"]
    assert not diagnosis.passed and diagnosis.detail.startswith("2 professional and institutional")
    assert not procedure.passed and procedure.detail.startswith("2 professional and dental")

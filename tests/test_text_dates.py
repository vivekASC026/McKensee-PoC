"""A date generated into a text column, written the way the column writes it.

A catalog element such as DOB or SERVICE_DATE makes a date. Sent to a char(8)
column holding 20250828, it reached SQL Server as 2025-08-28 -- ten characters
-- and the load stopped on the first row with "String data, right
truncation", naming no column. The plan now gives such a date the column's
measured form; a load with a value too long for its column is refused before
anything is written, naming the column; and a date anchored on a text-column
date can still read it.
"""

from __future__ import annotations

import pytest

from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.engine.execute import run_plan
from sdg.plan.build import _write_text_dates_as_measured
from sdg.plan.model import Plan, PlanField, PlanTable, new_meta
from sdg.profiling.model import ColumnProfile, ProfileReport
from sdg.writers.sqlserver import TooLong, check_widths


def _model(*columns: Column) -> PhysicalModel:
    return PhysicalModel(tables=[Table(name="dbo.member", schema_name="dbo",
                                       table_name="member", columns=list(columns))])


def _plan(*fields: PlanField) -> Plan:
    return Plan(
        meta=new_meta("text-dates", "seed-one"),
        tables=[PlanTable(name="dbo.member", mode="generate", count=50, level=0,
                          fields=list(fields))],
        generation_order=["dbo.member"],
    )


def _date(name: str, **params) -> PlanField:
    return PlanField(name=name, bucket="free", generator="date_relative",
                     params={"anchor": "1990-01-01", "min_days": 0, "max_days": 9000,
                             **params}, provenance="catalog")


def test_a_date_into_a_text_column_takes_the_columns_measured_form():
    model = _model(
        Column(name="birth_date", ordinal=1, type_name="char", max_length=8),
        Column(name="seen", ordinal=2, type_name="varchar", max_length=10),
        Column(name="wide", ordinal=3, type_name="varchar", max_length=20),
        Column(name="real_date", ordinal=4, type_name="date", max_length=3),
    )
    report = ProfileReport(columns=[
        ColumnProfile(table="dbo.member", column="birth_date",
                      fitted_format={"regex": r"\d{8}", "coverage": 1.0}),
        ColumnProfile(table="dbo.member", column="seen",
                      fitted_format={"regex": r"\d{2}/\d{2}/\d{4}", "coverage": 1.0}),
    ])
    plan = _plan(_date("birth_date"), _date("seen"), _date("wide"), _date("real_date"))
    _write_text_dates_as_measured(plan.tables, model, report)
    formats = {f.name: f.params.get("format") for f in plan.tables[0].fields}
    assert formats == {"birth_date": "%Y%m%d", "seen": "%m/%d/%Y",
                       "wide": "%Y-%m-%d", "real_date": None}

    rows = run_plan(plan).rows("dbo.member")
    assert all(len(r["birth_date"]) == 8 and r["birth_date"].isdigit() for r in rows)
    assert all(r["seen"][2] == "/" and len(r["seen"]) == 10 for r in rows)
    assert all(not isinstance(r["real_date"], str) for r in rows)


def test_an_unmeasured_narrow_text_column_gets_the_compact_form():
    model = _model(Column(name="dob", ordinal=1, type_name="nchar", max_length=16))
    plan = _plan(_date("dob"))
    _write_text_dates_as_measured(plan.tables, model, ProfileReport())
    assert plan.tables[0].fields[0].params["format"] == "%Y%m%d"


def test_a_date_can_be_anchored_on_a_text_column_date():
    plan = _plan(
        _date("birth_date", format="%Y%m%d"),
        PlanField(name="joined", bucket="free", generator="date_relative",
                  params={"anchor": "birth_date", "min_days": 6570, "max_days": 7000},
                  provenance="learned"),
    )
    rows = run_plan(plan).rows("dbo.member")
    from datetime import datetime

    for r in rows:
        born = datetime.strptime(r["birth_date"], "%Y%m%d").date()
        assert 6570 <= (r["joined"] - born).days <= 7000


def test_a_load_with_a_value_too_long_is_refused_naming_the_column():
    model = _model(Column(name="birth_date", ordinal=1, type_name="char", max_length=8))
    plan = _plan(_date("birth_date"))  # no format: dates come out ISO, ten long
    result = run_plan(plan)
    with pytest.raises(TooLong) as refused:
        check_widths(plan, result, model)
    said = str(refused.value)
    assert "Nothing was written" in said and "dbo.member.birth_date" in said
    assert "char(8)" in said and "date_relative" in said

    fitted = _plan(_date("birth_date", format="%Y%m%d"))
    check_widths(fitted, run_plan(fitted), model)

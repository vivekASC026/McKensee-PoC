"""What a reviewer at the customer would call obviously wrong, and is not.

A datetime LAST_UPD_TS matched to LAST_NAME. A group name generated as
sixteen random letters. A status that is always AC generated as two random
letters. A middle initial that is blank in four rows of ten generated in every
row. Ages 0 to 90 generated as a counter running past two hundred thousand.
A term date of 9999-12-31 measured as a gap of three million days. Each is
pinned here.
"""

from __future__ import annotations

from collections import Counter

import pytest

from sdg.catalog.loader import load_catalog
from sdg.discovery.classify import classify_column
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.inference import InferredKey, one_way
from sdg.discovery.metadata import extract
from sdg.discovery.model import Column, ForeignKey, PhysicalModel, Table
from sdg.engine.execute import _placeholder
from sdg.engine.generators.base import RowContext
from sdg.mapping.matchers import candidates_for, format_is_distinctive
from sdg.mapping.scorer import map_columns
from sdg.plan.proposer import propose
from sdg.profiling.checks import check_digits
from sdg.profiling.format import fit_format
from sdg.profiling.model import ColumnProfile, ProfileReport, TopValue
from sdg.profiling.run import profile


def _table(name: str, rows: int, *columns: Column, key: list[str] | None = None) -> Table:
    return Table(name=f"dbo.{name}", schema_name="dbo", table_name=name, row_count=rows,
                 columns=list(columns), primary_key=key or [])


def _column(name: str, type_name: str, *, length: int | None = None, **extra) -> Column:
    return Column(name=name, type_name=type_name, max_length=length, ordinal=1,
                  nullable=extra.pop("nullable", True), **extra)


def _rule(table: Table, column: str, column_profile: ColumnProfile, mappings=None, catalog=None):
    model = PhysicalModel(connection={}, discovered="now", tables=[table])
    report = ProfileReport(columns=[column_profile])
    return next(p for p in propose(model, report, [], mappings, catalog) if p.column == column)


def _profile(table: Table, column: str, **measures) -> ColumnProfile:
    measures.setdefault("row_count", table.row_count)
    return ColumnProfile(table=table.name, column=column, **measures)


# --- matching ---------------------------------------------------------------


def test_a_column_is_never_matched_to_an_element_its_type_cannot_hold():
    catalog = load_catalog()
    table = _table("CMC_MEME_MEMBER", 1000,
                   _column("LAST_UPD_TS", "datetime"), _column("MEME_SFX", "smallint"))
    model = PhysicalModel(connection={}, discovered="now", tables=[table])
    found = {
        c.name: [m.element for m in candidates_for(table, c, catalog, None, model, {})]
        for c in table.columns
    }
    assert "LAST_NAME" not in found["LAST_UPD_TS"]
    assert "MARITAL_STATUS" not in found["MEME_SFX"]


def test_a_shared_word_or_a_shape_alone_is_not_a_match():
    """CLCL_CREATED_TS shares CLCL with an alias of the age element; a subgroup
    id has a product code's shape. Neither is that element."""
    catalog = load_catalog()
    table = _table("CMC_SGSG_SUBGROUP", 19,
                   _column("SGSG_ID", "char", length=8), _column("CLCL_ROW_HASH", "char", length=64))
    model = PhysicalModel(connection={}, discovered="now", tables=[table])
    shaped = _profile(table, "SGSG_ID", distinct_count=19,
                      fitted_format={"regex": "[A-Z]{2}\\d{6}", "coverage": 1.0})
    assert candidates_for(table, table.columns[0], catalog, shaped, model, {}) == []
    assert candidates_for(table, table.columns[1], catalog, None, model, {}) == []
    # The real thing still matches: the name says so.
    member = _table("CMC_MEME_MEMBER", 1000, _column("MEME_LAST_NAME", "varchar", length=30))
    store = map_columns(PhysicalModel(connection={}, discovered="now", tables=[member]), catalog)
    assert store.for_column("dbo.CMC_MEME_MEMBER", "MEME_LAST_NAME").element == "LAST_NAME"


def test_only_a_distinctive_format_counts_toward_a_match():
    assert format_is_distinctive("PRV\\d{9}")
    assert format_is_distinctive("[A-Z]{3}\\d{6}")
    assert not format_is_distinctive("[A-Z]{2}")
    assert not format_is_distinctive("\\d{5}")
    assert not format_is_distinctive("[MDSP]")


# --- keys and roles -----------------------------------------------------------


def test_a_key_the_data_shows_backwards_of_a_declared_one_is_dropped():
    """PDDS_PROD_DESC declares its PDPD_ID a key to PDPD_PRODUCT. The data shows
    PDPD_PRODUCT's own key inside PDDS as well; taking that made the product
    key a reference to its own description, and neither table could load."""
    product = _table("CMC_PDPD_PRODUCT", 25, _column("PDPD_ID", "char", length=8), key=["PDPD_ID"])
    described = _table("CMC_PDDS_PROD_DESC", 25, _column("PDPD_ID", "char", length=8),
                       key=["PDPD_ID"])
    described.foreign_keys.append(ForeignKey(name="FK", columns=["PDPD_ID"],
                                             parent_table="dbo.CMC_PDPD_PRODUCT",
                                             parent_columns=["PDPD_ID"]))
    model = PhysicalModel(connection={}, discovered="now", tables=[product, described])
    backwards = InferredKey("dbo.CMC_PDPD_PRODUCT", "PDPD_ID", "dbo.CMC_PDDS_PROD_DESC", "PDPD_ID",
                            1.0, 25, 0, ["PDPD"])
    assert one_way(model, [backwards]) == []


def test_only_a_number_is_calculated_by_its_name():
    table = _table("CMC_CDML_CL_LINE", 10)
    model = PhysicalModel(connection={}, discovered="now", tables=[table])
    assert classify_column(table, _column("CDML_TOT_AMT", "decimal"), model)[0] == "derivable"
    assert classify_column(table, _column("CDML_CALC_IND", "char", length=1), model)[0] != "derivable"
    assert classify_column(table, _column("AGE_GROUP_CD", "char", length=2), model)[0] != "derivable"


# --- profiling ------------------------------------------------------------------


def test_the_format_of_words_keeps_its_spaces():
    fitted = fit_format(["Acme Manufacturing Inc", "City of Dayton", "Riverside University"])
    assert fitted is not None and " " in fitted.regex


def test_one_value_repeated_passes_no_check_digit_scheme():
    assert check_digits(["26"] * 50) is None


# --- rules ----------------------------------------------------------------------


def test_a_column_that_always_holds_one_value_is_generated_as_that_value():
    table = _table("CMC_SGSG_SUBGROUP", 19, _column("SGSG_STS", "char", length=2))
    rule = _rule(table, "SGSG_STS", _profile(table, "SGSG_STS", distinct_count=1,
                                              cardinality="constant", minimum="AC", maximum="AC"))
    assert (rule.generator, rule.params) == ("constant", {"value": "AC"})


def test_a_column_that_is_always_null_stays_null():
    table = _table("CMC_GRGR_GROUP", 8, _column("GRGR_TERM_DT", "date"))
    rule = _rule(table, "GRGR_TERM_DT", _profile(table, "GRGR_TERM_DT", null_count=8,
                                                  null_rate=1.0, cardinality="empty"))
    assert (rule.generator, rule.params) == ("constant", {"value": None})


def test_a_small_tables_names_are_names_and_its_states_are_states():
    names = ["Riverside University", "Metro Transit Authority", "Summit Retail Group"]
    table = _table("CMC_GRGR_GROUP", 8, _column("GRGR_NAME", "varchar", length=60),
                   _column("GRGR_STATE", "char", length=2))
    observed = _profile(table, "GRGR_NAME", distinct_count=3, cardinality="unique",
                        min_length=13, max_length=23,
                        top_values=[TopValue(value=v, count=1, share=1 / 3) for v in names])
    rule = _rule(table, "GRGR_NAME", observed)
    assert rule.generator == "lookup" and rule.params["values"] == names
    assert not rule.params.get("unique")

    catalog = load_catalog()
    model = PhysicalModel(connection={}, discovered="now", tables=[table])
    mappings = map_columns(model, catalog)
    state = _profile(table, "GRGR_STATE", distinct_count=8, cardinality="unique",
                     fitted_format={"regex": "[A-Z]{2}", "coverage": 1.0, "kind": "literal"})
    rule = _rule(table, "GRGR_STATE", state, mappings, catalog)
    assert rule.generator == "lookup" and rule.params.get("field") == "state"


def test_long_text_with_no_shape_is_written_as_words():
    table = _table("CMC_NOTE", 100_000, _column("NOTE_TEXT", "varchar", length=200))
    rule = _rule(table, "NOTE_TEXT", _profile(table, "NOTE_TEXT", distinct_count=90_000,
                                               cardinality="high", min_length=5, max_length=180))
    assert rule.generator == "pattern" and " " in rule.params["regex"]


def test_ages_that_run_in_steps_are_not_a_counter():
    table = _table("CMC_CLCL_CLAIM", 210_000, _column("CLCL_ME_AGE", "smallint"))
    ages = _profile(table, "CLCL_ME_AGE", distinct_count=91, cardinality="low",
                    minimum=0, maximum=90, mean=45.0, stddev=20.0,
                    sequence={"is_sequence": True, "step": 1, "max": 90, "agreement": 1.0})
    assert _rule(table, "CLCL_ME_AGE", ages).generator != "sequence"


def test_every_rule_keeps_how_often_its_column_is_empty():
    table = _table("CMC_MEME_MEMBER", 1000, _column("MEME_MID_INIT", "char", length=1))
    initials = _profile(table, "MEME_MID_INIT", null_count=400, null_rate=0.4, distinct_count=2,
                        cardinality="low",
                        top_values=[TopValue(value="A", count=300, share=0.5),
                                    TopValue(value="B", count=300, share=0.5)])
    rule = _rule(table, "MEME_MID_INIT", initials)
    assert [e["coverage"] for e in rule.evidence if e["kind"] == "null_rate"] == [0.4]


def test_an_empty_table_is_ruled_by_its_declaration():
    table = _table("CMC_EMPTY", 0, _column("X_ID", "int", nullable=False), key=["X_ID"])
    table.columns[0].is_primary_key = True
    rule = _rule(table, "X_ID", _profile(table, "X_ID", cardinality="empty"))
    assert rule.generator == "sequence"


def test_times_and_placeholder_dates_are_kept():
    table = _table("CMC_MEME_MEMBER", 1000, _column("MEME_TERM_DT", "datetime"))
    term = _profile(table, "MEME_TERM_DT", distinct_count=300, cardinality="high",
                    minimum="2019-01-01T00:00:00", maximum="2025-06-30T00:00:00",
                    has_time_component=True,
                    placeholders=[{"value": "9999-12-31T00:00:00", "count": 800, "share": 0.8}])
    rule = _rule(table, "MEME_TERM_DT", term)
    assert rule.generator == "date_relative" and rule.params["with_time"] is True
    assert rule.params["placeholders"] == [{"value": "9999-12-31", "share": 0.8}]
    assert rule.params["anchor"] == "2019-01-01"


def test_a_placeholder_is_written_in_its_share_of_rows():
    params = {"placeholders": [{"value": "9999-12-31", "share": 0.8}]}
    drawn = Counter(
        str(_placeholder(RowContext(seed="s", scenario="base", table="t", field_name="TERM_DT",
                                    params=params, row_index=i)))
        for i in range(2000))
    assert abs(drawn["9999-12-31"] / 2000 - 0.8) < 0.05
    assert drawn["None"] + drawn["9999-12-31"] == 2000


# --- measured on a database -------------------------------------------------------


@pytest.fixture(scope="module")
def placeholder_dates():
    registry = ConnectionRegistry()
    registry.register("scratch", "tempdb")
    rows = ",".join(
        f"({i}, '2020-01-{(i % 28) + 1:02d}', "
        + ("'9999-12-31'" if i % 5 else f"'2021-02-{(i % 28) + 1:02d}'")
        + f", '2020-01-01 {i % 24:02d}:15:00', '{i % 24:02d}:30:00')"
        for i in range(1, 201))
    setup = f"""
        IF OBJECT_ID('dbo.SDMT_TERMS') IS NOT NULL DROP TABLE dbo.SDMT_TERMS;
        CREATE TABLE dbo.SDMT_TERMS (ROW_ID int NOT NULL PRIMARY KEY, EFF_DT date NOT NULL,
                                     TERM_DT date NOT NULL, UPD_TS datetime NOT NULL,
                                     START_TM time NOT NULL);
        INSERT dbo.SDMT_TERMS VALUES {rows};
    """
    with registry.connect("scratch", autocommit=True) as conn:
        conn.cursor().execute(setup)
    try:
        yield registry
    finally:
        with registry.connect("scratch", autocommit=True) as conn:
            conn.cursor().execute("DROP TABLE dbo.SDMT_TERMS;")


@pytest.mark.db
def test_placeholder_dates_are_counted_apart_from_real_ones(placeholder_dates):
    model = extract(placeholder_dates, "scratch", include_schemas=["dbo"])
    model.tables = [model.table("dbo.SDMT_TERMS")]
    report = profile(placeholder_dates, model, "scratch")
    term = report.column("dbo.SDMT_TERMS", "TERM_DT")
    assert term.maximum.startswith("2021-02")
    assert [(p["value"][:10], p["count"]) for p in term.placeholders] == [("9999-12-31", 160)]
    assert report.column("dbo.SDMT_TERMS", "UPD_TS").has_time_component
    gaps = [o for o in report.orderings if {o.earlier, o.later} == {"EFF_DT", "TERM_DT"}]
    assert gaps and gaps[0].rows_compared == 40 and gaps[0].gap_max < 400
    # A time of day is not a date: SQL Server will not compare it with one.
    assert not [o for o in report.orderings if "START_TM" in (o.earlier, o.later)]
    # And its values are saved as text, as dates are.
    start = report.column("dbo.SDMT_TERMS", "START_TM")
    assert start.top_values and all(isinstance(v.value, str) for v in start.top_values)
    assert isinstance(start.minimum, str)


def test_a_small_run_is_allowed_the_null_rate_chance_gives_it():
    from sdg.writers.validate import NULL_RATE_TOLERANCE, null_rate_tolerance
    # 116 members asked for 40% blank: 46.6% is chance, not a wrong rule.
    assert null_rate_tolerance(0.399, 116) > abs(0.466 - 0.399)
    # A large run is held to the brief's five points.
    assert null_rate_tolerance(0.399, 50_000) == NULL_RATE_TOLERANCE
    assert null_rate_tolerance(0.0, 10) == NULL_RATE_TOLERANCE

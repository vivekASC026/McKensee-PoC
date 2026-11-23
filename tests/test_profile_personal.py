"""A profile never keeps a person's values.

A column whose name matches a catalog element marked `pii: sensitive` keeps
its counts and shape but no value: no smallest, largest or most common, however
few values it holds. A code column keeps its values, and so does a sensitive
code set (SEX): those are categories, and generation needs their shares. A
text column of many values keeps no range whatever the catalog says. All
values below are synthetic.
"""

from __future__ import annotations

from sdg.catalog.loader import load_catalog
from sdg.discovery.model import Column
from sdg.profiling.columns import _withhold_text_ranges
from sdg.profiling.model import ColumnProfile, ProfileReport, TopValue
from sdg.profiling.personal import personal, withhold_personal_values


def _profile(column: str, cardinality: str, **values) -> ColumnProfile:
    return ColumnProfile(table="dbo.CMC_MEME_MEMBER", column=column, row_count=100,
                         distinct_count=100 if cardinality == "unique" else 3,
                         cardinality=cardinality, min_length=4, max_length=9,
                         fitted_format={"regex": "^[A-Z]+$", "coverage": 1.0}, **values)


def _report(*columns: ColumnProfile) -> ProfileReport:
    return ProfileReport(connection={}, profiled="2026-11-11T00:00:00", columns=list(columns))


def test_names_ssns_and_birth_dates_keep_no_values():
    catalog = load_catalog()
    report = _report(
        _profile("MEME_LAST_NAME", "unique", minimum="AAAA", maximum="ZZZZ",
                 top_values=[TopValue(value="SAMPLE", count=1, share=0.01)]),
        _profile("MEME_SSN", "unique", minimum="000000001", maximum="999999998"),
        _profile("MEME_BIRTH_DT", "high", minimum="1930-01-01", maximum="2020-01-01",
                 distribution={"kind": "uniform", "min": 1.0, "max": 2.0, "skew": 0.1}),
    )
    cleared = withhold_personal_values(report, catalog)
    assert cleared == ["dbo.CMC_MEME_MEMBER.MEME_LAST_NAME", "dbo.CMC_MEME_MEMBER.MEME_SSN",
                       "dbo.CMC_MEME_MEMBER.MEME_BIRTH_DT"]
    for column in report.columns:
        assert column.minimum is None and column.maximum is None and not column.top_values
        # The shape stays: it holds no value from the column.
        assert column.row_count == 100 and column.max_length == 9 and column.fitted_format
    assert report.columns[2].distribution == {"kind": "uniform", "skew": 0.1}
    saved = report.model_dump_json()
    for value in ("AAAA", "ZZZZ", "SAMPLE", "000000001", "1930-01-01"):
        assert value not in saved


def test_codes_and_sensitive_categories_keep_their_values():
    catalog = load_catalog()
    top = [TopValue(value="F", count=50, share=0.5), TopValue(value="M", count=50, share=0.5)]
    report = _report(
        _profile("MEME_SEX", "low", minimum="F", maximum="M", top_values=top),
        _profile("MEME_STS", "low", minimum="AC", maximum="TM",
                 top_values=[TopValue(value="AC", count=90, share=0.9)]),
        _profile("MEME_CK", "unique", minimum=1, maximum=100),
    )
    assert withhold_personal_values(report, catalog) == []
    assert report.columns[0].top_values == top and report.columns[0].maximum == "M"
    assert report.columns[1].top_values[0].value == "AC"
    assert report.columns[2].maximum == 100, "a key's highest value is what new keys clear"


def test_an_identifier_is_not_withheld():
    """Keys are marked `identifier`: their range is what generated keys start past."""
    profile = _profile("MEME_ID", "unique", minimum="M000001", maximum="M000100")
    assert personal(profile, load_catalog()) is None


def test_a_small_tables_few_names_are_still_withheld():
    report = _report(_profile("MEME_FIRST_NAME", "low", minimum="ALEX", maximum="SAM",
                              top_values=[TopValue(value="ALEX", count=2, share=0.5)]))
    assert withhold_personal_values(report, load_catalog()) == [
        "dbo.CMC_MEME_MEMBER.MEME_FIRST_NAME"]
    assert report.columns[0].top_values is None and report.columns[0].minimum is None


def test_an_integer_ssns_sequence_keeps_no_bounds():
    report = _report(_profile("MEME_SSN", "unique", minimum=100000001, maximum=899999999,
                              sequence={"is_sequence": False, "step": 7, "agreement": 0.1,
                                        "min": 100000001, "max": 899999999}))
    withhold_personal_values(report, load_catalog())
    assert report.columns[0].sequence == {"is_sequence": False, "step": 7, "agreement": 0.1}


def test_a_text_column_of_many_values_keeps_no_range_and_a_code_keeps_its():
    phone = Column(name="MEME_PHONE", ordinal=1, type_name="varchar", max_length=20)
    status = Column(name="MEME_STS", ordinal=2, type_name="char", max_length=2)
    key = Column(name="MEME_CK", ordinal=3, type_name="int")
    profiles = [_profile("MEME_PHONE", "unique", minimum="5550000001", maximum="5559999999"),
                _profile("MEME_STS", "low", minimum="AC", maximum="TM"),
                _profile("MEME_CK", "unique", minimum=1, maximum=100)]
    _withhold_text_ranges([phone, status, key], profiles)
    assert (profiles[0].minimum, profiles[0].maximum) == (None, None)
    assert profiles[0].max_length == 9, "the shape stays"
    assert (profiles[1].minimum, profiles[1].maximum) == ("AC", "TM")
    assert (profiles[2].minimum, profiles[2].maximum) == (1, 100)


def test_a_key_named_after_a_sensitive_element_keeps_its_highest_value():
    key = _profile("GROUP_NAME_ID", "unique", minimum=1, maximum=500,
                   sequence={"is_sequence": True, "step": 1, "min": 1, "max": 500})
    report = _report(key)
    keys = frozenset({"DBO.CMC_MEME_MEMBER.GROUP_NAME_ID"})  # as key_columns gives them
    assert withhold_personal_values(report, load_catalog(), keys) == []
    assert key.sequence["max"] == 500 and key.maximum == 500


def test_values_that_step_evenly_are_not_a_key():
    """An SSN stored as a number can look like a sequence; only the schema says key."""
    ssn = _profile("MEME_SSN", "unique", minimum=100000001, maximum=100000099,
                   sequence={"is_sequence": True, "step": 1, "min": 100000001,
                             "max": 100000099})
    report = _report(ssn)
    assert withhold_personal_values(report, load_catalog()) == ["dbo.CMC_MEME_MEMBER.MEME_SSN"]
    assert ssn.minimum is None and "max" not in ssn.sequence


def test_a_format_with_literal_characters_is_dropped_from_a_personal_column():
    last, first = _profile("MEME_LAST_NAME", "unique"), _profile("MEME_FIRST_NAME", "unique")
    last.fitted_format = {"regex": "^SAMPLE[A-Z]{2}$", "kind": "literal"}
    first.fitted_format = {"regex": "^[A-Z]{4,9}$", "kind": "ranged"}
    report = _report(last, first)
    withhold_personal_values(report, load_catalog())
    assert report.columns[0].fitted_format is None
    assert report.columns[1].fitted_format == {"regex": "^[A-Z]{4,9}$", "kind": "ranged"}


class _Answering:
    """Answers the profiler's queries for one varchar column of 100 distinct values."""

    VALUES = [f"555{n:07d}" for n in range(100)]  # synthetic

    def query(self, name, sql, params=(), **_):
        if sql.startswith("SELECT COUNT_BIG(*)"):
            return [{"row_count": 100, "nulls_0": 0, "distinct_0": 100, "minlen_0": 10,
                     "maxlen_0": 10, "min_0": self.VALUES[0], "max_0": self.VALUES[-1]}]
        if " AS value" in sql:
            return []
        return [{"CONTACT_PHONE": v} for v in self.VALUES]


def test_profiling_a_text_column_of_many_values_keeps_no_range():
    from sdg.discovery.model import Table
    from sdg.profiling.columns import profile_table

    column = Column(name="CONTACT_PHONE", ordinal=1, type_name="varchar", max_length=20)
    table = Table(name="dbo.CONTACT", schema_name="dbo", table_name="CONTACT", row_count=100,
                  columns=[column])
    (profile,) = profile_table(_Answering(), table, "source", whole_max_rows=10**12)
    assert profile.cardinality == "unique"
    assert profile.minimum is None and profile.maximum is None
    assert profile.max_length == 10, "the shape stays"


def test_the_keys_come_from_the_schema_whatever_the_case():
    from sdg.discovery.model import PhysicalModel, Table
    from sdg.profiling.personal import key_columns

    key = Column(name="GROUP_NAME_ID", ordinal=1, type_name="int", is_primary_key=True)
    name = Column(name="GROUP_NAME", ordinal=2, type_name="varchar", max_length=60)
    table = Table(name="dbo.CMC_GRGR_GROUP", schema_name="dbo", table_name="CMC_GRGR_GROUP",
                  columns=[key, name])
    model = PhysicalModel.model_construct(tables=[table])
    keys = key_columns(model)
    assert keys == frozenset({"DBO.CMC_GRGR_GROUP.GROUP_NAME_ID"})
    profile = ColumnProfile(table="dbo.cmc_grgr_group", column="group_name_id",
                            cardinality="unique", maximum=500)
    assert personal(profile, load_catalog(), keys) is None
    named = ColumnProfile(table="dbo.CMC_GRGR_GROUP", column="GROUP_NAME", cardinality="high")
    assert personal(named, load_catalog(), keys) == "GROUP_NAME"

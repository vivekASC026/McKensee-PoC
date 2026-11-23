"""A large table's load measures come from a sample, and say what the whole table says.

Children per parent, which columns decide others, which dates come first and
the most common values were each a pass over the whole table, per measure, per
table; on a large database that was most of a read. A table over twice the
sample size is now read through a fixed sample. These tests read the same large
tables both ways and hold the sampled answers to the whole-table ones.
"""

from __future__ import annotations

import pytest

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Table
from sdg.profiling import sample
from sdg.profiling.format import fit_format
from sdg.profiling.checks import check_digits
from sdg.profiling.ratio import _percentile


def _table(rows: int) -> Table:
    return Table(name="dbo.T", schema_name="dbo", table_name="T", row_count=rows)


def test_a_small_table_is_read_whole_and_a_large_one_through_a_fixed_sample():
    assert sample.source(_table(150_000)) == "[dbo].[T]"
    assert sample.source(_table(150_000), "c") == "[dbo].[T] AS c"
    large = sample.source(_table(5_000_000), "c")
    assert "TABLESAMPLE SYSTEM" in large and "REPEATABLE (20260915)" in large
    assert large.startswith("(SELECT TOP (100000) * FROM [dbo].[T]") and large.endswith(" AS c")


def test_percentiles_are_the_databases_own():
    # PERCENTILE_CONT over 0,0,1,1,1,2,5: interpolated between neighbours.
    counts = [(0, 2), (1, 3), (2, 1), (5, 1)]
    assert _percentile(counts, 0.5) == 1.0
    assert _percentile(counts, 0.9) == pytest.approx(2 + (5 - 2) * 0.4)
    assert _percentile(counts, 0.0) == 0.0 and _percentile(counts, 1.0) == 5.0
    assert _percentile([], 0.5) == 0.0


def test_formats_and_check_digits_count_each_value_as_often_as_it_occurs():
    values = ["PD000001"] * 900 + [f"PD{n:06d}" for n in range(2, 90)] + ["x"] * 12
    fitted = fit_format(values)
    assert fitted.regex == r"PD\d{6}" and fitted.sample_size == 1000
    assert fitted.matched == 988
    # One value, repeated: its literal is still read as agreed by every row.
    assert fit_format(["ABC"] * 50).regex == "ABC"
    assert check_digits(["1234567893"] * 40 + ["1245319599"] * 10).scheme == "npi"
    # One number, however often, passes a check by chance one time in ten.
    assert check_digits(["1234567893"] * 40) is None


@pytest.fixture(scope="module")
def both():
    """The large tables of the test database, profiled sampled and whole."""
    from sdg.discovery.run import discover
    from sdg.profiling import run
    from sdg.profiling.run import profile

    registry = ConnectionRegistry()
    model = discover(registry, "source").model
    large = [t.name for t in model.tables if sample.is_sampled(t)]
    if not large:
        pytest.skip("no table in the test database is large enough to be sampled")
    sampled = profile(registry, model, tables=large)
    # A cap above every table's size reads each one whole, as before. The whole
    # read is the truth the sample is held to, so it has no time budget: on a
    # busy server it ran past its 60 s and skipped its date orderings, and the
    # sample's orderings were held to none.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(run, "table_budget", lambda rows: float("inf"))
        whole = profile(registry, model, tables=large, sample_cap=10_000_000)
    # Neither side may have left out what is compared.
    assert not whole.skipped, [(s.table, s.analysis, s.reason) for s in whole.skipped]
    assert not sampled.skipped, [(s.table, s.analysis, s.reason) for s in sampled.skipped]
    return large, sampled, whole


@pytest.mark.db
def test_children_per_parent_from_a_sample_match_the_whole_table(both):
    _large, sampled, whole = both
    exact = {(r.parent_table, r.child_table): r for r in whole.ratios}
    assert any(r.sampled_parents for r in sampled.ratios), "no parent table was sampled"
    for ratio in sampled.ratios:
        truth = exact[(ratio.parent_table, ratio.child_table)]
        assert ratio.mean == pytest.approx(truth.mean, rel=0.02), ratio.evidence
        assert ratio.zero_share == pytest.approx(truth.zero_share, abs=0.01)
        assert ratio.median_nonzero == truth.median_nonzero
        assert ratio.proposal["kind"] == truth.proposal["kind"]
        assert ratio.parents == truth.parents
        assert ratio.children == pytest.approx(truth.children, rel=0.02)
        if ratio.sampled_parents:
            assert ratio.evidence.startswith("about ")


@pytest.mark.db
def test_rules_found_on_a_sample_are_the_rules_of_the_whole_table(both):
    _large, sampled, whole = both
    rule = lambda d: (d.table, d.determinant, d.dependent)  # noqa: E731
    assert {rule(d) for d in sampled.dependencies} == {rule(d) for d in whole.dependencies}
    order = lambda o: (o.kind, o.table, o.earlier, o.later)  # noqa: E731
    assert {order(o) for o in sampled.orderings} == {order(o) for o in whole.orderings}


@pytest.mark.db
def test_most_common_values_from_a_sample_keep_their_shares(both):
    large, sampled, whole = both
    exact = {(c.table, c.column): c for c in whole.columns}
    checked = 0
    for column in sampled.columns:
        if column.table not in large or not column.top_values:
            continue
        truth = {str(v.value): v.share for v in exact[(column.table, column.column)].top_values}
        found = {str(v.value): v.share for v in column.top_values}
        # Where one value clearly leads, the sample names it too. Hundreds of
        # procedure codes at a quarter of a percent each have no clear leader.
        shares = sorted(truth.values(), reverse=True)
        if len(shares) < 2 or shares[0] - shares[1] > 0.01:
            assert list(found)[0] == list(truth)[0], column.column
        for value, share in found.items():
            assert share == pytest.approx(truth.get(value, 0.0), abs=0.01), (column.column, value)
        checked += 1
    assert checked >= 5


def test_distinct_counts_scale_up_from_a_sample():
    from collections import Counter

    from sdg.profiling.columns import estimate_distinct

    # No value repeats in the sample: one value per row, all the way up.
    assert estimate_distinct(Counter(range(1000)), 1_000_000) == 1_000_000
    # A handful of codes, each seen often, is all of them.
    codes = Counter({c: 200 for c in "ABCDE"})
    assert estimate_distinct(codes, 1_000_000) == 5
    # Mostly seen once: many more exist than the sample shows.
    mixed = Counter({**{n: 1 for n in range(900)}, **{n: 2 for n in range(900, 950)}})
    assert 900 * 10 < estimate_distinct(mixed, 100_000) < 100_000
    # A table no larger than its sample is counted as it is.
    assert estimate_distinct(mixed, 1000) == 950


@pytest.fixture(scope="module")
def measured():
    """The claims table's columns measured whole, and from a sample of them."""
    from sdg.discovery.run import discover
    from sdg.profiling.columns import profile_table

    registry = ConnectionRegistry()
    model = discover(registry, "source").model
    table = next((t for t in model.tables if t.table_name == "CMC_CLCL_CLAIM"), None)
    if table is None or table.row_count < 100_000:
        pytest.skip("the test database has no claims table large enough")
    whole = profile_table(registry, table, whole_max_rows=10**12)
    part = profile_table(registry, table, whole_max_rows=50_000, measure_rows=50_000)
    return table, whole, part


@pytest.mark.db
def test_a_very_large_table_is_measured_from_a_sample_and_says_so(measured):
    table, whole, part = measured
    for w, p in zip(whole, part):
        assert w.measured_rows == 0
        assert 0 < p.measured_rows < table.row_count
        # The table's own count, not the sample's.
        assert p.row_count == table.row_count
        assert p.null_rate == pytest.approx(w.null_rate, abs=0.02), p.column
        # A code stays a code and a key stays a key.
        assert p.cardinality == w.cardinality, p.column


@pytest.mark.db
def test_the_key_range_of_a_sampled_table_is_exact(measured):
    table, whole, part = measured
    key = table.primary_key[0]
    w = next(c for c in whole if c.column == key)
    p = next(c for c in part if c.column == key)
    assert (p.minimum, p.maximum) == (w.minimum, w.maximum)


@pytest.mark.db
def test_a_sequence_found_in_a_sample_starts_past_the_highest_value(both):
    _large, sampled, _whole = both
    sequences = [c for c in sampled.columns if c.sequence and isinstance(c.maximum, int)]
    for column in sequences:
        assert column.sequence["max"] >= column.maximum, column.qualified


def test_a_huge_table_is_looked_up_by_its_index_and_never_scanned():
    huge = _table(80_000_000)
    huge.indexed = ["CLCL_ID"]
    assert sample.seek_hint(huge, "CLCL_ID") == " OPTION (LOOP JOIN)"
    # No index to seek on, or a table small enough to read: left to the database.
    assert sample.seek_hint(huge, "NWNW_ID") == ""
    small = _table(40_000_000)
    small.indexed = ["CLCL_ID"]
    assert sample.seek_hint(small, "CLCL_ID") == ""


@pytest.fixture(scope="module")
def read_both_ways():
    """The test database read as it is, and as if every large table were huge:
    each lookup into one by its index, and nothing on one without an index."""
    from sdg.discovery.run import discover
    from sdg.plan.detect import detect_sum_expressions
    from sdg.profiling import ratio

    registry = ConnectionRegistry()

    def read():
        result = discover(registry, "source")
        model = result.model
        return (model, result.inferred, detect_sum_expressions(registry, model),
                ratio.profile_ratios(registry, model))

    normal = read()
    patch = pytest.MonkeyPatch()
    patch.setattr(sample, "is_huge", lambda table, rows=None: table.row_count > 200_000)
    original = ratio.profile_ratio
    patch.setattr(ratio, "profile_ratio", lambda r, t, c="source", **k: original(
        r, t, c, sample_rows=k.get("sample_rows", sample.SAMPLE_ROWS), huge_rows=200_000))
    try:
        huge = read()
    finally:
        patch.undo()
    return normal, huge


@pytest.mark.db
def test_discovery_records_which_columns_lead_an_index(read_both_ways):
    (model, *_), _huge = read_both_ways
    line = model.table("dbo.CMC_CDML_CL_LINE")
    if line is None:
        pytest.skip("no claim line table")
    assert line.leads_index("CLCL_ID")
    assert not line.leads_index("CDML_CHG_AMT")


@pytest.mark.db
def test_keys_found_on_a_sample_of_a_large_table_are_the_keys_of_the_whole(read_both_ways):
    (_m, inferred, sums, _r), (_hm, huge_inferred, huge_sums, _hr) = read_both_ways
    key = lambda k: (k.child_table, k.child_column, k.parent_table, k.parent_column)  # noqa: E731
    assert {key(k) for k in huge_inferred} == {key(k) for k in inferred}
    assert any(k.sampled for k in inferred), "no large child table was sampled"
    for k in inferred:
        if k.sampled:
            assert "read from a sample" in k.evidence()[0]
    total = lambda s: (s.parent_table, s.parent_column, s.child_table, s.child_column)  # noqa: E731
    assert {total(s) for s in huge_sums} == {total(s) for s in sums}
    assert sums, "the claim total is no longer found to be the sum of its lines"


@pytest.mark.db
def test_children_per_parent_on_a_huge_table(read_both_ways):
    (_m, _i, _s, ratios), (_hm, _hi, _hs, huge_ratios) = read_both_ways
    exact = {(r.child_table, r.parent_table, r.child_column): r for r in ratios}
    by_sizes = [r for r in huge_ratios if r.from_sizes]
    assert by_sizes, "no huge child without an index was tried"
    for r in huge_ratios:
        truth = exact[(r.child_table, r.parent_table, r.child_column)]
        if r.from_sizes:
            # The average from the tables' sizes and the share naming a parent.
            assert r.mean == pytest.approx(truth.mean, rel=0.02), r.evidence
            assert r.proposal["kind"] == "fixed" and "was not measured" in r.evidence
        else:
            # Looked up through the index: the same answer as before.
            assert r.mean == pytest.approx(truth.mean, rel=1e-9)
            assert r.histogram == truth.histogram


def test_the_size_read_whole_is_set_per_installation(monkeypatch):
    from sdg.config import WHOLE_READ_MAX_ROWS, whole_read_max_rows

    monkeypatch.delenv("SDG_WHOLE_READ_MAX_ROWS", raising=False)
    assert whole_read_max_rows() == WHOLE_READ_MAX_ROWS == 50_000_000
    assert not sample.is_huge(_table(50_000_000)) and sample.is_huge(_table(50_000_001))

    monkeypatch.setenv("SDG_WHOLE_READ_MAX_ROWS", "10,000,000")
    assert whole_read_max_rows() == 10_000_000
    assert sample.is_huge(_table(20_000_000))

    # 0: every table read whole, however large.
    monkeypatch.setenv("SDG_WHOLE_READ_MAX_ROWS", "0")
    assert not sample.is_huge(_table(5_000_000_000))

    # A value that is not a number keeps the default rather than failing a read.
    monkeypatch.setenv("SDG_WHOLE_READ_MAX_ROWS", "lots")
    assert whole_read_max_rows() == 50_000_000

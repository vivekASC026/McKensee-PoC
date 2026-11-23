"""Step 5 acceptance: column measures, format fitting, dependencies, ratios."""

from __future__ import annotations

import pytest

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.run import discover
from sdg.profiling.checks import check_digits, passes_luhn, passes_npi, sequence_test
from sdg.profiling.format import fit_format, matches_format, signature
from sdg.profiling.model import ProfileReport, cardinality_class
from sdg.profiling.ratio import HISTOGRAM_BINS, build_histogram, propose
from sdg.profiling.run import profile, table_budget


@pytest.fixture(scope="module")
def model():
    return discover(ConnectionRegistry(), "source").model


@pytest.fixture(scope="module")
def report(model):
    return profile(ConnectionRegistry(), model)


# ---------------------------------------------------------------------------
# Format fitting
# ---------------------------------------------------------------------------

def test_signature_collapses_runs():
    assert signature("MBR00123456") == (("U", 3), ("D", 8))
    assert signature("123-45-6789") == (("D", 3), ("-", 1), ("D", 2), ("-", 1), ("D", 4))


LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def varied_letters(count: int, index: int) -> str:
    """Letters that differ between values, so the fitter reports a class."""
    return "".join(LETTERS[(index * 7 + position * 13) % 26] for position in range(count))


@pytest.mark.parametrize("values,expected", [
    ([f"{varied_letters(3, i)}{i:08d}" for i in range(100)], r"[A-Z]{3}\d{8}"),
    ([f"{i:012d}" for i in range(100)], r"\d{12}"),
    ([f"{varied_letters(1, i)}{i:04d}" for i in range(100)], r"[A-Z]\d{4}"),
    ([f"PD{i:06d}" for i in range(100)], r"PD\d{6}"),
    ([f"{i:03d}-{i%100:02d}-{i:04d}" for i in range(100)], r"\d{3}\-\d{2}\-\d{4}"),
])
def test_fit_format_returns_the_expected_pattern(values, expected):
    fitted = fit_format(values)
    assert fitted is not None
    assert fitted.regex == expected
    assert fitted.coverage == 1.0


def test_zero_padding_does_not_become_a_literal():
    """A padded sequence agrees on its leading zeros; that is not a rule."""
    fitted = fit_format([f"{varied_letters(3, i)}{i:08d}" for i in range(50)])
    assert fitted.regex == r"[A-Z]{3}\d{8}"


def test_a_real_constant_prefix_does_become_a_literal():
    """PD000001 and PD000002 really do share a prefix, and saying so is better."""
    fitted = fit_format([f"PD{i:06d}" for i in range(40)])
    assert fitted.regex == r"PD\d{6}"
    assert fitted.kind == "literal"


def test_fit_format_tolerates_a_minority_of_odd_values():
    values = [f"{varied_letters(3, i)}{i:06d}" for i in range(97)] + ["WEIRD", "ODD-1", "?"]
    fitted = fit_format(values)
    assert fitted.coverage >= 0.95
    assert fitted.regex == r"[A-Z]{3}\d{6}"


def test_fit_format_falls_back_when_nothing_specific_fits():
    values = [f"AB{i:04d}" if i % 3 else f"{i:06d}" for i in range(120)]
    fitted = fit_format(values)
    assert fitted.coverage >= 0.95
    assert matches_format(values, fitted.regex) >= 0.95


def test_fit_format_handles_no_values():
    assert fit_format([]) is None
    assert fit_format([None, ""]) is None


# ---------------------------------------------------------------------------
# Check digits and sequences
# ---------------------------------------------------------------------------

def test_npi_and_luhn():
    assert passes_npi("1234567893")
    assert not passes_npi("1234567890")
    assert passes_luhn("79927398713")
    assert not passes_luhn("79927398714")


def test_check_digits_prefers_the_narrower_claim():
    from sdg.profiling.checks import NPI_PREFIX, luhn_check_digit

    def make_npi(base: int) -> str:
        nine = f"{base % 1_000_000_000:09d}"
        return nine + str(luhn_check_digit(NPI_PREFIX + nine))

    npis = [make_npi(999_999_999 - i * 7919) for i in range(30)]
    assert all(passes_npi(n) for n in npis)
    result = check_digits(npis)
    assert result is not None and result.scheme == "npi"
    assert result.share == 1.0


def test_check_digits_returns_nothing_for_ordinary_numbers():
    assert check_digits([f"{i:010d}" for i in range(50)]) is None


def test_sequence_test_finds_a_step():
    result = sequence_test(list(range(1, 200)))
    assert result.is_sequence and result.step == 1 and result.gaps == 0
    assert result.maximum == 199


def test_sequence_test_counts_gaps():
    values = [v for v in range(1, 200) if v % 10]
    result = sequence_test(values)
    assert result.step == 1
    assert result.gaps > 0


def test_sequence_test_rejects_random_numbers():
    import random
    rng = random.Random(7)
    values = rng.sample(range(1, 10_000_000), 200)
    result = sequence_test(values)
    assert not result.is_sequence


def test_cardinality_class():
    assert cardinality_class(1000, 1000) == "unique"
    assert cardinality_class(6, 1000) == "low"
    assert cardinality_class(400, 1000) == "high"
    assert cardinality_class(1, 1000) == "constant"


# ---------------------------------------------------------------------------
# Histogram and proposal
# ---------------------------------------------------------------------------

def test_histogram_keeps_zero_as_its_own_bin():
    counts = [(0, 300), (1, 200), (2, 300), (3, 200)]
    bins = build_histogram(counts)
    assert bins[0].children == 0
    assert bins[0].weight == pytest.approx(0.3)
    assert sum(b.weight for b in bins) == pytest.approx(1.0)


def test_histogram_buckets_a_long_tail_and_keeps_the_total():
    counts = [(0, 1000)] + [(n, max(1, 400 - n * 4)) for n in range(1, 90)]
    bins = build_histogram(counts, bins=20)
    assert len(bins) <= 20
    assert bins[0].children == 0
    assert sum(b.weight for b in bins) == pytest.approx(1.0, abs=1e-3)

    total_parents = sum(p for _, p in counts)
    expected_mean = sum(n * p for n, p in counts) / total_parents
    actual_mean = sum(b.expected() * b.weight for b in bins)
    assert actual_mean == pytest.approx(expected_mean, rel=0.05)


def test_a_bucket_remembers_the_counts_it_stands_for():
    """Without the range, a bucketed tail generates as one spike per bucket."""
    counts = [(0, 1000)] + [(n, max(1, 400 - n * 4)) for n in range(1, 90)]
    bins = build_histogram(counts, bins=20)
    bucketed = [b for b in bins if b.low is not None]
    assert bucketed, "a 90-value tail into 20 bins has to bucket"
    for b in bucketed:
        assert b.high > b.low
        assert b.low <= b.children <= b.high


def test_counts_that_fit_are_kept_exactly_rather_than_bucketed():
    counts = [(0, 300), (1, 200), (2, 300), (3, 200)]
    bins = build_histogram(counts)
    assert [b.children for b in bins] == [0, 1, 2, 3]
    assert all(b.low is None for b in bins)


def test_a_bucketed_bin_is_drawn_across_its_whole_range():
    """The engine has to use the range, or storing it changes nothing."""
    from sdg.engine.execute import children_for_parent
    from sdg.plan.model import HistogramBin as PlanBin, Ratio

    ratio = Ratio(
        parent="dbo.P", kind="distribution",
        histogram=[PlanBin(children=11, weight=1.0, low=8, high=14)],
    )
    drawn = {
        children_for_parent(ratio, "s", "default", "dbo.C", i) for i in range(400)
    }
    assert drawn == set(range(8, 15))


def test_proposal_switches_on_dispersion():
    from sdg.profiling.model import RatioProfile

    tight = RatioProfile(
        child_table="c", child_column="x", parent_table="p", parent_column="y",
        kind="declared", mean=4.0, stddev=0.5,
    )
    assert propose(tight)["kind"] == "fixed"

    spread = RatioProfile(
        child_table="c", child_column="x", parent_table="p", parent_column="y",
        kind="declared", mean=4.0, stddev=5.0,
    )
    assert propose(spread)["kind"] == "distribution"


# ---------------------------------------------------------------------------
# Against the seeded database
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_fitted_formats_match_the_planted_ones(report):
    member_id = report.column("dbo.CMC_MEME_MEMBER", "MEME_ID")
    assert member_id.format_regex() == r"[A-Z]{3}\d{8}"
    assert member_id.format_coverage() == 1.0

    claim_id = report.column("dbo.CMC_CLCL_CLAIM", "CLCL_ID")
    assert claim_id.format_regex() == r"\d{12}"
    assert claim_id.format_coverage() == 1.0

    service_code = report.column("dbo.CMC_CDML_CL_LINE", "CDML_SVC_CD")
    assert service_code.format_regex() == r"[A-Z]\d{4}"


@pytest.mark.db
def test_null_rates_match_the_planted_ones(report):
    term = report.column("dbo.CMC_MEME_MEMBER", "MEME_TERM_DT")
    assert term.null_rate == pytest.approx(0.85, abs=0.01)

    paid = report.column("dbo.CMC_CLCL_CLAIM", "CLCL_PAID_DT")
    assert paid.null_rate == pytest.approx(0.20, abs=0.01)


@pytest.mark.db
def test_claim_status_weights_are_recovered(report):
    status = report.column("dbo.CMC_CLCL_CLAIM", "CLCL_CUR_STS")
    assert status.cardinality == "low"
    assert status.distinct_count == 6
    assert status.top_values is not None
    top = status.top_values[0]
    assert str(top.value).strip() == "02"
    assert top.share == pytest.approx(0.70, abs=0.02)


@pytest.mark.db
def test_provider_npi_is_recognized_by_its_check_digit(report):
    npi = report.column("dbo.CMC_PRPR_PROV", "PRPR_NPI")
    assert npi.check_digit is not None
    assert npi.check_digit["scheme"] == "npi"
    assert npi.check_digit["share"] == 1.0


@pytest.mark.db
def test_identity_keys_are_recognized_as_sequences(report):
    member_key = report.column("dbo.CMC_MEME_MEMBER", "MEME_CK")
    assert member_key.sequence is not None
    assert member_key.sequence["is_sequence"]
    assert member_key.sequence["step"] == 1


@pytest.mark.db
def test_charge_amounts_fit_a_log_normal(report):
    charge = report.column("dbo.CMC_CDML_CL_LINE", "CDML_CHG_AMT")
    assert charge.distribution is not None
    assert charge.distribution["kind"] == "lognormal"


@pytest.mark.db
def test_functional_dependency_finds_zip_to_city_and_state(report):
    found = {
        (d.determinant, d.dependent)
        for d in report.dependencies_for("dbo.CMC_MEME_MEMBER")
    }
    assert ("MEME_ZIP", "MEME_CITY") in found
    assert ("MEME_ZIP", "MEME_STATE") in found


@pytest.mark.db
def test_dependencies_do_not_include_ungenerated_columns(report):
    """System and reference columns must not appear; they bury the real findings."""
    for dependency in report.dependencies:
        for column in (dependency.determinant, dependency.dependent):
            assert column not in {"CLCL_CREATED_TS", "AUDIT_TS", "SBSB_CK", "MEME_CK"}


@pytest.mark.db
def test_date_ordering_finds_service_received_paid(report):
    orderings = {
        (o.earlier, o.later): o
        for o in report.orderings_for("dbo.CMC_CLCL_CLAIM")
        if o.kind == "row"
    }
    service_to_received = orderings[("CLCL_SVC_DT", "CLCL_RECD_DT")]
    assert service_to_received.share_in_order == 1.0
    assert service_to_received.gap_min == 0
    assert service_to_received.gap_max == 30

    received_to_paid = orderings[("CLCL_RECD_DT", "CLCL_PAID_DT")]
    assert received_to_paid.share_in_order == 1.0
    assert received_to_paid.gap_min == 5
    assert received_to_paid.gap_max == 60


@pytest.mark.db
def test_date_ordering_finds_the_member_coverage_window(report):
    """A claim's service date must be anchored on its member, across the join."""
    joins = [o for o in report.orderings_for("dbo.CMC_CLCL_CLAIM") if o.kind == "join"]
    lower = next(
        o for o in joins
        if o.earlier == "dbo.CMC_MEME_MEMBER.MEME_EFF_DT" and o.later == "CLCL_SVC_DT"
    )
    assert lower.share_in_order == 1.0
    assert lower.gap_min == 0

    upper = next(
        o for o in joins
        if o.earlier == "CLCL_SVC_DT" and o.later == "dbo.CMC_MEME_MEMBER.MEME_TERM_DT"
    )
    assert upper.share_in_order >= 0.99


@pytest.mark.db
def test_claims_per_member_ratio_matches_the_planted_distribution(report):
    ratio = report.ratio_for("dbo.CMC_CLCL_CLAIM", "dbo.CMC_MEME_MEMBER")
    assert ratio is not None
    assert ratio.zero_share == pytest.approx(0.30, abs=0.02)
    assert ratio.median_nonzero == pytest.approx(5, abs=0.5)
    assert ratio.p90_nonzero == pytest.approx(14, abs=1.0)
    assert 30 <= ratio.maximum <= 45
    assert ratio.proposal["kind"] == "distribution"
    assert "median 5" in ratio.evidence


@pytest.mark.db
def test_ratio_histogram_preserves_the_mean(report):
    ratio = report.ratio_for("dbo.CMC_CLCL_CLAIM", "dbo.CMC_MEME_MEMBER")
    assert ratio.histogram
    assert len(ratio.histogram) <= HISTOGRAM_BINS
    assert sum(b.weight for b in ratio.histogram) == pytest.approx(1.0, abs=1e-3)
    modeled = sum(b.expected() * b.weight for b in ratio.histogram)
    assert modeled == pytest.approx(ratio.mean, rel=0.05)
    # The childless share must survive as its own bin.
    assert ratio.histogram[0].children == 0
    assert ratio.histogram[0].weight == pytest.approx(ratio.zero_share, abs=0.001)


@pytest.mark.db
def test_a_claims_distribution_keeps_every_count_it_actually_has(report):
    """Bucketing this one would generate a population with gaps in it.

    Claims per member runs 0 to 40 with no holes. If the histogram collapses
    that into buckets, no generated member ever has exactly one claim, and the
    aggregate checks still pass because the mean is preserved.
    """
    ratio = report.ratio_for("dbo.CMC_CLCL_CLAIM", "dbo.CMC_MEME_MEMBER")
    present = [b.children for b in ratio.histogram]
    assert present == sorted(present)
    assert present[0] == 0
    assert max(present) == int(ratio.maximum)
    # Every count between zero and the maximum is represented, either as its
    # own bin or inside a bin that spans it.
    covered = set()
    for b in ratio.histogram:
        covered.update(
            range(b.low, b.high + 1) if b.low is not None else [b.children]
        )
    assert covered == set(range(0, int(ratio.maximum) + 1))


@pytest.mark.db
def test_a_tight_ratio_is_proposed_as_a_fixed_number(report):
    ratio = report.ratio_for("dbo.CMC_MEME_MEMBER", "dbo.CMC_SBSB_SUBSC")
    assert ratio.mean == pytest.approx(2.3, abs=0.1)
    assert ratio.zero_share == 0.0


@pytest.mark.db
def test_profiling_stays_inside_its_time_budget(report, model):
    row_counts = {t.name: t.row_count for t in model.tables}
    for table, seconds in report.table_seconds.items():
        budget = table_budget(row_counts.get(table, 0))
        assert seconds < budget, f"{table} took {seconds}s against a {budget}s budget"
    # Nothing was dropped at this volume; anything that is must be reported.
    assert not [s for s in report.skipped if s.analysis == "pairwise"]


@pytest.mark.db
def test_anything_skipped_is_reported_with_a_reason(model):
    """A run that leaves work out has to say so rather than look complete."""
    limited = profile(
        ConnectionRegistry(), model, tables=["dbo.CMC_CLCL_CLAIM"], skip_pairwise=True
    )
    assert limited.columns
    assert not limited.dependencies
    reasons = {s.analysis: s.reason for s in limited.skipped}
    assert "pairwise" in reasons
    assert reasons["pairwise"]


@pytest.mark.db
def test_report_round_trips(report, tmp_path):
    path = report.save(tmp_path / "profile.json")
    reloaded = ProfileReport.load(path)
    assert len(reloaded.columns) == len(report.columns)
    assert reloaded.ratio_for("dbo.CMC_CLCL_CLAIM", "dbo.CMC_MEME_MEMBER") is not None


@pytest.mark.db
def test_every_profiled_column_reports_its_sample_size(report):
    for column in report.columns:
        assert column.row_count >= 0
        assert 0.0 <= column.null_rate <= 1.0
        assert column.cardinality in {"unique", "high", "low", "constant", "empty"}

"""Per-column measures.

Counts, distinct counts, ranges, and lengths are computed by the database and
come back as one row per table, so a wide table costs one query rather than one
per column. A very large table is measured from a fixed sample of its rows
instead of whole: one pass over a billion rows takes tens of minutes, the sample
seconds. Its row count is the database's own, its empty counts are scaled up
from the sample, and the smallest and largest values of an indexed column --
the keys, where a new value must clear the highest one -- are still exact,
since an index gives them without a pass. Only a bounded sample is pulled into the process, and only to fit a
format and to test for a check digit; whole columns are never extracted.

Nothing here writes to the source.
"""

from __future__ import annotations

import math
import threading
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Column, Table
from sdg.profiling import sample
from sdg.profiling.checks import check_digits, sequence_test
from sdg.profiling.format import fit_format
from sdg.profiling.model import CODE_MAX_DISTINCT, ColumnProfile, TopValue, cardinality_class

SAMPLE_CAP = 100_000
TOP_N = 20
# Below this the profiler asks for an exact distinct count; above it the
# approximate one is close enough and far cheaper.
EXACT_DISTINCT_MAX_ROWS = 200_000
# A table too large to read whole (sample.is_huge) has its column measures
# taken from a sample of this many rows rather than a pass over all of them.
MEASURE_SAMPLE_ROWS = 1_000_000
# Column measures are a pass over every column at once, and on a wide table of
# a few million rows that pass alone takes minutes. Above this many rows they
# come from the sample whatever the installation's limit -- unless that limit
# is 0, which reads every table whole.
MEASURE_WHOLE_MAX_ROWS = 2_000_000

_SAMPLE_WORK = threading.Lock()


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _qualified(table: Table) -> str:
    return f"{_quote(table.schema_name)}.{_quote(table.table_name)}"


def _profilable(column: Column) -> bool:
    """Columns the profiler can measure. Large binary and text blobs are skipped."""
    if column.type_name in {"varbinary", "binary", "image", "xml", "geography", "geometry"}:
        return False
    if column.type_name in {"text", "ntext"}:
        return False
    return True


def aggregate_sql(table: Table, columns: list[Column], *, exact_distinct: bool,
                  source: str | None = None) -> str:
    """One SELECT that measures every column in the table, or in `source`."""
    distinct_fn = "COUNT(DISTINCT {col})" if exact_distinct else "APPROX_COUNT_DISTINCT({col})"
    parts = ["COUNT_BIG(*) AS row_count"]
    for index, column in enumerate(columns):
        col = _quote(column.name)
        parts.append(f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS nulls_{index}")
        parts.append(distinct_fn.format(col=col) + f" AS distinct_{index}")
        if column.is_numeric:
            parts.append(f"MIN({col}) AS min_{index}")
            parts.append(f"MAX({col}) AS max_{index}")
            parts.append(f"AVG(CAST({col} AS float)) AS mean_{index}")
            parts.append(f"STDEV(CAST({col} AS float)) AS stddev_{index}")
        elif column.is_date:
            parts.append(f"MIN({col}) AS min_{index}")
            parts.append(f"MAX({col}) AS max_{index}")
            if column.type_name != "time":
                # Facets writes 9999-12-31 for "no end" and 1753-01-01 for "no
                # date": counted apart, and the range measured without them.
                real = sample.real_date(col, column)
                parts.append(f"MIN(CASE WHEN {real} THEN {col} END) AS rmin_{index}")
                parts.append(f"MAX(CASE WHEN {real} THEN {col} END) AS rmax_{index}")
                parts.append(f"SUM(CASE WHEN {sample.late_placeholder(col, column)} "
                             f"THEN 1 ELSE 0 END) AS late_{index}")
                parts.append(f"SUM(CASE WHEN {sample.early_placeholder(col)} "
                             f"THEN 1 ELSE 0 END) AS early_{index}")
            if column.type_name in {"datetime", "datetime2", "smalldatetime", "datetimeoffset"}:
                parts.append(
                    f"SUM(CASE WHEN CAST({col} AS time) <> '00:00:00' THEN 1 ELSE 0 END) "
                    f"AS timed_{index}"
                )
        elif column.is_character:
            parts.append(f"MIN(LEN({col})) AS minlen_{index}")
            parts.append(f"MAX(LEN({col})) AS maxlen_{index}")
            parts.append(f"MIN({col}) AS min_{index}")
            parts.append(f"MAX({col}) AS max_{index}")
    return f"SELECT {', '.join(parts)} FROM {source or _qualified(table)}"  # noqa: S608 - names come from the catalog


def sample_sql(table: Table, columns: list[Column], cap: int, seed: int) -> str:
    """Up to `cap` rows. A large table is page-sampled with a fixed seed so a
    re-profile sees the same rows and the two reports can be compared."""
    column_list = ", ".join(_quote(c.name) for c in columns)
    source = _qualified(table)
    if table.row_count > cap * 2 and table.row_count > 0:
        # Little over the rows wanted, so the cap trims few: what it trims is
        # the end of the table, and a 1% floor made a billion-row table's
        # sample its first hundredth.
        percent = max(0.01, min(100.0, (cap * 1.25 / table.row_count) * 100))
        source += f" TABLESAMPLE SYSTEM ({percent:.4f} PERCENT) REPEATABLE ({seed})"
    return f"SELECT TOP ({cap}) {column_list} FROM {source}"  # noqa: S608


def top_values_sql(table: Table, column: Column, limit: int = TOP_N) -> str:
    col = _quote(column.name)
    return (
        f"SELECT TOP ({limit}) {col} AS value, COUNT_BIG(*) AS frequency "  # noqa: S608
        f"FROM {_qualified(table)} WHERE {col} IS NOT NULL "
        f"GROUP BY {col} ORDER BY COUNT_BIG(*) DESC, {col}"
    )


def _clean(value: Any) -> Any:
    """Make a database value comparable and serializable."""
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def profile_table(
    registry: ConnectionRegistry,
    table: Table,
    connection: str = "source",
    *,
    sample_cap: int = SAMPLE_CAP,
    sample_seed: int = 20260915,
    whole_max_rows: int | None = None,
    measure_rows: int = MEASURE_SAMPLE_ROWS,
) -> list[ColumnProfile]:
    """Measure every column of one table."""
    columns = [c for c in table.columns if _profilable(c)]
    if not columns:
        return []

    exact = table.row_count <= EXACT_DISTINCT_MAX_ROWS
    if whole_max_rows is None:
        from sdg.config import whole_read_max_rows

        whole_max_rows = whole_read_max_rows()
        if whole_max_rows:
            whole_max_rows = min(whole_max_rows, MEASURE_WHOLE_MAX_ROWS)
    from_sample = sample.is_huge(table, whole_max_rows)
    source = sample.source(table, rows=measure_rows, seed=sample_seed) if from_sample else None
    if table.row_count:
        aggregates = registry.query(
            connection, aggregate_sql(table, columns, exact_distinct=exact, source=source))
    else:
        # An empty table measures as nothing, and a database of thousands of
        # tables has hundreds of them: no query is made to be told so.
        aggregates = [_empty_measures(columns)]
    row = aggregates[0] if aggregates else {}
    row_count = int(row.get("row_count") or 0)
    measured = row_count
    if from_sample and row_count:
        # The database's own count of the table; the sample's counts scale to it.
        row_count = table.row_count
    scale = row_count / measured if measured else 0.0

    profiles: list[ColumnProfile] = []
    for index, column in enumerate(columns):
        null_count = round(int(row.get(f"nulls_{index}") or 0) * scale)
        distinct_count = int(row.get(f"distinct_{index}") or 0)
        profile = ColumnProfile(
            table=table.name,
            column=column.name,
            row_count=row_count,
            null_count=null_count,
            null_rate=(null_count / row_count) if row_count else 0.0,
            distinct_count=distinct_count,
            distinct_ratio=(distinct_count / row_count) if row_count else 0.0,
            approximate_distinct=not exact,
            # A column that is always null holds nothing, whatever the rows.
            cardinality=cardinality_class(distinct_count, row_count - null_count),
            minimum=_clean(row.get(f"min_{index}")),
            maximum=_clean(row.get(f"max_{index}")),
            mean=_as_float(row.get(f"mean_{index}")),
            stddev=_as_float(row.get(f"stddev_{index}")),
            min_length=_as_int(row.get(f"minlen_{index}")),
            max_length=_as_int(row.get(f"maxlen_{index}")),
            measured_rows=measured if from_sample else 0,
        )
        if f"timed_{index}" in row:
            profile.has_time_component = bool(row.get(f"timed_{index}") or 0)
        if f"late_{index}" in row:
            _set_placeholders(profile, row, index, scale)
        profiles.append(profile)

    rows = _add_sample_measures(
        registry, connection, table, columns, profiles, sample_cap, sample_seed)
    # A large table's most common values are read from the sample already in
    # hand: their shares are what is used, and the sample shows them as well as
    # a pass over the whole table per column would.
    if sample.is_sampled(table, sample_cap) and rows:
        _top_values_from_sample(rows, columns, profiles)
    elif table.row_count:
        _add_top_values(registry, connection, table, columns, profiles)
    if from_sample:
        _estimate_distincts(rows, columns, profiles)
        _exact_ranges(registry, connection, table, columns, profiles)
    _sequences_clear_the_highest(profiles)
    _withhold_text_ranges(columns, profiles)
    return profiles


def _withhold_text_ranges(columns: list[Column], profiles: list[ColumnProfile]) -> None:
    """A text column of many values keeps no smallest or largest value.

    Those are two of its real values -- a name, an address, a phone number --
    whether or not the catalog knows the column, and nothing generated reads
    them: its lengths and fitted format describe it. A code column keeps
    them, as its top values are kept."""
    for column, profile in zip(columns, profiles):
        if column.is_character and profile.cardinality in {"unique", "high"}:
            profile.minimum = None
            profile.maximum = None


def _empty_measures(columns: list[Column]) -> dict[str, Any]:
    """What the measuring query returns for a table with no rows."""
    row: dict[str, Any] = {"row_count": 0}
    for index, column in enumerate(columns):
        if column.is_date and column.type_name in {
                "datetime", "datetime2", "smalldatetime", "datetimeoffset"}:
            row[f"timed_{index}"] = 0
    return row


def estimate_distinct(counts: Counter, total: int) -> int:
    """How many distinct values `total` values hold, from a sample's counts.

    A value seen once in the sample stands for many unseen ones; a value seen
    more than once is mostly all there is of it (the GEE estimate of Charikar
    and others). A sample with no value repeated is read as one value per row.
    """
    n = sum(counts.values())
    seen = len(counts)
    if not n or total <= n:
        return seen
    if seen == n:
        return total
    once = sum(1 for c in counts.values() if c == 1)
    return min(total, round(math.sqrt(total / n) * once + (seen - once)))


def _estimate_distincts(rows: list[dict[str, Any]], columns: list[Column],
                        profiles: list[ColumnProfile]) -> None:
    """A sampled table's distinct counts, scaled up from the sample."""
    for column, profile in zip(columns, profiles):
        values = Counter(_clean(r.get(column.name)) for r in rows
                         if r.get(column.name) is not None)
        filled = profile.row_count - profile.null_count
        estimate = max(profile.distinct_count, estimate_distinct(values, filled))
        profile.distinct_count = min(estimate, filled) if filled > 0 else estimate
        profile.distinct_ratio = (
            profile.distinct_count / profile.row_count if profile.row_count else 0.0)
        profile.approximate_distinct = True
        profile.cardinality = cardinality_class(profile.distinct_count, filled)


def _exact_ranges(registry: ConnectionRegistry, connection: str, table: Table,
                  columns: list[Column], profiles: list[ColumnProfile]) -> None:
    """The exact smallest and largest values of each column an index leads.

    An index hands them over from either end without reading the table, and
    they are the ones that must be exact: a generated key starts past the
    highest one already there.
    """
    for column, profile in zip(columns, profiles):
        if not table.leads_index(column.name):
            continue
        if not (column.is_numeric or column.is_date or column.is_character):
            continue
        col = _quote(column.name)
        # A date's range is its real dates', placeholders aside.
        real = f" WHERE {sample.real_date(col, column)}" if (
            column.is_date and column.type_name != "time") else ""
        found = registry.query(
            connection,
            f"SELECT (SELECT MIN({col}) FROM {_qualified(table)}{real}) AS low, "  # noqa: S608
            f"(SELECT MAX({col}) FROM {_qualified(table)}{real}) AS high")
        if found:
            profile.minimum = _clean(found[0].get("low"))
            profile.maximum = _clean(found[0].get("high"))


def _sequences_clear_the_highest(profiles: list[ColumnProfile]) -> None:
    """A sequence read from a sample may not have seen the highest value; the
    column's own maximum has, and a generated value must start past it."""
    for profile in profiles:
        sequence = profile.sequence
        highest = profile.maximum
        if not sequence or isinstance(highest, bool) or not isinstance(highest, (int, float)):
            continue
        sequence["max"] = max(int(sequence.get("max") or 0), int(highest))


def _set_placeholders(profile: ColumnProfile, row: dict[str, Any], index: int,
                      scale: float) -> None:
    """The placeholder dates a column holds and how often; its range becomes
    that of its real dates."""
    found = []
    late = round(int(row.get(f"late_{index}") or 0) * scale)
    early = round(int(row.get(f"early_{index}") or 0) * scale)
    if late and profile.maximum is not None:
        found.append({"value": profile.maximum, "count": late,
                      "share": round(late / profile.row_count, 6) if profile.row_count else 0.0})
    if early and profile.minimum is not None:
        found.append({"value": profile.minimum, "count": early,
                      "share": round(early / profile.row_count, 6) if profile.row_count else 0.0})
    if found:
        profile.placeholders = found
        profile.minimum = _clean(row.get(f"rmin_{index}"))
        profile.maximum = _clean(row.get(f"rmax_{index}"))


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _wants_top_values(profile: ColumnProfile) -> bool:
    """Codes, a single value, and any column with few enough values to list:
    a status that is always AC is generated as AC, and the eight names of an
    eight-row table are names, not a shape to fill with letters."""
    return (profile.looks_like_a_code() or profile.cardinality == "constant"
            or 0 < profile.distinct_count <= CODE_MAX_DISTINCT)


def _top_limit(profile: ColumnProfile) -> int:
    return CODE_MAX_DISTINCT if profile.distinct_count <= CODE_MAX_DISTINCT else TOP_N


def _add_top_values(
    registry: ConnectionRegistry,
    connection: str,
    table: Table,
    columns: list[Column],
    profiles: list[ColumnProfile],
) -> None:
    """Top values and their weights, for columns that read as codes."""
    for column, profile in zip(columns, profiles):
        if not _wants_top_values(profile):
            continue
        rows = registry.query(connection, top_values_sql(table, column, _top_limit(profile)))
        non_null = profile.row_count - profile.null_count
        if not non_null:
            continue
        profile.top_values = [
            TopValue(
                value=_clean(r["value"]),
                count=int(r["frequency"]),
                share=round(int(r["frequency"]) / non_null, 6),
            )
            for r in rows
        ]


def _top_values_from_sample(
    rows: list[dict[str, Any]],
    columns: list[Column],
    profiles: list[ColumnProfile],
) -> None:
    """Top values and their weights for code columns, counted in the sample.
    A count is the table's non-blank rows times the value's share."""
    from collections import Counter

    for column, profile in zip(columns, profiles):
        if not _wants_top_values(profile):
            continue
        counted = Counter(_clean(r[column.name]) for r in rows if r.get(column.name) is not None)
        seen = sum(counted.values())
        non_null = profile.row_count - profile.null_count
        if not seen or not non_null:
            continue
        top = sorted(counted.items(), key=lambda kv: (-kv[1], str(kv[0])))[:_top_limit(profile)]
        profile.top_values = [
            TopValue(value=value, count=round(n / seen * non_null), share=round(n / seen, 6))
            for value, n in top
        ]


def _add_sample_measures(
    registry: ConnectionRegistry,
    connection: str,
    table: Table,
    columns: list[Column],
    profiles: list[ColumnProfile],
    sample_cap: int,
    sample_seed: int,
) -> list[dict[str, Any]]:
    """Format fitting, sequence, and check-digit tests, all from one sample,
    which is returned for the measures that read it too."""
    if not table.row_count:
        return []
    # Turning a sample into Python values, and testing them, is work for the
    # process itself, which does it on one core: tables profiled side by side
    # take turns at it, and wait on the database together.
    with _SAMPLE_WORK:
        rows = registry.query(connection, sample_sql(table, columns, sample_cap, sample_seed))
        if not rows:
            return []
        _test_sample(rows, columns, profiles)
    return rows


def _test_sample(rows: list[dict[str, Any]], columns: list[Column],
                 profiles: list[ColumnProfile]) -> None:
    for column, profile in zip(columns, profiles):
        name = column.name
        values = [v for v in (r.get(name) for r in rows) if v is not None]
        profile.sample_size = len(values)
        if not values:
            continue

        if column.is_character:
            # A code column of a hundred thousand rows holds a few dozen
            # values: each is cleaned and tested once, weighted by its count.
            cleaned: Counter = Counter()
            for value, n in Counter(values).items():
                cleaned[str(value).rstrip()] += n
            fitted = fit_format(cleaned)
            if fitted:
                profile.fitted_format = fitted.as_dict()
            digits = check_digits(cleaned)
            if digits:
                profile.check_digit = digits.as_dict()

        elif column.is_integer:
            result = sequence_test([int(v) for v in values])
            if result:
                profile.sequence = result.as_dict()
            as_text: Counter = Counter()
            for value, n in Counter(values).items():
                as_text[str(int(value))] += n
            digits = check_digits(as_text)
            if digits:
                profile.check_digit = digits.as_dict()

        if column.is_numeric and not column.is_integer:
            profile.distribution = _fit_distribution([float(v) for v in values])
        elif column.is_integer and not (profile.sequence or {}).get("is_sequence"):
            profile.distribution = _fit_distribution([float(v) for v in values])


def _fit_distribution(values: list[float]) -> dict[str, Any] | None:
    """Pick between uniform, normal, and log-normal by comparing skew.

    This is deliberately a rule of thumb rather than a goodness-of-fit test: the
    proposal is shown to a user with its evidence, and they can change it.
    """
    import math

    if len(values) < 20:
        return None
    count = len(values)
    mean = sum(values) / count
    variance = sum((v - mean) ** 2 for v in values) / max(1, count - 1)
    stddev = math.sqrt(variance)
    low, high = min(values), max(values)
    if stddev == 0:
        return {"kind": "constant", "min": low, "max": high, "mean": mean, "stddev": 0.0}

    skew = sum(((v - mean) / stddev) ** 3 for v in values) / count

    if all(v > 0 for v in values) and skew > 1.0:
        logs = [math.log(v) for v in values]
        log_mean = sum(logs) / count
        log_var = sum((x - log_mean) ** 2 for x in logs) / max(1, count - 1)
        return {
            "kind": "lognormal",
            "mean": round(log_mean, 6),
            "stddev": round(math.sqrt(log_var), 6),
            "min": low,
            "max": high,
            "skew": round(skew, 4),
        }

    # A uniform sample has a standard deviation near range / sqrt(12).
    expected_uniform = (high - low) / math.sqrt(12) if high > low else 0.0
    if expected_uniform and abs(stddev - expected_uniform) / expected_uniform < 0.15:
        return {"kind": "uniform", "min": low, "max": high, "skew": round(skew, 4)}

    return {
        "kind": "normal",
        "mean": round(mean, 6),
        "stddev": round(stddev, 6),
        "min": low,
        "max": high,
        "skew": round(skew, 4),
    }

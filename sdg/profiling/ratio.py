"""How many children each parent has.

This is the measure that decides volumes. A flat "four claims per member"
produces data that no test exercises properly; real populations have parents
with none, parents with a handful, and a long tail of parents with many. The
profiler measures the real shape and proposes it.

The share of parents with no children is kept as its own bin rather than being
averaged into the first bucket, because it is usually the single most important
number in the distribution and the one a flat ratio loses.
"""

from __future__ import annotations

from dataclasses import dataclass

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import PhysicalModel, Table
from sdg.profiling import sample
from sdg.profiling.model import HistogramBin, RatioProfile
from sdg.profiling.sample import SAMPLE_ROWS

# The build brief: below this, the spread is small enough that a fixed ratio
# describes the data as well as a distribution does.
FIXED_RATIO_MAX_DISPERSION = 0.25
# Enough that a realistic children-per-parent distribution keeps every count
# it actually has. Bucketing below is what a comb-shaped population comes from,
# so it is a fallback for pathological spreads rather than the normal path.
HISTOGRAM_BINS = 64


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _qualified(table: Table) -> str:
    return f"{_quote(table.schema_name)}.{_quote(table.table_name)}"


def _per_parent_cte(parent: Table, parent_column: str, child: Table, child_column: str,
                    sample_rows: int = SAMPLE_ROWS) -> str:
    # A large parent table is read through a fixed sample of its parents; each
    # sampled parent's children are all counted, so its count is exact.
    return (
        "WITH per_parent AS ("
        f" SELECT p.{_quote(parent_column)} AS parent_key, COUNT(c.{_quote(child_column)}) AS n"
        f" FROM {sample.source(parent, 'p', rows=sample_rows)}"
        f" LEFT JOIN {_qualified(child)} AS c"
        f" ON c.{_quote(child_column)} = p.{_quote(parent_column)}"
        f"{sample.collate_as(child.column(child_column), parent.column(parent_column))}"
        f" GROUP BY p.{_quote(parent_column)}"
        ")"
    )


# How many parents have each number of children: one pass, and everything
# else -- the totals, the spread, the percentiles -- follows from it.
COUNTS_SQL = """
{cte}
SELECT n, COUNT(*) AS parents
FROM per_parent
GROUP BY n
ORDER BY n{hint}
"""


@dataclass
class RatioTarget:
    child_table: Table
    child_column: str
    parent_table: Table
    parent_column: str
    kind: str


def targets(model: PhysicalModel) -> list[RatioTarget]:
    """Every single-column foreign key, declared or inferred."""
    out: list[RatioTarget] = []
    for child in model.tables:
        for foreign_key in child.foreign_keys:
            if len(foreign_key.columns) != 1:
                continue
            parent = model.table(foreign_key.parent_table)
            if parent is None:
                continue
            out.append(
                RatioTarget(
                    child_table=child,
                    child_column=foreign_key.columns[0],
                    parent_table=parent,
                    parent_column=foreign_key.parent_columns[0],
                    kind=foreign_key.kind,
                )
            )
    return out


def build_histogram(counts: list[tuple[int, int]], bins: int = HISTOGRAM_BINS) -> list[HistogramBin]:
    """Turn (children, parents) counts into at most `bins` weighted bins.

    Zero keeps its own bin. The rest are used as they are when they fit, and
    bucketed by equal width when they do not, with each bucket represented by
    the mean number of children inside it so the total is preserved. A bucket
    that covers more than one count also records that range, because a bucket
    drawn as a single number turns a smooth tail into a comb.
    """
    if not counts:
        return []
    zero = next((parents for children, parents in counts if children == 0), 0)
    positive = [(children, parents) for children, parents in counts if children > 0]
    total = sum(parents for _, parents in counts)
    if not total:
        return []

    out: list[HistogramBin] = []
    if zero:
        out.append(HistogramBin(children=0, weight=round(zero / total, 6)))

    remaining_bins = bins - len(out)
    if not positive:
        return out

    if len(positive) <= remaining_bins:
        for children, parents in positive:
            out.append(HistogramBin(children=children, weight=round(parents / total, 6)))
        return out

    low = positive[0][0]
    high = positive[-1][0]
    width = max(1, (high - low + 1 + remaining_bins - 1) // remaining_bins)
    bucket: dict[int, list[int]] = {}
    for children, parents in positive:
        index = (children - low) // width
        bucket.setdefault(index, [0, 0, children, children])
        bucket[index][0] += parents
        bucket[index][1] += children * parents
        bucket[index][2] = min(bucket[index][2], children)
        bucket[index][3] = max(bucket[index][3], children)

    for index in sorted(bucket):
        parents, child_total, lowest, highest = bucket[index]
        representative = max(1, round(child_total / parents))
        out.append(HistogramBin(
            children=representative,
            weight=round(parents / total, 6),
            low=lowest if highest > lowest else None,
            high=highest if highest > lowest else None,
        ))
    return out


def profile_ratio(
    registry: ConnectionRegistry, target: RatioTarget, connection: str = "source",
    *, sample_rows: int = SAMPLE_ROWS, huge_rows: int | None = None,
) -> RatioProfile | None:
    child = target.child_table
    if sample.is_huge(child, huge_rows) and not child.leads_index(target.child_column):
        # Each parent's children are found through an index on the child's
        # key; without one, counting them reads every child row.
        return _from_sizes(registry, target, connection, sample_rows)
    cte = _per_parent_cte(
        target.parent_table, target.parent_column, child, target.child_column,
        sample_rows,
    )
    hint = sample.seek_hint(child, target.child_column, huge_rows)
    counts = [
        (int(r["n"]), int(r["parents"]))
        for r in registry.query(connection, COUNTS_SQL.format(cte=cte, hint=hint))
    ]
    seen = sum(p for _, p in counts)
    if not seen:
        return None
    mean = sum(n * p for n, p in counts) / seen
    stddev = (sum(p * (n - mean) ** 2 for n, p in counts) / (seen - 1)) ** 0.5 if seen > 1 else 0.0
    childless = sum(p for n, p in counts if n == 0)
    nonzero = [(n, p) for n, p in counts if n > 0]

    # A sample of parents describes the shape; the totals are the table's.
    sampled = sample.is_sampled(target.parent_table, sample_rows)
    parents = int(target.parent_table.row_count) if sampled else seen
    children = round(mean * parents) if sampled else sum(n * p for n, p in counts)

    profile = RatioProfile(
        child_table=target.child_table.name,
        child_column=target.child_column,
        parent_table=target.parent_table.name,
        parent_column=target.parent_column,
        kind=target.kind,
        parents=parents,
        children=children,
        minimum=counts[0][0],
        maximum=counts[-1][0],
        mean=round(mean, 4),
        stddev=round(stddev, 4),
        median=_percentile(counts, 0.5),
        p90=_percentile(counts, 0.9),
        p99=_percentile(counts, 0.99),
        median_nonzero=_percentile(nonzero, 0.5),
        p90_nonzero=_percentile(nonzero, 0.9),
        zero_share=round(childless / seen, 6),
        histogram=build_histogram(counts),
        sampled_parents=seen if sampled else 0,
    )
    profile.proposal = propose(profile)
    profile.evidence = describe(profile)
    return profile


FILLED_SQL = "SELECT COUNT(*) AS rows, COUNT({column}) AS filled FROM {source}"


def _from_sizes(registry: ConnectionRegistry, target: RatioTarget, connection: str,
                sample_rows: int) -> RatioProfile | None:
    """The average children per parent from the two tables' own row counts,
    when the spread cannot be measured without reading the whole child table.
    Only child rows that name a parent count, and the share that do is read
    from the child's sample. The average is what sizes a load; the spread is
    said to be unmeasured."""
    parents = int(target.parent_table.row_count)
    if not parents:
        return None
    found = registry.query(connection, FILLED_SQL.format(
        column=_quote(target.child_column),
        source=sample.source(target.child_table, rows=sample_rows)))
    read = int(found[0]["rows"] or 0) if found else 0
    filled = int(found[0]["filled"] or 0) if found else 0
    share = filled / read if read else 1.0
    children = round(int(target.child_table.row_count) * share)
    mean = children / parents
    profile = RatioProfile(
        child_table=target.child_table.name,
        child_column=target.child_column,
        parent_table=target.parent_table.name,
        parent_column=target.parent_column,
        kind=target.kind,
        parents=parents,
        children=children,
        minimum=0,
        maximum=0,
        mean=round(mean, 4),
        stddev=0.0,
        median=round(mean, 4),
        p90=round(mean, 4),
        p99=round(mean, 4),
        median_nonzero=round(mean, 4),
        p90_nonzero=round(mean, 4),
        zero_share=0.0,
        histogram=[],
        from_sizes=True,
    )
    profile.proposal = {"kind": "fixed", "value": round(mean, 4), "dispersion": 0.0}
    profile.evidence = (
        f"about {mean:,.2f} per parent on average, from the two tables' sizes "
        f"(about {children:,} rows naming one, over {parents:,}); how the count varies "
        f"between parents "
        f"was not measured, since {target.child_column} has no index and counting "
        f"would read every row of {target.child_table.name}"
    )
    return profile


def _percentile(counts: list[tuple[int, int]], share: float) -> float:
    """PERCENTILE_CONT over parents' child counts, given as (count, parents)
    in ascending order: interpolated between the two parents either side."""
    total = sum(p for _, p in counts)
    if not total:
        return 0.0
    position = share * (total - 1)

    def at(rank: int) -> int:
        seen = 0
        for n, p in counts:
            seen += p
            if rank < seen:
                return n
        return counts[-1][0]

    low = int(position)
    below, above = at(low), at(min(low + 1, total - 1))
    return round(below + (above - below) * (position - low), 4)


def propose(profile: RatioProfile) -> dict:
    """A fixed ratio when the spread is small, a distribution when it is not."""
    dispersion = (profile.stddev / profile.mean) if profile.mean else 0.0
    if profile.mean and dispersion < FIXED_RATIO_MAX_DISPERSION:
        return {
            "kind": "fixed",
            "value": round(profile.mean, 4),
            "dispersion": round(dispersion, 4),
            "reason": (
                f"standard deviation is {dispersion:.2f} of the mean, below "
                f"{FIXED_RATIO_MAX_DISPERSION}, so one number describes it"
            ),
        }
    return {
        "kind": "distribution",
        "dispersion": round(dispersion, 4),
        "zero_share": profile.zero_share,
        "reason": (
            f"standard deviation is {dispersion:.2f} of the mean, at or above "
            f"{FIXED_RATIO_MAX_DISPERSION}, so the shape matters"
        ),
    }


def describe(profile: RatioProfile) -> str:
    """The sentence the hierarchy view shows next to the editable ratio."""
    child = profile.child_table.split('.')[-1]
    parent = profile.parent_table.split('.')[-1]
    counted = (
        f"about {profile.children:,} {child} rows across {profile.parents:,} {parent} rows "
        f"(from {profile.sampled_parents:,} of them)"
        if profile.sampled_parents else
        f"observed {profile.children:,} {child} rows across {profile.parents:,} {parent} rows"
    )
    return (
        f"{counted}; "
        f"{profile.zero_share:.0%} have none, median {profile.median_nonzero:g} among those "
        f"that do, 90th percentile {profile.p90_nonzero:g}, maximum {profile.maximum:,}"
    )


def profile_ratios(
    registry: ConnectionRegistry, model: PhysicalModel, connection: str = "source",
    *, sample_rows: int = SAMPLE_ROWS, workers: int = 1,
) -> list[RatioProfile]:
    """Children per parent for every link, several links at a time when
    `workers` allows, listed in the links' own order."""
    found = targets(model)

    def one(target: RatioTarget) -> RatioProfile | None:
        # Looked up at call time, so a test that swaps profile_ratio sees it.
        return profile_ratio(registry, target, connection, sample_rows=sample_rows)

    if workers > 1 and len(found) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, found))
    else:
        results = [one(t) for t in found]
    return [r for r in results if r is not None]

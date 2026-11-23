"""What profiling learned, stored next to the physical model.

Kept as JSON so a re-profile can be compared against the previous one after the
schema or the data changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sdg.discovery.model import indexed

# Design document section 6.5 and the build brief: a column with this few
# distinct values, or this small a share of distinct values, reads as a code.
CODE_MAX_DISTINCT = 50
CODE_MAX_RATIO = 0.001
UNIQUE_RATIO = 0.99


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopValue(Base):
    value: Any
    count: int
    share: float


class ColumnProfile(Base):
    table: str
    column: str
    row_count: int = 0
    null_count: int = 0
    null_rate: float = 0.0
    distinct_count: int = 0
    distinct_ratio: float = 0.0
    approximate_distinct: bool = True
    cardinality: str = "unknown"  # unique | high | low | constant | empty

    minimum: Any = None
    maximum: Any = None
    mean: float | None = None
    stddev: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    has_time_component: bool | None = None
    # Dates standing for "none" -- 9999-12-31, 1753-01-01 -- with their counts
    # and shares. minimum and maximum are then the real dates'.
    placeholders: list[dict[str, Any]] | None = None

    top_values: list[TopValue] | None = None
    fitted_format: dict[str, Any] | None = None
    sequence: dict[str, Any] | None = None
    check_digit: dict[str, Any] | None = None
    distribution: dict[str, Any] | None = None
    sample_size: int = 0
    # The rows the counts and ranges were measured on, when a very large table
    # was measured from a sample; 0 when it was read whole.
    measured_rows: int = 0

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    def format_regex(self) -> str | None:
        return (self.fitted_format or {}).get("regex")

    def format_coverage(self) -> float:
        return float((self.fitted_format or {}).get("coverage") or 0.0)

    def looks_like_a_code(self) -> bool:
        return self.cardinality == "low"


class FunctionalDependency(Base):
    table: str
    determinant: str
    dependent: str
    distinct_determinant: int
    distinct_pair: int
    holds: bool
    coverage: float


class DateOrdering(Base):
    kind: str  # row | join
    table: str
    earlier: str
    later: str
    rows_compared: int
    share_in_order: float
    gap_min: int | None = None
    gap_max: int | None = None
    gap_mean: float | None = None
    gap_stddev: float | None = None
    parent_table: str | None = None


class HistogramBin(Base):
    children: int
    weight: float
    # The inclusive range this bin stands for. Set only when the bin had to
    # cover several counts, so the engine can spread across it instead of
    # emitting one spike where the real data had a run of values.
    low: int | None = None
    high: int | None = None

    def expected(self) -> float:
        """Average children this bin contributes, matching how it is drawn."""
        if self.low is not None and self.high is not None and self.high > self.low:
            return (self.low + self.high) / 2
        return float(self.children)


class RatioProfile(Base):
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    kind: str  # declared | inferred
    parents: int = 0
    children: int = 0
    minimum: int = 0
    maximum: int = 0
    mean: float = 0.0
    stddev: float = 0.0
    median: float = 0.0
    p90: float = 0.0
    p99: float = 0.0
    # The brief's planted median describes parents that have children, so both
    # figures are reported and the tests can name which one they mean.
    median_nonzero: float = 0.0
    p90_nonzero: float = 0.0
    zero_share: float = 0.0
    histogram: list[HistogramBin] = Field(default_factory=list)
    proposal: dict[str, Any] = Field(default_factory=dict)
    evidence: str = ""
    # How many parents the shape was measured on, when a large parent table
    # was sampled; 0 when every parent was counted. Parents and children are
    # then the table's row count and the estimate it gives.
    sampled_parents: int = 0
    # True when the average came from the two tables' row counts alone, the
    # spread unmeasured: a huge child table with no index on its key.
    from_sizes: bool = False


class SkippedAnalysis(Base):
    table: str
    analysis: str
    reason: str


class ProfileReport(Base):
    connection: dict[str, Any] = Field(default_factory=dict)
    profiled: str = ""
    sample_cap: int = 0
    seconds: float = 0.0
    columns: list[ColumnProfile] = Field(default_factory=list)
    dependencies: list[FunctionalDependency] = Field(default_factory=list)
    orderings: list[DateOrdering] = Field(default_factory=list)
    ratios: list[RatioProfile] = Field(default_factory=list)
    skipped: list[SkippedAnalysis] = Field(default_factory=list)
    table_seconds: dict[str, float] = Field(default_factory=dict)

    def column(self, table: str, column: str) -> ColumnProfile | None:
        return indexed(self, self.columns, _table_and_column).get((table, column))

    def ratio_for(self, child_table: str, parent_table: str) -> RatioProfile | None:
        return next(
            (
                r for r in self.ratios
                if r.child_table == child_table and r.parent_table == parent_table
            ),
            None,
        )

    def dependencies_for(self, table: str) -> list[FunctionalDependency]:
        return [d for d in self.dependencies if d.table == table and d.holds]

    def orderings_for(self, table: str) -> list[DateOrdering]:
        return list(indexed(self, self.orderings, _table_of, many=True).get(table, ()))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(mode="json", exclude_none=True), indent=2, default=str)
            + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "ProfileReport":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def cardinality_class(distinct_count: int, row_count: int) -> str:
    """unique, high, low, constant, or empty."""
    if row_count == 0:
        return "empty"
    if distinct_count <= 1:
        return "constant"
    ratio = distinct_count / row_count
    if ratio >= UNIQUE_RATIO:
        return "unique"
    if distinct_count <= CODE_MAX_DISTINCT or ratio <= CODE_MAX_RATIO:
        return "low"
    return "high"


def _table_and_column(profile: ColumnProfile) -> tuple[str, str]:
    return (profile.table, profile.column)


def _table_of(ordering: DateOrdering) -> str:
    return ordering.table

"""Propose a generation rule for every column.

The structural bucket is applied first and cannot be overridden: an identity
column is skipped no matter what its values look like, and a foreign key
references its parent no matter what element it maps to. Only once the bucket
allows a choice does the column's profile decide, following the decision table
in design document section 7.2.

Every proposal carries the evidence that produced it and the share of rows that
supported it, because a proposal a user cannot check is a guess.
"""

from __future__ import annotations

import os

from dataclasses import dataclass, field
from typing import Any

from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.mapping.matchers import DISTINCTIVE_FORMAT_LITERALS, format_literals
from sdg.plan.detect import SumExpression
from sdg.profiling.model import ColumnProfile, DateOrdering, ProfileReport

# A distinct ratio at or above this reads as one value per row.
UNIQUE_RATIO = 0.99
# Below this share of rows covered, a fitted format is not worth proposing.
FORMAT_MIN_COVERAGE = 0.95
# A free-text fallback needs a length to work with.
DEFAULT_TEXT_LENGTH = 20
# A lookup has to be able to enumerate its column. Top-N is capped, so a column
# with hundreds of distinct values can trip the low-cardinality rule while its
# top twenty cover almost nothing; below this share the list is not the rule.
LOOKUP_MIN_COVERAGE = 0.8
# Below this many rows, every value of a column being different says nothing:
# eight groups have eight names and eight states. Only a key is unique there.
SMALL_TABLE_ROWS = 100
# A whole-number column whose values run in steps is a counter only if no two
# rows share a value: ages 0 to 90 run in steps too.
SEQUENCE_MIN_DISTINCT_SHARE = 0.99
# Free text this long or longer, with no shape, is written as words.
WORDS_MIN_LENGTH = 15


@dataclass
class Proposal:
    table: str
    column: str
    bucket: str
    generator: str
    params: dict[str, Any] = field(default_factory=dict)
    confidence: str = "low"
    coverage: float | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    provenance: str = "learned"
    note: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "bucket": self.bucket,
            "generator": self.generator,
            "params": self.params,
            "confidence": self.confidence,
            "coverage": self.coverage,
            "evidence": self.evidence,
            "provenance": self.provenance,
            "note": self.note,
        }


def clamp_coverage(value: float | None) -> float | None:
    """Keep a coverage figure inside [0, 1].

    Shares are rounded before they are summed, so a set of weights that really
    does cover every row can add up to 1.000001. The plan schema rejects that,
    and rightly: coverage above one means nothing.
    """
    if value is None:
        return None
    return round(min(1.0, max(0.0, float(value))), 6)


def _evidence(kind: str, detail: str, coverage: float | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": kind, "detail": detail}
    if coverage is not None:
        item["coverage"] = clamp_coverage(coverage)
    return item


# ---------------------------------------------------------------------------
# Structural proposals: the bucket decides, the profile does not get a vote
# ---------------------------------------------------------------------------


def _system(table: Table, column: Column) -> Proposal:
    return Proposal(
        table=table.name,
        column=column.name,
        bucket="system",
        generator="skip",
        confidence="high",
        coverage=1.0,
        provenance="structural",
        evidence=[_evidence("structural", column.bucket_reason or "the database fills this")],
        note="left to the database",
    )


def _reference(table: Table, column: Column) -> Proposal | None:
    foreign_key = table.foreign_key_columns().get(column.name)
    if foreign_key is None:
        return None
    # The driving parent is the one the ratio counts against; the hierarchy
    # builder sets that later, so the proposal asks for a hash pick and the
    # resolver upgrades it when this turns out to be the driving parent.
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "reference",
        generator="reference",
        params={
            "parent_table": foreign_key.parent_table,
            "parent_column": foreign_key.parent_columns[0],
            "pick": "hash",
        },
        confidence="high",
        coverage=foreign_key.coverage if foreign_key.coverage is not None else 1.0,
        provenance="structural",
        evidence=[
            _evidence(
                "declared_fk" if foreign_key.kind == "declared" else "value_inclusion",
                column.bucket_reason or f"foreign key to {foreign_key.parent_table}",
                foreign_key.coverage,
            )
        ],
    )


def _composite_key_counter(table: Table, column: Column) -> Proposal | None:
    """The numbering column of a composite key made of a foreign key plus a counter.

    (CLCL_ID, CDML_SEQ_NO) is the shape: the first part points at the parent,
    the second counts lines inside it. Profiling sees five distinct values and
    would happily call it a code list, which would produce duplicate keys, so
    the structure has to decide this one.
    """
    if len(table.primary_key) < 2 or column.name not in table.primary_key:
        return None
    if not column.is_integer or column.is_identity:
        return None
    foreign_key_columns = set(table.foreign_key_columns())
    others = [c for c in table.primary_key if c != column.name]
    if not others or not set(others) <= foreign_key_columns:
        return None
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "business_key",
        generator="sequence",
        params={"start": 1, "step": 1, "scope": "parent"},
        confidence="high",
        coverage=1.0,
        provenance="structural",
        evidence=[
            _evidence(
                "composite_key",
                f"part of the primary key ({', '.join(table.primary_key)}) alongside "
                f"{', '.join(others)}, which references a parent; this column numbers "
                f"the rows inside each one",
                1.0,
            )
        ],
    )


def _code_from_constraint(
    table: Table, column: Column, profile: ColumnProfile | None = None
) -> Proposal | None:
    if not column.check_values:
        return None
    values = list(column.check_values)
    params: dict[str, Any] = {"values": values}
    evidence = [
        _evidence(
            "check_constraint",
            f"the constraint allows exactly {', '.join(values)}",
            1.0,
        )
    ]
    # The constraint says which values are legal; the data says how often each
    # one actually turns up. Both are worth keeping.
    observed = {
        str(top.value).rstrip(): top.share for top in (profile.top_values or [])
    } if profile else {}
    if observed and all(v in observed for v in values):
        params["weights"] = [round(observed[v], 6) for v in values]
        busiest = max(values, key=lambda v: observed[v])
        evidence.append(
            _evidence(
                "top_values",
                f"observed weights, with {busiest!r} the most common at "
                f"{observed[busiest]:.0%}",
                sum(observed[v] for v in values),
            )
        )
    return Proposal(
        table=table.name,
        column=column.name,
        bucket="code",
        generator="lookup",
        params=params,
        confidence="high",
        coverage=1.0,
        provenance="structural",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Profile-driven proposals (design document 7.2)
# ---------------------------------------------------------------------------


def _weighted_lookup(table: Table, column: Column, profile: ColumnProfile) -> Proposal | None:
    if not profile.top_values:
        return None
    values = [t.value for t in profile.top_values]
    weights = [round(t.share, 6) for t in profile.top_values]
    covered = sum(weights)
    # Generating from a list that covers a twentieth of the column would throw
    # away most of its values. Say nothing and let the format speak instead.
    if covered < LOOKUP_MIN_COVERAGE and len(values) < profile.distinct_count:
        return None
    top = profile.top_values[0]
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator="lookup",
        params={"values": values, "weights": weights},
        confidence="high" if covered >= 0.99 else "medium",
        coverage=clamp_coverage(covered),
        evidence=[
            _evidence(
                "top_values",
                f"{profile.distinct_count} distinct values; {top.value!r} is the most "
                f"common at {top.share:.0%}",
                covered,
            )
        ],
    )


def _sequence(table: Table, column: Column, profile: ColumnProfile) -> Proposal | None:
    sequence = profile.sequence
    if not sequence or not sequence.get("is_sequence"):
        return None
    filled = profile.row_count - profile.null_count
    if (column.bucket != "business_key" and filled
            and profile.distinct_count < SEQUENCE_MIN_DISTINCT_SHARE * filled):
        return None
    maximum = sequence.get("max") or 0
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "business_key",
        generator="sequence",
        # Start after the highest value already present, so generated rows do
        # not collide with the ones already in the table.
        params={"start": int(maximum) + 1, "step": int(sequence.get("step") or 1)},
        confidence="high",
        coverage=clamp_coverage(sequence.get("agreement") or 0.0),
        evidence=[
            _evidence(
                "sequence",
                f"values advance by {sequence.get('step')} with {sequence.get('gaps')} "
                f"gaps; the highest is {maximum:,}",
                sequence.get("agreement"),
            )
        ],
    )


def _unique(table: Table, column: Column, profile: ColumnProfile) -> bool:
    """Whether no two rows may share a value. Part of a composite key is not
    unique on its own: a member's PCP rows share their type and differ by
    member. Only a key of one column is."""
    sole_key = column.is_primary_key and len(table.primary_key or []) <= 1
    if column.is_unique or sole_key:
        return True
    if profile.cardinality != "unique":
        return False
    return profile.row_count >= SMALL_TABLE_ROWS or column.bucket == "business_key"


def _pattern(table: Table, column: Column, profile: ColumnProfile) -> Proposal | None:
    regex = profile.format_regex()
    coverage = profile.format_coverage()
    if not regex or coverage < FORMAT_MIN_COVERAGE:
        return None
    # The widest format of text with spaces says only "letters and spaces":
    # generated, it is noise. Words are written as words.
    if (profile.fitted_format or {}).get("kind") == "fallback" and " " in regex:
        return None
    # Part of a composite key is not unique on its own: a member's PCP rows
    # share their type and differ by member. Only a key of one column is.
    unique = _unique(table, column, profile)
    params: dict[str, Any] = {"regex": regex}
    if unique:
        params["unique"] = True
    evidence = [
        _evidence("fitted_format", f"{regex} covers {coverage:.0%} of the sample", coverage)
    ]
    if profile.check_digit:
        evidence.append(
            _evidence(
                "check_digit",
                f"{profile.check_digit['share']:.0%} of values pass the "
                f"{profile.check_digit['scheme']} check-digit test",
                profile.check_digit["share"],
            )
        )
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "business_key",
        generator="pattern",
        params=params,
        confidence="high" if coverage >= 0.999 else "medium",
        coverage=clamp_coverage(coverage),
        evidence=evidence,
        note=(
            "the pattern reproduces the shape but not the check digit"
            if profile.check_digit
            else ""
        ),
    )


def _gap_width(ordering: DateOrdering) -> int:
    if ordering.gap_min is None or ordering.gap_max is None:
        return 10**9
    return ordering.gap_max - ordering.gap_min


def best_anchor(column_name: str, orderings: list[DateOrdering]) -> DateOrdering | None:
    """The tightest ordering that ends at this column.

    Tightest wins because it says the most: a received date sitting nought to
    thirty days after service is a far better rule than the same date sitting
    somewhere inside the member's eight-year coverage window.
    """
    anchors = [o for o in orderings if o.later == column_name]
    return min(anchors, key=_gap_width) if anchors else None


def anchor_map(orderings: list[DateOrdering]) -> dict[str, str]:
    """Each date column in a table mapped to the column it anchors on."""
    chosen: dict[str, str] = {}
    for ordering in orderings:
        anchor = best_anchor(ordering.later, orderings)
        if anchor is not None and "." not in ordering.later:
            chosen[ordering.later] = anchor.earlier
    return chosen


def _would_cycle(column_name: str, candidate: str, anchors: dict[str, str]) -> bool:
    """Does `candidate` end up depending on `column_name`?

    A received date anchors on the service date, so the service date cannot in
    turn be bounded by the received date. The walk follows anchors until it
    leaves the row, which a parent-table reference does immediately.
    """
    seen: set[str] = set()
    current = candidate
    while current and current not in seen:
        if current == column_name:
            return True
        seen.add(current)
        current = anchors.get(current, "")
    return False


def best_ceiling(
    column_name: str, orderings: list[DateOrdering], anchors: dict[str, str]
) -> DateOrdering | None:
    """The tightest ordering that starts at this column and does not loop back."""
    candidates = sorted(
        (o for o in orderings if o.earlier == column_name), key=_gap_width
    )
    for candidate in candidates:
        if not _would_cycle(column_name, candidate.later, anchors):
            return candidate
    return None


def _date_relative(
    table: Table,
    column: Column,
    profile: ColumnProfile,
    orderings: list[DateOrdering],
    anchors: dict[str, str] | None = None,
) -> Proposal | None:
    anchor = best_anchor(column.name, orderings)
    if anchor is None:
        return None
    ceiling = best_ceiling(column.name, orderings, anchors or anchor_map(orderings))

    params: dict[str, Any] = {
        "anchor": anchor.earlier,
        "min_days": int(anchor.gap_min or 0),
        "max_days": int(anchor.gap_max or 0),
    }
    evidence = [
        _evidence(
            "date_ordering",
            f"follows {anchor.earlier} by {anchor.gap_min} to {anchor.gap_max} days in "
            f"{anchor.share_in_order:.1%} of {anchor.rows_compared:,} rows",
            anchor.share_in_order,
        )
    ]
    # The observed maximum bounds a row whose anchor sits late in the range.
    # Without it, the widest gap seen anywhere gets applied to a member whose
    # coverage started last year, and dates run past anything in the source.
    observed_max = profile.maximum if isinstance(profile.maximum, str) else None
    if observed_max:
        params["max_date"] = observed_max[:10]
        evidence.append(
            _evidence(
                "observed_range",
                f"no value in the column is later than {observed_max[:10]}",
                1.0,
            )
        )

    if ceiling is not None and ceiling.later != anchor.earlier:
        params["upper_anchor"] = ceiling.later
        evidence.append(
            _evidence(
                "date_ordering",
                f"stays at or before {ceiling.later} in {ceiling.share_in_order:.1%} of "
                f"{ceiling.rows_compared:,} rows",
                ceiling.share_in_order,
            )
        )
    if column.nullable and profile.null_rate > 0:
        evidence.append(
            _evidence("null_rate", f"null in {profile.null_rate:.0%} of rows", profile.null_rate)
        )

    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator="date_relative",
        params=params,
        confidence="high" if anchor.share_in_order >= 0.999 else "medium",
        coverage=clamp_coverage(anchor.share_in_order),
        evidence=evidence,
    )


def _date_range(table: Table, column: Column, profile: ColumnProfile) -> Proposal | None:
    """A date with no ordering: a range between the observed bounds."""
    if not isinstance(profile.minimum, str) or not isinstance(profile.maximum, str):
        return None
    from datetime import date

    try:
        low = date.fromisoformat(profile.minimum[:10])
        high = date.fromisoformat(profile.maximum[:10])
    except ValueError:
        return None
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator="date_relative",
        params={
            "anchor": low.isoformat(),
            "min_days": 0,
            "max_days": max(0, (high - low).days),
        },
        confidence="medium",
        coverage=1.0,
        evidence=[
            _evidence(
                "observed_range",
                f"values run from {low.isoformat()} to {high.isoformat()}",
                1.0,
            )
        ],
    )


def _number(table: Table, column: Column, profile: ColumnProfile) -> Proposal | None:
    distribution = profile.distribution
    if not distribution or distribution.get("kind") == "constant":
        return None
    decimals = column.scale if column.type_name in {"decimal", "numeric"} else 0
    params: dict[str, Any] = {
        "distribution": distribution["kind"],
        "min": distribution.get("min"),
        "max": distribution.get("max"),
        "decimals": int(decimals or 0),
    }
    if distribution["kind"] in {"normal", "lognormal"}:
        params["mean"] = distribution["mean"]
        params["stddev"] = distribution["stddev"]
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator="number",
        params=params,
        confidence="medium",
        coverage=1.0,
        evidence=[
            _evidence(
                "distribution_fit",
                f"{distribution['kind']} fit between {distribution.get('min')} and "
                f"{distribution.get('max')}",
                1.0,
            )
        ],
    )


def _expression(
    table: Table, column: Column, sums: dict[str, SumExpression]
) -> Proposal | None:
    found = sums.get(f"{table.name}.{column.name}")
    if found is None:
        return None
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "derivable",
        generator="expression",
        params={
            "expression": found.expression(),
            "decimals": int(column.scale or 2),
        },
        confidence="high",
        coverage=clamp_coverage(found.match_rate),
        evidence=[_evidence("expression_match", found.detail(), found.match_rate)],
    )


def _constant(table: Table, column: Column, profile: ColumnProfile) -> Proposal | None:
    if profile.cardinality != "constant":
        return None
    if profile.top_values:
        value = profile.top_values[0].value
    elif profile.minimum is not None and profile.minimum == profile.maximum:
        value = profile.minimum
    else:
        return None
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator="constant",
        params={"value": value},
        confidence="high",
        coverage=1.0,
        evidence=[
            _evidence("single_value", f"every row holds {value!r}", 1.0)
        ],
    )


def _element_dictionary(
    table: Table, column: Column, profile: ColumnProfile, element
) -> Proposal | None:
    """Design document 7.2: a name or address column draws from its element.

    Only for columns where the data alone gives a poor answer. A fitted pattern
    over surnames produces letter soup; the element's dictionary produces names.
    Low-cardinality columns keep their observed weights instead, because there
    the data is the better evidence.
    """
    if element is None or element.generator != "lookup":
        return None
    if not column.is_character or profile.cardinality not in {"high", "unique"}:
        return None
    if column.bucket == "business_key":
        return None
    values = element.params.get("values")
    if not values:
        return None
    params = {k: v for k, v in element.params.items() if k != "dictionary"}
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator="lookup",
        params=params,
        confidence="high",
        coverage=1.0,
        provenance="catalog",
        evidence=[
            _evidence(
                "element_dictionary",
                f"maps to {element.name}, whose dictionary holds {len(values):,} "
                f"values; a pattern fitted to {profile.distinct_count:,} distinct "
                f"values would only reproduce their shape",
                1.0,
            )
        ],
    )


def _free_text(table: Table, column: Column, profile: ColumnProfile) -> Proposal:
    """Nothing fit. Say so, and flag it low so it reaches the review queue."""
    low = profile.min_length or 1
    high = profile.max_length or DEFAULT_TEXT_LENGTH
    # Never longer than the column holds: an empty column has no measured
    # lengths, and the default would write past a char(5).
    width = column.character_length() if column.is_character else None
    if width:
        high = min(high, width)
        low = min(low, high)
    if column.is_character and high >= WORDS_MIN_LENGTH:
        # Names, descriptions, addresses: words with spaces between them,
        # never one run of letters as long as the column.
        words = max(0, min(6, high // 8 - 1))
        generator, params = "pattern", {"regex": f"[A-Z][a-z]{{2,8}}( [A-Z][a-z]{{1,8}}){{0,{words}}}"}
    elif column.is_character:
        generator, params = "pattern", {"regex": f"[A-Za-z]{{{max(1, low)},{max(1, high)}}}"}
    elif column.is_numeric:
        generator, params = "number", {"distribution": "uniform", "min": 0, "max": 1000, "decimals": int(column.scale or 0)}
    else:
        generator, params = "constant", {"value": None}
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator=generator,
        params=params,
        confidence="low",
        coverage=None,
        provenance="physical",
        evidence=[
            _evidence(
                "fallback",
                "no profile signature fit; falling back to the column's type and length",
            )
        ],
        note="nothing in the data suggested a rule; please confirm or replace",
    )


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def propose_for_column(
    table: Table,
    column: Column,
    profile: ColumnProfile | None,
    orderings: list[DateOrdering],
    sums: dict[str, SumExpression],
    element=None,
    anchors: dict[str, str] | None = None,
) -> Proposal:
    # 1. Structure first. These cannot be overridden by anything in the data.
    if column.bucket == "system":
        return _system(table, column)

    reference = _reference(table, column)
    if reference is not None:
        return reference

    counter = _composite_key_counter(table, column)
    if counter is not None:
        return counter

    constraint = _code_from_constraint(table, column, profile)
    if constraint is not None:
        return constraint

    # 2. A tested expression beats a distribution: it is exact, not fitted.
    expression = _expression(table, column, sums)
    if expression is not None:
        return expression

    if profile is None or profile.row_count == 0:
        # No rows to measure. The schema still says what this column is, and a
        # target waiting to be filled is the ordinary case rather than an
        # error, so it gets a rule from its declared type instead of nothing.
        return _from_model(table, column)

    # 3. The profile signature, in the order of design document 7.2.
    if profile.null_count >= profile.row_count and column.nullable:
        return Proposal(
            table=table.name, column=column.name, bucket=column.bucket or "free",
            generator="constant", params={"value": None}, confidence="high",
            coverage=1.0,
            evidence=[_evidence("always_null", f"null in all {profile.row_count:,} rows", 1.0)],
        )

    constant = _constant(table, column, profile)
    if constant is not None:
        return constant

    dictionary = _element_dictionary(table, column, profile, element)
    if dictionary is not None:
        return dictionary

    if column.is_date:
        relative = _date_relative(table, column, profile, orderings, anchors)
        if relative is not None:
            return relative

    if profile.cardinality == "unique" and _unique(table, column, profile):
        if column.is_integer:
            sequence = _sequence(table, column, profile)
            if sequence is not None:
                return sequence
        pattern = _pattern(table, column, profile)
        if pattern is not None:
            return pattern

    if (profile.cardinality == "unique" and not _unique(table, column, profile)
            and column.is_character):
        # A small table's values, each once: its states, its group names.
        # Its dates and amounts are a range, below. Values that share a
        # fixed prefix -- CUST000012 -- are written in that shape.
        if format_literals(profile.format_regex()) >= DISTINCTIVE_FORMAT_LITERALS:
            pattern = _pattern(table, column, profile)
            if pattern is not None:
                return pattern
        observed = _weighted_lookup(table, column, profile)
        if observed is not None:
            return observed

    if profile.looks_like_a_code():
        lookup = _weighted_lookup(table, column, profile)
        if lookup is not None:
            return lookup

    if column.is_date:
        ranged = _date_range(table, column, profile)
        if ranged is not None:
            return ranged

    if column.is_integer:
        sequence = _sequence(table, column, profile)
        if sequence is not None:
            return sequence

    if column.is_numeric:
        number = _number(table, column, profile)
        if number is not None:
            return number

    if column.is_character:
        pattern = _pattern(table, column, profile)
        if pattern is not None:
            return pattern

    return _free_text(table, column, profile)


# Defaults by declared type, for a database with nothing in it yet. Deliberately
# plain: these are the rules a person would write knowing only the DDL, and
# anything cleverer would be inventing evidence.
_INTEGER_TYPES = ('int', 'bigint', 'smallint', 'tinyint')
_DECIMAL_TYPES = ('decimal', 'numeric', 'money', 'smallmoney', 'float', 'real')
_DATE_TYPES = ('date', 'datetime', 'datetime2', 'smalldatetime', 'datetimeoffset')
# Where a date column with nothing to measure is anchored. Stated, so a
# plan built twice is the same plan; override with SDG_AS_OF.
MODEL_ANCHOR = os.getenv("SDG_AS_OF", "2026-01-01")
_TEXT_TYPES = ('varchar', 'nvarchar', 'char', 'nchar', 'text', 'ntext')


def _from_model(table: Table, column: Column) -> Proposal:
    """A rule read off the column's declaration.

    Reached only when there is no profile, which means a target that holds no
    rows. Every branch here is a statement about the type rather than about any
    data, and says so: `physical` provenance, low confidence, and an evidence
    line naming the declaration it came from.
    """
    kind = (column.type_name or "").lower()
    width = column.max_length
    declared = f"declared {column.type_name}" + (
        f"({width})" if width and width > 0 and kind in _TEXT_TYPES else ""
    )

    def proposal(generator: str, params: dict, note: str) -> Proposal:
        return Proposal(
            table=table.name,
            column=column.name,
            bucket=column.bucket or "free",
            generator=generator,
            params=params,
            confidence="low",
            provenance="physical",
            evidence=[_evidence("physical", f"{declared}; no rows to measure")],
            note=note,
        )

    # A CHECK constraint is the strongest thing a schema can say: these values
    # and no others.
    if column.check_values:
        return proposal(
            "lookup",
            {"values": list(column.check_values)},
            "the values its CHECK constraint allows",
        )

    if column.is_primary_key or column.is_unique:
        if kind in _INTEGER_TYPES:
            return proposal("sequence", {"start": 1, "step": 1},
                            "a counter, because every row needs its own")
        if kind in _TEXT_TYPES:
            size = max(4, min(int(width or 12), 24))
            return proposal(
                "pattern",
                {"regex": f"[A-Z0-9]{{{size}}}", "unique": True},
                "a unique string of the declared width",
            )

    if kind in _INTEGER_TYPES:
        return proposal("number", {"distribution": "uniform", "min": 1, "max": 10000,
                                   "decimals": 0},
                        "a whole number, bounded by nothing but the type")
    if kind in _DECIMAL_TYPES:
        places = int(column.scale or 2)
        return proposal("number", {"distribution": "uniform", "min": 0,
                                   "max": 10 ** max(1, int((column.precision or 8) / 2)),
                                   "decimals": places},
                        f"a number to {places} places, from the declared precision")
    if kind in _DATE_TYPES:
        # A stated anchor, never the clock. A plan that means something
        # different depending on the day it is built is not a plan, and this
        # project spends real effort elsewhere making sure nothing reads the
        # time; a default here would be the one place that did.
        return proposal(
            "date_relative",
            {"anchor": MODEL_ANCHOR, "min_days": -3650, "max_days": 0},
            f"a date in the ten years to {MODEL_ANCHOR}",
        )
    if kind == "bit":
        return proposal("lookup", {"values": ["0", "1"]}, "a flag")
    if kind in _TEXT_TYPES:
        size = max(1, min(int(width or 10), 20))
        return proposal("pattern", {"regex": f"[A-Za-z]{{{size}}}"},
                        "letters to the declared width")

    # A type nothing here knows. Skipping is honest; guessing is not.
    return Proposal(
        table=table.name,
        column=column.name,
        bucket=column.bucket or "free",
        generator="skip",
        confidence="low",
        provenance="physical",
        evidence=[_evidence("physical", f"{declared}, which has no default rule")],
        note="give this column a rule, or profile a database that has rows in it",
    )


def propose(
    model: PhysicalModel,
    report: ProfileReport | None = None,
    sums: list[SumExpression] | None = None,
    mappings=None,
    catalog=None,
) -> list[Proposal]:
    """Propose a rule for every column in the model.

    `mappings` and `catalog` are optional: without them the proposals come from
    the data alone, which is the honest baseline. With them, a name or address
    column can be offered its element's dictionary instead of a pattern fitted
    to the letters that happen to be in the sample.
    """
    by_parent = {f"{s.parent_table}.{s.parent_column}": s for s in (sums or [])}
    proposals: list[Proposal] = []
    for table in model.tables:
        orderings = report.orderings_for(table.name) if report else []
        # Anchors are chosen for the whole table before any ceiling is, so a
        # ceiling can be rejected when it would depend on the column it bounds.
        anchors = anchor_map(orderings)
        for column in table.columns:
            profile = report.column(table.name, column.name) if report else None
            element = None
            if mappings is not None and catalog is not None:
                mapping = mappings.for_column(table.name, column.name)
                if mapping is not None and mapping.element:
                    element = catalog.element(mapping.element)
            proposal = propose_for_column(
                table, column, profile, orderings, by_parent, element, anchors
            )
            proposals.append(_fitted(table, column, _observed(column, profile, proposal)))
    return proposals


def _observed(column: Column, profile: ColumnProfile | None, proposal: Proposal) -> Proposal:
    """What the data says of every row, whatever rule writes the rest: how
    often the column is empty, whether its dates carry a time, and how often it
    holds a placeholder date such as 9999-12-31."""
    if (profile is None or not profile.row_count or proposal.generator == "skip"
            or (proposal.generator == "constant" and proposal.params.get("value") is None)):
        return proposal
    kinds = {e.get("kind") for e in proposal.evidence}
    # A link's empty rows come from the rows it links: a claim whose member's
    # plan has no network has none. A rate on top would count them twice.
    if (column.nullable and profile.null_rate > 0 and "null_rate" not in kinds
            and proposal.generator != "reference"):
        proposal.evidence.append(
            _evidence("null_rate", f"null in {profile.null_rate:.0%} of rows", profile.null_rate))
    if proposal.generator == "date_relative":
        params = dict(proposal.params)
        if profile.has_time_component and column.type_name in {
                "datetime", "datetime2", "smalldatetime", "datetimeoffset"}:
            params["with_time"] = True
        filled = profile.row_count - profile.null_count
        if profile.placeholders and filled:
            params["placeholders"] = [
                {"value": str(p["value"])[:10], "share": round(min(1.0, p["count"] / filled), 6)}
                for p in profile.placeholders]
            for p in profile.placeholders:
                proposal.evidence.append(_evidence(
                    "placeholder", f"{str(p['value'])[:10]} in {p['share']:.0%} of rows", p["share"]))
        proposal.params = params
    return proposal


def _fitted(table: Table, column: Column, proposal: Proposal) -> Proposal:
    """The proposal, or the column's own type when the proposal cannot fit it.

    A catalog dictionary of first names offered to a four-character column, or
    a landscape's codes wider than the column they would land in, fail as a
    truncation partway through a load. The column's declaration is the one
    thing certainly true about it, so where a rule cannot be stored there the
    rule comes from the declaration instead -- and says why.
    """
    from sdg.plan.editing import check_fits

    problems = check_fits(proposal.generator, dict(proposal.params or {}), column)
    if not problems or proposal.provenance == "physical":
        return proposal
    fallback = _from_model(table, column)
    fallback.evidence = [
        _evidence("does_not_fit", f"{problems[0]} -- so it is generated from "
                  f"{column.type_description()} instead"),
        *fallback.evidence,
    ]
    return fallback

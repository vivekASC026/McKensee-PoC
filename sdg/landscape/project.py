"""Say what a landscape implies about a particular target.

The plan builder consumes a ProfileReport: measurements keyed by table and
column. A landscape is measurements keyed by element. Projecting one onto a
target turns the second into the first -- for every column of the target that
carries an element the landscape knows, a measurement is written as though that
column had been profiled.

It had not been. Nothing reads the target's data; a target may hold no rows at
all, and generating into an empty database is the ordinary case rather than the
awkward one. What is being said is "a column holding this element looks like
this, in the landscape you are generating from".

Every projected measurement is marked, because a measurement of a thing and a
measurement of something else carrying the same element are not the same claim
and a plan should never be unable to tell them apart.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sdg.landscape.model import LandscapeShape
from sdg.profiling.model import (
    ColumnProfile,
    HistogramBin,
    ProfileReport,
    RatioProfile,
    SkippedAnalysis,
    TopValue,
)

# What a projected measurement is worth saying about. A column whose element
# the landscape has never seen gets nothing, and the plan proposes from the
# column's own type instead -- which is what it would have done anyway.
PROJECTED = "projected from the landscape"


def project(
    shape: LandscapeShape,
    model,
    mappings,
    *,
    rows_per_table: dict[str, int] | None = None,
    subjects: dict[str, str] | None = None,
) -> ProfileReport:
    """A ProfileReport for `model`, written from what the landscape holds.

    `subjects` maps a target table to the store table whose kind of thing it
    holds -- "my orders are like their claim lines". Stated rather than
    guessed, because the alternative is deciding that a column called
    customer_id means the same as one called MEME_CK, and being wrong about
    that silently produces a cascade of the wrong shape.

    It is only needed where the key columns carry no shared element. Where they
    do, the elements match the relationship on their own.
    """
    element_of = {
        f"{m.table}.{m.column}": m.element
        for m in mappings.mappings if m.element
    }

    report = ProfileReport(
        connection={"projected_from": shape.derived_from, "source": shape.source},
        profiled=PROJECTED,
    )

    for table in model.tables:
        rows = (rows_per_table or {}).get(table.name, shape.rows_seen)
        for column in table.columns:
            qualified = f"{table.name}.{column.name}"
            element = element_of.get(qualified)
            known = shape.element(element) if element else None
            if known is None:
                continue
            # The target's own type decides. Where what the landscape holds
            # cannot be stored in this column -- text in a number, a value
            # longer than the column, a number past its precision -- the
            # landscape is not used for it, and the column is generated from
            # its type alone, as though the element were unknown.
            misfit = fits(column, known)
            if misfit:
                report.skipped.append(SkippedAnalysis(
                    table=table.name, analysis=f"landscape for {column.name}",
                    reason=f"{element}: {misfit}; generated from {column.type_description()} instead",
                ))
                continue
            report.columns.append(_as_column(table.name, column.name, known, rows))

    report.ratios = _ratios(shape, model, element_of, subjects or {})
    return report


def _as_column(table: str, column: str, known, rows: int) -> ColumnProfile:
    """One element's shape, written as though this column had been measured."""
    profile = ColumnProfile(
        table=table,
        column=column,
        row_count=rows,
        null_rate=known.null_rate,
        null_count=int(rows * known.null_rate),
        distinct_ratio=known.distinct_ratio,
        distinct_count=max(1, int(rows * known.distinct_ratio)),
        approximate_distinct=True,
        cardinality=known.cardinality,
        minimum=known.minimum,
        maximum=known.maximum,
        mean=known.mean,
        stddev=known.stddev,
        min_length=known.min_length,
        max_length=known.max_length,
        has_time_component=known.has_time_component,
        distribution=known.distribution,
        sample_size=0,
    )
    if known.format_regex:
        profile.fitted_format = {"regex": known.format_regex, "coverage": 1.0,
                                 "source": PROJECTED}
    if known.values:
        # Counts rather than weights, because that is what a profile carries
        # and the proposer reads. The weights are the landscape's.
        profile.top_values = [
            TopValue(
                value=v["value"],
                count=max(1, int(rows * v["weight"])),
                share=v["weight"],
            )
            for v in known.values
        ]
    return profile


_INTEGER_RANGE = {
    "tinyint": (0, 255),
    "smallint": (-32_768, 32_767),
    "int": (-2_147_483_648, 2_147_483_647),
    "bigint": (-9_223_372_036_854_775_808, 9_223_372_036_854_775_807),
}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def fits(column, known) -> str | None:
    """Why the landscape's values cannot be stored in this column, or None.

    Checked against what the landscape actually holds -- its range, its
    lengths, its listed values -- rather than against the type it was stored
    as: a number held as text fits a text column of the right width, and a
    code of digits fits a number column. What cannot be stored is refused.
    """
    values = [v.get("value") for v in (known.values or [])]
    seen = [v for v in values + [known.minimum, known.maximum] if v is not None]

    if column.is_date:
        for value in seen:
            if isinstance(value, (date, datetime)):
                continue
            if not _DAY.match(str(value)):
                return f"holds {value!r}, which is not a date"
        return None

    if column.type_name == "bit":
        allowed = {"0", "1", "true", "false"}
        bad = [v for v in values if str(v).strip().lower() not in allowed]
        return f"holds {bad[0]!r}, which is not a bit" if bad else None

    if column.is_numeric:
        numbers = []
        for value in seen:
            number = _number(value)
            if number is None:
                return f"holds {value!r}, which is not a number"
            numbers.append(number)
        if column.is_integer:
            low, high = _INTEGER_RANGE[column.type_name]
            for number in numbers:
                if number != int(number):
                    return f"holds {number}, which is not a whole number"
                if not low <= number <= high:
                    return f"holds {number:,.0f}, beyond what {column.type_name} can hold"
        elif column.type_name in ("decimal", "numeric") and column.precision:
            whole = column.precision - (column.scale or 0)
            for number in numbers:
                if abs(number) >= 10 ** whole:
                    return (f"holds {number:,}, more than {whole} digits before the point "
                            f"that {column.type_description()} allows")
        return None

    if column.is_character:
        width = column.character_length()
        if width is None:
            return None  # (max): anything fits
        longest = known.max_length or 0
        for value in seen:
            if not isinstance(value, (date, datetime)):
                longest = max(longest, len(str(value).strip()))
        if longest > width:
            return f"holds values up to {longest} characters; the column holds {width}"
        return None

    return None


def _entity(table) -> str | None:
    """The column naming one row of this table, apart from its parent's key.

    A claim's is its claim id; a line's is its line number, the part of its
    key that is not the claim it belongs to.
    """
    keys = sorted((c for c in table.columns if c.is_primary_key),
                  key=lambda c: c.primary_key_ordinal or 0)
    in_foreign = {name for fk in table.foreign_keys for name in fk.columns}
    own = [c.name for c in keys if c.name not in in_foreign]
    if own:
        return own[0]
    return keys[0].name if len(keys) == 1 else None


def _ratios(
    shape: LandscapeShape, model, element_of: dict[str, str],
    subjects: dict[str, str],
) -> list[RatioProfile]:
    """Children per parent, matched onto this target's foreign keys.

    A relationship in the landscape is between two elements. A foreign key in
    the target is between two columns. They meet where the target's key carries
    the element the landscape measured -- which is the same join a pattern uses
    to cross from one database to another, one level up.
    """
    out: list[RatioProfile] = []
    for table in model.tables:
        for fk in table.foreign_keys:
            if not fk.columns or not fk.parent_columns:
                continue
            child_column = fk.columns[0]
            parent_column = fk.parent_columns[0]
            # A stated mapping first. A person who has said what their tables
            # hold knows something no amount of column matching can recover.
            known = None
            child_subject = subjects.get(table.name)
            parent_subject = subjects.get(fk.parent_table)
            if child_subject and parent_subject:
                known = shape.between(child_subject, parent_subject)

            if known is None:
                child_element = element_of.get(f"{table.name}.{child_column}")
                parent_element = element_of.get(f"{fk.parent_table}.{parent_column}")
                if not child_element or not parent_element:
                    continue
                # Which thing per which parent: claims per member, not claim
                # lines per member. The thing is what names this table's rows.
                entity = _entity(table)
                thing = element_of.get(f"{table.name}.{entity}") if entity else None
                known = (
                    (shape.relationship(thing, parent_element) if thing else None)
                    or shape.relationship(child_element, parent_element)
                    or shape.relationship(parent_element, parent_element)
                    or shape.relationship(child_element, child_element)
                )
            if known is None:
                continue

            out.append(RatioProfile(
                child_table=table.name,
                child_column=child_column,
                parent_table=fk.parent_table,
                parent_column=parent_column,
                kind="declared" if fk.kind == "declared" else "inferred",
                parents=known.parents,
                children=known.children,
                minimum=known.minimum,
                maximum=known.maximum,
                mean=known.mean,
                stddev=known.stddev,
                median=known.median,
                p90=known.p90,
                p99=known.p99,
                histogram=[
                    HistogramBin(children=b["children"], weight=b["weight"])
                    for b in (known.histogram or [])
                ],
            ))
    return out


def landscape_keys(model, mappings, shape: LandscapeShape) -> list[str]:
    """Keys an empty table cannot show, said by its elements and the landscape.

    A key nobody declared is inferred from rows -- every claim's member exists
    among members -- and an empty table has none. Its columns still carry
    elements: a claim's MEME_CK is MEMBER_KEY, which is what names a row of
    the member table. Where the landscape measured this table's thing per that
    key -- claims per member -- the key is added. A table with several such
    columns joins the one its rows are most closely held by: claims per member
    rather than claims per subscriber.
    """
    from sdg.discovery.model import ForeignKey

    element_of = {f"{m.table}.{m.column}": m.element for m in mappings.mappings if m.element}
    named_by: dict[str, tuple[str, str]] = {}
    for table in model.tables:
        keys = [c for c in table.columns if c.is_primary_key]
        if len(keys) == 1:
            element = element_of.get(f"{table.name}.{keys[0].name}")
            if element:
                named_by.setdefault(element, (table.name, keys[0].name))

    added: list[str] = []
    for table in model.tables:
        if table.row_count:
            continue
        entity = _entity(table)
        thing = element_of.get(f"{table.name}.{entity}") if entity else None
        if not thing:
            continue
        joined = {name for fk in table.foreign_keys for name in fk.columns}
        best = None
        for column in table.columns:
            if column.is_primary_key or column.name in joined:
                continue
            element = element_of.get(f"{table.name}.{column.name}")
            parent = named_by.get(element) if element else None
            if parent is None or parent[0] == table.name:
                continue
            measured = shape.relationship(thing, element)
            if measured is None:
                continue
            if best is None or measured.mean < best[2].mean:
                best = (column.name, parent, measured)
        if best is None:
            continue
        column, (parent_table, parent_column), measured = best
        table.foreign_keys.append(ForeignKey(
            name=None, columns=[column], parent_table=parent_table,
            parent_columns=[parent_column], kind="inferred",
            evidence=[f"{column} carries {element_of[f'{table.name}.{column}']}, the key of "
                      f"{parent_table}; the landscape holds {measured.mean:.2f} "
                      f"{thing} per one ({measured.observed_in[0]})"],
        ))
        added.append(f"{table.name}.{column} -> {parent_table}")
    return added


def landscape_sums(model, mappings, shape: LandscapeShape) -> list:
    """Totals in an empty table that the landscape shows to be sums.

    Matched on elements: a claim table's CHARGE_AMOUNT is the sum of its lines'
    CHARGE_AMOUNT when the lines join the claim on the key the landscape
    grouped by. Only for a total whose own table has no rows -- a table with
    rows shows it for itself, or shows that it is not.
    """
    from sdg.plan.detect import SumExpression

    element_of = {f"{m.table}.{m.column}": m.element for m in mappings.mappings if m.element}
    out = []
    for child in model.tables:
        for fk in child.foreign_keys:
            if len(fk.columns) != 1:
                continue
            parent = next((t for t in model.tables if t.name == fk.parent_table), None)
            if parent is None or parent.row_count:
                continue
            key = element_of.get(f"{child.name}.{fk.columns[0]}")
            for known in shape.sums:
                if known.key_element != key:
                    continue
                totals = [c.name for c in parent.columns
                          if element_of.get(f"{parent.name}.{c.name}") == known.total_element
                          and c.name not in fk.parent_columns]
                parts = [c.name for c in child.columns
                         if element_of.get(f"{child.name}.{c.name}") == known.part_element
                         and c.name not in fk.columns]
                for total in totals:
                    for part in parts:
                        out.append(SumExpression(
                            parent_table=parent.name, parent_column=total,
                            child_table=child.name, child_column=part,
                            rows_compared=known.groups, matched=known.matched,
                        ))
    return out

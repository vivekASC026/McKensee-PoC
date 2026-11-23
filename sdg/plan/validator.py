"""Check a plan before anything runs.

Seven conditions block a run. Each one is a case where generation would either
fail outright or produce data that quietly fails a later check, and each one is
cheap to detect now and expensive to discover after a load.

Everything else is a warning: worth showing, not worth stopping for.
"""

from __future__ import annotations

from sdg.discovery.model import CHARACTER_TYPES
from sdg.engine.pattern import PatternError, pattern_space
from sdg.plan.model import Plan, Warning_

# The build brief: a key whose pattern cannot comfortably outnumber the rows
# being asked for will collide.
PATTERN_SPACE_MULTIPLE = 10


def validate(plan: Plan) -> list[Warning_]:
    """Return every warning. Any with severity 'block' stops generation."""
    warnings: list[Warning_] = []

    for table in plan.tables:
        if table.mode == "reuse" and table.source is None:
            warnings.append(
                Warning_(
                    code="reuse_without_source",
                    severity="block",
                    message=(
                        f"{table.name} is set to reuse but no source is bound. "
                        f"Bind a connection, table, and key column."
                    ),
                    table=table.name,
                )
            )
        if table.mode != "generate":
            continue

        rows = table.effective_count or table.count or 0

        if table.count is None and table.ratio is None:
            warnings.append(
                Warning_(
                    code="root_without_count",
                    severity="block",
                    message=(
                        f"{table.name} has no generated parent, so it is a root and "
                        f"needs an absolute row count."
                    ),
                    table=table.name,
                )
            )
        if table.ratio is None and table.driving_parent:
            warnings.append(
                Warning_(
                    code="child_without_ratio",
                    severity="block",
                    message=(
                        f"{table.name} is a child of {table.driving_parent} and needs a "
                        f"ratio saying how many rows each parent gets."
                    ),
                    table=table.name,
                )
            )

        for field in table.fields:
            warnings.extend(_check_field(table, field, rows))

    warnings.extend(_check_order(plan))
    warnings.extend(_check_references_resolve(plan))
    return warnings


def _check_field(table, field, rows: int) -> list[Warning_]:
    warnings: list[Warning_] = []
    qualified = f"{table.name}.{field.name}"

    if field.generator == "skip":
        return warnings

    # A character business key needs a format. An integer one is covered by a
    # sequence, so the rule is scoped to the case that actually fails.
    if field.bucket == "business_key" and _is_character(field):
        has_format = field.generator == "pattern" and field.params.get("regex")
        if not has_format and field.generator != "sequence":
            warnings.append(
                Warning_(
                    code="business_key_without_format",
                    severity="block",
                    message=(
                        f"{qualified} is a business key with no format to generate from. "
                        f"Give it a pattern, or map it to an element that has one."
                    ),
                    table=table.name,
                    field=field.name,
                )
            )

    if field.bucket == "derivable" and field.generator != "expression":
        # A person who has looked at the column and chosen another rule has
        # settled this. The tool read it as derived; that reading is a proposal
        # like every other one it makes, and the whole point of the review step
        # is that somebody who knows the schema can disagree. Blocking an
        # override would leave the plan permanently unbuildable with no way to
        # say "no, it is just a number" -- which is exactly what happened the
        # first time this tool was pointed at a schema it had not seen.
        #
        # Still said, because being derived-looking is worth knowing even once
        # it has been overruled.
        overridden = field.user_override or field.provenance == "user"
        warnings.append(
            Warning_(
                code="derivable_without_expression",
                severity="warn" if overridden else "block",
                message=(
                    f"{qualified} is a derived column but has no expression, so it "
                    f"would be generated independently of the values it should "
                    f"follow." + (" You chose this rule for it." if overridden else "")
                ),
                table=table.name,
                field=field.name,
            )
        )

    if field.generator == "pattern" and field.params.get("unique") and rows:
        regex = field.params.get("regex", "")
        try:
            space = pattern_space(regex)
        except PatternError as exc:
            warnings.append(
                Warning_(
                    code="pattern_not_usable",
                    severity="block",
                    message=f"{qualified}: {exc}",
                    table=table.name,
                    field=field.name,
                )
            )
        else:
            if space < rows * PATTERN_SPACE_MULTIPLE:
                warnings.append(
                    Warning_(
                        code="pattern_space_too_small",
                        severity="block",
                        message=(
                            f"{qualified}: the pattern {regex} can produce {space:,} "
                            f"values, which is less than {PATTERN_SPACE_MULTIPLE} times "
                            f"the {rows:,} rows requested. Widen the format or ask for "
                            f"fewer rows."
                        ),
                        table=table.name,
                        field=field.name,
                    )
                )

    # The schema cannot compare two values, so a band upside down is caught
    # here rather than as an error partway through generation.
    bounds = {"number": ("min", "max"), "date_relative": ("min_days", "max_days")}
    if field.generator in bounds:
        low_name, high_name = bounds[field.generator]
        low, high = field.params.get(low_name), field.params.get(high_name)
        try:
            upside_down = low is not None and high is not None and float(low) > float(high)
        except (TypeError, ValueError):
            upside_down = False  # not numbers: the generator says so with context
        if upside_down:
            warnings.append(
                Warning_(
                    code="bounds_reversed",
                    severity="block",
                    message=(
                        f"{qualified}: {low_name} {low} is above {high_name} {high}. "
                        f"Swap them or correct one."
                    ),
                    table=table.name,
                    field=field.name,
                )
            )

    if field.confidence == "low":
        warnings.append(
            Warning_(
                code="low_confidence_rule",
                severity="warn",
                message=(
                    f"{qualified} was resolved at low confidence: {field.generator}. "
                    f"Check it before relying on this data."
                ),
                table=table.name,
                field=field.name,
            )
        )

    # A column whose values carry a check digit cannot be reproduced by a
    # pattern: the shape will be right and the digit will not.
    if field.generator == "pattern" and any(
        e.kind == "check_digit" for e in (field.evidence or [])
    ):
        warnings.append(
            Warning_(
                code="check_digit_not_reproduced",
                severity="warn",
                message=(
                    f"{qualified}: the values carry a check digit, and a pattern "
                    f"reproduces the shape but not the digit. Generated values will "
                    f"not pass a validator that tests it."
                ),
                table=table.name,
                field=field.name,
            )
        )

    return warnings


def _is_character(field) -> bool:
    data_type = (field.data_type or "").split("(")[0].strip().lower()
    return data_type in CHARACTER_TYPES


def _check_references_resolve(plan: Plan) -> list[Warning_]:
    """Every foreign key a generated table writes must have somewhere to point.

    Choosing a mode for a table is choosing it for that table's rows, but the
    foreign keys of other tables do not go away. Exclude a table a generated
    one references and the plan is asking for a column whose parent does not
    exist -- the run gets as far as the first row and stops, after the plan has
    been built, reviewed and declared ready.

    The condition is cheap to see here. A reference field names its parent
    table; that table has to be generated as well, or reused with a source
    bound, for the key to resolve. Anything else is a reference into nothing.

    Checked against the plan's own reference fields rather than the model's
    foreign keys, because the plan is what will run: a key the mapping turned
    into something other than a reference is not this check's business.
    """
    warnings: list[Warning_] = []
    modes = {table.name: table.mode for table in plan.tables}

    for table in plan.tables:
        if table.mode != "generate":
            continue
        for field in table.fields:
            from sdg.plan.model import points_at

            parent = points_at(field.params, field.generator)
            if not parent:
                continue
            qualified = f"{table.name}.{field.name}"
            mode = modes.get(str(parent))

            if mode is None:
                warnings.append(
                    Warning_(
                        code="reference_to_unknown_table",
                        severity="block",
                        message=(
                            f"{qualified} points at {parent}, which is not in the "
                            f"plan at all. Discover it, or give this column a rule "
                            f"that does not reference a table."
                        ),
                        table=table.name,
                        field=field.name,
                    )
                )
            elif mode == "exclude":
                warnings.append(
                    Warning_(
                        code="reference_to_excluded_table",
                        severity="block",
                        message=(
                            f"{qualified} points at {parent}, which is excluded, so "
                            f"there are no keys for it to use. Set {parent} to "
                            f"generate or reuse, or exclude {table.name} as well."
                        ),
                        table=table.name,
                        field=field.name,
                    )
                )

    return warnings


def _check_order(plan: Plan) -> list[Warning_]:
    """Every generated table must appear in the generation order."""
    warnings: list[Warning_] = []
    ordered = set(plan.generation_order)
    for table in plan.tables:
        if table.mode == "generate" and table.name not in ordered:
            warnings.append(
                Warning_(
                    code="table_not_in_generation_order",
                    severity="block",
                    message=f"{table.name} is set to generate but is not in the generation order.",
                    table=table.name,
                )
            )
    return warnings


def blocking(warnings: list[Warning_]) -> list[Warning_]:
    return [w for w in warnings if w.severity == "block"]


def summarize(warnings: list[Warning_]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for warning in warnings:
        counts[warning.code] = counts.get(warning.code, 0) + 1
    return dict(sorted(counts.items()))

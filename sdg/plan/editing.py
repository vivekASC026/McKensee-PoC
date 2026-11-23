"""What a person is allowed to change about a rule, and how to show it.

Anything the tool worked out for itself has to be changeable, so this describes
every generator's parameters in enough detail for an interface to build a form
for them. The description lives here rather than in the interface because the
generators are defined here: one source of truth, and a new generator cannot be
added without saying how it is edited.

Structural rules are editable too, but they are marked, because the schema
forced them and overriding one usually produces data the post-run validator
will reject.
"""

from __future__ import annotations

from typing import Any

# Rules the database's own shape decided. A person may still override one, but
# the interface says plainly what they are taking on.
STRUCTURAL_WARNING = (
    "The schema forced this rule. Changing it is likely to produce data that "
    "fails the checks after a run."
)

GENERATORS: dict[str, dict[str, Any]] = {
    "pattern": {
        "label": "Pattern",
        "summary": "Values matching a regular expression.",
        "fields": [
            {
                "name": "regex",
                "label": "Format",
                "type": "text",
                "required": True,
                "help": "Literals, character classes such as [A-Z] and \\d, and counted "
                        "repeats such as {3} or {2,4}.",
                "placeholder": "[A-Z]{3}\\d{8}",
            },
            {
                "name": "unique",
                "label": "Every value distinct",
                "type": "boolean",
                "help": "Walks the pattern space so no two rows collide.",
            },
        ],
    },
    "lookup": {
        "label": "Lookup",
        "summary": "Drawn from a list, optionally weighted.",
        "fields": [
            {
                "name": "values",
                "label": "Values",
                "type": "lines",
                "required": True,
                "help": "One per line.",
            },
            {
                "name": "weights",
                "label": "Weights",
                "type": "numbers",
                "help": "One per line, in the same order as the values. Leave empty to "
                        "draw evenly.",
            },
            {
                "name": "record_key",
                "label": "Shared draw",
                "type": "text",
                "help": "Columns naming the same key draw the same record, which is how "
                        "ZIP, city, and state stay consistent.",
            },
            {
                "name": "field",
                "label": "Field within the record",
                "type": "text",
                "help": "Which key to read when the values are records rather than "
                        "single values.",
            },
        ],
    },
    "number": {
        "label": "Number",
        "summary": "Drawn from a distribution and clamped to a range.",
        "fields": [
            {
                "name": "distribution",
                "label": "Distribution",
                "type": "select",
                "required": True,
                "options": ["uniform", "normal", "lognormal"],
            },
            {"name": "min", "label": "Lowest", "type": "number"},
            {"name": "max", "label": "Highest", "type": "number"},
            {
                "name": "mean",
                "label": "Mean",
                "type": "number",
                "help": "For a log-normal, this describes the underlying normal.",
            },
            {"name": "stddev", "label": "Standard deviation", "type": "number"},
            {"name": "decimals", "label": "Decimal places", "type": "integer"},
        ],
    },
    "date_relative": {
        "label": "Date, relative",
        "summary": "An anchor plus a gap, so dates keep their order.",
        "fields": [
            {
                "name": "anchor",
                "label": "Anchor",
                "type": "text",
                "required": True,
                "help": "A column in this row, a schema.table.COLUMN in a parent row, or "
                        "a fixed date such as 2020-01-01.",
            },
            {"name": "min_days", "label": "Earliest, in days after", "type": "integer"},
            {"name": "max_days", "label": "Latest, in days after", "type": "integer"},
            {
                "name": "format",
                "label": "Written as",
                "type": "text",
                "help": "For a date kept in a text column, how it is written: "
                        "%Y%m%d for 20250828, %m/%d/%Y for 08/28/2025. Empty "
                        "for a date column.",
            },
            {
                "name": "upper_anchor",
                "label": "Never later than",
                "type": "text",
                "help": "Another column, which bounds the window from above.",
            },
            {
                "name": "max_date",
                "label": "Never later than (fixed)",
                "type": "text",
                "help": "An absolute ceiling, as an ISO date.",
            },
        ],
    },
    "sequence": {
        "label": "Sequence",
        "summary": "Counts upward, across the table or inside each parent.",
        "fields": [
            {"name": "start", "label": "Starts at", "type": "integer", "required": True},
            {"name": "step", "label": "Steps by", "type": "integer"},
            {
                "name": "scope",
                "label": "Counts",
                "type": "select",
                "options": ["table", "parent"],
                "help": "Across the whole table, or restarting inside each parent row.",
            },
        ],
    },
    "reference": {
        "label": "Reference",
        "summary": "Points at a real row in a parent table.",
        "fields": [
            {"name": "parent_table", "label": "Parent table", "type": "text", "required": True},
            {"name": "parent_column", "label": "Parent key", "type": "text", "required": True},
            {
                "name": "pick",
                "label": "Which parent",
                "type": "select",
                "options": ["driving", "hash"],
                "help": "The row that drove this one into existence, or one picked from "
                        "the parent's whole key set.",
            },
        ],
    },
    "expression": {
        "label": "Expression",
        "summary": "Calculated from other columns.",
        "fields": [
            {
                "name": "expression",
                "label": "Expression",
                "type": "text",
                "required": True,
                "help": "Arithmetic and a small set of functions over other columns. "
                        "sum(schema.table.COLUMN) totals a child table.",
                "placeholder": "sum(dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT)",
            },
            {"name": "decimals", "label": "Decimal places", "type": "integer"},
        ],
    },
    "constant": {
        "label": "Constant",
        "summary": "The same value in every row.",
        "fields": [{"name": "value", "label": "Value", "type": "text", "required": True}],
    },
    "street_address": {
        "label": "Street address",
        "summary": "A house number and a street from a list: 4182 Oak Ave.",
        "fields": [
            {"name": "min_number", "label": "Lowest house number", "type": "integer"},
            {"name": "max_number", "label": "Highest house number", "type": "integer"},
        ],
    },
    "template": {
        "label": "Template",
        "summary": "Other columns of the row written into a fixed shape.",
        "fields": [
            {
                "name": "template",
                "label": "Template",
                "type": "text",
                "required": True,
                "help": "Columns in braces: {FIRST_NAME}.{LAST_NAME}@example.com",
                "placeholder": "{FIRST_NAME}.{LAST_NAME}@example.com",
            },
        ],
    },
    "skip": {
        "label": "Leave to the database",
        "summary": "Nothing is written; the column keeps its default, identity, or "
                   "computed value.",
        "fields": [],
    },
}


def generator_catalog() -> list[dict[str, Any]]:
    """Every generator and its editable fields, for building a form."""
    return [
        {
            "name": name,
            "label": spec["label"],
            "summary": spec["summary"],
            "fields": spec["fields"],
        }
        for name, spec in GENERATORS.items()
    ]


# What each field type is called when something else was typed into it.
_TYPE_WORDS = {
    "integer": "a whole number",
    "number": "a number",
    "numbers": "numbers, one per line",
    "boolean": "a yes or no",
}


def _not_a(label: str, kind: str, value: Any) -> str:
    return f"{label} needs {_TYPE_WORDS.get(kind, 'a ' + kind)}. {value!r} is not."


def coerce(generator: str, params: dict[str, Any]) -> dict[str, Any]:
    """Put edited parameters back into the types the engine expects.

    A form hands back strings. The engine wants numbers where it asked for
    numbers, and a parameter left empty should be absent rather than set to an
    empty string, which would read as a real value.
    """
    spec = GENERATORS.get(generator)
    if spec is None:
        return dict(params)

    by_name = {field["name"]: field for field in spec["fields"]}
    cleaned: dict[str, Any] = {}

    for key, value in params.items():
        field = by_name.get(key)
        if value is None or value == "" or value == []:
            continue
        if field is None:
            cleaned[key] = value
            continue

        kind = field["type"]
        try:
            if kind == "integer":
                cleaned[key] = int(value)
            elif kind == "number":
                cleaned[key] = float(value)
            elif kind == "boolean":
                cleaned[key] = _boolean(value)
            elif kind == "numbers":
                numbers = [float(v) for v in value if str(v).strip() != ""]
                if numbers:
                    cleaned[key] = numbers
            elif kind == "lines":
                items = [v for v in value if str(v).strip() != ""] if isinstance(value, list) else value
                if items:
                    cleaned[key] = items
            else:
                cleaned[key] = value
        except (TypeError, ValueError) as exc:
            raise ValueError(_not_a(field["label"], kind, value)) from exc

    return cleaned


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _boolean(value: Any) -> bool:
    """A yes or no from a form. bool("false") is True, so strings are read."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(value)


# Ends every problem check_fits reports, so a caller can tell a value too wide
# for its column from any other reason a rule was refused.
TOO_WIDE = "The load would stop partway through."


def check_fits(generator: str, params: dict[str, Any], column: Any) -> list[str]:
    """Problems an edit would only reveal partway through a load.

    A pattern wider than its column does not fail when it is saved, or when the
    plan is built, or in a dry run of six rows. It fails as a truncation error
    from the driver after tens of thousands of rows are already committed, so
    it is worth catching at the point someone types it.
    """
    if column is None or not getattr(column, "is_character", False):
        return []
    width = column.character_length()
    if not width or width < 0:
        return []

    longest: int | None = None
    if generator == "pattern" and params.get("regex"):
        from sdg.engine.pattern import PatternError, compile_pattern

        try:
            longest = compile_pattern(str(params["regex"])).widest()
        except PatternError:
            return []  # already reported by check()
    elif generator == "lookup" and params.get("values"):
        # Whole records -- a pattern's addresses -- are measured by the field
        # this column takes from them.
        field = params.get("field")
        values = [str(v.get(field, "")) if isinstance(v, dict) and field else v
                  for v in params["values"]]
        values = [v for v in values if isinstance(v, str)]
        longest = max((len(v) for v in values), default=None)
    elif generator == "constant" and isinstance(params.get("value"), str):
        longest = len(params["value"])

    if longest is not None and longest > width:
        return [
            f"{column.name} holds {width} characters and this rule can produce "
            f"{longest}. {TOO_WIDE}"
        ]
    return []


def check(generator: str, params: dict[str, Any]) -> list[str]:
    """Problems a person should see before saving, phrased for them."""
    spec = GENERATORS.get(generator)
    if spec is None:
        return [f"There is no generator called {generator}."]

    problems: list[str] = []
    for field in spec["fields"]:
        value = params.get(field["name"])
        if field.get("required") and (value is None or value == "" or value == []):
            problems.append(f"{field['label']} is needed for a {spec['label'].lower()} rule.")

    if generator == "pattern" and params.get("regex"):
        from sdg.engine.pattern import PatternError, compile_pattern

        try:
            compile_pattern(str(params["regex"]))
        except PatternError as exc:
            problems.append(f"That format cannot be used: {exc}")

    if generator == "expression" and params.get("expression"):
        from sdg.engine.expressions import ExpressionError, compile_expression

        try:
            compile_expression(str(params["expression"]))
        except ExpressionError as exc:
            problems.append(f"That expression cannot be used: {exc}")

    if generator == "lookup":
        values = params.get("values") or []
        weights = params.get("weights")
        if weights and len(weights) != len(values):
            problems.append(
                f"There are {len(values)} values but {len(weights)} weights; they have "
                f"to match, or leave the weights empty."
            )

    if generator == "number":
        low, high = params.get("min"), params.get("max")
        if low is not None and high is not None and float(low) > float(high):
            problems.append("The lowest value is above the highest.")
        if params.get("distribution") in {"normal", "lognormal"}:
            if params.get("mean") is None or params.get("stddev") is None:
                problems.append(
                    "A normal or log-normal distribution needs a mean and a standard "
                    "deviation."
                )

    if generator == "date_relative":
        low, high = params.get("min_days"), params.get("max_days")
        if low is not None and high is not None and int(low) > int(high):
            problems.append("The earliest gap is larger than the latest.")

    return problems


# ---------------------------------------------------------------------------
# How many children each parent gets
# ---------------------------------------------------------------------------

# The measured shape is a histogram, which is the honest answer and not
# something anyone should retype. So the choice offered is between keeping it
# and replacing it with a shape a person can state in a sentence.
RATIO_SHAPES: dict[str, dict[str, Any]] = {
    "measured": {
        "label": "As measured",
        "summary": "The distribution found in the source, tail and all.",
        "fields": [],
    },
    "fixed": {
        "label": "A fixed number",
        "summary": "Every parent gets the same number of children.",
        "fields": [
            {
                "name": "value",
                "label": "Children per parent",
                "type": "number",
                "required": True,
                "help": "A fraction is allowed: 2.5 gives half the parents three.",
            },
        ],
    },
    "range": {
        "label": "Between two numbers",
        "summary": "A count drawn evenly between a low and a high.",
        "fields": [
            {"name": "min", "label": "Fewest", "type": "integer", "required": True},
            {"name": "max", "label": "Most", "type": "integer", "required": True},
        ],
    },
}

# Offered with both replacement shapes, because a flat ratio with no childless
# share is the thing that makes generated data look unlike the real thing.
ZERO_SHARE_FIELD: dict[str, Any] = {
    "name": "zero_share",
    "label": "Share with no children",
    "type": "number",
    "help": "Between 0 and 1. Drawn before the count, so it lowers the average.",
}


def ratio_shapes() -> list[dict[str, Any]]:
    """Every shape a children-per-parent rule can take, for building a form."""
    return [
        {
            "name": name,
            "label": shape["label"],
            "summary": shape["summary"],
            "fields": (
                list(shape["fields"]) + [ZERO_SHARE_FIELD] if shape["fields"] else []
            ),
        }
        for name, shape in RATIO_SHAPES.items()
    ]


def coerce_ratio(shape: str, params: dict[str, Any]) -> dict[str, Any]:
    """Put an edited ratio back into the types the planner expects."""
    spec = RATIO_SHAPES.get(shape)
    if spec is None:
        return dict(params)
    by_name = {f["name"]: f for f in list(spec["fields"]) + [ZERO_SHARE_FIELD]}
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        field = by_name.get(key)
        if value is None or value == "":
            continue
        if field is None:
            cleaned[key] = value
            continue
        try:
            cleaned[key] = int(value) if field["type"] == "integer" else float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(_not_a(field["label"], field["type"], value)) from exc
    return cleaned


def check_ratio(shape: str, params: dict[str, Any]) -> list[str]:
    """Problems with an edited ratio, phrased for the person who typed it."""
    spec = RATIO_SHAPES.get(shape)
    if spec is None:
        return [f"There is no ratio shape called {shape}."]

    problems: list[str] = []
    for field in spec["fields"]:
        if field.get("required") and params.get(field["name"]) is None:
            problems.append(f"{field['label']} is needed for {spec['label'].lower()}.")

    value = params.get("value")
    if value is not None and float(value) < 0:
        problems.append("Children per parent cannot be negative.")

    low, high = params.get("min"), params.get("max")
    if low is not None and int(low) < 0:
        problems.append("The fewest cannot be negative.")
    if low is not None and high is not None and int(low) > int(high):
        problems.append("The fewest is above the most.")

    share = params.get("zero_share")
    if share is not None and not 0 <= float(share) < 1:
        problems.append(
            "The share with no children has to be between 0 and 1, and cannot be all "
            "of them."
        )

    return problems

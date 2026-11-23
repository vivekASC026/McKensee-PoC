"""Structural classification.

Every column lands in exactly one bucket. The bucket decides what the tool is
allowed to do with the column, regardless of what any element rule says, so
this runs before mapping and cannot be overridden by it.

Buckets, in the order they are tested (design document section 6.5):

    system        identity, computed, or a defaulted timestamp; the database
                  fills it and the writer leaves it out
    reference     part of a foreign key, declared or inferred
    code          a check constraint value list, or a foreign key to a small
                  static table
    derivable     named like a calculated column, or declared as one
    business_key  unique or part of the primary key, and not an identity
    free          everything else
"""

from __future__ import annotations

import re
from typing import Iterable

from sdg.discovery.model import Column, PhysicalModel, Table

# A foreign key into a table this small is treated as a code list rather than
# as a reference to a population. Providers (50 and up at every seeded scale)
# stay references; the 25-row product table becomes a code column.
CODE_TABLE_MAX_ROWS = 50

# Name fragments that mark a column as calculated from others.
DERIVABLE_TOKENS = {"TOT", "TOTAL", "SUM", "AGE", "CALC", "CALCULATED", "DERIVED", "FULLNAME"}

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])")


def tokenize(name: str) -> list[str]:
    """Split a column name into upper-case tokens: MEME_BIRTH_DT -> MEME, BIRTH, DT."""
    return [part.upper() for part in _TOKEN_SPLIT.split(name) if part]


def classify_column(
    table: Table,
    column: Column,
    model: PhysicalModel,
    *,
    derivable_columns: Iterable[str] = (),
) -> tuple[str, str]:
    """Return (bucket, the reason it landed there)."""
    qualified = f"{table.name}.{column.name}"
    declared_derivable = {d.upper() for d in derivable_columns}

    if column.is_computed:
        return "system", "computed column; the database calculates it"
    if column.is_identity:
        return "system", "identity column; the database assigns it"
    if column.default_definition and column.is_date:
        return "system", f"defaulted timestamp, {column.default_definition}"

    foreign_key = table.foreign_key_columns().get(column.name)
    if foreign_key is not None:
        parent = model.table(foreign_key.parent_table)
        parent_rows = parent.row_count if parent else None
        kind = "inferred" if foreign_key.kind == "inferred" else "declared"
        # An empty parent (or one whose count was never read, which is 0 by
        # default) holds no codes to reuse, so it is not read as a code table.
        if parent_rows and parent_rows < CODE_TABLE_MAX_ROWS:
            return "code", (
                f"{kind} foreign key to {foreign_key.parent_table}, which holds "
                f"{parent_rows} rows and reads as a code table"
            )
        return "reference", f"{kind} foreign key to {foreign_key.parent_table}"

    if column.check_values:
        return "code", (
            f"check constraint allows {len(column.check_values)} values: "
            f"{', '.join(column.check_values[:6])}"
        )

    if qualified.upper() in declared_derivable or column.name.upper() in declared_derivable:
        return "derivable", "declared as derivable"
    tokens = set(tokenize(column.name))
    # A total or an age is a number; CDML_CALC_IND and AGE_GROUP_CD are not.
    overlap = tokens & DERIVABLE_TOKENS if column.is_numeric else set()
    if overlap:
        return "derivable", f"name carries {', '.join(sorted(overlap))}, which reads as calculated"

    if column.is_primary_key:
        return "business_key", "part of the primary key and not an identity"
    if column.is_unique:
        return "business_key", "unique constraint and not an identity"

    return "free", "no structural rule applies"


def classify(
    model: PhysicalModel, *, derivable_columns: Iterable[str] = ()
) -> PhysicalModel:
    """Fill in the bucket on every column. Modifies and returns the model."""
    for table in model.tables:
        for column in table.columns:
            bucket, reason = classify_column(
                table, column, model, derivable_columns=derivable_columns
            )
            column.bucket = bucket
            column.bucket_reason = reason
    return model


def bucket_counts(model: PhysicalModel) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _table, column in model.columns():
        if column.bucket:
            counts[column.bucket] = counts.get(column.bucket, 0) + 1
    return dict(sorted(counts.items()))

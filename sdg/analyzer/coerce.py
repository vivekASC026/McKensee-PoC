"""Safe coercion for key columns that may be unconfigured.

A FACETS key column is only populated when a generation rule defines it. With no
such rule the column is filled with "" upstream, and any downstream int cast then
raises `invalid literal for int() with base 10: ''` — from inside pandas, so the
traceback points at array.py rather than at the column or the caller.

This has surfaced three times in different places (MEME_CK in de-duplication,
SBSB_CK and GRGR_CK when attaching claims to a member pool). Rather than guard
each site as it appears, coercion goes through here.
"""

from __future__ import annotations

import pandas as pd

from sdg.packs import held

# Suffixes that identify a FACETS surrogate key. These are the columns that get
# cast to int downstream and so must never hold a blank.
KEY_SUFFIXES = tuple(held("features:analyzer.key_suffixes"))

# Columns that are numeric despite not ending in _CK. Dates are deliberately
# absent — CLCL_LOW_SVC_DT looks numeric and is not, and listing it made the
# self-check report every healthy claims run as having blank keys.
NUMERIC_COLUMNS = frozenset(held("features:analyzer.numeric_columns"))


def is_numeric_column(name: str) -> bool:
    upper = str(name).strip().upper()
    return upper.endswith(KEY_SUFFIXES) or upper in NUMERIC_COLUMNS


def to_int(series, default: int = 0):
    """Coerce a column to int, mapping anything unusable to `default`.

    Blanks, None and non-numeric text become `default` rather than raising. The
    return is always an int64 Series, so `.values` and `.astype(int)` downstream
    behave regardless of what the column held.
    """
    if series is None:
        return pd.Series(dtype="int64")
    return (pd.to_numeric(series, errors="coerce")
            .fillna(default)
            .astype("int64"))


def blank_key_columns(df) -> dict[str, int]:
    """Key columns holding blanks, and how many rows each affects.

    Used by the self-check: a blank key is not fatal any more, but it means a
    generation rule is missing, and that is worth surfacing before a demo rather
    than discovering it in a downstream cast.
    """
    if df is None or df.empty:
        return {}
    out = {}
    for col in df.columns:
        if not is_numeric_column(col):
            continue
        blanks = pd.to_numeric(df[col], errors="coerce").isna().sum()
        if blanks:
            out[str(col)] = int(blanks)
    return out


def one_int(value, default: int = 0) -> int:
    """A single value coerced to int, with a fallback instead of an exception.

    For scalars pulled out of a frame or a SQL row, where a NULL or a blank is a
    data condition to handle rather than a crash to propagate.
    """
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default

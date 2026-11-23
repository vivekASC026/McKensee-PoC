"""What a person calls a FACETS column: "Product ID" for PDPD_ID, "Plan
Description" for PLDS_DESC. Taken from the FACETS data dictionary by
scripts/build_facets_names.py; screens and reports show these, while generated
files and database loads keep the column names Facets needs.
"""

from __future__ import annotations

from functools import lru_cache

from sdg import packs


@lru_cache(maxsize=1)
def names() -> dict[str, str]:
    return packs.active().column_names()


def name(column: str) -> str:
    """The column's business name; the column itself where the dictionary has none."""
    found = names()
    return found.get(column) or found.get(column.upper()) or column

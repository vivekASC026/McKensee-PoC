"""Personal values are never kept in a profile.

A profile is saved to work/profile.json and shown on the review page, so the
smallest, largest and most common values of a column holding names, birth
dates, SSNs or email addresses would put a real person's details in a file.
A column is personal when its name matches a catalog element marked
`pii: sensitive` that is not a code set, however many values it holds: a
small table's few names are still names. SEX or MARITAL_STATUS are marked
sensitive too, but the catalog marks them `code_set: true` -- their values are
categories, not a person, and generation needs their shares.

The counts, lengths and fitted format stay: they describe the column's shape
without holding any value from it (a format with literal characters goes).
Generation then follows the catalog's rule for the element rather than the
source's range.

Known gap: a text column the catalog does not mark keeps its values when it
holds few of them, because those are what generation draws codes from. A
personal column of few values -- a contact name on a small table -- keeps them
until the catalog names it: add an alias to a sensitive element.
"""

from __future__ import annotations

from sdg.catalog.model import Catalog
from sdg.discovery.model import PhysicalModel
from sdg.profiling.model import ColumnProfile, ProfileReport


def key_columns(model: PhysicalModel) -> frozenset[str]:
    """Every primary-key and identity column, as TABLE.COLUMN in upper case."""
    return frozenset(f"{t.name}.{c.name}".upper() for t in model.tables for c in t.columns
                     if c.is_primary_key or c.is_identity)


def personal(profile: ColumnProfile, catalog: Catalog,
             keys: frozenset[str] = frozenset()) -> str | None:
    """The sensitive catalog element a column's name matches, unless a code set.

    A key (primary key or identity, in `keys` from key_columns) is never
    personal, whatever its name holds (GROUP_NAME_ID has GROUP_NAME in it):
    its highest value is what a new key starts past. Values that merely step
    evenly -- an SSN or a date stored as a number -- are not a key."""
    from sdg.mapping.matchers import alias_match

    if profile.qualified.upper() in keys:
        return None
    for element in catalog.elements:
        if element.pii == "sensitive" and not element.code_set and alias_match(
                profile.column, element):
            return element.name
    return None


def withhold_personal_values(report: ProfileReport, catalog: Catalog,
                             keys: frozenset[str] = frozenset()) -> list[str]:
    """Clear every value a personal column's profile would keep; the columns cleared."""
    cleared: list[str] = []
    for profile in report.columns:
        if personal(profile, catalog, keys) is None:
            continue
        profile.minimum = None
        profile.maximum = None
        profile.top_values = None
        for measure in ("distribution", "sequence"):
            held = getattr(profile, measure)
            if held and held.get("kind") == "constant":
                # Every row holds one value, so its mean is that value.
                setattr(profile, measure, None)
            elif held:
                setattr(profile, measure,
                        {k: v for k, v in held.items() if k not in {"min", "max"}})
        # A literal in a fitted format is a character every sampled value shares.
        if (profile.fitted_format or {}).get("kind") == "literal":
            profile.fitted_format = None
        cleared.append(profile.qualified)
    return cleared

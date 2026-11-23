"""A pattern's scope, as sdm.patterns keeps it: written and read in one place.

The scope is where a pattern was found or defined -- which records (the pack's
entity key) and which scope filters (the pack's store.combo_key: for health a
group, plan category, plan and product; for a bank a branch and a product),
with the names they were shown under. It is kept in scope_json, in the pack's
own words, for any pack.

The health pack also has a column of its own per level (store.scope_columns,
store.scope_label_columns), which every reader used before scope_json; they
are written as before, and read for a row from before scope_json. Two record
kinds over one scope stay apart in combo_key: health folds a tag into one
level (store.entity_tag), a pack without one puts the record key in front.
"""

from __future__ import annotations

import json
from typing import Any

from sdg import packs
from sdg.packs import held

# The scope filters, in the order stored keys are made of them.
SCOPE_KEYS: tuple[str, ...] = tuple(held("features:store.combo_key"))
COLUMNS: dict[str, str] = held("features:store.scope_columns")
LABEL_COLUMNS: dict[str, str] = held("features:store.scope_label_columns")
TAG: dict[str, Any] = held("features:store.entity_tag")
LEGACY: list[dict[str, str]] = held("features:store.legacy_entity_markers")
# What the screens call each level's name in a pattern ("group", "plan"); a
# pack without its own names uses the level's key.
LABEL_NAMES: dict[str, str] = held("features:store.scope_label_names")

TAG_COLUMN: str | None = TAG.get("column") or None

# Written into SQL by name (update_user_pattern's SET): plain names only.
for _column in [*COLUMNS.values(), *LABEL_COLUMNS.values()]:
    packs.plain_name(_column, "features.json store.scope_columns")
TAGS: dict[str, str] = TAG.get("tags") or {}


def tag_of(entity: str) -> str | None:
    """The tag folded into TAG_COLUMN for this record kind (None: no tag)."""
    if not TAG_COLUMN:
        return None
    return TAGS.get(entity) or TAGS.get(_known(entity))


def untagged(values: Any) -> list | None:
    """A level's values without the record-kind tag: bookkeeping, not a value."""
    if not values:
        return None
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    kept = [v for v in values if str(v) not in TAGS.values()]
    return kept or None


def key_prefix(entity: str) -> str:
    """What sets one record kind's keys apart where the pack has no tag."""
    return "" if TAG_COLUMN else f"{_known(entity)}|"


def _known(entity: str | None) -> str:
    """A record key of the pack: the one given, else the primary."""
    pack = packs.active()
    return entity if entity in pack.entities else pack.primary


def _parse(value: Any) -> list | None:
    """A stored level back into a list of values, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        return [v for v in value if v is not None] or None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        # Not what any version of the code wrote: nothing to filter by.
        return None
    if parsed is None:
        return None
    if isinstance(parsed, list):
        return [v for v in parsed if v is not None] or None
    return [parsed]


def _scope_json(row: dict) -> dict:
    try:
        held_ = json.loads(row.get("scope_json") or "null")
    except (TypeError, ValueError):
        return {}
    return held_ if isinstance(held_, dict) else {}


# -- writing -----------------------------------------------------------------

def columns(entity: str, filters: dict, labels: dict | None = None,
            tagged: dict | None = None) -> dict[str, Any]:
    """The sdm.patterns columns that hold a scope, for an insert or update.

    `filters` are the scope values as the pattern states them (no tag);
    `tagged`, where given, is what the pack's own columns get instead (the
    key builders fold the tag in themselves, and their form is kept).
    """
    entity = _known(entity)
    labels = labels or {}
    scope = {key: untagged(filters.get(key)) for key in SCOPE_KEYS}
    out: dict[str, Any] = {
        "entity": entity,
        "scope_json": json.dumps({
            "entity": entity,
            "filters": {k: v for k, v in scope.items() if v},
            "labels": {k: str(labels[k]) for k in SCOPE_KEYS if labels.get(k)},
        }, sort_keys=True, default=str),
    }
    source = tagged if tagged is not None else scope
    for key, column in COLUMNS.items():
        out[column] = json.dumps(source.get(key))
    for key, column in LABEL_COLUMNS.items():
        out[column] = labels.get(key, "") or ""
    return out


# -- reading -----------------------------------------------------------------

def entity_of(row: dict) -> str:
    """Which record kind a stored pattern describes: a key of the pack.

    scope_json says so outright. A row from before it says so by the markers
    the code of its day left (store.legacy_entity_markers: health's claims
    tag, its "[Claims]" name prefix, its "Claims" category label, the flag
    in a claims analysis's profile); a row
    with none of them is the primary records'. The entity column is not
    read: it defaulted to the primary on every row written before it was
    set.
    """
    said = _scope_json(row).get("entity")
    if said in packs.active().entities:
        return said
    for marker in LEGACY:
        value = str(row.get(marker["column"]) or
                    (row.get("name") if marker["column"] == "contextual_name" else "") or "")
        if "contains" in marker and marker["contains"] in value:
            return _known(marker["entity"])
        if "equals" in marker and value.strip().lower() == marker["equals"].lower():
            return _known(marker["entity"])
    return packs.active().primary


def filters_of(row: dict) -> dict[str, list]:
    """The scope filters a stored pattern was found in or defined over."""
    held_ = _scope_json(row)
    if held_:
        return {k: list(v) for k, v in (held_.get("filters") or {}).items() if v}
    out = {}
    for key, column in COLUMNS.items():
        values = untagged(_parse(row.get(column)))
        if values:
            out[key] = values
    return out


def labels_of(row: dict) -> dict[str, str]:
    """The names a stored pattern's scope values were shown under."""
    # A scope written over an older row without names (a walk that had none
    # to give) leaves the names that row's columns hold.
    held_ = {k: str(v) for k, v in (_scope_json(row).get("labels") or {}).items() if v}
    return held_ or {key: str(row.get(column)) for key, column in LABEL_COLUMNS.items()
                     if row.get(column)}


def named_labels(labels: dict[str, str]) -> dict[str, str | None]:
    """A scope's names under what the screens call each level."""
    return {LABEL_NAMES.get(key, key): labels.get(key) or None for key in SCOPE_KEYS}


def labels_from_names(given: dict | None) -> dict[str, str]:
    """Names sent by level key, or by what the screens call the level."""
    given = given or {}
    return {key: str(given.get(key) or given.get(LABEL_NAMES.get(key, key)) or "")
            for key in SCOPE_KEYS
            if given.get(key) or given.get(LABEL_NAMES.get(key, key))}


def numeric_keys(entity: str) -> set[str]:
    """The scope filters that reach SQL as numbers (the loader's "ids")."""
    from sdg.analyzer import entity_loader

    try:
        known = entity_loader.config(_known(entity)).get("filters") or []
    except (KeyError, LookupError):
        return set()
    return {f["key"] for f in known if f.get("kind") == "ids"}


def path(labels: dict[str, str | None]) -> list[str]:
    """A scope's names broadest first, as the screens show it ("Acme ›
    Medical › PPO"): the levels it narrows, not the ones it spans ("All")."""
    return [str(labels[k]) for k in SCOPE_KEYS
            if labels.get(k) and str(labels[k]).strip() and labels[k] != "All"]


def levels(labels: dict[str, str | None]) -> list[str | None]:
    """A scope's name at each level, in the levels' order ("All" where it
    spans one, none where nothing was named): what a screen reads one level
    of by its place (the first two, the first)."""
    return [labels.get(k) or None for k in SCOPE_KEYS]


class StoreNotUpgraded(ValueError):
    """The store cannot hold this pack's scope: it has no scope_json column.
    A ValueError, so the screens are told it in words, as other refusals are."""


def require_column(columns: set[str]) -> None:
    """Refuse to keep a pattern whose scope would be lost.

    A pack with columns of its own (health) keeps its scope there too; one
    without them keeps it only in scope_json, so a store from before that
    column would keep the pattern with no scope at all -- refused instead,
    saying what to run. An unknown shape (none read) is let through: the
    insert says what is missing itself.
    """
    if columns and not COLUMNS and "scope_json" not in columns:
        raise StoreNotUpgraded(
            "The SDM platform database has no sdm.patterns.scope_json column, which "
            "this business pack keeps a pattern's scope in. Run "
            "scripts\\customer\\01_sdm_upgrade.sql (or the store setup) and restart.")


# How a scope kept with no names reads in scope_json (columns() writes it
# with sorted keys), for a LIKE: what the backfill may name later.
UNNAMED = '%"labels": {}%'


def require_store() -> None:
    """Refuse before work that keeps patterns (a discovery walk), not after
    it: a pack with columns of its own needs nothing from the store."""
    if COLUMNS:
        return
    from sdg.analyzer import db

    with db.get_connection() as conn:
        require_column(db._table_shape(conn.cursor(), f"{db._SCHEMA}.patterns")[1])

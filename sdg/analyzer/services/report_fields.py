"""Which fields a pattern's counts are made on.

The counts and percentages of a pattern (the card's Counts and %, the one-sheet
export) are made on a list of fields. It starts from the fields every pattern
is described by -- age, sex, location, group, plan, product, and for members
their claims -- and a person can switch any of them off, or add any column
of the denorm table: each of its values is then counted like the rest. The
choice is kept in the pattern store, beside the patterns it describes.

A column with personal data (a name, an SSN, a birth date) is not offered: a
count of names describes people, not a pattern.
"""

from __future__ import annotations

import logging
from typing import Any

from sdg.analyzer.config import SQL_SCHEMA
from sdg.analyzer.facets_names import name
from sdg.packs import held, is_child, is_primary

TABLE = f"{SQL_SCHEMA}.report_fields"
CUSTOM = "col:"
# A child record's columns that belong to its lines, not to it (health: a
# claim line's CDML_ columns), by how their names begin.
LINE_PREFIXES = tuple(held("features:child_analysis.line_column_prefixes"))
# The row key a column is read back by, per kind of pattern.
KEYS = held("features:store.report_keys")
# Columns that say who someone is, never counted.
PERSONAL = set(held("features:store.personal_columns"))

log = logging.getLogger(__name__)


def _conn():
    from sdg.analyzer.entity_loader import connect

    return connect()


def ensure_table() -> None:
    conn = _conn()
    try:
        conn.cursor().execute(f"""
            IF OBJECT_ID(N'{TABLE}', N'U') IS NULL
            CREATE TABLE {TABLE} (
                entity     NVARCHAR(20)  NOT NULL,
                field      NVARCHAR(140) NOT NULL,
                position   INT           NOT NULL,
                chosen_by  NVARCHAR(100) NULL,
                chosen_at  DATETIME2     NULL,
                CONSTRAINT PK_report_fields PRIMARY KEY (entity, field)
            )""")
        conn.commit()
    finally:
        conn.close()


def builtins(entity: str) -> list[dict[str, Any]]:
    """The fields every pattern of this kind is described by, in order."""
    from .pattern_report import FIELDS

    from sdg import packs

    # The primary's own fields, then its fields counted over its children
    # (kept in the pack as "member_claims").
    if is_primary(entity):
        return [*FIELDS.get(entity, []), *FIELDS.get("member_claims", [])]
    if is_child(entity):
        return list(FIELDS.get(entity, []))
    pack = packs.active()
    raise ValueError(f"Patterns are of {pack.term(pack.primary, 'many')} or of "
                     f"{pack.term(pack.child, 'many')}, not {entity!r}.")


def columns(entity: str) -> list[str]:
    """The denorm columns a person can add: every one not already a field and
    not personal."""
    from sdg.analyzer.entity_loader import present_columns, table

    used = {f.get("column") for f in builtins(entity)} | {
        f.get("describe") for f in builtins(entity)}
    # Raised, not read as "no columns": a failed read would otherwise drop
    # every added column from the chosen fields and the report.
    try:
        have = present_columns(table(entity))
    except Exception:
        log.exception("Could not read the columns of the %s table", entity)
        raise
    return sorted(c for c in have if c not in used and c not in PERSONAL)


def _stored(entity: str) -> list[str] | None:
    """The fields chosen and kept, or None when nobody has chosen.

    A store that cannot be read is raised, not taken for "nobody chose": the
    report would otherwise look valid while counting other fields.
    """
    try:
        ensure_table()
        conn = _conn()
        try:
            rows = conn.cursor().execute(
                f"SELECT field FROM {TABLE} WHERE entity = ? ORDER BY position",
                entity).fetchall()
        finally:
            conn.close()
    except Exception:
        log.exception("Could not read the chosen report fields for %s", entity)
        raise
    return [str(r[0]) for r in rows] or None


def chosen(entity: str) -> list[str]:
    """The fields chosen, in order: every built-in one until somebody chooses."""
    known = {f["key"] for f in builtins(entity)}
    stored = _stored(entity)
    if stored is None:
        return [f["key"] for f in builtins(entity)]
    extra = set(columns(entity))
    return [k for k in stored if k in known or (k.startswith(CUSTOM)
                                                 and k[len(CUSTOM):] in extra)]


def offered(entity: str) -> list[dict[str, Any]]:
    """Every field that can be counted, built-in and denorm, and whether it is chosen."""
    picked = chosen(entity)
    out = [{"key": f["key"], "label": f["label"], "column": f.get("column"),
            "builtin": True, "chosen": f["key"] in picked} for f in builtins(entity)]
    out += [{"key": CUSTOM + c, "label": name(c), "column": c, "builtin": False,
             "chosen": CUSTOM + c in picked} for c in sorted(columns(entity), key=name)]
    return out


def choose(entity: str, keys: list[str], by: str = "user") -> list[str]:
    """Keep the fields counted, in the order given."""
    from datetime import datetime

    known = {f["key"] for f in builtins(entity)}
    extra = set(columns(entity))
    unknown = [k for k in keys if k not in known
               and not (k.startswith(CUSTOM) and k[len(CUSTOM):] in extra)]
    if unknown:
        raise ValueError(f"Not a field of {entity} patterns: {', '.join(unknown)}.")
    keys = list(dict.fromkeys(keys))
    if not keys:
        raise ValueError("Choose at least one field to count.")
    ensure_table()
    conn = _conn()
    try:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {TABLE} WHERE entity = ?", entity)
        now = datetime.now()
        for position, key in enumerate(keys):
            cursor.execute(f"INSERT INTO {TABLE} (entity, field, position, chosen_by, "
                           "chosen_at) VALUES (?, ?, ?, ?, ?)",
                           entity, key, position, by, now)
        conn.commit()
    finally:
        conn.close()
    return keys


def reset(entity: str) -> list[str]:
    """Back to the built-in fields."""
    ensure_table()
    conn = _conn()
    try:
        conn.cursor().execute(f"DELETE FROM {TABLE} WHERE entity = ?", entity)
        conn.commit()
    finally:
        conn.close()
    return chosen(entity)


def specs(entity: str) -> list[dict[str, Any]]:
    """The chosen fields as the counts read them; an added column is counted
    on its values (a number or date with many values, in bands)."""
    by_key = {f["key"]: f for f in builtins(entity)}
    out = []
    for key in chosen(entity):
        if key in by_key:
            out.append(by_key[key])
            continue
        column = key[len(CUSTOM):]
        basis = "members" if is_primary(entity) else (
            "lines" if column.startswith(LINE_PREFIXES) else "claims")
        out.append({"key": key, "label": name(column), "column": column, "custom": True,
                    "basis": basis})
    return out

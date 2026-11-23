"""Patterns a person defines, by a rule on each feature they choose.

The analyzer finds patterns; a person often already knows one -- "members of
the Acme group on a PPO plan, aged 30 to 45" -- and wants it treated exactly as
a found one: counted against the book, described, saved, written to a file,
and looked inside for the patterns it holds.

So a defined pattern is a scope. Its group, plan category, plan and product go
in the filters every analysis already takes, and the rest of its rules ride
along as `conditions`, which the loaders apply to the rows they load. Because
the scope is all that rebuilds a pattern's rows, nothing downstream needs to
know a pattern was defined rather than found: analyzing the scope with k=1 is
the defined pattern itself, and with k left to the analyzer it is the patterns
inside it.

A rule is a condition: one of a list of values, or a range. What the matched
rows hold -- their shares of each value, their spread of ages -- is measured
from them, the same as for any pattern.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from sdg import packs
from sdg.packs import held, held_or, is_primary

log = logging.getLogger(__name__)

# k that means "the whole scope as one pattern" rather than a clustering.
WHOLE = 1

# Every feature a rule can be set on. `scope` features are the hierarchy the
# loaders filter in SQL; the rest are conditions on the loaded rows. `column`
# is where the value is read; for a range on members it is a measure the
# loader derives (age and tenure, as of the reference date).
FEATURES: dict[str, list[dict[str, Any]]] = held("features:patterns.defined")

# Values offered for a list rule, most common first. A diagnosis list runs to
# thousands; the long tail can still be typed.
MAX_OPTIONS = 300


def _feature(entity: str, key: str) -> dict[str, Any]:
    for feature in FEATURES.get(entity, []):
        if feature["key"] == key:
            return feature
    raise ValueError(f"“{key}” is not a feature of a {entity} pattern.")


def _table(entity: str) -> str:
    from sdg.analyzer import entity_loader

    return entity_loader.table(entity)


def catalog(entity: str) -> list[dict[str, Any]]:
    """The features of a pattern of this kind, each with what it can hold.

    A list feature carries the values the pattern store has, most common first,
    with how many members (or claim lines) hold each. A range feature on a
    stored column carries its low and high; age and tenure on members are
    measured as of the reference date and carry none.
    """
    if entity not in FEATURES:
        pack = packs.active()
        raise ValueError(f"A pattern is of {pack.term(pack.primary, 'many')} or of "
                         f"{pack.term(pack.child, 'many')}, not {entity!r}.")
    from sdg.analyzer.entity_loader import connect as _conn
    from sdg.analyzer.sql import read_sql

    table = _table(entity)
    member_key = packs.plain_name(held("features:store.member_key"), "store.member_key")
    count = f"COUNT(DISTINCT [{member_key}])" if is_primary(entity) else "COUNT(*)"
    out = []
    # A scope filter the loader reads as ids is sent as numbers (health: a
    # group), so a pattern kept from it is the same pattern either way.
    from sdg.analyzer.services.filter_models import listed

    ids = {f["key"] for f in listed(entity) if f["kind"] == "ids"}
    conn = _conn()
    try:
        for feature in FEATURES[entity]:
            item = {k: v for k, v in feature.items()
                    if k not in ("column", "describe", "source")}
            if feature.get("scope") and feature["key"] in ids:
                item["numeric"] = True
            column = feature["column"]
            if feature.get("source"):
                item["options"] = _joined_options(feature, conn)
            elif feature["kind"] == "in":
                label = (f"MAX(LTRIM(RTRIM(CAST({feature['describe']} AS NVARCHAR(200)))))"
                         if feature.get("describe") else "NULL")
                frame = read_sql(
                    f"SELECT TOP {MAX_OPTIONS} LTRIM(RTRIM(CAST({column} AS NVARCHAR(100)))) AS v, "
                    f"{label} AS label, {count} AS n FROM {table} "
                    f"WHERE {column} IS NOT NULL GROUP BY {column} ORDER BY n DESC",
                    conn)
                item["options"] = [
                    {"value": str(r["v"]), "label": str(r["label"]) if r["label"] else None,
                     "count": int(r["n"])}
                    for _, r in frame.iterrows() if str(r["v"]).strip()
                ]
            elif not column.startswith("_"):
                frame = read_sql(f"SELECT MIN({column}) AS lo, MAX({column}) AS hi FROM {table}",
                                 conn)
                lo, hi = frame.iloc[0]["lo"], frame.iloc[0]["hi"]
                item["min"] = float(lo) if lo is not None else None
                item["max"] = float(hi) if hi is not None else None
            out.append(item)
    finally:
        conn.close()
    return out


def _joined_options(feature: dict[str, Any], conn) -> list[dict[str, Any]]:
    """The values of a feature joined from beside the denorm, most common first."""
    from sdg.analyzer.config import SQL_SCHEMA
    from sdg.analyzer.sql import read_sql
    from sdg.analyzer.provider_features import ENTITY_WORDS

    s = SQL_SCHEMA
    queries = {
        "_pcp_specialty": f"SELECT LTRIM(RTRIM(p.PRCF_MCTR_SPEC)) AS v, COUNT(DISTINCT m.MEME_CK) AS n "
                          f"FROM {s}.member_pcp m JOIN {s}.provider_denorm p ON p.PRPR_ID = m.PRPR_ID "
                          f"GROUP BY p.PRCF_MCTR_SPEC",
        "_pcp_entity": f"SELECT LTRIM(RTRIM(p.PRPR_ENTITY)) AS v, COUNT(DISTINCT m.MEME_CK) AS n "
                       f"FROM {s}.member_pcp m JOIN {s}.provider_denorm p ON p.PRPR_ID = m.PRPR_ID "
                       f"GROUP BY p.PRPR_ENTITY",
        "_provider_entity": f"SELECT LTRIM(RTRIM(PRPR_ENTITY)) AS v, COUNT(*) AS n "
                            f"FROM {s}.provider_denorm GROUP BY PRPR_ENTITY",
        "_network_name": f"SELECT LTRIM(RTRIM(NWNW_NAME)) AS v, COUNT(DISTINCT PRPR_ID) AS n "
                         f"FROM {s}.provider_network GROUP BY NWNW_NAME",
        "_network_type": f"SELECT LTRIM(RTRIM(NWNW_MCTR_TYPE)) AS v, COUNT(DISTINCT PRPR_ID) AS n "
                         f"FROM {s}.provider_network GROUP BY NWNW_MCTR_TYPE",
    }
    column = feature["column"]
    if column == "_has_pcp":
        return [{"value": "Y", "label": "Yes", "count": None},
                {"value": "N", "label": "No", "count": None}]
    try:
        frame = read_sql(queries[column], conn)
    except Exception as exc:
        from sdg.analyzer.claims_data_loader import _missing_table

        if _missing_table(exc):
            # The tables beside the denorm are not there yet.
            log.info("No values for %s: the tables beside the denorm are not there yet",
                     feature.get("key"))
            return []
        # Anything else (permissions, a broken query) is not "no values".
        log.exception("Could not read the values of %s", feature.get("key"))
        raise
    words = ENTITY_WORDS if column in ("_pcp_entity", "_provider_entity") else {}
    out = [{"value": words.get(str(r["v"]), str(r["v"])), "label": None, "count": int(r["n"])}
           for _, r in frame.sort_values("n", ascending=False).iterrows() if str(r["v"]).strip()]
    if column == "_network_type":
        out.append({"value": "OUT", "label": "Out of network", "count": None})
    if column == "_network_name":
        out.append({"value": "Out of network", "label": None, "count": None})
    return out


def check(entity: str, conditions: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The conditions, each one a rule this kind of pattern has, made tidy.

    A list rule keeps its values as text; a range keeps the bounds it was given.
    A rule with nothing in it is dropped rather than read as "nothing matches".
    """
    out: dict[str, Any] = {}
    for key, rule in (conditions or {}).items():
        feature = _feature(entity, key)
        if feature.get("scope"):
            raise ValueError(f"{feature['label']} is part of the scope, not a condition.")
        if not isinstance(rule, dict):
            raise ValueError(f"The rule for {feature['label']} is not one.")
        if feature["kind"] == "in":
            values = [str(v).strip() for v in rule.get("in") or [] if str(v).strip()]
            if values:
                out[key] = {"in": sorted(set(values))}
        else:
            lo, hi = rule.get("min"), rule.get("max")
            lo = float(lo) if lo not in (None, "") else None
            hi = float(hi) if hi not in (None, "") else None
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"{feature['label']}: the low end is above the high end.")
            if lo is not None or hi is not None:
                out[key] = {k: v for k, v in (("min", lo), ("max", hi)) if v is not None}
    return out


def mask(frame: pd.DataFrame, entity: str, conditions: dict[str, Any]) -> pd.Series:
    """Which rows meet every condition."""
    keep = pd.Series(True, index=frame.index)
    for key, rule in conditions.items():
        column = _feature(entity, key)["column"]
        if column not in frame.columns:
            raise ValueError(f"Pattern data has no {column} to apply criteria to.")
        values = frame[column]
        if "in" in rule:
            keep &= values.astype(str).str.strip().isin(rule["in"])
        else:
            numbers = pd.to_numeric(values, errors="coerce")
            if "min" in rule:
                keep &= numbers >= rule["min"]
            if "max" in rule:
                keep &= numbers <= rule["max"]
    return keep


def split(filters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A scope's SQL filters, and its conditions on the loaded rows."""
    rest = {k: v for k, v in filters.items() if k != "conditions"}
    return rest, dict(filters.get("conditions") or {})


def narrow_members(loaded: tuple, conditions: dict[str, Any]) -> tuple:
    """A member load cut down to the members meeting every condition."""
    members, labels, family, used = loaded
    keep = mask(members, packs.active().primary, conditions)
    members = members[keep]
    if members.empty:
        raise ValueError(f"No {packs.active().term(packs.active().primary, 'many')} meet "
                         f"these rules: {_said(conditions)}")
    key = held("features:store.member_key")
    ids = set(members[key].tolist())
    if labels is not None and key in labels.columns:
        labels = labels[labels[key].isin(ids)]
    household = (held_or("features:primary_analysis.family", None) or {}).get("household")
    if family is not None and key in family.columns:
        # Family is measured across the households the members belong to.
        homes = set(members[household].tolist()) if household in members.columns else None
        family = (family[family[household].isin(homes)] if homes is not None
                  else family[family[key].isin(ids)])
    return members, labels, family, {**(used or {}), "conditions": conditions}


def narrow_claims(loaded: tuple, conditions: dict[str, Any]) -> tuple:
    """A claims load cut down to the lines meeting every condition."""
    lines, used = loaded
    lines = lines[mask(lines, packs.active().child, conditions)]
    if lines.empty:
        raise ValueError(f"No {packs.active().term(packs.active().child, 'many')} meet "
                         f"these rules: {_said(conditions)}")
    return lines, {**(used or {}), "conditions": conditions}


def _said(conditions: dict[str, Any]) -> str:
    parts = []
    for key, rule in conditions.items():
        if "in" in rule:
            parts.append(f"{key} one of {', '.join(rule['in'])}")
        else:
            parts.append(f"{key} {rule.get('min', '…')}–{rule.get('max', '…')}")
    return "; ".join(parts)

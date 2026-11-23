"""Which features system discovered patterns are found on.

A pattern is a group the clustering finds, and it finds groups on the features
it is given. Which ones is a choice: a book where providers matter wants
patterns that differ by provider specialty and network; one about enrollment
wants age, tenure and plan. The choice is kept in the pattern store, beside
the patterns it shapes, and starts from the features the analyzer always used.

Numbers (age, tenure, an amount) are clustered on their standardized value;
everything else on its values, the commonest MAX_VALUES of them kept apart and
the rest pooled, so a feature with thousands of values -- a diagnosis, a
provider -- shapes the clusters without swamping them.
"""

from __future__ import annotations

import logging
from typing import Any

from sdg.analyzer.config import SQL_SCHEMA
from sdg.packs import held

log = logging.getLogger(__name__)

TABLE = f"{SQL_SCHEMA}.discovery_features"
DEFAULTS = held("features:patterns.discovery_defaults")
# A list feature's values beyond this many, by frequency, cluster as one.
MAX_VALUES = 20


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
                feature    NVARCHAR(60)  NOT NULL,
                chosen_by  NVARCHAR(100) NULL,
                chosen_at  DATETIME2     NULL,
                CONSTRAINT PK_discovery_features PRIMARY KEY (entity, feature)
            )""")
        conn.commit()
    finally:
        conn.close()


def _known(entity: str) -> dict[str, dict[str, Any]]:
    from .defined_patterns import FEATURES

    if entity not in FEATURES:
        raise ValueError(f"Patterns are of members or of claims, not {entity!r}.")
    # Scope features are what an analysis is narrowed to; the rest are what
    # its patterns can differ by. Both can be clustered on.
    return {f["key"]: f for f in FEATURES[entity]}


_CHOSEN: dict[str, list[str]] = {}


def chosen(entity: str) -> list[str]:
    """The features patterns of this kind are found on."""
    if entity in _CHOSEN:
        return list(_CHOSEN[entity])
    known = _known(entity)
    keys: list[str] = []
    try:
        ensure_table()
        conn = _conn()
        try:
            rows = conn.cursor().execute(
                f"SELECT feature FROM {TABLE} WHERE entity = ?", entity).fetchall()
        finally:
            conn.close()
        keys = [str(r[0]) for r in rows if str(r[0]) in known]
    except Exception:
        # The defaults for this call only, and not kept: one failed read must
        # not replace the saved choice until restart (it also keys the cache).
        log.warning("Could not read the chosen discovery features for %s; using the "
                    "defaults until the store can be read", entity, exc_info=True)
        return list(DEFAULTS[entity])
    keys = [k for k in known if k in keys] or list(DEFAULTS[entity])
    _CHOSEN[entity] = keys
    return list(keys)


def choose(entity: str, keys: list[str], by: str = "user") -> list[str]:
    """Keep the features patterns of this kind are found on."""
    from datetime import datetime

    known = _known(entity)
    unknown = [k for k in keys if k not in known]
    if unknown:
        raise ValueError(f"Not a feature of {entity}: {', '.join(unknown)}.")
    keys = [k for k in known if k in set(keys)]
    if not keys:
        raise ValueError("Choose at least one feature to find patterns on.")
    ensure_table()
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {TABLE} WHERE entity = ?", entity)
        for key in keys:
            cur.execute(f"INSERT INTO {TABLE} (entity, feature, chosen_by, chosen_at) "
                        f"VALUES (?, ?, ?, ?)", entity, key, by, datetime.now())
        conn.commit()
    finally:
        conn.close()
    _CHOSEN.pop(entity, None)
    _forget_analyses()
    return chosen(entity)


def _forget_analyses() -> None:
    """Patterns found on other features are not these patterns."""
    from . import cache, disk_cache

    cache.clear_all()
    disk_cache._FINGERPRINT = None


def signature() -> str:
    """The choice, folded into every cached analysis's key."""
    return ";".join(f"{e}:{','.join(chosen(e))}" for e in sorted(DEFAULTS))


def columns(entity: str) -> tuple[list[str], list[str]]:
    """(number columns, list columns) the clustering reads, for the chosen features."""
    known = _known(entity)
    numbers, lists = [], []
    for key in chosen(entity):
        feature = known[key]
        (numbers if feature["kind"] == "range" else lists).append(feature["column"])
    return numbers, lists


def described(entity: str) -> list[dict[str, Any]]:
    """Every feature, whether patterns are found on it, for the Config screen."""
    known = _known(entity)
    picked = set(chosen(entity))
    return [{"key": k, "label": f["label"], "kind": f["kind"],
             "scope": bool(f.get("scope")), "joined": bool(f.get("source")),
             "chosen": k in picked, "default": k in DEFAULTS[entity]}
            for k, f in known.items()]


def matrix(frame, entity: str, weight: float = 2.0):
    """The clustering's input: chosen numbers standardized and weighted, chosen
    lists one-hot over their commonest values."""
    import numpy as np
    import pandas as pd
    from sklearn.preprocessing import StandardScaler

    numbers, lists = columns(entity)
    numbers = [c for c in numbers if c in frame.columns]
    lists = [c for c in lists if c in frame.columns]
    blocks = []
    if numbers:
        values = frame[numbers].apply(pd.to_numeric, errors="coerce")
        values = values.fillna(values.median()).fillna(0.0).values
        blocks.append(StandardScaler().fit_transform(values) * weight)
    for column in lists:
        text = frame[column].astype(str).str.strip().where(frame[column].notna(), "(none)")
        top = text.value_counts().index[:MAX_VALUES]
        text = text.where(text.isin(top), "(other)")
        blocks.append(pd.get_dummies(text, dtype=float).values)
    if not blocks:
        return None
    return np.hstack(blocks)

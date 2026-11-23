"""A pattern's scenarios: the combinations of chosen features its own rows hold.

One pattern is usually several test scenarios -- the same kind of member on
different plans, female and male, younger and older; the same kind of claim
for different claim types and diagnoses. Listing them from the pattern's own
rows gives the scenarios that actually occur in it, with how many rows each
has, and each one chosen is kept as a pattern of exactly its rows, so it can
be generated -- all of them in one load, or in one file -- like any other.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from . import saved_patterns
from sdg.packs import held, is_child

# The features a pattern can be split on, the column each is read from, and
# how it is shown. Ages are split into bands rather than read year by year.
DIMENSIONS: dict[str, list[dict[str, Any]]] = held(
    "features:patterns.scenario_dimensions")

AGE_BANDS = [(0, 17, "0-17"), (18, 34, "18-34"), (35, 49, "35-49"), (50, 64, "50-64"),
             (65, 200, "65+")]
# The scenario value of a row that has none for a feature.
NONE = "(none)"
# Past this many scenarios the list stops being a choice anybody can make.
MAX_SCENARIOS = 200
# What a pattern is split on until somebody chooses: the Scenarios panel, the
# counts and every download start here, so they agree. A claims pattern is of
# one claim type, so claim type is no split of it.
DEFAULT_SPLIT = held("features:patterns.scenario_split")


def dimensions(entity: str) -> list[dict[str, Any]]:
    return [{k: v for k, v in d.items() if k in ("key", "label")}
            for d in DIMENSIONS.get(entity, [])]


def _band(value: Any) -> Optional[str]:
    try:
        age = float(value)
    except (TypeError, ValueError):
        return None
    if age != age:
        return None
    # Ages are counted in whole years: 17.4 is 17, not a gap between bands. A
    # birth date after the as-of date is a baby born since, so it is 0.
    age = max(int(age // 1), 0)
    for low, high, label in AGE_BANDS:
        if low <= age <= high:
            return label
    return None


def _split(frame: pd.DataFrame, entity: str, keys: list[str]) -> tuple[pd.DataFrame, list[dict]]:
    """The frame with one column per chosen dimension, as scenario values."""
    chosen = [d for d in DIMENSIONS.get(entity, []) if d["key"] in keys]
    if not chosen:
        raise ValueError("Choose at least one feature to split the pattern on.")
    out = pd.DataFrame(index=frame.index)
    for d in chosen:
        if d["column"] not in frame.columns:
            raise ValueError(f"This pattern's rows carry no {d['label'].lower()}.")
        values = frame[d["column"]]
        split = values.map(_band) if d.get("bands") else values.where(values.notna()).map(
            lambda v: None if v is None or (isinstance(v, float) and v != v) or not str(v).strip()
            else str(v).strip())
        # A row with no value for a feature is its own scenario, not dropped:
        # the list accounts for every row of the pattern.
        out[d["key"]] = split.fillna(NONE)
    return out, chosen


def _labels(frame: pd.DataFrame, chosen: list[dict]) -> dict[str, dict[str, str]]:
    """What each value of each dimension is called, where the rows say."""
    out: dict[str, dict[str, str]] = {}
    for d in chosen:
        describe = d.get("describe")
        if describe and describe in frame.columns:
            pairs = frame[[d["column"], describe]].dropna().astype(str)
            out[d["key"]] = {k.strip(): v.strip() for k, v in
                             zip(pairs[d["column"]], pairs[describe])}
    return out


def rows_of(entity: str, filters: dict, k: Optional[int], cluster_id: int,
            claim_type: Optional[str] = None, pattern_id: Optional[int] = None) -> pd.DataFrame:
    """The pattern's own rows, all of them: an analyzed one by its analysis, a
    saved one by its id. Uncapped, so every scenario's count is the pattern's."""
    from . import pattern_data

    if pattern_id is not None:
        return pattern_data.every_row(entity, {}, None, 0, pattern_id)
    scope = (saved_patterns._claims_filters(filters, claim_type) if is_child(entity)
             else saved_patterns._member_filters(filters))
    return pattern_data.every_row(entity, scope, k, cluster_id)


def list_scenarios(entity: str, keys: list[str], frame: pd.DataFrame) -> dict[str, Any]:
    """Every scenario of the chosen features the rows hold, largest first."""
    values, chosen = _split(frame, entity, keys)
    total = len(frame) or 1
    labels = _labels(frame, chosen)
    counts = values.value_counts()
    scenarios = []
    for combo, n in counts.head(MAX_SCENARIOS).items():
        combo = combo if isinstance(combo, tuple) else (combo,)
        picked = {d["key"]: str(v) for d, v in zip(chosen, combo)}
        scenarios.append({
            "values": picked,
            "label": " · ".join(
                f"{d['label']} {labels.get(d['key'], {}).get(picked[d['key']], picked[d['key']])}"
                for d in chosen),
            "rows": int(n),
            "share": round(int(n) / total, 6),
        })
    return {
        "dimensions": [{"key": d["key"], "label": d["label"]} for d in chosen],
        "rows": len(frame),
        "scenarios": scenarios,
        "more": max(0, len(counts) - MAX_SCENARIOS),
    }


def keep_scenarios(entity: str, keys: list[str], picked: list[dict[str, str]],
                   filters: dict, k: Optional[int], cluster_id: int,
                   claim_type: Optional[str], name: str,
                   pattern_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Keep each chosen scenario as a pattern of exactly its rows."""
    frame = rows_of(entity, filters, k, cluster_id, claim_type, pattern_id)
    values, chosen = _split(frame, entity, keys)
    row_keys = saved_patterns.row_keys(frame, entity)
    labels = _labels(frame, chosen)
    if pattern_id is not None:
        # A saved pattern is kept as records of the analysis it came from.
        import json

        from sdg.analyzer import db

        profile = json.loads((db.get_pattern(pattern_id) or {}).get("profile_json") or "{}")
        origin = profile.get("saved_from") or {}
        filters = origin.get("filters") or {}
        k = origin.get("k")
        cluster_id = int(origin.get("cluster_id") or 0)
        claim_type = origin.get("claim_type")
    kept = []
    for scenario in picked:
        mask = pd.Series(True, index=values.index)
        for d in chosen:
            mask &= values[d["key"]] == str(scenario.get(d["key"]))
        chosen_keys = [key for key, hit in zip(row_keys, mask.tolist()) if hit]
        if not chosen_keys:
            continue
        said = " · ".join(
            f"{d['label']} {labels.get(d['key'], {}).get(str(scenario.get(d['key'])), scenario.get(d['key']))}"
            for d in chosen)
        result = saved_patterns.save(
            entity, filters, k, cluster_id, claim_type=claim_type,
            record_keys=chosen_keys, name=f"{name} · {said}",
        )
        kept.append({**result, "scenario": scenario, "label": said})
    return kept

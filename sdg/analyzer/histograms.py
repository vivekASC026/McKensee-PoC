"""How a continuous feature is spread, inside a pattern and across its scope.

A pattern's mean, range and deviation say where it sits; they do not say what
shape it has. Two patterns can share a mean and a range and still be one hump
and two, and a charge's long tail is invisible in its average. These are the
counts behind those figures.

The bins are drawn once over the whole scope and every pattern is counted into
the same ones, so a pattern's bars sit directly against the scope's and against
each other's. The last bin is open at the top: it is cut at the 99th
percentile, because one charge of 7,000 among a million under 700 would
otherwise squash every other bar into the first one.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

BINS = 20
TOP_QUANTILE = 0.99


def _nice_step(span: float, bins: int) -> float:
    """A round bin width: 1, 2, 2.5 or 5 times a power of ten."""
    raw = span / max(bins, 1)
    if raw <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def _numbers(values) -> np.ndarray:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    return series.to_numpy(dtype=float)


def edges_for(values, bins: int = BINS) -> dict | None:
    """Bin edges over a scope, on round numbers, open at the top if it must be."""
    data = _numbers(values)
    if data.size == 0:
        return None
    low, high = float(data.min()), float(data.max())
    cut = float(np.quantile(data, TOP_QUANTILE)) if data.size > 100 else high
    if cut <= low:
        cut = high
    step = _nice_step(cut - low, bins) if cut > low else 1.0
    start = math.floor(low / step) * step
    count = max(1, math.ceil((cut - start) / step))
    edges = [round(start + i * step, 6) for i in range(count + 1)]
    if edges[-1] < cut:
        edges.append(round(edges[-1] + step, 6))
    return {"edges": edges, "open_top": bool(high > edges[-1])}


def counts_for(values, bins: dict) -> list[int]:
    """How many values fall in each bin; anything past the top goes in the last."""
    data = _numbers(values)
    edges = np.asarray(bins["edges"], dtype=float)
    if data.size == 0:
        return [0] * (len(edges) - 1)
    clipped = np.clip(data, edges[0], edges[-1])
    counts, _ = np.histogram(clipped, bins=edges)
    return [int(c) for c in counts]


def describe(frame: pd.DataFrame, columns: dict[str, str], masks: dict[int, np.ndarray]):
    """The scope's distribution per feature, and each pattern's on the same bins.

    `columns` maps the name a payload uses for a feature to the column holding
    it. Returns (scope, per_pattern): scope is {feature: {edges, counts,
    open_top}}, per_pattern is {cluster_id: {feature: counts}}.
    """
    scope: dict[str, dict] = {}
    per_pattern: dict[int, dict[str, list[int]]] = {cid: {} for cid in masks}
    for feature, column in columns.items():
        if column not in frame.columns:
            continue
        values = frame[column].to_numpy()
        bins = edges_for(values)
        if bins is None:
            continue
        scope[feature] = {**bins, "counts": counts_for(values, bins)}
        for cid, mask in masks.items():
            per_pattern[cid][feature] = counts_for(values[mask], bins)
    return scope, per_pattern

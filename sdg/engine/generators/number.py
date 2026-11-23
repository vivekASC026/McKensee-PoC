"""Number generator.

Uniform, normal, or log-normal, clamped to a range and rounded to a fixed
number of decimals. For the log-normal, `mean` and `stddev` describe the
underlying normal, which is how the profiler reports its fit.
"""

from __future__ import annotations

import math
from decimal import Decimal

from sdg.engine.generators.base import GenerationError, RowContext, register

# A draw outside the clamp is retried rather than pinned to the boundary, so a
# clamped distribution does not grow a spike at each end.
MAX_RETRIES = 12


@register("number")
def number(ctx: RowContext) -> int | Decimal:
    distribution = ctx.require("distribution")
    low = ctx.params.get("min")
    high = ctx.params.get("max")
    decimals = int(ctx.params.get("decimals", 0))
    rnd = ctx.stream()

    bands = ctx.params.get("bands")
    if bands:
        # A slice by its share, then a value within it: the pattern's spread.
        low_b, high_b, _share = bands[ctx.stream("band").weighted_index(
            [float(b[2]) for b in bands])]
        raw = rnd.uniform(float(low_b), float(high_b))
    elif distribution == "uniform":
        if low is None or high is None:
            raise GenerationError(
                f"{ctx.table}.{ctx.field_name}: a uniform number needs min and max"
            )
        raw = rnd.uniform(float(low), float(high))
    elif distribution in ("normal", "lognormal"):
        mean = ctx.params.get("mean")
        stddev = ctx.params.get("stddev")
        if mean is None or stddev is None:
            raise GenerationError(
                f"{ctx.table}.{ctx.field_name}: a {distribution} number needs mean and stddev"
            )
        raw = _draw_clamped(rnd, distribution, float(mean), float(stddev), low, high)
    else:
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: unknown distribution {distribution!r}"
        )

    if low is not None:
        raw = max(float(low), raw)
    if high is not None:
        raw = min(float(high), raw)

    if decimals == 0:
        return int(round(raw))
    quantum = Decimal(1).scaleb(-decimals)
    return Decimal(repr(raw)).quantize(quantum)


def _draw_clamped(rnd, distribution: str, mean: float, stddev: float, low, high) -> float:
    for _ in range(MAX_RETRIES):
        value = mean + stddev * rnd.gauss()
        if distribution == "lognormal":
            value = math.exp(value)
        if low is not None and value < float(low):
            continue
        if high is not None and value > float(high):
            continue
        return value
    # Everything was outside the range; fall back to the midpoint of the clamp.
    if low is not None and high is not None:
        return (float(low) + float(high)) / 2
    return math.exp(mean) if distribution == "lognormal" else mean

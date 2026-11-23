"""Date-relative generator.

A date is an anchor plus a gap. The anchor is a field in the same row, a field
in a parent row, or a fixed date. This is what keeps service, received, and
paid dates in the right order, and what keeps a claim inside its member's
coverage window.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from sdg.engine.generators.base import GenerationError, RowContext, register


@register("date_relative")
def date_relative(ctx: RowContext):
    anchor = _as_date(ctx, ctx.require("anchor"))
    if anchor is None:
        return None

    low = int(ctx.params.get("min_days", 0))
    high = int(ctx.params.get("max_days", 0))
    if high < low:
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: max_days {high} is below min_days {low}"
        )

    # An upper anchor narrows the window before the draw rather than clamping
    # after it. Clamping would pile every out-of-range draw onto the boundary:
    # a member whose coverage ended early would have almost every claim dated
    # on the termination date itself, which is not what the data looks like.
    for ceiling_param in ("upper_anchor", "max_date"):
        reference = ctx.params.get(ceiling_param)
        if not reference:
            continue
        ceiling = _as_date(ctx, reference)
        if ceiling is not None:
            high = min(high, (ceiling - anchor).days)
    if high < low:
        high = low

    rnd = ctx.stream()
    bands = ctx.params.get("bands")
    if bands:
        # A slice by its share, then a day within it, kept inside the window.
        band_low, band_high, _share = bands[ctx.stream("band").weighted_index(
            [float(b[2]) for b in bands])]
        band_low, band_high = max(low, int(band_low)), min(high, int(band_high))
        gap = rnd.randint(band_low, band_high) if band_high >= band_low else \
            min(max(int(band_low), low), high)
    elif ctx.params.get("distribution") == "lognormal" and high > low:
        # A long tail toward the upper bound: most gaps short, a few long.
        span = high - low
        draw = math.exp(rnd.gauss() * 0.8) - 1.0
        gap = low + int(min(span, max(0.0, draw / math.e * span)))
    else:
        gap = rnd.randint(low, high)

    try:
        result = anchor + timedelta(days=gap)
    except OverflowError:
        # Counted from 9999-12-31, "no end": there is no later date.
        result = anchor

    if ctx.params.get("with_time"):
        seconds = ctx.stream("time").below(86_400)
        result = datetime.combine(result, time()) + timedelta(seconds=seconds)
    # A date kept in a text column is written the way that column writes it:
    # 20250828 in a char(8), not 2025-08-28, which does not fit.
    fmt = ctx.params.get("format")
    return result.strftime(fmt) if fmt else result


def _as_date(ctx: RowContext, reference) -> date | None:
    """Resolve an anchor to a date. A literal ISO date is used as written."""
    if isinstance(reference, datetime):
        return reference.date()
    if isinstance(reference, date):
        return reference
    if not isinstance(reference, str):
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: anchor {reference!r} is not a date or a field name"
        )

    literal = _parse_iso(reference)
    if literal is not None:
        return literal

    value = ctx.resolve_name(reference)
    if value is None:
        # The anchor is null, so this date cannot exist either.
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = _parse_iso(str(value))
    if parsed is None:
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: anchor {reference} holds {value!r}, not a date"
        )
    return parsed


# The ways a date is written into a text column, besides ISO.
TEXT_DATE_FORMATS = ("%Y%m%d", "%m/%d/%Y", "%m%d%Y")


def _parse_iso(text: str) -> date | None:
    """A date written as text: ISO, or one of the forms a text column holds."""
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None

"""Sequence generator.

Numbers rows across a table, or restarts inside each driving parent so a
composite key such as (CLCL_ID, CDML_SEQ_NO) comes out right.
"""

from __future__ import annotations

from sdg.engine.generators.base import RowContext, register


@register("sequence")
def sequence(ctx: RowContext) -> int | str:
    start = int(ctx.require("start"))
    step = int(ctx.params.get("step", 1))
    scope = ctx.params.get("scope", "table")
    position = ctx.child_index if scope == "parent" else ctx.row_index
    value = start + position * step
    # An identifier written as text: EMP0000042, a prefix and a zero-filled
    # number of a fixed width.
    prefix = str(ctx.params.get("prefix") or "")
    suffix = str(ctx.params.get("suffix") or "")
    width = int(ctx.params.get("width") or 0)
    if prefix or suffix or width:
        return f"{prefix}{str(value).zfill(width)}{suffix}"
    return value

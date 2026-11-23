"""Pattern generator.

Produces values that match a regular expression. When the column is unique the
generator walks the pattern space with a coprime stride, so distinct rows get
distinct values without keeping a set of what it has already produced.
"""

from __future__ import annotations

from sdg.engine.generators.base import GenerationError, RowContext, register
from sdg.engine.pattern import PatternError, compile_pattern


@register("pattern")
def pattern(ctx: RowContext) -> str:
    regex = ctx.require("regex")
    try:
        compiled = compile_pattern(regex)
    except PatternError as exc:
        raise GenerationError(f"{ctx.table}.{ctx.field_name}: {exc}") from exc

    if ctx.params.get("unique"):
        # The starting point depends on the field and seed but not the row, so
        # the walk is stable and each row index lands on its own value.
        offset = ctx.field_stream("offset").next_u64() % compiled.space
        return compiled.unique_value(ctx.row_index, offset)

    return compiled.random_value(ctx.stream().next_u64())

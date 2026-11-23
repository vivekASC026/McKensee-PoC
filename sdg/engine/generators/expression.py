"""Expression generator.

Computes a value from other fields. When the expression aggregates over a table
that has not been generated yet, the generator returns a DeferredValue and the
engine fills it in once that child table exists.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sdg.engine.expressions import ExpressionError, compile_expression
from sdg.engine.generators.base import DeferredValue, GenerationError, RowContext, register


@register("expression")
def expression(ctx: RowContext) -> Any:
    source = ctx.require("expression")
    try:
        compiled = compile_expression(source)
    except ExpressionError as exc:
        raise GenerationError(f"{ctx.table}.{ctx.field_name}: {exc}") from exc

    if compiled.aggregates:
        # Children do not exist yet. The engine resolves this after their table
        # has been generated.
        return DeferredValue(source)

    missing = [name for name in compiled.names if not ctx.can_resolve(name)]
    if missing:
        raise GenerationError(
            f"{ctx.table}.{ctx.field_name}: expression needs {missing} which this row "
            f"does not have; fields available: {sorted(ctx.row)}"
        )

    try:
        value = compiled.evaluate(ctx.resolve_name, _no_aggregates)
    except ExpressionError as exc:
        raise GenerationError(f"{ctx.table}.{ctx.field_name}: {exc}") from exc

    return _round_to_decimals(value, ctx.params.get("decimals"))


def _no_aggregates(function: str, target: str):  # pragma: no cover - guarded above
    raise GenerationError(f"{function}({target}) cannot be evaluated here")


def _round_to_decimals(value: Any, decimals: Any) -> Any:
    if decimals is None or value is None or isinstance(value, str):
        return value
    places = int(decimals)
    if places == 0:
        return int(round(float(value)))
    quantum = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(quantum)

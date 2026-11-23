"""Text made from parts: a street address, and a template over the row.

`street_address` is a house number and a street from a list -- 4182 Oak Ave --
drawn like any other value, so the same seed and row give the same address.

`template` writes other fields of the row into a fixed shape:
"{FIRST_NAME}.{LAST_NAME}@example.com". A field it names that the row does not
hold leaves the value empty rather than half-written.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sdg.engine.generators.base import GenerationError, RowContext, register

_TOKEN = re.compile(r"\{([A-Za-z_][\w.]*)\}")


@register("street_address")
def street_address(ctx: RowContext) -> str:
    streets = ctx.require("streets")
    if not streets:
        raise GenerationError(f"{ctx.table}.{ctx.field_name}: street_address has no streets")
    low = int(ctx.params.get("min_number", 100))
    high = int(ctx.params.get("max_number", 9999))
    rnd = ctx.stream()
    number = rnd.randint(low, high)
    street = streets[rnd.below(len(streets))]
    parts = [street] if isinstance(street, str) else list(street)
    return " ".join([str(number)] + [str(p).strip() for p in parts if str(p).strip()])


@register("template")
def template(ctx: RowContext) -> str | None:
    shape = str(ctx.require("template"))
    missing = False

    def fill(match: re.Match) -> str:
        nonlocal missing
        name = match.group(1)
        if not ctx.can_resolve(name):
            missing = True
            return ""
        value = ctx.resolve_name(name)
        if value in (None, ""):
            missing = True
            return ""
        return value.isoformat() if isinstance(value, date) else str(value).strip()

    out = _TOKEN.sub(fill, shape)
    if missing:
        return None
    return out.lower() if ctx.params.get("lower") else out


def tokens(shape: str) -> list[str]:
    """The fields a template names."""
    return _TOKEN.findall(shape or "")


__all__: list[Any] = ["street_address", "template", "tokens"]

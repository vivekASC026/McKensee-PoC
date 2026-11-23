"""Constant and skip generators.

`constant` writes the same value into every row. `skip` writes nothing: the
column is left to the database, which is what identity, computed, and defaulted
columns need.
"""

from __future__ import annotations

from typing import Any

from sdg.engine.generators.base import RowContext, register


class Skipped:
    """Marker meaning the writer must leave this column out of the INSERT."""

    _instance = None

    def __new__(cls) -> "Skipped":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<skipped>"


SKIPPED = Skipped()


@register("constant")
def constant(ctx: RowContext) -> Any:
    return ctx.require("value")


@register("skip")
def skip(ctx: RowContext) -> Skipped:
    return SKIPPED

"""The scope combinations a walk visits, from the business pack's levels.

Discovery and the baseline both walk a record kind's scope: every value of the
first level (health: each group), then each of those narrowed by the next
level (a plan category, a plan...), down to the depth asked for. Which levels,
their columns and the filter each becomes are the pack's (loaders.<entity>
.scope, .sizes and .filters); nothing here names one.

A combination is a dict of filters, {filter key: [value]}, as the loader
takes them: a level of ids gives numbers, a level of values gives text.
"""

from __future__ import annotations

from typing import Any, Optional

from sdg.analyzer import entity_loader


def _levels(entity: str) -> list[dict[str, Any]]:
    """The entity's scope levels, broadest first, each with its filter key.

    Only the levels a pattern's scope is made of (store.combo_key): a level
    beyond them narrows an analysis (health's claim type) but no stored
    pattern is found in it, so no walk visits it.
    """
    from sdg.analyzer.pattern_scope import SCOPE_KEYS

    cfg = entity_loader.config(entity)
    by_column = {f["column"]: f for f in cfg["filters"]}
    out = []
    for level in cfg.get("scope", []):
        found = by_column.get(level["key"])
        if found is None or found["key"] not in SCOPE_KEYS:
            break
        out.append({**level, "filter": found["key"], "ids": found["kind"] == "ids"})
    return out


def size_depth(entity: str) -> int:
    """How deep the aggregate of sizes goes (loaders.<entity>.sizes)."""
    return len(entity_loader.config(entity)["sizes"]["levels"])


def _value(level: dict, raw: Any) -> Any:
    """A level's value as its filter takes it; None where it cannot be one."""
    if not level["ids"]:
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def depth_of(combo: dict, entity: str) -> int:
    """How many of the entity's scope levels a combination narrows by."""
    return sum(1 for level in _levels(entity) if combo.get(level["filter"]))


def first_filter(entity: str) -> Optional[str]:
    """The broadest level's filter (health: grgr_ck), where there is one."""
    levels = _levels(entity)
    return levels[0]["filter"] if levels else None


def by_size(entity: str, max_depth: int, limit_first: Optional[int], cap: int,
            minimum: int) -> Optional[list[dict]]:
    """Combinations ordered largest first, from one aggregate query per depth.

    The walk drops the small combinations before loading any rows and spends
    its budget on the ones that will produce patterns. None where the
    aggregate cannot be read, so the caller can walk unranked instead.
    `limit_first` caps the distinct values of the first level, not rows.
    """
    try:
        grid = entity_loader.combination_sizes(entity, max_depth=max_depth, minimum=minimum)
    except Exception as e:
        print(f"[discovery.{entity}] size pre-pass unavailable, walking unranked: "
              f"{type(e).__name__}: {e}")
        return None
    if grid is None or len(grid) == 0:
        return None

    levels = _levels(entity)
    if not levels:
        return None
    seen_first: dict = {}
    combos: list[dict] = []
    for row in grid.to_dict("records"):
        if len(combos) >= cap:
            break
        depth = int(row["depth"])
        first = _value(levels[0], row.get(levels[0]["key"]))
        if first is None:
            continue
        if limit_first:
            if first not in seen_first and len(seen_first) >= limit_first:
                continue
            seen_first.setdefault(first, True)
        combo = {levels[0]["filter"]: [first]}
        for level in levels[1:depth]:
            raw = row.get(level["key"])
            if raw is None:
                continue
            value = _value(level, raw)
            if value is not None:
                combo[level["filter"]] = [value]
        combos.append(combo)
    return combos


def every(entity: str, max_depth: int, limit_first: Optional[int], cap: int,
          deepest: Optional[int] = None) -> list[dict]:
    """Every combination down to `max_depth`, shallowest first under each
    first-level value (the level's choices narrowed by the ones above).

    One choices query per combination: the walk that needs no aggregate.
    `deepest` bounds the depth further (discovery walks no deeper than its
    size levels go).
    """
    levels = _levels(entity)
    depth = min(max_depth, len(levels), deepest if deepest is not None else len(levels))
    combos: list[dict] = []

    def walk(at: int, combo: dict) -> None:
        if at >= depth:
            return
        level = levels[at]
        chosen = entity_loader.given(**combo)
        choices = entity_loader.options(entity, level["level"], chosen)
        if at == 0 and limit_first:
            choices = choices[:limit_first]
        for choice in choices:
            if len(combos) >= cap:
                return
            value = _value(level, choice["key"])
            if value is None:
                continue
            narrower = {**combo, level["filter"]: [value]}
            combos.append(narrower)
            walk(at + 1, narrower)

    walk(0, {})
    return combos[:cap]

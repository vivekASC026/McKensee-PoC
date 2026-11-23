"""The source tables a plan needs, derived from its chosen levels.

Never typed by hand: each record kind needs its record table and its lines,
and each selected level needs the tables on its path, its own table and the
table its label is kept in. A table leaves the list when nothing selected
needs it any more. Every edit to a plan calls derive, so the list always
matches the hierarchy: this is the realignment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sdg.accelerator.hierarchy import Join, to_dict

if TYPE_CHECKING:
    from sdg.accelerator.plan import SourcePlan

ROLES = ("primary", "child")

# The kind of the join from a record's lines to it: the lines' own key starts
# with the record's key, which is what makes them its lines.
LINES_JOIN = "key"


@dataclass
class SourceTable:
    table: str
    # Each {"role": primary|child, "level": level id or None,
    #       "as": record|lines|level|label|bridge}.
    serves: list[dict[str, Any]] = field(default_factory=list)
    # How the table is reached from its record.
    joins: list[Join] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "serves": [dict(s) for s in self.serves],
                "joins": [to_dict(j) for j in self.joins]}

    @classmethod
    def from_dict(cls, held: dict[str, Any]) -> "SourceTable":
        return cls(table=held["table"], serves=[dict(s) for s in held.get("serves") or []],
                   joins=[Join.from_dict(j) for j in held.get("joins") or []])


def derive(plan: "SourcePlan") -> list[SourceTable]:
    """The tables the plan's record kinds and selected levels need, in the
    order they are first needed: primary before child, record, lines, then
    each selected level's path in level order."""
    found: dict[str, SourceTable] = {}

    def serve(table: str, role: str, level: str | None, as_: str, join: Join | None = None) -> None:
        held = found.setdefault(table, SourceTable(table=table))
        entry = {"role": role, "level": level, "as": as_}
        if entry not in held.serves:
            held.serves.append(entry)
        if join is not None and join not in held.joins:
            held.joins.append(join)

    for role in ROLES:
        kind = plan.record_kinds.get(role)
        if not kind:
            continue
        record = kind["table"]
        serve(record, role, None, "record")
        lines = kind.get("lines_table")
        if lines:
            key = list(kind.get("key_columns") or [])
            serve(lines, role, None, "lines", Join(lines, key, record, key, LINES_JOIN))
        levels = sorted((lv for lv in plan.levels(role) if lv.selected), key=lambda lv: lv.order)
        for level in levels:
            for join in level.path[:-1]:
                serve(join.to_table, role, level.id, "bridge", join)
            if level.path:
                serve(level.path[-1].to_table, role, level.id, "level", level.path[-1])
            else:
                serve(record, role, level.id, "level")
            if level.label_table and level.label_table != level.table:
                serve(level.label_table, role, level.id, "label", level.label_join)
    return list(found.values())

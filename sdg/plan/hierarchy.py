"""Arrange tables by their foreign keys and work out how many rows each gets.

Tables set to reuse or exclude are not parents. Only generated tables form the
graph, which is what lets a run generate claims against members that already
exist without treating the member table as something to fill.

A table with no generated parent is a root and takes an absolute row count.
Every other table takes a ratio to one driving parent, and its effective count
falls out of that parent's count. The whole set of counts is computed before
anything runs, so a user sees the size of the run before committing to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sdg.discovery.model import PhysicalModel


class HierarchyError(ValueError):
    """The foreign-key graph cannot be arranged into a generation order."""


@dataclass
class Node:
    name: str
    parents: list[str] = field(default_factory=list)
    level: int = 0
    driving_parent: str | None = None
    count: int | None = None
    effective_count: int = 0
    ratio_mean: float | None = None

    @property
    def is_root(self) -> bool:
        return not self.parents


@dataclass
class Hierarchy:
    nodes: dict[str, Node] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)

    def roots(self) -> list[Node]:
        return [self.nodes[name] for name in self.order if self.nodes[name].is_root]

    def total_rows(self) -> int:
        return sum(n.effective_count for n in self.nodes.values())


def build(
    model: PhysicalModel,
    modes: dict[str, str],
    *,
    driving_parents: dict[str, str] | None = None,
) -> Hierarchy:
    """Build the graph over the tables set to generate."""
    generated = {name for name, mode in modes.items() if mode == "generate"}
    overrides = driving_parents or {}

    hierarchy = Hierarchy()
    for table in model.tables:
        if table.name not in generated:
            continue
        parents = sorted(
            {
                fk.parent_table
                for fk in table.foreign_keys
                # A self-reference does not make a table its own parent.
                if fk.parent_table in generated and fk.parent_table != table.name
            }
        )
        hierarchy.nodes[table.name] = Node(name=table.name, parents=parents)

    _assign_levels(hierarchy)
    _assign_driving_parents(hierarchy, overrides)
    hierarchy.order = _topological_order(hierarchy)
    return hierarchy


def _assign_levels(hierarchy: Hierarchy) -> None:
    """Level is the longest path from a root. Cycles are an error."""
    resolving: set[str] = set()
    resolved: dict[str, int] = {}

    def level_of(name: str, trail: tuple[str, ...]) -> int:
        if name in resolved:
            return resolved[name]
        if name in resolving:
            cycle = " -> ".join(trail + (name,))
            raise HierarchyError(
                f"the foreign keys form a cycle, which the proof of concept cannot "
                f"generate: {cycle}. Set one of these tables to reuse or exclude."
            )
        resolving.add(name)
        node = hierarchy.nodes[name]
        value = 0 if not node.parents else 1 + max(
            level_of(parent, trail + (name,)) for parent in node.parents
        )
        resolving.discard(name)
        resolved[name] = value
        node.level = value
        return value

    for name in sorted(hierarchy.nodes):
        level_of(name, ())


def _assign_driving_parents(hierarchy: Hierarchy, overrides: dict[str, str]) -> None:
    """The driving parent is the deepest one, unless the user names another.

    Deepest wins because it is the most specific: a claim belongs to a member
    far more than it belongs to a product, and counting claims per member is
    the measurement that carries information.
    """
    for node in hierarchy.nodes.values():
        if not node.parents:
            continue
        override = overrides.get(node.name)
        if override:
            if override not in node.parents:
                raise HierarchyError(
                    f"{node.name}: {override} was named as the driving parent, but the "
                    f"only parents are {', '.join(node.parents) or 'none'}"
                )
            node.driving_parent = override
            continue
        node.driving_parent = max(
            node.parents, key=lambda p: (hierarchy.nodes[p].level, p)
        )


def _topological_order(hierarchy: Hierarchy) -> list[str]:
    """Parents before children, then by level and name so the order is stable."""
    return sorted(
        hierarchy.nodes,
        key=lambda name: (hierarchy.nodes[name].level, name),
    )


def compute_counts(
    hierarchy: Hierarchy,
    root_counts: dict[str, int],
    ratios: dict[str, float],
    *,
    reference_sizes: dict[str, int] | None = None,
) -> Hierarchy:
    """Fill in the effective count for every table.

    A root takes its absolute count. Every other table takes its driving
    parent's count multiplied by the mean children per parent. A driving parent
    that is a reference set contributes the size of that set.
    """
    sizes = reference_sizes or {}
    missing_counts = [n.name for n in hierarchy.roots() if n.name not in root_counts]
    if missing_counts:
        raise HierarchyError(
            f"no row count for root table{'s' if len(missing_counts) > 1 else ''}: "
            f"{', '.join(missing_counts)}"
        )

    for name in hierarchy.order:
        node = hierarchy.nodes[name]
        if node.is_root:
            node.count = int(root_counts[name])
            node.effective_count = node.count
            continue

        ratio = ratios.get(name)
        if ratio is None:
            raise HierarchyError(
                f"no ratio for {name}, which is a child of {node.driving_parent}. "
                f"Every child table needs a ratio to its driving parent."
            )
        node.ratio_mean = ratio
        parent = hierarchy.nodes.get(node.driving_parent or "")
        parent_rows = parent.effective_count if parent else sizes.get(node.driving_parent or "", 0)
        node.effective_count = int(round(parent_rows * ratio))
    return hierarchy


def describe(hierarchy: Hierarchy) -> list[str]:
    """One line per table, indented by level, for the hierarchy view."""
    lines: list[str] = []
    for name in hierarchy.order:
        node = hierarchy.nodes[name]
        indent = "  " * node.level
        if node.is_root:
            detail = f"{node.effective_count:,} rows"
        else:
            detail = (
                f"{node.effective_count:,} rows "
                f"({node.ratio_mean:.2f} per {node.driving_parent.split('.')[-1]})"
            )
        lines.append(f"{indent}{name}  {detail}")
    return lines

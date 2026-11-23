"""Load the keys of tables the run reuses rather than generates.

In most real runs some tables already hold data and the new rows have to point
at them. Those tables are set to reuse, bound to a source, and their keys are
loaded at the start of the run.

Three load strategies, and the manifest records which was used so a run can be
reproduced: full for small sets, a sampled subset for large ones, and a
snapshot for a portable run. Sampling is seeded, so the same seed picks the
same keys.

The source may be a different database from the target. Nothing here needs a
linked server; the keys are read over the same connection the profiler uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sdg.discovery.connections import ConnectionRegistry
from sdg.engine.seed import ValueRandom, value_seed
from sdg.plan.filters import parse_filter


@dataclass
class ReferenceSet:
    table: str
    connection: str
    source_table: str
    key_column: str
    keys: list[Any] = field(default_factory=list)
    attributes: dict[Any, dict[str, Any]] = field(default_factory=dict)
    strategy: str = "full"
    filter: str | None = None
    sample_seed: str | None = None
    available: int = 0

    @property
    def size(self) -> int:
        return len(self.keys)

    def as_record(self) -> dict[str, Any]:
        record = {
            "table": self.table,
            "strategy": self.strategy,
            "size": self.size,
            "source": f"{self.connection}:{self.source_table}.{self.key_column}",
        }
        if self.filter:
            record["filter"] = self.filter
        if self.sample_seed:
            record["sample_seed"] = self.sample_seed
        return record

    def describe(self) -> str:
        detail = f"{self.size:,} keys loaded ({self.strategy})"
        if self.filter:
            excluded = self.available - self.size
            detail += (
                f"; {self.filter} kept {self.size:,} of {self.available:,}, "
                f"leaving out {excluded:,}"
            )
        elif self.strategy == "sample" and self.available > self.size:
            detail += f" sampled from {self.available:,}"
        return detail


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _qualified(schema: str | None, table: str) -> str:
    if "." in table:
        schema, _, table = table.partition(".")
    return f"{_quote(schema)}.{_quote(table)}" if schema else _quote(table)


def load_reference_set(
    registry: ConnectionRegistry,
    *,
    table: str,
    connection: str,
    source_table: str,
    key_column: str,
    schema: str | None = "dbo",
    attributes: Iterable[str] = (),
    filter: str | None = None,
    strategy: str = "full",
    sample_size: int | None = None,
    seed: str = "reference-set",
) -> ReferenceSet:
    """Read one reference set. Read-only, one query."""
    attribute_list = list(attributes)
    columns = ", ".join(_quote(c) for c in [key_column, *attribute_list])
    source = _qualified(schema, source_table)
    # The filter is read as conditions; its values are parameters (filters.py).
    condition, params = parse_filter(filter) if filter else ("", [])
    where = f" WHERE {condition}" if condition else ""
    # Ordered so the same rows come back in the same order on every run, which
    # is what makes a seeded sample reproducible.
    sql = f"SELECT {columns} FROM {source}{where} ORDER BY {_quote(key_column)}"  # noqa: S608

    rows = registry.query(connection, sql, params)
    # Count what the filter left behind, so "37 of 40" can be said truthfully
    # rather than reporting the already-filtered count as the total.
    available = len(rows)
    if filter:
        total = registry.scalar(connection, f"SELECT COUNT(*) FROM {source}")  # noqa: S608
        available = int(total or available)

    if strategy == "sample" and sample_size and sample_size < available:
        rows = _seeded_sample(rows, sample_size, seed, table)

    result = ReferenceSet(
        table=table,
        connection=connection,
        source_table=f"{schema}.{source_table}" if schema else source_table,
        key_column=key_column,
        keys=[_clean(row[key_column]) for row in rows],
        strategy=strategy,
        filter=filter,
        sample_seed=seed if strategy in {"sample", "snapshot"} else None,
        available=available,
    )
    if attribute_list:
        result.attributes = {
            _clean(row[key_column]): {a: _clean(row[a]) for a in attribute_list}
            for row in rows
        }
    return result


def _clean(value: Any) -> Any:
    return value.rstrip() if isinstance(value, str) else value


def _seeded_sample(rows: list[dict], size: int, seed: str, scope: str) -> list[dict]:
    """A reproducible subset: rows ranked by a seeded hash, then the first N."""
    ranked = sorted(
        rows,
        key=lambda row: ValueRandom(
            value_seed(seed, scope, str(next(iter(row.values()))), 0)
        ).next_u64(),
    )
    return ranked[:size]


def load_for_plan(
    registry: ConnectionRegistry,
    sources: dict[str, dict[str, Any]],
    *,
    seed: str = "reference-set",
) -> dict[str, ReferenceSet]:
    """Load every reference set a plan binds, keyed by the table it stands in for."""
    loaded: dict[str, ReferenceSet] = {}
    for table, binding in sources.items():
        loaded[table] = load_reference_set(
            registry,
            table=table,
            connection=binding.get("connection", "source"),
            source_table=binding["table"],
            key_column=binding["key_column"],
            schema=binding.get("schema", "dbo"),
            attributes=binding.get("attributes", ()),
            filter=binding.get("filter"),
            strategy=binding.get("load", "full"),
            sample_size=binding.get("sample_size"),
            seed=seed,
        )
    return loaded


def catalog_reuse_bindings(catalog) -> dict[str, dict[str, Any]]:
    """Bindings for every element the catalog marks as reuse.

    These are loaded even when no column in this application maps to the
    element, because the catalog belongs to the organization and the run should
    report what master data it can reach.
    """
    bindings: dict[str, dict[str, Any]] = {}
    for element in catalog.elements:
        domain = element.reuse_domain
        if domain is None:
            continue
        bindings[f"element:{element.name}"] = {
            "connection": domain.connection,
            "table": domain.table,
            "schema": domain.schema_ or "dbo",
            "key_column": domain.key_column,
            "attributes": domain.attributes,
            "filter": domain.filter,
            "load": domain.load,
            "sample_size": domain.sample_size,
        }
    return bindings

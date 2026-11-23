"""Discovery in one call: extract, infer, classify.

The order matters. Inference has to run before classification, because a column
that turns out to be an undeclared foreign key belongs in the reference bucket,
not wherever its name and type would otherwise have put it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from sdg.discovery.classify import bucket_counts, classify
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.inference import InferredKey, attach, infer
from sdg.discovery.metadata import extract
from sdg.discovery.model import PhysicalModel


@dataclass
class DiscoveryResult:
    model: PhysicalModel
    inferred: list[InferredKey] = field(default_factory=list)
    # Pairs the database would not check, each with the reason it gave.
    untested: list[tuple] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "tables": len(self.model.tables),
            "columns": sum(len(t.columns) for t in self.model.tables),
            "declared_foreign_keys": sum(
                1 for t in self.model.tables for fk in t.foreign_keys if fk.kind == "declared"
            ),
            "inferred_foreign_keys": len(self.inferred),
            "untested_pairs": len(self.untested),
            "buckets": bucket_counts(self.model),
        }


def discover(
    registry: ConnectionRegistry | None = None,
    connection: str = "source",
    *,
    include_schemas: Iterable[str] | None = None,
    exclude_tables: Iterable[str] | None = None,
    exclude_schemas: Iterable[str] | None = None,
    derivable_columns: Iterable[str] = (),
    infer_keys: bool = True,
) -> DiscoveryResult:
    registry = registry or ConnectionRegistry()
    if exclude_schemas is None:
        # The pattern analyzer keeps its own tables in this database. They are
        # the tool's working state, not data anyone wants generated, so they
        # are left out unless something asks for them by name.
        from sdg.analyzer.config import SQL_SCHEMA as ANALYZER_SCHEMA

        exclude_schemas = (ANALYZER_SCHEMA,)
    model = extract(
        registry,
        connection,
        include_schemas=include_schemas,
        exclude_tables=exclude_tables,
        exclude_schemas=exclude_schemas,
    )
    inferred: list[InferredKey] = []
    untested: list[tuple] = []
    if infer_keys:
        inferred = infer(registry, model, connection, untested=untested)
        attach(model, inferred)
    classify(model, derivable_columns=derivable_columns)
    return DiscoveryResult(model=model, inferred=inferred, untested=untested)

"""Turn evidence into a confidence, and confidence into a decision.

The rule is the one the build brief sets: high confidence needs an alias, or
foreign-key propagation, or a format together with a shared name token. A
format on its own is never enough, because a two-letter code matches half a
dozen elements equally well. High-confidence matches are accepted; everything
else goes to the review queue with its nearest element attached, so the user is
deciding between named options rather than starting from nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sdg.catalog.model import Catalog
from sdg.discovery.model import PhysicalModel, indexed
from sdg.mapping.matchers import Match, candidates_for
from sdg.profiling.model import ProfileReport

# Buckets whose rule comes from the structure, not from an element. They are
# still reported, but they are not counted against the auto-map rate: a column
# the database fills needs no element and asking a user about it is noise.
STRUCTURAL_BUCKETS = {"system"}

# One strong kind, or a format backed by a name token.
STRONG_KINDS = {"alias", "fk_propagation", "check_digit"}
# Below this gap, two elements are close enough that the choice goes to a person.
AMBIGUITY_GAP = 0.15


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MappingCandidate(Base):
    element: str
    score: float
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ColumnMapping(Base):
    table: str
    column: str
    bucket: str | None = None
    element: str | None = None
    confidence: str | None = None  # high | medium | low
    accepted: bool = False
    reason: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    alternatives: list[MappingCandidate] = Field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    def needs_review(self) -> bool:
        return not self.accepted and self.bucket not in STRUCTURAL_BUCKETS


class MappingStore(Base):
    created: str = ""
    catalog_version: str = ""
    mappings: list[ColumnMapping] = Field(default_factory=list)

    def for_column(self, table: str, column: str) -> ColumnMapping | None:
        return indexed(self, self.mappings, _mapping_key).get((table, column))

    def statistics(self) -> dict[str, Any]:
        """The proof-of-concept success measure, and the parts it is made of."""
        countable = [m for m in self.mappings if m.bucket not in STRUCTURAL_BUCKETS]
        high = [m for m in countable if m.confidence == "high"]
        by_kind: dict[str, int] = {}
        for mapping in high:
            for kind in sorted({e["kind"] for e in mapping.evidence}):
                by_kind[kind] = by_kind.get(kind, 0) + 1
        return {
            "columns": len(self.mappings),
            "system_columns": len(self.mappings) - len(countable),
            "mappable_columns": len(countable),
            "high": len(high),
            "medium": sum(1 for m in countable if m.confidence == "medium"),
            "low": sum(1 for m in countable if m.confidence == "low"),
            "unmapped": sum(1 for m in countable if not m.element),
            "auto_map_rate": round(len(high) / len(countable), 4) if countable else 0.0,
            "high_confidence_by_evidence": dict(sorted(by_kind.items())),
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "MappingStore":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def score_match(match: Match) -> tuple[str, str]:
    """Return (confidence, the reason for it)."""
    kinds = match.kinds()
    strong = kinds & STRONG_KINDS
    if strong:
        return "high", f"{', '.join(sorted(strong))} evidence"
    if "format" in kinds and "name_token" in kinds:
        return "high", "format matches and the name shares a token"
    if "format" in kinds:
        return "medium", "format matches, but nothing in the name confirms it"
    if "name_token" in kinds:
        return "low", "only the name suggests it"
    return "low", "weak evidence"


def map_columns(
    model: PhysicalModel, catalog: Catalog, report: ProfileReport | None = None
) -> MappingStore:
    """Map every column, in an order that lets foreign keys inherit."""
    store = MappingStore(
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        catalog_version=catalog.version,
    )
    accepted: dict[str, str] = {}

    # Parents first, so a foreign key can inherit what its parent already holds.
    for table in _tables_parents_first(model):
        for column in table.columns:
            profile = report.column(table.name, column.name) if report else None
            candidates = candidates_for(table, column, catalog, profile, model, accepted)
            mapping = _decide(table.name, column, candidates)
            store.mappings.append(mapping)
            if mapping.accepted and mapping.element:
                accepted[mapping.qualified] = mapping.element
    return store


def _decide(table_name: str, column, candidates: list[Match]) -> ColumnMapping:
    mapping = ColumnMapping(table=table_name, column=column.name, bucket=column.bucket)
    if not candidates:
        mapping.confidence = None
        mapping.reason = "no element matched"
        return mapping

    best = candidates[0]
    confidence, reason = score_match(best)

    # Two elements that fit almost equally well are a question, not an answer.
    if len(candidates) > 1 and confidence == "high":
        runner_up = candidates[1]
        runner_up_confidence, _ = score_match(runner_up)
        if runner_up_confidence == "high" and best.score() - runner_up.score() < AMBIGUITY_GAP:
            confidence = "medium"
            reason = (
                f"{best.element} and {runner_up.element} fit about equally well "
                f"({best.score():.2f} against {runner_up.score():.2f})"
            )

    mapping.element = best.element
    mapping.confidence = confidence
    mapping.reason = reason
    mapping.accepted = confidence == "high"
    mapping.evidence = [e.as_dict() for e in best.evidence]
    mapping.alternatives = [
        MappingCandidate(
            element=candidate.element,
            score=candidate.score(),
            evidence=[e.as_dict() for e in candidate.evidence],
        )
        for candidate in candidates[1:4]
    ]
    return mapping


def _tables_parents_first(model: PhysicalModel):
    """Tables ordered so a foreign key's parent is mapped before the child."""
    remaining = {t.name: t for t in model.tables}
    ordered = []
    while remaining:
        ready = [
            table for table in remaining.values()
            if all(
                fk.parent_table not in remaining or fk.parent_table == table.name
                for fk in table.foreign_keys
            )
        ]
        if not ready:  # a cycle; order is then arbitrary but stable
            ready = [remaining[min(remaining)]]
        for table in sorted(ready, key=lambda t: t.name):
            ordered.append(table)
            remaining.pop(table.name)
    return ordered


def _mapping_key(mapping: ColumnMapping) -> tuple[str, str]:
    return (mapping.table, mapping.column)

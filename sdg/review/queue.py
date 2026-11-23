"""What still needs a person.

Four things reach the queue: a mapping the tool is not sure about, a proposal
below high confidence, a business key with no format to generate from, and a
derivable column with no expression behind it. Everything else has been
accepted automatically and can still be changed.

The queue is also written out as a decisions file the user edits by hand. That
file is the input to the resolver, so an approval is a recorded change to the
plan rather than a click that leaves no trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdg.discovery.model import CHARACTER_TYPES, PhysicalModel
from sdg.mapping.scorer import MappingStore
from sdg.plan.proposer import Proposal

REASON_LABELS = {
    "mapping_uncertain": "the element it maps to is not certain",
    "proposal_uncertain": "the proposed rule is not certain",
    "business_key_without_format": "it is a business key with no format to generate from",
    "derivable_without_expression": "it is a derived column with no expression",
    "unmapped": "no catalog element matched",
}


@dataclass
class QueueItem:
    table: str
    column: str
    bucket: str
    reasons: list[str] = field(default_factory=list)
    element: str | None = None
    mapping_confidence: str | None = None
    mapping_evidence: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    generator: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    proposal_confidence: str | None = None
    proposal_evidence: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    def explain(self) -> str:
        return "; ".join(REASON_LABELS.get(r, r) for r in self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "bucket": self.bucket,
            "reasons": self.reasons,
            "explanation": self.explain(),
            "element": self.element,
            "mapping_confidence": self.mapping_confidence,
            "alternatives": self.alternatives,
            "generator": self.generator,
            "params": self.params,
            "proposal_confidence": self.proposal_confidence,
            "note": self.note,
        }


def build_queue(
    model: PhysicalModel,
    mappings: MappingStore,
    proposals: list[Proposal],
) -> list[QueueItem]:
    by_column = {p.qualified: p for p in proposals}
    items: list[QueueItem] = []

    for table in model.tables:
        for column in table.columns:
            qualified = f"{table.name}.{column.name}"
            if column.bucket == "system":
                continue

            mapping = mappings.for_column(table.name, column.name)
            proposal = by_column.get(qualified)
            reasons: list[str] = []

            if mapping is not None and not mapping.accepted:
                reasons.append("unmapped" if not mapping.element else "mapping_uncertain")

            if proposal is not None and proposal.confidence != "high":
                reasons.append("proposal_uncertain")

            # A business key with no format cannot be generated at all. The
            # exception is an integer key, which a sequence covers.
            if column.bucket == "business_key" and column.type_name in CHARACTER_TYPES:
                has_format = bool(
                    proposal and proposal.generator == "pattern" and proposal.params.get("regex")
                )
                if not has_format:
                    reasons.append("business_key_without_format")

            if column.bucket == "derivable":
                has_expression = bool(proposal and proposal.generator == "expression")
                if not has_expression:
                    reasons.append("derivable_without_expression")

            if not reasons:
                continue

            items.append(
                QueueItem(
                    table=table.name,
                    column=column.name,
                    bucket=column.bucket or "free",
                    reasons=reasons,
                    element=mapping.element if mapping else None,
                    mapping_confidence=mapping.confidence if mapping else None,
                    mapping_evidence=mapping.evidence if mapping else [],
                    alternatives=[a.element for a in (mapping.alternatives if mapping else [])],
                    generator=proposal.generator if proposal else None,
                    params=proposal.params if proposal else {},
                    proposal_confidence=proposal.confidence if proposal else None,
                    proposal_evidence=proposal.evidence if proposal else [],
                    note=proposal.note if proposal else "",
                )
            )
    return items


def decisions_template(items: list[QueueItem]) -> dict[str, Any]:
    """A file the user edits by hand to approve or change what the tool proposed.

    Every queued column appears with `approved: false` and the tool's own
    answer already filled in, so approving is deleting a word rather than
    typing a rule from scratch.
    """
    return {
        "_comment": (
            "Edit this file to approve or change what the tool proposed, then run "
            "`sdg plan`. Set approved to true to accept the element and rule shown. "
            "Change element, generator, or params to override. Entries left with "
            "approved false stay as blocking warnings on the plan."
        ),
        "columns": [
            {
                "table": item.table,
                "column": item.column,
                "approved": False,
                "element": item.element,
                "generator": item.generator,
                "params": item.params,
                "_why": item.explain(),
                "_alternatives": item.alternatives,
            }
            for item in items
        ],
        "tables": {},
    }


def load_decisions(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"columns": [], "tables": {}}
    document = json.loads(path.read_text(encoding="utf-8"))
    document.setdefault("columns", [])
    document.setdefault("tables", {})
    return document


def save_decisions(document: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def summarize(items: list[QueueItem]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in items:
        for reason in item.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return {"queued_columns": len(items), "by_reason": dict(sorted(counts.items()))}

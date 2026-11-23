"""Which analysis serves a record kind, chosen by its role in the pack.

The primary records are analyzed by the profiler in service.py, the child
records by claims_service.py (health: members and claims; a bank: customers
and accounts). What each returns is read here by role too: how many records
the scope held, and how many each pattern covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from sdg import packs


@dataclass(frozen=True)
class Analysis:
    entity: str
    role: str
    analyze: Callable[[dict, Optional[int]], dict]
    # The result's count of records in scope, and each pattern's size.
    subset_key: str
    size_key: str

    def subset(self, result: dict) -> int:
        return int(result.get(self.subset_key) or 0)

    def size(self, pattern: dict) -> int:
        return int(pattern.get(self.size_key) or 0)


def for_entity(entity: Any) -> Analysis:
    """The analysis of a record kind; LookupError for one the pack has not."""
    if packs.is_primary(entity):
        from sdg.analyzer.services import service

        return Analysis(str(entity), "primary", lambda f, k: service.analyze(f, k=k),
                        "subset_members", "size")
    if packs.is_child(entity):
        from sdg.analyzer.services import claims_service

        return Analysis(str(entity), "child", lambda f, k: claims_service.analyze(f, k=k),
                        "subset_claims", "n_claims")
    pack = packs.active()
    raise LookupError(f"entity must be {pack.primary!r} or {pack.child!r}.")

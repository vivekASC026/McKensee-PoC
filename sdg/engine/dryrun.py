"""Dry run: execute a plan in memory and hand back data frames.

Nothing is written anywhere. This is what the preview grid shows and what a
user looks at before committing to a real run.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

import pandas as pd

from sdg.engine.execute import ReferenceSets, RunResult, run_plan
from sdg.plan.model import Plan

DEFAULT_ROWS = 5


@dataclass
class DryRun:
    frames: dict[str, pd.DataFrame] = dataclass_field(default_factory=dict)
    result: RunResult | None = None

    def preview(self, rows: int = DEFAULT_ROWS) -> dict[str, pd.DataFrame]:
        return {name: frame.head(rows) for name, frame in self.frames.items()}

    def counts(self) -> dict[str, int]:
        return {name: len(frame) for name, frame in self.frames.items()}


def dry_run(
    plan: Plan,
    *,
    rows: int = DEFAULT_ROWS,
    reference_sets: ReferenceSets | None = None,
) -> DryRun:
    """Generate at most `rows` rows per table and return them as data frames."""
    result = run_plan(plan, limit=rows, reference_sets=reference_sets)
    return DryRun(frames=to_frames(result, plan), result=result)


def to_frames(result: RunResult, plan: Plan | None = None) -> dict[str, pd.DataFrame]:
    """Convert generated rows to data frames, keeping the plan's column order."""
    frames: dict[str, pd.DataFrame] = {}
    for name, table_result in result.tables.items():
        columns = _columns_for(name, table_result, plan)
        frames[name] = pd.DataFrame(table_result.rows, columns=columns)
    return frames


def _columns_for(name: str, table_result, plan: Plan | None) -> list[str]:
    if plan is not None:
        table = plan.table(name)
        if table is not None:
            return [f.name for f in table.fields if f.generator != "skip"]
    seen: dict[str, None] = {}
    for row in table_result.rows:
        seen.update(dict.fromkeys(row))
    return list(seen)


def format_preview(frames: dict[str, pd.DataFrame], rows: int = DEFAULT_ROWS) -> str:
    """A plain-text preview, used by the CLI."""
    blocks: list[str] = []
    for name, frame in frames.items():
        head = frame.head(rows)
        blocks.append(f"{name}  ({len(frame):,} rows)")
        blocks.append(head.to_string(index=False) if len(head) else "  (no rows)")
        blocks.append("")
    return "\n".join(blocks)

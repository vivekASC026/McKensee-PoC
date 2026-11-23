"""The record of what a run did.

Enough to reproduce the run, and enough to undo it. The plan version and seed
say what was generated; the keys say which rows were added, so cleanup can
remove exactly those and nothing else.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sdg.plan.model import Plan
from sdg.writers.sqlserver import WriteReport


def build_manifest(
    plan: Plan,
    report: WriteReport,
    *,
    connection: str,
    database: str,
    server: str | None = None,
    reference_sizes: dict[str, int] | None = None,
    disable_triggers: bool = False,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run": {
            "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(report.seconds, 3),
            "rows_written": report.total_rows(),
            "connection": connection,
            "database": database,
            # Cleanup refuses unless the target is still this database here.
            **({"server": server} if server else {}),
            "triggers_disabled": disable_triggers,
        },
        "plan": {
            "name": plan.meta.name,
            "version": plan.meta.version,
            "seed": plan.meta.seed,
            "created": plan.meta.created,
            "catalog": plan.meta.catalog,
        },
        "generation_order": list(plan.generation_order),
        "tables": [t.as_record() for t in report.tables],
        "reference_sets": [
            record.model_dump(mode="json", exclude_none=True)
            for record in (plan.meta.reference_sets or [])
        ],
        "reference_sizes": reference_sizes or {},
        # Keys are what makes cleanup exact rather than approximate. The whole
        # primary key is recorded, so a table keyed on (claim, line number) can
        # be cleaned up as precisely as one keyed on a single column.
        "keys": {t.table: t.key_values for t in report.tables if t.key_columns},
        "validation": validation or {},
    }


def merge_manifests(earlier: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    """One manifest covering both runs.

    A manifest is the only record of which rows a run wrote, and cleanup reads
    it to remove exactly those. Saving a new one over an old one therefore
    loses the earlier run's rows -- they stay in the target with nothing able
    to find them again.

    That did not matter while a second run collided on the primary key and
    could not happen. Once the key advances past what is already there, running
    twice is an ordinary thing to do, and the rows have to stay accounted for.

    Keys are merged per table and deduplicated: a run that rewrote a key the
    earlier run also wrote should be removed once, not twice.
    """
    merged = dict(later)

    rows_by_table: dict[str, dict[str, Any]] = {}
    for record in list(earlier.get("tables") or []) + list(later.get("tables") or []):
        name = record.get("table")
        if name is None:
            continue
        held = rows_by_table.get(name)
        if held is None:
            rows_by_table[name] = dict(record)
        else:
            held["rows_written"] = int(held.get("rows_written") or 0) + int(
                record.get("rows_written") or 0
            )
            held["seconds"] = round(
                float(held.get("seconds") or 0) + float(record.get("seconds") or 0), 3
            )
    merged["tables"] = list(rows_by_table.values())

    keys: dict[str, list] = {}
    for source in (earlier.get("keys") or {}, later.get("keys") or {}):
        for table, values in source.items():
            seen = keys.setdefault(table, [])
            known = {_hashable(v) for v in seen}
            seen.extend(v for v in values if _hashable(v) not in known)
    merged["keys"] = keys

    run = dict(merged.get("run") or {})
    run["rows_written"] = int((earlier.get("run") or {}).get("rows_written") or 0) + int(
        (later.get("run") or {}).get("rows_written") or 0
    )
    run["runs"] = int((earlier.get("run") or {}).get("runs") or 1) + 1
    merged["run"] = run
    return merged


def _hashable(value: Any):
    return tuple(value) if isinstance(value, list) else value


def save_manifest(manifest: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def load_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def summarize(manifest: dict[str, Any]) -> str:
    run = manifest.get("run", {})
    tables = manifest.get("tables", [])
    lines = [
        f"seed {manifest.get('plan', {}).get('seed')} "
        f"into {run.get('database')} in {run.get('seconds')}s",
    ]
    for table in tables:
        lines.append(
            f"  {table['table']}: {table['rows_written']:,} rows"
            + (" (identity insert)" if table.get("identity_insert") else "")
        )
    lines.append(f"  total {run.get('rows_written', 0):,} rows")
    return "\n".join(lines)

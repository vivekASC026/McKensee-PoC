"""A database load as a generation request: kept for audit, regenerated as evidence.

Nothing loaded is kept. A load's request holds what regenerates its rows: the
plan as it was, where each generated key started (read from the target just
before writing), which version of each algorithm made the values, and a
fingerprint of each table's rows as they were written. The keys of the tables
the load pointed at -- plans, groups, read from the source -- are not kept
either, only a fingerprint of them: regenerating reads them again, and says
so when the source no longer holds what it held then.

Regenerating never writes to a database. It makes the rows again and gives
them as CSV, with the evidence of whether each table came out as it was
written.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable

from sdg.plan.model import Plan

FORMAT = "database"
SUBJECT = "load"


def _text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _line(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, default=_text, separators=(",", ":")).encode("utf-8")


def written_columns(plan: Plan, table: str) -> list[str]:
    """The columns a load writes: every field the plan does not skip."""
    found = plan.table(table)
    return [f.name for f in found.fields if f.generator != "skip"] if found else []


def rows_fingerprint(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """SHA-256 of a table's rows as written, in order, column by column."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_line([row.get(c) for c in columns]))
        digest.update(b"\n")
    return digest.hexdigest()


def reference_fingerprint(reference) -> dict[str, Any]:
    """What a load pointed at, without its keys: how many, and their fingerprint."""
    digest = hashlib.sha256()
    for key in reference.keys:
        digest.update(_line([key, reference.attributes.get(key)]))
        digest.update(b"\n")
    return {
        "source": f"{reference.connection}:{reference.source_table}.{reference.key_column}",
        "strategy": reference.strategy,
        "size": reference.size,
        "available": reference.available,
        "filter": reference.filter,
        "fingerprint": digest.hexdigest(),
    }


def sequence_starts(plan: Plan) -> dict[str, int]:
    """Where each generated key started: `table.column` to its first value."""
    return {
        f"{table.name}.{field.name}": int(field.params.get("start") or 1)
        for table in plan.tables
        if table.mode == "generate"
        for field in table.fields
        if field.generator == "sequence"
    }


def with_starts(plan: Plan, starts: dict[str, int]) -> Plan:
    """The plan with its keys starting where the load's did, not where the target is now."""
    adjusted = plan.model_copy(deep=True)
    for table in adjusted.tables:
        for field in table.fields:
            start = starts.get(f"{table.name}.{field.name}")
            if field.generator == "sequence" and start is not None:
                field.params["start"] = start
    return adjusted


def tables_written(plan: Plan, result, names: list[str]) -> list[dict[str, Any]]:
    out = []
    for name in names:
        columns = written_columns(plan, name)
        rows = result.rows(name)
        out.append({"table": name, "rows": len(rows),
                    "fingerprint": rows_fingerprint(rows, columns)})
    return out


def overall(tables: list[dict[str, Any]]) -> str:
    """One fingerprint for the load: its tables' fingerprints, in order."""
    return hashlib.sha256(_line([[t["table"], t["fingerprint"]] for t in tables])).hexdigest()


# ---------------------------------------------------------------------------
# Recording a load
# ---------------------------------------------------------------------------

def record(*, plan: Plan, ran: Plan, result, report, references: dict[str, Any],
           target: dict[str, str], partial: bool, who: str, version: str,
           build: str) -> dict[str, Any]:
    """The load's request and its record, once its rows are in the target.

    `plan` is the plan as it stands; `ran` is the copy the load ran, with its
    keys moved past what the target held."""
    from sdg.analyzer.services import generation_requests as gr
    from sdg.engine.generators import algorithm_versions

    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    names = [t.table for t in report.tables]
    tables = tables_written(ran, result, names)
    request = gr.open_request(
        pattern_key=f"load:{target['database']}"[:100], started=started,
        seed=plan.meta.seed, pattern_id=None,
        pattern_name=f"{plan.meta.name or 'The plan'} into {target['database']}",
        subject=SUBJECT, pattern_body=None, created_by=who,
    )
    params = {
        "target": target,
        "plan": {"name": plan.meta.name, "version": plan.meta.version,
                 "seed": plan.meta.seed},
        "starts": sequence_starts(ran),
        "tables": tables,
        "rows": sum(t["rows"] for t in tables),
        "reference_sets": {name: reference_fingerprint(r) for name, r in references.items()},
        "partial": partial,
    }
    snapshots = {
        "plan": gr.keep_snapshot("plan", plan.to_dict()),
        "algorithms": gr.keep_snapshot("algorithms", algorithm_versions()),
    }
    kept = gr.record_file(
        request_id=request["id"], file_name=f"{target['database']} load", format=FORMAT,
        subject=SUBJECT, params=params, snapshots=snapshots, sdm_version=version,
        sdm_build=build, fingerprint=overall(tables), size=0, written_by=who,
    )
    return {"request": request["id"], "file": kept["id"]}


# ---------------------------------------------------------------------------
# Regenerating one
# ---------------------------------------------------------------------------

def regenerate(original: dict[str, Any], *, load_references: Callable[[Plan], tuple[Any, dict]],
               who: str, version: str, build: str) -> tuple[bytes, str, dict[str, Any]]:
    """The load's rows made again from its record alone, as a zip of CSVs.

    `load_references` reads the keys the plan points at, read-only, the way a
    load reads them. Returns the zip, its name and the evidence it holds."""
    from sdg.analyzer.services import generation_requests as gr
    from sdg.engine.execute import refuse_never_generated, run_plan
    from sdg.engine.generators import pinned

    params = original["params"]
    plan = with_starts(Plan.from_dict(gr.snapshot(original["snapshots"]["plan"])),
                       params.get("starts") or {})
    versions = gr.snapshot(original["snapshots"]["algorithms"])
    refuse_never_generated(plan)
    sets, loaded = load_references(plan)
    with pinned(versions):
        result = run_plan(plan, reference_sets=sets)

    kept_tables = params.get("tables") or []
    now = tables_written(plan, result, [t["table"] for t in kept_tables])
    by_name = {t["table"]: t for t in now}
    tables = [{
        "table": t["table"],
        "rows_written": t["rows"],
        "rows_now": by_name[t["table"]]["rows"],
        "matched": by_name[t["table"]]["fingerprint"] == t["fingerprint"],
    } for t in kept_tables]

    kept_refs = params.get("reference_sets") or {}
    refs_now = {name: reference_fingerprint(r) for name, r in loaded.items()}
    references = [{
        "table": name,
        "source": held["source"],
        "keys_then": held["size"],
        "keys_now": (refs_now.get(name) or {}).get("size"),
        "matched": (refs_now.get(name) or {}).get("fingerprint") == held["fingerprint"],
    } for name, held in kept_refs.items()]

    fingerprint = overall(now)
    matched = fingerprint == original["fingerprint"]
    request = gr.get(original["request_id"]) or {}
    evidence = {
        "request": original["request_id"],
        "loaded": {"at": original.get("written_at"), "by": original.get("written_by"),
                   "into": params.get("target"), "sdm_version": original.get("sdm_version"),
                   "sdm_build": original.get("sdm_build"), "partial": params.get("partial")},
        "plan": params.get("plan"),
        "seed": request.get("seed"),
        "regenerated": {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "by": who, "sdm_version": version, "sdm_build": build},
        "matched": matched,
        "tables": tables,
        "reference_sets": references,
        "explanation": explain(tables, references, original, build),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("evidence.json", json.dumps(evidence, indent=2, default=_text) + "\n")
        for t in kept_tables:
            columns = written_columns(plan, t["table"])
            text = io.StringIO()
            writer = csv.writer(text, lineterminator="\n")
            writer.writerow(columns)
            for row in result.rows(t["table"]):
                writer.writerow(["" if row.get(c) is None else _text(row.get(c)) for c in columns])
            archive.writestr(f"{t['table']}.csv", text.getvalue())

    name = f"request-{original['request_id']}-{_safe(params.get('target', {}).get('database'))}-load.zip"
    replay_params = {**params, "regenerated": {"tables": tables, "reference_sets": references}}
    gr.record_file(
        request_id=original["request_id"], file_name=name, format=FORMAT, subject=SUBJECT,
        params=replay_params, snapshots=original["snapshots"], sdm_version=version,
        sdm_build=build, fingerprint=fingerprint, size=len(buffer.getvalue()),
        written_by=who, replay_of=original["id"],
    )
    return buffer.getvalue(), name, evidence


def explain(tables: list[dict[str, Any]], references: list[dict[str, Any]],
            original: dict[str, Any], build: str) -> str:
    """Why the rows came out as they did, in a sentence."""
    if all(t["matched"] for t in tables):
        return "Every table came out as it was written."
    reasons = []
    changed = [r["table"] for r in references if not r["matched"]]
    if changed:
        reasons.append("the source no longer holds the same "
                       + ", ".join(changed) + " the load pointed at")
    if original.get("sdm_build") and original["sdm_build"] != build:
        reasons.append("SDM has changed since the load")
    differ = ", ".join(t["table"] for t in tables if not t["matched"])
    return (f"{differ} differ" + (": " + "; ".join(reasons) if reasons else "") + ".")


def _safe(name: Any) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name or "target"))

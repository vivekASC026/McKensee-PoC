"""HTTP API for the web interface.

Endpoints return the shape a screen needs rather than the shape an artifact
happens to have. The schema endpoint, for instance, folds the physical model,
the profile, the mapping, and the proposal into one row per column, because
that is the unit a person looks at.
"""

from __future__ import annotations

import contextvars
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field

from sdg import __version__, packs
from sdg.analyzer import db as _db
from sdg.analyzer import pattern_scope
from sdg.api.service import Service, ServiceError
from sdg.config import database_generation, protected_databases
from sdg.discovery.errors import plain

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"

# Python takes content types from the Windows registry, and many managed
# machines map .js to text/plain. A browser will not run a module script
# served as text/plain: the interface stays blank with no error on screen.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# The schema lists every route, so it is under /api with the rest: only the
# screens' token reads it.
app = FastAPI(title="SDM", version=__version__, docs_url="/api/docs",
              openapi_url="/api/openapi.json")
service = Service()


@app.middleware("http")
async def _only_its_own_screens(request, call_next):
    """Every request names this machine, and every API call carries the token
    the screens were given (sdg/api/guard.py)."""
    from sdg.api import guard

    refused = guard.refusal(request.url.path, request.headers)
    return refused if refused is not None else await call_next(request)


@app.exception_handler(ServiceError)
async def _service_error(_request, exc: ServiceError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": str(exc)})


# The routes of pattern analysis, where the business pack offers it (phase 6b:
# every pack); the analyzer's routers carry the same dependency.
_ANALYSIS = Depends(packs.offered("pattern_analysis", "Pattern analysis"))
# Files and plans written from a pattern: the pack's own module, as its
# records' profiles must be read back into what a file writes (health).
_GENERATION = Depends(packs.offered("pattern_generation", "Generating from patterns"))


@app.exception_handler(_db.PatternArchived)
async def _pattern_archived(_request, exc):
    """A write to a pattern found under an earlier build: read-only."""
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": str(exc), "refusal": "archived"})


@app.exception_handler(packs.PackRebuilt)
async def _pack_rebuilt(_request, exc: packs.PackRebuilt):
    """Analysis while the running pack's folder holds a newer build."""
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": str(exc), "refusal": "rebuilt"})


@app.exception_handler(packs.NotOffered)
async def _not_offered(_request, exc: packs.NotOffered):
    """A health format asked of another business's pack."""
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unexpected(_request, exc: Exception):
    """Anything nothing caught reaches the screen as a sentence, never as
    "Internal Server Error"."""
    from fastapi.responses import JSONResponse

    # The screen gets the sentence; the traceback stays in the server's log.
    log.error("Unexpected error on %s %s", _request.method, _request.url.path,
              exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": plain(exc)})


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # An edit that cannot be read as the type it belongs to. It is the
        # person's typing, not a fault, so it gets the same treatment as any
        # other refused edit rather than a stack trace. Logged with its
        # traceback all the same: a ValueError from a fault reads the same way.
        log.warning("%s refused: %s", getattr(fn, "__qualname__", fn), exc, exc_info=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # surfaced to the user rather than swallowed
        log.exception("Unexpected error in %s", getattr(fn, "__qualname__", fn))
        raise HTTPException(status_code=500, detail=f"{plain(exc)}") from exc


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@app.get("/api/pack")
def business_pack() -> dict[str, Any]:
    """The installation's business pack: what its records are called and play,
    the formats it offers and the words the screens use (SDG_PACK in .env)."""
    pack = packs.active()
    return {
        "id": pack.id,
        "name": pack.name,
        "entities": [{"key": key, "role": entity["role"], "parent": entity.get("parent"),
                      "terms": pack.glossary.get(key) or {}}
                     for key, entity in pack.entities.items()],
        "formats": pack.formats,
        "modules": pack.data.get("modules") or [],
        "glossary": {k: v for k, v in pack.glossary.items() if not k.startswith("_")},
        # The filter a child analysis is narrowed to one type of record by
        # (health: claim_type), where the pack has one.
        "type_filter": pack.features.get("store", {}).get("type_filter"),
        # Which of a pattern's measures its card states (age, tenure, amount).
        "facts": {k: v for k, v in (pack.features.get("screen_facts") or {}).items()
                  if not k.startswith("_")},
    }


@app.get("/api/pack/column-names")
def business_column_names() -> dict[str, Any]:
    """The business names of the active pack's columns."""
    return packs.active().column_names()


@app.get("/api/state")
def state() -> dict[str, Any]:
    """What exists so far, and the headline number for each step."""
    settings = service.settings
    steps: list[dict[str, Any]] = [
        {
            "id": "connect",
            "label": "Connect",
            "done": service.exists("connections"),
            # The database being worked on: the one chosen on Connect.
            "detail": service.working_database()["database"],
        }
    ]

    # Reading the structure and measuring it are one step to look at: the
    # same columns, what they are and what they hold. They are still two
    # stages of a read, and Connect shows each, so both are said here.
    model = service.model
    report = service.report
    found = [f"{len(model.tables)} tables"] if model else []
    if report:
        found.append(f"{len(report.columns)} columns")
    steps.append({
        "id": "discover",
        "label": "Discover and Profile",
        "done": model is not None and report is not None,
        "detail": " · ".join(found),
        "value": len(model.tables) if model else None,
        "read": model is not None,
        "measured": report is not None,
    })

    # Matching columns to elements and deciding what the tool would not are
    # one screen: the decisions waiting first, then every column's match.
    mappings = service.mappings
    stats = mappings.statistics() if mappings else None
    queued = None
    if model is not None and mappings is not None and service.exists("proposals"):
        queued = service.queued_count()
    said = [f"{stats['auto_map_rate']:.0%} auto-mapped"] if stats else []
    if queued is not None:
        said.append(f"{queued} to decide")
    steps.append({
        "id": "map",
        "label": "Map and Review",
        "done": service.exists("proposals"),
        "detail": " · ".join(said),
        "value": round(stats["auto_map_rate"] * 100) if stats else None,
        "mapped": mappings is not None,
        "proposed": service.exists("proposals"),
    })

    steps.append({
        "id": "patterns",
        "label": "Patterns",
        "done": mappings is not None,
        "detail": "ready" if mappings is not None else "",
        "value": None,
    })

    plan = service.plan
    total = (
        sum(t.effective_count or 0 for t in plan.tables if t.mode == "generate")
        if plan else None
    )

    # One step with two tabs: a database load, or files. The plan belongs to
    # the first -- the cascade of tables and how many rows each gets.
    manifest = service.manifest
    steps.append({
        "id": "run",
        "label": "Generation",
        "done": manifest is not None,
        "detail": (
            f"{manifest['run']['rows_written']:,} written" if manifest
            else (f"{total:,} rows planned" if total else "")
        ),
        "value": manifest["run"]["rows_written"] if manifest else total,
    })

    working = service.working_database()
    return {
        "version": __version__,
        # The database every step reads and writes: the one chosen on Connect.
        "database": working["database"],
        # Patterns always come from the pattern store, whatever is worked on.
        "landscape": settings.denorm_db,
        "referenceDatabase": settings.ref_db,
        "server": working["server"],
        "steps": steps,
        "settings": service.read_settings(),
        # Off for an installation that is to make files only.
        "databaseGeneration": database_generation(),
        # Production, which the screens never read: Connect says so rather
        # than offering a read that is refused.
        "workingProtected": str(working["database"] or "").strip().upper()
        in protected_databases(),
    }


@app.post("/api/connect")
def connect() -> dict[str, Any]:
    return _guard(service.connect)


@app.post("/api/discover")
def discover() -> dict[str, Any]:
    return _guard(service.discover)


@app.post("/api/profile")
def profile() -> dict[str, Any]:
    return _guard(service.profile)


@app.post("/api/map")
def map_columns() -> dict[str, Any]:
    return _guard(service.map)


@app.post("/api/review")
def review() -> dict[str, Any]:
    return _guard(service.review)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _column_payload(table, column, report, mappings, proposal, decision=None) -> dict[str, Any]:
    column_profile = report.column(table.name, column.name) if report else None
    mapping = mappings.for_column(table.name, column.name) if mappings else None

    # What the tool worked out on its own. It is kept whole, because the editor
    # offers to put it back and the panel says what it was.
    proposed_element = mapping.element if mapping else None
    proposed_generator = proposal.generator if proposal else None
    proposed_params = proposal.params if proposal else {}

    element = proposed_element
    accepted = mapping.accepted if mapping else False
    generator = proposed_generator
    params = proposed_params
    provenance = proposal.provenance if proposal else None

    # A person's decision beats the proposal, which is the precedence the
    # resolver applies when it builds the plan. Reporting the proposal here
    # would describe a rule the run is not going to use.
    if decision and decision.get("approved"):
        if "element" in decision:
            element = decision["element"]
            accepted = element is not None
        if decision.get("generator"):
            generator = decision["generator"]
            params = decision.get("params") or {}
        provenance = "user"

    return {
        "table": table.name,
        "name": column.name,
        "type": column.type_description(),
        "nullable": column.nullable,
        "bucket": column.bucket,
        "bucketReason": column.bucket_reason,
        "isPrimaryKey": column.is_primary_key,
        "isIdentity": column.is_identity,
        "isComputed": column.is_computed,
        "profile": _profile_payload(column_profile),
        "element": element,
        "decision": decision,
        "mappingConfidence": mapping.confidence if mapping else None,
        "mappingAccepted": accepted,
        "mappingReason": mapping.reason if mapping else "",
        "mappingEvidence": mapping.evidence if mapping else [],
        "alternatives": [a.element for a in mapping.alternatives] if mapping else [],
        "generator": generator,
        "params": params,
        "proposedElement": proposed_element,
        "proposedGenerator": proposed_generator,
        "proposedParams": proposed_params,
        "provenance": provenance,
        "confidence": proposal.confidence if proposal else None,
        "coverage": proposal.coverage if proposal else None,
        "evidence": proposal.evidence if proposal else [],
        "note": proposal.note if proposal else "",
    }


def _decisions_by_column() -> dict[tuple[str, str], dict[str, Any]]:
    """Everything a person has said, read once rather than once per column."""
    return {
        (entry.get("table"), entry.get("column")): entry
        for entry in service.read_decisions().get("columns", [])
    }


def _profile_payload(column_profile) -> dict[str, Any] | None:
    if column_profile is None:
        return None
    return {
        "rowCount": column_profile.row_count,
        "nullRate": column_profile.null_rate,
        "distinctCount": column_profile.distinct_count,
        "distinctRatio": column_profile.distinct_ratio,
        "cardinality": column_profile.cardinality,
        "minimum": _plain(column_profile.minimum),
        "maximum": _plain(column_profile.maximum),
        "mean": column_profile.mean,
        "stddev": column_profile.stddev,
        "minLength": column_profile.min_length,
        "maxLength": column_profile.max_length,
        "format": column_profile.fitted_format,
        "checkDigit": column_profile.check_digit,
        "sequence": column_profile.sequence,
        "distribution": column_profile.distribution,
        "topValues": [
            {"value": _plain(t.value), "count": t.count, "share": t.share}
            for t in (column_profile.top_values or [])
        ],
    }


def _plain(value):
    return value if value is None or isinstance(value, (int, float, str, bool)) else str(value)


@app.get("/api/schema")
def schema() -> dict[str, Any]:
    model = _guard(service.require_model)
    report = service.report
    mappings = service.mappings
    by_column = {p.qualified: p for p in service.proposals()} if mappings else {}
    decisions = _decisions_by_column()

    tables = []
    for table in model.tables:
        tables.append({
            "name": table.name,
            "schema": table.schema_name,
            "table": table.table_name,
            "rowCount": table.row_count,
            "primaryKey": table.primary_key,
            "triggers": table.triggers,
            "foreignKeys": [
                {
                    "kind": fk.kind,
                    "name": fk.name,
                    "columns": fk.columns,
                    "parentTable": fk.parent_table,
                    "parentColumns": fk.parent_columns,
                    "coverage": fk.coverage,
                    "evidence": fk.evidence,
                }
                for fk in table.foreign_keys
            ],
            "columns": [
                _column_payload(
                    table, column, report, mappings,
                    by_column.get(f"{table.name}.{column.name}"),
                    decisions.get((table.name, column.name)),
                )
                for column in table.columns
            ],
        })

    return {
        "tables": tables,
        "dependencies": [d.model_dump() for d in (report.dependencies if report else [])],
        "orderings": [o.model_dump() for o in (report.orderings if report else [])],
        "ratios": [r.model_dump() for r in (report.ratios if report else [])],
    }


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


def _queue_items():
    from sdg.review.queue import build_queue

    return build_queue(
        service.require_model(), service.require_mappings(), service.proposals()
    )


@app.get("/api/queue")
def queue() -> dict[str, Any]:
    items = _guard(_queue_items)
    decisions = {
        f"{d.get('table')}.{d.get('column')}": d
        for d in service.read_decisions().get("columns", [])
    }
    return {
        "items": [
            {
                **item.as_dict(),
                "qualified": item.qualified,
                "mappingEvidence": item.mapping_evidence,
                "proposalEvidence": item.proposal_evidence,
                "decision": decisions.get(item.qualified),
            }
            for item in items
        ],
        "decided": sum(
            1 for item in items if decisions.get(item.qualified, {}).get("approved")
        ),
    }


class Decision(BaseModel):
    table: str
    column: str
    approved: bool | None = None
    element: str | None = None
    generator: str | None = None
    params: dict[str, Any] | None = None
    # Drop the override and go back to what the tool worked out.
    reset: bool | None = None


@app.post("/api/decisions")
def record_decision(decision: Decision) -> dict[str, Any]:
    # element is allowed to be null: "this column maps to nothing" is an answer.
    payload = decision.model_dump(exclude_unset=True)
    return _guard(service.record_decision, payload)


@app.get("/api/ratio-shapes")
def ratio_shapes() -> dict[str, Any]:
    """The shapes a children-per-parent rule can take, for building a form."""
    from sdg.plan.editing import ratio_shapes as shapes

    return {"shapes": shapes()}


@app.get("/api/generators")
def generators() -> dict[str, Any]:
    """Every rule a column can be given, and what each one needs."""
    from sdg.plan.editing import STRUCTURAL_WARNING, generator_catalog

    return {
        "generators": generator_catalog(),
        "structuralWarning": STRUCTURAL_WARNING,
    }


# ---------------------------------------------------------------------------
# Where generated rows go
# ---------------------------------------------------------------------------


@app.get("/api/databases")
def list_databases() -> dict[str, Any]:
    """Every database on this server, and which one each role points at."""
    return {
        "databases": _guard(service.databases),
        "source": service.settings.source_db,
        "store": service.settings.denorm_db,
        "target": service.registry.database_for("target"),
        "server": service.settings.mssql.server,
    }


class TargetRequest(BaseModel):
    database: str


@app.get("/api/databases/onboarded")
def onboarded_databases() -> dict[str, Any]:
    """The databases onboarded here, and whether rows written into them remain."""
    return {"databases": _guard(service.onboarded)}


class OffboardRequest(BaseModel):
    key: str


@app.post("/api/databases/offboard")
def offboard_database(req: OffboardRequest) -> dict[str, Any]:
    """Forget an onboarded database. Patterns and the analysis store are untouched."""
    return _guard(service.offboard, req.key)


@app.post("/api/target")
def choose_target(req: TargetRequest) -> dict[str, Any]:
    """Point generation at a database on the configured server."""
    return _guard(service.set_target, req.database)


class TargetConnection(BaseModel):
    """A target somewhere other than the configured server.

    `trusted` is Windows authentication, which is both the common case on a
    domain-joined machine and the one where no password is stored anywhere.
    """

    database: str
    server: str | None = None
    port: int | None = None
    trusted: bool = False
    user: str | None = None
    password: str | None = None
    driver: str | None = None
    trustCert: bool = True


@app.post("/api/target/test")
def test_target(req: TargetConnection) -> dict[str, Any]:
    """Reach a connection without saving it."""
    return _guard(service.test_target_connection, req.model_dump())


@app.post("/api/target/connection")
def save_target(req: TargetConnection) -> dict[str, Any]:
    """Check a connection, keep it, and generate into it from now on."""
    return _guard(service.save_target_connection, req.model_dump())


@app.delete("/api/target/connection")
def forget_target() -> dict[str, Any]:
    """Back to the database named in `.env`, on the configured server."""
    return _guard(service.clear_target_connection)


@app.get("/api/target/connection")
def read_target() -> dict[str, Any]:
    """What is configured, with the password withheld."""
    stored = _guard(service.target_connection)
    if not stored:
        return {"configured": False}
    return {
        "configured": True,
        **{k: v for k, v in stored.items() if k != "password"},
        "hasPassword": bool(stored.get("password")),
    }


# ---------------------------------------------------------------------------
# Settings and plan
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    rootCounts: dict[str, int] | None = None
    # Which kind of thing each table holds, named by the store table that holds
    # the same kind. Stated the way a root count is: the tool can read the
    # shape of a database but not what its rows mean.
    subjects: dict[str, str] | None = None
    # Where generated rows go. Named here rather than only in .env so a target
    # can be chosen without restarting the process.
    targetDb: str | None = None
    modes: dict[str, str] | None = None
    drivingParents: dict[str, str] | None = None
    ratios: dict[str, dict[str, Any] | None] | None = None
    seed: str | None = None
    planName: str | None = None


@app.put("/api/settings")
def put_settings(values: Settings) -> dict[str, Any]:
    return _guard(service.write_settings, values.model_dump(exclude_none=True))


@app.post("/api/plan")
def build() -> dict[str, Any]:
    plan, graph, extra = _guard(service.build)
    return _plan_payload(plan, graph, extra)


@app.get("/api/plan")
def get_plan() -> dict[str, Any]:
    plan = service.plan
    if plan is None:
        return {"built": False}
    # Stale: built before a rule, a pattern or a count changed. The screen
    # rebuilds it rather than show a load the decisions no longer describe.
    return {"built": True, "stale": service.plan_stale(), **_plan_payload(plan, None, {})}


def _plan_payload(plan, graph, extra: dict[str, Any]) -> dict[str, Any]:
    provenance: dict[str, int] = {}
    for table in plan.tables:
        for field in table.fields:
            provenance[field.provenance] = provenance.get(field.provenance, 0) + 1

    nodes = []
    generated_names = {t.name for t in plan.tables if t.mode == "generate"}

    for table in plan.tables:
        node = graph.nodes.get(table.name) if graph else None
        ratio = table.ratio
        # A reloaded plan has no graph behind it, but the ratio it carries is
        # enough to recover the mean, so the cascade reads the same either way.
        ratio_mean = node.ratio_mean if node else None
        if ratio_mean is None and ratio is not None:
            ratio_mean = round(ratio.mean_children(), 4)
        nodes.append({
            "name": table.name,
            "mode": table.mode,
            "level": table.level,
            "drivingParent": table.driving_parent,
            # Every parent this table could be driven by, which is what the
            # builder validates an override against. Only a parent that is
            # itself generated counts: the cascade is over generated tables,
            # and a reference set sits outside it. Computed from the plan
            # rather than the graph, because a reloaded plan has no graph and
            # the two must not disagree about what is on offer.
            "parents": sorted(
                {
                    r.parent_table
                    for r in (table.relationships or [])
                    if r.parent_table in generated_names and r.parent_table != table.name
                }
            ),
            "count": table.count,
            "effectiveCount": table.effective_count,
            "ratioMean": ratio_mean,
            "ratio": ratio.model_dump(exclude_none=True) if ratio else None,
            "ratioOverride": bool(ratio and ratio.provenance == "user"),
            "fields": [
                {
                    "name": f.name,
                    "bucket": f.bucket,
                    "element": f.element,
                    "generator": f.generator,
                    "params": f.params,
                    "provenance": f.provenance,
                    "confidence": f.confidence,
                    "coverage": f.coverage,
                    "userOverride": f.user_override,
                    "nullRate": f.null_rate,
                    "dataType": f.data_type,
                    "evidence": [e.model_dump(exclude_none=True) for e in (f.evidence or [])],
                    "overrode": [o.model_dump(exclude_none=True) for o in (f.overrode or [])],
                }
                for f in table.fields
            ],
        })

    return {
        "built": True,
        "meta": plan.meta.model_dump(exclude_none=True),
        "generationOrder": plan.generation_order,
        "tables": nodes,
        "warnings": [w.model_dump(exclude_none=True) for w in plan.warnings],
        "blocking": sum(1 for w in plan.warnings if w.severity == "block"),
        "provenance": dict(sorted(provenance.items())),
        "totalRows": sum(
            t.effective_count or 0 for t in plan.tables if t.mode == "generate"
        ),
        **extra,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@app.post("/api/dryrun")
def dryrun(rows: int = 6) -> dict[str, Any]:
    return _guard(service.dry_run, rows)


@app.post("/api/generate")
def generate() -> dict[str, Any]:
    """A load into the target, kept as a generation request for audit."""
    from sdg.analyzer.services import generation_requests as gr

    # Where the record goes is reached before any row is written: a load the
    # audit cannot record is not made.
    try:
        gr.ensure_tables()
    except Exception as e:
        raise HTTPException(status_code=503, detail=(
            f"The load's record cannot be kept, so nothing was loaded: {e}"))
    return _guard(service.generate, _record_load)


def _record_load(plan, ran, result, report, references, partial: bool,
                 loaded_by: Service | None = None) -> dict[str, Any]:
    """A database load kept as a generation request, for audit."""
    from sdg.api import load_requests

    registry = (loaded_by or service).registry
    return load_requests.record(
        plan=plan, ran=ran, result=result, report=report, references=references,
        target={"server": registry.settings_for("target").server,
                "database": registry.database_for("target")},
        partial=partial, who=_who(), version=__version__, build=_sdm_build(),
    )


@app.post("/api/validate")
def validate() -> dict[str, Any]:
    return _guard(service.validate)


@app.post("/api/cleanup")
def cleanup() -> dict[str, Any]:
    return _guard(service.cleanup)


@app.get("/api/run")
def run() -> dict[str, Any]:
    manifest = service.manifest
    return {"hasRun": manifest is not None, "manifest": manifest}


@app.get("/api/catalog")
def catalog() -> dict[str, Any]:
    loaded = service.catalog
    mappings = service.mappings
    used: dict[str, list[str]] = {}
    if mappings:
        for mapping in mappings.mappings:
            if mapping.element:
                used.setdefault(mapping.element, []).append(mapping.qualified)
    return {
        "organization": loaded.organization,
        "version": loaded.version,
        "summary": loaded.summary(),
        "elements": [
            {
                "name": element.name,
                "description": element.description,
                "domain": element.domain,
                "rule": element.rule,
                "generator": element.generator,
                "format": element.format,
                "pii": element.pii,
                "consistencyScope": element.consistency_scope,
                "aliases": element.aliases,
                "checkDigit": element.check_digit,
                "reuseDomain": (
                    element.reuse_domain.model_dump(exclude_none=True, by_alias=True)
                    if element.reuse_domain else None
                ),
                "usedBy": used.get(element.name, []),
                "valueCount": len(element.params.get("values") or []),
            }
            for element in loaded.elements
        ],
    }


# ---------------------------------------------------------------------------
# The pattern analyzer
# ---------------------------------------------------------------------------

# The analyzer arrived as its own FastAPI process on another port, with its own
# CORS rules and its own copy of the interface. It is mounted here instead, so
# there is one process, one origin, and one place a browser has to be pointed.
# Its routes keep the paths they had, under /api.
from sdg.api.routers import (  # noqa: E402  (mounted after the native routes)
    accelerator, baseline, claims, discovery, entities, files, hierarchy, layouts,
    patterns, rules, story,
)

for _router in (
    hierarchy.router, patterns.router, claims.router, story.router,
    baseline.router, discovery.router, layouts.router, rules.router,
    files.router, entities.router, accelerator.router,
):
    app.include_router(_router, prefix="/api")

# The analyzer's own generation routes are gone. It had a second generator --
# its own synthetic engine and its own validator -- and two engines meant two
# definitions of a valid row. The plan engine generates; /api/export writes the
# result in whichever shape another system reads, including the analyzer's EDI.


class ToPlanRequest(BaseModel):
    """A pattern the analyzer produced, on its way to becoming generation rules."""

    pattern: dict[str, Any]
    # The kind of record the pattern is of, as the plan's kind anchors name it
    # (the glossary's word: health's member or claim, a bank's customer or account).
    kind: str = packs.active().term(packs.active().primary, "one")
    as_of: str | None = None
    apply: bool = False
    # Whether the pattern describes the whole run or its own share of the
    # population. A share needs the pattern's support, which the analyzer
    # measured; this tool will not invent one.
    as_cohort: bool = False


@app.post("/api/patterns/to-plan", dependencies=[_ANALYSIS, _GENERATION])
def pattern_to_plan(request: ToPlanRequest) -> dict[str, Any]:
    """Resolve a discovered pattern onto the columns it governs.

    This is the join between the two halves: the analyzer says what a cohort
    looks like, and this says which columns would have to carry which rules for
    the generator to produce one. Nothing is written unless `apply` is set, and
    what is written is an ordinary review decision, so `sdg plan` picks it up
    the same way it picks up an edit made by hand.
    """
    from sdg.analyzer.bridge import to_pattern
    from sdg.plan.patterns import apply_to_decisions, resolve_pattern

    pattern = request.pattern
    # Where the store holds no addresses, as a customer's denorm does not, a
    # member's ZIP, city and state still go in together, from the store's own
    # address list.
    primary = packs.active().term(packs.active().primary, "one")
    if request.kind == primary and not pattern.get("geography") and _addresses():
        share = 1 / len(_addresses())
        pattern = {**pattern, "geography": [{**a, "share": share} for a in _addresses()]}
    analyzer_pattern = to_pattern(pattern, kind=request.kind, as_of=request.as_of)
    # A pattern reaches the database through the plan's columns. With no plan
    # yet, one is built here rather than sending the person off to build it
    # and come back: it is the step they would take anyway.
    # A preview (apply false) never builds: it reads the plan there is, even
    # one out of date, and with none it says the columns are worked out when
    # the pattern is loaded. Applying builds a missing or out-of-date plan.
    plan = service.plan
    if not request.apply and plan is None:
        return {"pattern": analyzer_pattern.model_dump(mode="json"), "resolution": None,
                "applied": [], "plan": "none"}
    if plan is None or (request.apply and service.plan_stale()):
        plan = _guard(service.build)[0]
    resolution = _guard(
        resolve_pattern,
        analyzer_pattern,
        catalog=service.catalog,
        mappings=service.require_mappings(),
        plan=plan,
        model=service.model,
    )

    written: list[str] = []
    if request.apply and resolution.targets:
        document, written = _guard(
            apply_to_decisions, [resolution], service.read_decisions(),
            as_cohort=request.as_cohort,
        )
        service.write_decisions(document)

    return {
        "pattern": analyzer_pattern.model_dump(mode="json"),
        "resolution": resolution.model_dump(mode="json"),
        "applied": written,
    }


@app.get("/api/edi/snip")
def snip_rules() -> dict[str, Any]:
    """The SNIP rule files in effect, the trading partners, and where a
    customer's own files go -- so the screen can say what is being checked."""
    from sdg.writers.snip import engine

    return {"rules": engine.rule_files(), "partners": engine.partners(),
            "customerDir": str(engine.customer_dir())}


class SnipCheck(BaseModel):
    """The file this screen would write, and a trading partner's rules to add."""

    export: dict[str, Any]
    partner: str | None = None


@app.post("/api/edi/snip/check")
def snip_check(request: SnipCheck) -> dict[str, Any]:
    """SNIP-style checks of exactly the file the export would write."""
    from sdg.writers.snip import validate

    export_request = ExportRequest(**request.export)
    snip = export_request.format.startswith("edi")
    if _modeled(export_request.format):
        # A file modeled on a sample is checked as its format says it can be.
        from sdg.writers import samples

        modeled = _modeled_config(export_request.format, export_request.sample_config)
        fmt = samples.FORMATS.get(modeled["format"])
        snip = fmt is not None and fmt.VALIDATION == "snip"
    if not snip:
        raise HTTPException(status_code=422, detail="SNIP checks apply to EDI formats only.")
    stream, _media, _name, _kept = _request_stream(export_request)
    try:
        text = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in stream
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return validate(text, partner=request.partner)


@app.post("/api/samples/check")
def sample_check(request: SnipCheck) -> dict[str, Any]:
    """The file a sample config writes, checked as its format checks (FHIR: models,
    references, value sets). X12 is checked by /api/edi/snip/check."""
    from sdg.writers import samples

    export_request = ExportRequest(**request.export)
    if not _modeled(export_request.format):
        raise HTTPException(status_code=404, detail="Choose a sample configuration to check.")
    modeled = _modeled_config(export_request.format, export_request.sample_config)
    fmt = samples.FORMATS.get(modeled["format"])
    if fmt is None or not hasattr(fmt, "check"):
        raise HTTPException(status_code=422,
                            detail=f"{modeled['format']} files are not checked here.")
    export_request.packaged = False
    stream, _media, _name, _kept = _request_stream(export_request)
    try:
        text = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in stream
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    from sdg.writers.samples import profile

    return fmt.check(text, profile.for_kind(modeled["format"], modeled["kind"]) or {})


@app.post("/api/edi/snip/check-file")
async def snip_check_file(file: UploadFile = File(...),
                          partner: str | None = Form(None)) -> dict[str, Any]:
    """SNIP-style checks of an X12 file from anywhere -- another system's too."""
    from sdg.writers.snip import validate

    # One byte past the limit is enough to know the file is too large.
    raw = await file.read(MAX_SAMPLE_BYTES + 1)
    _sample_fits(raw)
    if b"\x00" in raw:
        # Latin-1 decodes any bytes, so a binary file would be checked as text.
        raise HTTPException(status_code=422, detail=(
            "The file is not a text file. Choose an X12 file to check."))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    report = validate(text, partner=partner or None)
    report["file"] = file.filename
    return report


@app.get("/api/edi/formats")
def edi_formats() -> dict[str, Any]:
    """Every transaction set this installation can write.

    Described in files rather than compiled in, so a format somebody adds
    appears here without anything being rebuilt.
    """
    from sdg.writers.edi_formats import formats

    return {"formats": [f.summary() for f in formats()]}


class SampleRead(BaseModel):
    """A sample file and the config whose places are proposed for it.

    The master is a config's JSON text (a masking config or a generation
    config) or a saved generation config by id, whose sample is used when no
    other is given. The format is read off the sample."""

    sample: str | None = None
    sampleName: str | None = None
    master: str | None = None
    masterName: str | None = None
    configId: int | None = None
    # A standard file (sdg/writers/samples/defaults): its sample, unless
    # another was chosen, and its master.
    defaultId: str | None = None
    # No master named: the standard configuration of the sample's format.
    auto: bool = False


class SampleText(BaseModel):
    sample: str


# The largest sample read, by every way a sample comes in (sdg.api.limits).
from sdg.api.limits import MAX_SAMPLE_BYTES, MAX_SAMPLE_MB  # noqa: E402,F401
from sdg.api.limits import sample_fits as _sample_fits  # noqa: E402


@app.post("/api/samples/detect")
def sample_detect(request: SampleText) -> dict[str, Any]:
    """What a sample is: a format a file is modeled on (X12, FHIR, HL7 v2,
    HL7 v3), with its kind and standard configuration, or a flat file."""
    from sdg.writers import samples

    _sample_fits(request.sample)
    try:
        fmt = samples.detect(request.sample)
    except samples.SampleError:
        return {"format": "flat"}
    try:
        sample = fmt.read_sample(request.sample)
    except samples.SampleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"format": fmt.FORMAT, "label": fmt.LABEL, "kind": sample.kind,
            "version": sample.version, "subject": fmt.subject(sample.kind),
            "defaultId": _standard_for(fmt, sample)}


def _standard_for(fmt: Any, sample: Any) -> str | None:
    """The standard file a sample is read against when no master is named:
    the one of its format whose sample is of its kind (or nearest in name),
    else the format's first."""
    import os

    from sdg.writers import samples
    from sdg.writers.samples import standard

    best, nearest = None, -1
    for entry in standard.listing():
        if entry["format"] != fmt.FORMAT:
            continue
        try:
            kind = fmt.read_sample(standard.files(entry["id"])["sample"]).kind or ""
        except samples.SampleError:
            continue
        if kind == sample.kind:
            return entry["id"]
        near = len(os.path.commonprefix([kind, sample.kind or ""]))
        if near > nearest:
            best, nearest = entry["id"], near
    return best


@app.get("/api/samples/defaults")
def sample_defaults() -> dict[str, Any]:
    """The standard sample and master config of each format, as the EDI tab has
    its transaction sets."""
    from sdg.writers.samples import standard

    return {"defaults": [{"id": d["id"], "format": d["format"], "name": d["name"],
                          "sampleName": d["sample"]} for d in standard.listing()]}


def _standard(default_id: str) -> dict[str, str]:
    from sdg.writers.samples import standard

    found = standard.files(default_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"No standard file {default_id}.")
    return found


def _standard_suggestions(rows: list[dict[str, Any]]) -> None:
    """A standard master is a masking config: what masking leaves alone (sex,
    state, diagnosis, charges) it does not name. Written for a pattern, those
    are the pattern's values, so SDM's own suggestion stands where the
    standard master is silent."""
    for row in rows:
        if not row.get("algorithm") and row.get("suggested") and not row.get("kept"):
            row["algorithm"] = row["suggested"]
            row["reason"] = "the standard layout generates it"


def _standard_config(default_id: str) -> dict[str, Any]:
    """A standard file as a generation config, exactly as its tab proposes it
    before anything is changed: what a pattern's Generate file writes FHIR,
    HL7 v2 or HL7 v3 through, with no config saved first."""
    from sdg.writers import samples
    from sdg.writers.samples import engine as sample_engine

    found = _standard(default_id)
    try:
        fmt = samples.detect(found["sample"])
        sample = fmt.read_sample(found["sample"])
        entries = fmt.read_config(found["master"])
        rows = fmt.propose(sample, entries, samples.algorithm_rules())
        _standard_suggestions(rows)
        vary = []
        if hasattr(fmt, "structure"):
            vary = sample_engine.write_vary(
                [{"at": v.at, "present": v.present, "min": v.low, "max": v.high,
                  **({"mix": [[c, s] for c, s in v.mix]} if v.mix else {})}
                 for v in sample_engine.read_vary(found["master"])])
        config = fmt.write_config(
            rows, transaction=sample.kind, version=sample.version,
            based_on=found["master_name"], sample_name=found["sample_name"], vary=vary or None)
    except samples.SampleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"format": fmt.FORMAT, "kind": sample.kind, "name": default_id,
            "sample_name": found["sample_name"], "sample_text": found["sample"],
            "config": config}


def _unsaved_key(body: dict[str, Any] | None) -> str:
    """An unsaved configuration's name in a run: the digest of what was accepted."""
    from sdg.analyzer.services import generation_requests as gr

    if not body or not body.get("rows"):
        raise HTTPException(status_code=422,
                            detail="Upload a sample and accept its rules first.")
    sample = body.get("sample")
    _sample_fits(sample if isinstance(sample, str) else None)
    return gr.digest(body)[:16]


# A run of an unsaved configuration keeps the configuration and facts about
# its sample, not the sample. A sample uploaded is not SDM's to keep, so its
# run can't be written again; one read from a standard file or a saved
# configuration is still in SDM, so its run is written again from there --
# while it is the same sample.
UNSAVED_RERUN = ("This run can't be rerun: it was made from an unsaved sample that SDM "
                 "doesn't keep. Upload the sample again or save its configuration.")
UNSAVED_CHANGED = "This run can't be rerun: the sample it was made from has changed."
UNSAVED_GONE = "This run can't be rerun: the sample it was made from is no longer in SDM."


def _unsaved_source(body: dict[str, Any]) -> dict[str, Any] | None:
    """Where an unsaved configuration's sample came from, when no sample was
    uploaded: the saved configuration or the standard file it was read from
    (the same order `_gen_config` takes them in). None for an uploaded one."""
    if body.get("sample"):
        return None
    if body.get("fromConfigId") is not None:
        return {"config": int(body["fromConfigId"])}
    if body.get("fromDefaultId") is not None:
        return {"standard": str(body["fromDefaultId"])}
    return None


def _sample_facts(text: str, built: dict[str, Any]) -> dict[str, Any]:
    """What a run keeps of a sample in place of its text."""
    import hashlib

    from sdg.writers import samples

    data = text.encode("utf-8")
    facts: dict[str, Any] = {"digest": hashlib.sha256(data).hexdigest(), "size": len(data),
                             "format": built.get("format"), "kind": built.get("kind"),
                             "name": built.get("sample_name")}
    try:
        fmt = samples.module(built["format"])
        facts["counts"] = fmt.facts(fmt.read_sample(text))[0]
    except Exception:  # an optional fact: the run is kept without it
        pass
    return facts


def _unsaved_kept(body: dict[str, Any]) -> dict[str, Any]:
    """An unsaved configuration as its run keeps it: the configuration built
    from what was accepted, with the sample's digest, size and counts in place
    of its text, and where it came from when it was not uploaded. A body kept
    before this (with its sample) stays as it was."""
    if "sample_text" not in body:
        return body
    facts = _sample_facts(body.get("sample_text") or "", body)
    facts["source"] = body.get("sample_source")
    kept = {k: v for k, v in body.items() if k not in ("sample_text", "sample_source")}
    kept["sample"] = facts
    return kept


def _source_sample(source: dict[str, Any]) -> str | None:
    """The text of the sample a standard file or a saved configuration holds
    now; None when it is no longer there."""
    from sdg.analyzer.services import sample_gen_configs
    from sdg.writers.samples import standard

    if "standard" in source:
        found = standard.files(str(source["standard"]))
        return found["sample"] if found else None
    if "config" in source:
        saved = sample_gen_configs.get_config(int(source["config"]))
        return saved.get("sample_text") if saved else None
    return None


def _verified_sample(kept: dict[str, Any]) -> tuple[str | None, str | None]:
    """The sample a run of an unsaved configuration is written again from,
    and why it can't be when it can't: the one it kept (written before
    2026-11-06), or the one its source holds now when that is unchanged. One
    read, so the text written is the text checked."""
    import hashlib

    if "sample_text" in kept:
        # Kept with its sample; one kept empty has nothing to write from.
        if not kept["sample_text"]:
            return None, UNSAVED_RERUN
        return kept["sample_text"], None
    facts = kept.get("sample") if isinstance(kept.get("sample"), dict) else {}
    source = facts.get("source")
    if not isinstance(source, dict) or not source:
        return None, UNSAVED_RERUN
    try:
        text = _source_sample(source)
    except Exception:  # the configuration can't be read: said, not raised
        text = None
    if text is None:
        return None, UNSAVED_GONE
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != facts.get("digest"):
        return None, UNSAVED_CHANGED
    return text, None


def unsaved_rerun_refusal(kept: dict[str, Any]) -> str | None:
    """Why a run of an unsaved configuration can't be written again, or None
    when it can."""
    return _verified_sample(kept)[1]


# Every snapshot the purge of unsaved samples rewrote, one JSON object a line.
UNSAVED_PURGE_LOG = "unsaved-sample-purge.jsonl"


def _found_source(text: str, fmt: str | None) -> dict[str, Any] | None:
    """Where SDM still holds a sample of exactly this text -- a standard file
    of its format, else a saved configuration -- or None."""
    import hashlib

    from sdg.analyzer.services import sample_gen_configs
    from sdg.writers.samples import standard

    wanted = hashlib.sha256(text.encode("utf-8")).hexdigest()

    def same(other: str | None) -> bool:
        return bool(other) and hashlib.sha256(other.encode("utf-8")).hexdigest() == wanted

    for entry in standard.listing():
        if fmt and entry.get("format") != fmt:
            continue
        found = standard.files(entry["id"])
        if found and same(found["sample"]):
            return {"standard": entry["id"]}
    for listed in sample_gen_configs.list_configs():
        saved = sample_gen_configs.get_config(int(listed["id"]))
        if saved and same(saved.get("sample_text")):
            return {"config": int(listed["id"])}
    return None


def purge_unsaved_samples(*, apply: bool, only: set[str] | None = None) -> list[dict[str, Any]]:
    """Runs of unsaved configurations kept before 2026-11-06 hold their
    sample's text. Each such snapshot is rewritten to what a run keeps now
    (`_unsaved_kept`): the sample's digest, size, format, kind, name and
    counts, and where SDM still holds it when it does. Nothing else changes:
    only kind 'unsaved' rows holding a sample, each under the key its files
    name (the key is the snapshot's identity, not its body's digest), and
    nothing is deleted. A dry run (apply False) changes nothing; each change
    is logged before it is made. Run again, there is nothing left to do."""
    from datetime import datetime

    from sdg.analyzer.services import generation_requests as gr

    done = []
    for key, body in gr.unsaved_with_sample(only):
        text = body.get("sample_text") or ""
        try:
            source = _found_source(text, body.get("format"))
        except Exception:  # not determinable: kept without a source
            source = None
        kept = _unsaved_kept({**body, "sample_source": source})
        entry = {"digest": key, "source": source, "name": body.get("sample_name"),
                 "size_before": len(gr.dumps(body)), "size_after": len(gr.dumps(kept)),
                 "sample_digest": kept["sample"]["digest"]}
        if apply:
            logged = {"at": datetime.now().isoformat(timespec="seconds"), "who": _who(),
                      **entry}
            with (service.work / UNSAVED_PURGE_LOG).open("a", encoding="utf-8") as log:
                log.write(json.dumps(logged) + "\n")
            entry["rewritten"] = gr.rewrite_snapshot(key, "unsaved", kept)
        done.append(entry)
    return done


def _kept_sample_text(modeled: dict[str, Any]) -> str:
    """The sample a run of an unsaved configuration is written again from:
    the one it kept, or the identical one its source still holds. Refused
    (409) when there is none -- before anything is generated."""
    text, refusal = _verified_sample(modeled)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    return text or ""


def _unsaved_config(body: dict[str, Any] | None, key: str) -> dict[str, Any]:
    """A configuration accepted on the screen and not saved, built as Save
    configuration as would build it. Its run keeps the configuration, not the
    sample (`_unsaved_kept`); asked for again without it, it is gone."""
    if body is None:
        raise HTTPException(status_code=409, detail=(
            "This configuration was not saved, so it can't be written again. "
            "Upload the sample again or save its configuration."))
    if _unsaved_key(body) != key:
        raise HTTPException(status_code=422, detail="The configuration does not match its name.")
    try:
        request = GenConfigSave(
            name=str(body.get("name") or "custom"), sample=body.get("sample"),
            sampleName=body.get("sampleName"), basedOn=body.get("basedOn"),
            rows=body["rows"], vary=body.get("vary"),
            fromConfigId=body.get("fromConfigId"), fromDefaultId=body.get("fromDefaultId"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=plain(e))
    fmt, sample, config, sample_text, sample_name = _gen_config(request)
    return {"format": fmt.FORMAT, "kind": sample.kind, "name": request.name,
            "sample_name": sample_name, "sample_text": sample_text, "config": config,
            "sample_source": _unsaved_source(body)}


def _modeled_config(export_format: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """The config a sample-modeled file is written through: a saved one
    (sample:<id>), a standard file as it stands (standard:<id>), or one
    accepted on the screen and not saved (unsaved, sent as `body`; kept with
    the run as unsaved:<digest>)."""
    from sdg.analyzer.services import sample_gen_configs

    kind, _, key = export_format.partition(":")
    if kind == "standard":
        return _source(f"standard:{key}", lambda: _standard_config(key))
    if kind == "unsaved":
        key = key or _unsaved_key(body)
        return _source(f"unsaved:{key}", lambda: _unsaved_config(body, key))
    modeled = (_source(f"config:{key}", lambda: sample_gen_configs.get_config(int(key)))
               if key.isdigit() else None)
    if modeled is None:
        raise HTTPException(status_code=404, detail=f"No sample config {key}.")
    return modeled


def _modeled(export_format: str) -> bool:
    return export_format.startswith(("sample:", "standard:", "unsaved"))


@app.post("/api/samples/read")
def sample_read(request: SampleRead) -> dict[str, Any]:
    """Every place of the sample, with the algorithm the master proposes."""
    from sdg.analyzer.services import sample_gen_configs
    from sdg.writers import samples

    _sample_fits(request.sample)
    # A configuration file chosen as the master is read the same way.
    _sample_fits(request.master)
    sample_text, master, based_on = request.sample, request.master, request.masterName
    sample_name = request.sampleName
    default_id = request.defaultId
    if (request.auto and sample_text and not master and default_id is None
            and request.configId is None):
        try:
            fmt = samples.detect(sample_text)
            default_id = _standard_for(fmt, fmt.read_sample(sample_text))
        except samples.SampleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if default_id is not None:
            found = _standard(default_id)
            master, based_on = found["master"], found["master_name"]
    if request.defaultId is not None:
        found = _standard(request.defaultId)
        master, based_on = found["master"], found["master_name"]
        if not sample_text:
            sample_text, sample_name = found["sample"], found["sample_name"]
    if request.configId is not None:
        saved = sample_gen_configs.get_config(request.configId)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"No sample config {request.configId}.")
        master, based_on = json.dumps(saved["config"]), saved["name"]
        sample_text = sample_text or saved["sample_text"]
    if not sample_text:
        raise HTTPException(status_code=422, detail="Choose a sample file first.")
    try:
        fmt = samples.detect(sample_text)
        sample = fmt.read_sample(sample_text)
        entries = fmt.read_config(master) if master else []
    except samples.SampleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    algorithms = samples.algorithm_rules()
    rows = fmt.propose(sample, entries, algorithms)
    if request.defaultId is not None and request.configId is None:
        _standard_suggestions(rows)
    counts, summary = fmt.facts(sample)
    # What can be optional or repeat, where the format's reader varies a sample
    # (FHIR, CDA); and what the config read already varies.
    from sdg.writers.samples import engine as sample_engine

    varies = hasattr(fmt, "structure")
    try:
        vary = [{"at": v.at, "present": v.present, "min": v.low, "max": v.high,
                 **({"mix": [[c, s] for c, s in v.mix]} if v.mix else {})}
                for v in sample_engine.read_vary(master)] if varies and master else []
    except samples.SampleError:
        vary = []
    return {
        "structure": fmt.structure(sample) if varies else None,
        "vary": vary,
        "format": fmt.FORMAT, "kind": sample.kind, "version": sample.version,
        "sampleName": sample_name, "basedOn": based_on, "defaultId": default_id,
        "counts": counts, "summary": summary, "subject": fmt.subject(sample.kind),
        "validation": fmt.VALIDATION, "extension": fmt.EXTENSION,
        "rows": rows,
        "algorithms": sorted(algorithms),
    }


class GenConfigSave(BaseModel):
    """Save the chosen places as a new generation config."""

    name: str
    sample: str | None = None
    sampleName: str | None = None
    basedOn: str | None = None
    rows: list[dict[str, Any]]
    # Places in some documents and not others, or repeated: {at, present, min, max}.
    vary: list[dict[str, Any]] | None = None
    # Read from a saved config and no other sample chosen: that config's sample.
    fromConfigId: int | None = None
    # Read from a standard file and no other sample chosen: its sample.
    fromDefaultId: str | None = None


@app.get("/api/samples/configs")
def sample_config_list() -> dict[str, Any]:
    from sdg.analyzer.services import sample_gen_configs

    return {"configs": sample_gen_configs.list_configs()}


@app.get("/api/samples/configs/{config_id}")
def sample_config_get(config_id: int) -> dict[str, Any]:
    from sdg.analyzer.services import sample_gen_configs

    from sdg.writers import samples

    saved = sample_gen_configs.get_config(config_id)
    if saved is None:
        raise HTTPException(status_code=404, detail=f"No sample config {config_id}.")
    samples.module(saved["format"])  # refused, by name, where the pack does not offer it
    saved.pop("sample_text", None)
    return saved


def _gen_config(request: GenConfigSave) -> tuple[Any, Any, dict[str, Any], str, str | None]:
    """The format, the sample read, the config written from the rows, and the sample's text and name."""
    from sdg.analyzer.services import sample_gen_configs
    from sdg.writers import samples

    _sample_fits(request.sample)
    sample_text, sample_name = request.sample, request.sampleName
    if not sample_text and request.fromConfigId is not None:
        source = sample_gen_configs.get_config(request.fromConfigId)
        if source is None:
            raise HTTPException(status_code=404,
                                detail=f"No sample config {request.fromConfigId}.")
        sample_text, sample_name = source["sample_text"], sample_name or source["sample_name"]
    if not sample_text and request.fromDefaultId is not None:
        found = _standard(request.fromDefaultId)
        sample_text, sample_name = found["sample"], sample_name or found["sample_name"]
    if not sample_text:
        raise HTTPException(status_code=422, detail="Choose a sample file first.")
    try:
        fmt = samples.detect(sample_text)
        sample = fmt.read_sample(sample_text)
    except samples.SampleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    from sdg.writers.samples import engine as sample_engine

    try:
        vary = sample_engine.write_vary(request.vary) if hasattr(fmt, "structure") else []
    except samples.SampleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    config = fmt.write_config(
        request.rows, transaction=sample.kind, version=sample.version,
        based_on=request.basedOn, sample_name=sample_name, vary=vary or None)
    if not fmt.positions(config):
        raise HTTPException(status_code=422, detail="No position takes a generated value: "
                            "choose an algorithm for at least one.")
    return fmt, sample, config, sample_text, sample_name


@app.post("/api/samples/configs")
def sample_config_save(request: GenConfigSave) -> dict[str, Any]:
    """Save as: always a new config; the master it came from is not touched."""
    from sdg.analyzer.services import sample_gen_configs

    fmt, sample, config, sample_text, sample_name = _gen_config(request)
    try:
        return sample_gen_configs.save_as(
            name=request.name, format=fmt.FORMAT, kind=sample.kind, version=sample.version,
            based_on=request.basedOn, config=config, sample_name=sample_name,
            sample_text=sample_text)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.put("/api/samples/configs/{config_id}")
def sample_config_update(config_id: int, request: GenConfigSave) -> dict[str, Any]:
    """Save changes: a saved config changed in place (a standard file or a
    masking config is never one: those are only ever saved as)."""
    from sdg.analyzer.services import sample_gen_configs

    _sample_fits(request.sample)
    held = sample_gen_configs.get_config(config_id)
    if held is None:
        raise HTTPException(status_code=404, detail=f"No sample config {config_id}.")
    # Read back from itself, the screen names the config as its own master;
    # what it was built from is what it keeps.
    request.basedOn = held.get("based_on")
    if request.fromConfigId is None and not request.sample:
        request.fromConfigId = config_id
    fmt, sample, config, sample_text, sample_name = _gen_config(request)
    try:
        return sample_gen_configs.update(
            config_id, name=request.name, format=fmt.FORMAT, kind=sample.kind,
            version=sample.version, config=config, sample_name=sample_name,
            sample_text=sample_text)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/api/samples/configs/{config_id}")
def sample_config_delete(config_id: int) -> dict[str, Any]:
    from sdg.analyzer.services import sample_gen_configs

    if not sample_gen_configs.delete_config(config_id):
        raise HTTPException(status_code=404, detail=f"No sample config {config_id}.")
    return {"deleted": config_id}


@app.get("/api/patterns/for-files")
def patterns_for_files() -> dict[str, Any]:
    """The saved patterns, which are what a file can be written for.

    Saved means somebody kept it -- from an analysis, from chosen records, or
    by starring it in discovery. The patterns a discovery walk stores on its
    own are its working state, not a list anyone chose, and are left out.
    Where the pack writes nothing from a pattern (no pattern_generation),
    none is a file's to be written for.
    """
    import json

    from sdg.analyzer import db

    if not packs.offers("pattern_generation"):
        return {"patterns": []}

    out = []
    for row in db.get_saved_patterns():
        if not row.get("profile_json"):
            continue
        profile = json.loads(row["profile_json"])
        out.append({
            "pattern_id": int(row["id"]),
            "name": str(row.get("contextual_name") or f"Pattern {row['id']}"),
            "domain": pattern_scope.entity_of(row),
            "member_count": int(profile.get("n_members") or row.get("member_count") or 0),
            "saved": row.get("saved_by") is not None,
            "origin": row.get("origin") or "discovered",
        })
    # Most recently saved first, which is the order get_saved_patterns reads.
    return {"patterns": out}


class KeepPattern(BaseModel):
    """A pattern to save: what an analysis found, or only chosen records of it."""

    domain: str = packs.active().primary   # the primary or child records' key
    filters: dict[str, Any] = {}
    k: int | None = None
    cluster_id: int
    claim_type: str | None = None
    # Row names from the rows view; empty keeps the whole pattern.
    record_keys: list[str] = []
    name: str | None = None
    # "user_defined" for a pattern somebody defined by its rules.
    origin: str | None = None


@app.get("/api/discovery-features", dependencies=[_ANALYSIS])
def discovery_features_get() -> dict[str, Any]:
    """Every feature of members and of claims, and which ones patterns are found on."""
    from sdg.analyzer.services import discovery_features

    return {entity: discovery_features.described(entity) for entity in (packs.active().primary, packs.active().child)}


class DiscoveryFeatures(BaseModel):
    entity: str
    features: list[str]


@app.put("/api/discovery-features", dependencies=[_ANALYSIS])
def discovery_features_put(request: DiscoveryFeatures) -> dict[str, Any]:
    """Find patterns of this kind on these features from now on."""
    from sdg.analyzer.services import discovery_features

    try:
        discovery_features.choose(request.entity, request.features)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {request.entity: discovery_features.described(request.entity)}


@app.get("/api/report-fields", dependencies=[_ANALYSIS])
def report_fields_get() -> dict[str, Any]:
    """Every field a pattern's counts can be made on, built-in and denorm, and
    which are chosen."""
    from sdg.analyzer.services import report_fields

    return {entity: report_fields.offered(entity) for entity in (packs.active().primary, packs.active().child)}


class ReportFields(BaseModel):
    entity: str
    fields: list[str]
    # Back to the built-in fields, whatever `fields` says.
    reset: bool = False


@app.put("/api/report-fields", dependencies=[_ANALYSIS])
def report_fields_put(request: ReportFields) -> dict[str, Any]:
    """Count patterns of this kind on these fields, in this order, from now on."""
    from sdg.analyzer.services import report_fields

    try:
        if request.reset:
            report_fields.reset(request.entity)
        else:
            report_fields.choose(request.entity, request.fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {request.entity: report_fields.offered(request.entity)}


@app.get("/api/patterns/features", dependencies=[_ANALYSIS])
def pattern_features(entity: str = packs.active().primary) -> dict[str, Any]:
    """What a pattern can be defined by: each feature and the values it can take."""
    from sdg.analyzer.services import defined_patterns

    try:
        return {"entity": entity, "features": defined_patterns.catalog(entity)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/patterns/profile", dependencies=[_ANALYSIS])
def pattern_profile(request: KeepPattern) -> dict[str, Any]:
    """What saving this pattern -- or only its chosen records -- would store
    as its profile, measured and not saved."""
    from sdg.analyzer.services import saved_patterns

    try:
        return {"profile": saved_patterns.profile_of(
            request.domain, request.filters, request.k, request.cluster_id,
            claim_type=request.claim_type, record_keys=request.record_keys)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/patterns/keep", dependencies=[_ANALYSIS])
def keep_pattern(request: KeepPattern) -> dict[str, Any]:
    """Save a pattern so it is ready under File Generation next time."""
    from sdg.analyzer.services import saved_patterns

    try:
        return saved_patterns.save(
            request.domain, request.filters, request.k, request.cluster_id,
            claim_type=request.claim_type, record_keys=request.record_keys,
            name=request.name, origin=request.origin,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


class LivePattern(BaseModel):
    """A pattern that exists only as the result of an analysis, not yet saved.

    Addressed the way the analysis addressed it -- its filters, k and cluster --
    because that is what rebuilds its members.
    """

    profile: dict[str, Any]
    domain: str = packs.active().primary   # the primary or child records' key
    filters: dict[str, Any] = {}
    k: int | None = None
    cluster_id: int
    claim_type: str | None = None


class ExportRequest(BaseModel):
    """What to write, in what shape, and for whom."""

    format: str = "csv"            # csv | fixed | edi:<id> | edi834 | edi837
    subject: str = packs.active().primary  # the primary or child records' key
    rows: int | None = None        # cap on records written
    root_limit: int | None = 2000  # cap on root rows generated, for speed
    layout_id: int | None = None   # for format=fixed or a delimited layout
    as_of: str = "2026-06-30"
    # EDI only: how many groups -- members, claims -- go in one ST/SE set. One
    # transaction holding a whole book is valid and nobody sends it.
    per_transaction: int | None = None
    # A pattern: a saved one by id, or one an analysis just found. With one,
    # members are new people with the pattern's values and claims are made for
    # the members the pattern holds. Without one, the plan's run is written.
    pattern_id: int | None = None
    pattern: LivePattern | None = None
    # Each field below also takes its neutral name (count, children_per_record...);
    # the record keeps the field's own name, so a request kept before reads the same.
    members: int = Field(100, validation_alias=AliasChoices("members", "count"))
    claims_per_member: int | None = Field(
        None, validation_alias=AliasChoices("claims_per_member", "children_per_record"))
    lines_per_claim: int | None = Field(
        None, validation_alias=AliasChoices("lines_per_claim", "lines_per_child"))
    # Several scenarios of a pattern, each kept as a pattern of its own rows,
    # written into one file: each scenario's count of records, tagged with it.
    scenarios: list["ScenarioPart"] | None = None
    # A format whose download is a package (CDA: a zip of documents) is written
    # unpacked when this is false: a preview, the checks.
    packaged: bool = True
    # The generation request this file belongs to: the moment the request was
    # started (with the pattern, it makes the seed: new people per request), or
    # an earlier request by its id. Without either, the installation's seed.
    request_started: str | None = None
    request_id: int | None = None
    # Kept in the request's record: a file downloaded, not a preview or a check.
    record: bool = False
    # The name the file was saved under, for the record.
    file_name: str | None = None
    # Claims for the members of an earlier request: `pattern_id` is the claims
    # pattern, superseded by the request's members wherever both say something
    # of a member. Written under a request of their own, linked to that one.
    of_request: int | None = None
    # Percent of the records with children; children each, else as the pattern measured.
    claimed_share: float = Field(
        100, validation_alias=AliasChoices("claimed_share", "with_children_share"))
    claims_min: int | None = Field(
        None, validation_alias=AliasChoices("claims_min", "children_min"))
    claims_max: int | None = Field(
        None, validation_alias=AliasChoices("claims_max", "children_max"))
    # How many claims each, or lines each claim, in a mix: [[1, 60], [2, 30]].
    claims_mix: list[list[float]] | None = Field(
        None, validation_alias=AliasChoices("claims_mix", "children_mix"))
    lines_min: int | None = None
    lines_max: int | None = None
    lines_mix: list[list[float]] | None = None
    # format "unsaved": a sample configuration accepted on the screen and not
    # saved -- {sample, sampleName, basedOn, rows, vary, fromDefaultId,
    # fromConfigId}. The run keeps its configuration by its digest (format
    # unsaved:<digest>), not the sample: Rerun says the sample is needed.
    sample_config: dict[str, Any] | None = None


class ScenarioPart(BaseModel):
    pattern_id: int
    label: str
    count: int = 1


ExportRequest.model_rebuild()


# Elements a file carries when no layout says otherwise.
DEFAULT_ELEMENTS = packs.held("pack:files.default_elements")


def _export_records(request: ExportRequest, rules: dict[str, dict], *,
                    members_seed: str | None = None):
    """The records to write, keyed by element, and the rules to fill them with.

    A pattern replaces the rules for the elements it names. Members from a
    pattern are new; claims from a pattern belong to the members it holds,
    because a claim is always made by somebody who exists.
    """
    import json

    from sdg.writers import elements

    if request.of_request is not None and packs.is_child(request.subject):
        return _claims_for_request(request, rules)
    if request.pattern_id is None and request.pattern is None:
        # The plan's run: this one belongs to the database being worked on.
        # The plan is kept with the file, so writing it again runs the plan it
        # was written from; the keys it reuses are read live and noted by
        # digest, so a file written again that differs can say it was them.
        from sdg.analyzer.services import generation_requests as gr
        from sdg.plan.model import Plan

        plan = Plan.from_dict(_source("plan", lambda: service.require_plan().to_dict()))
        result = _guard(service.run_for_export, request.root_limit, plan=plan)
        sources = _SOURCES.get()
        if sources is not None:
            sources.digests["reused keys"] = gr.digest(service.export_reused_keys())
        if packs.is_child(request.subject):
            return elements.run_claim_lines(result, plan, limit=request.rows), rules
        return elements.run_members(result, plan, limit=request.rows), rules

    if request.pattern is not None:
        profile = dict(request.pattern.profile)
        scope = dict(request.pattern.filters)
    else:
        row = _pattern_row(request.pattern_id)
        if row is None or not row.get("profile_json"):
            raise HTTPException(status_code=404,
                                detail=f"No pattern {request.pattern_id}.")
        profile = json.loads(row["profile_json"])
        scope = pattern_scope.filters_of(row)

    claims_pattern = bool(profile.get("claims_pattern")) or (
        packs.is_child(request.pattern.domain) if request.pattern is not None
        else packs.is_child(pattern_scope.entity_of(row)))
    rules = elements.with_pattern(rules, elements.pattern_rules(
        profile, service.catalog,
        kind=_kind(packs.active().child if claims_pattern else packs.active().primary),
        as_of=request.as_of,
    ))

    # The pattern's group, plan and product, where no combination of the
    # pattern's already draws them together with what they depend on.
    combined = {e for e, r in rules.items() if r.get("record_key")}
    rules = elements.with_pattern(rules, {e: r for e, r in _scope_rules(scope, rules).items()
                                          if e not in combined})
    # Where the pattern carries no addresses -- the store holds none, as a
    # customer's denorm does not -- a ZIP, its city and state are still drawn
    # together, from the store's own address list.
    rules = elements.with_pattern(rules, {e: r for e, r in _address_rules().items()
                                          if not rules.get(e, {}).get("record_key")})

    if not packs.is_child(request.subject):
        count = request.members if request.rows is None else min(request.members, request.rows)
        return elements.new_members(count, rules, seed=members_seed or _file_seed(),
                                    as_of=request.as_of), rules

    per_member, per_claim = _claim_shape(profile, claims_pattern)
    per_member = request.claims_per_member or per_member
    per_claim = request.lines_per_claim or per_claim
    # A capped file needs only the members its lines reach; a preview of six
    # lines should not read four thousand people to show one.
    needed = None if request.rows is None else -(-request.rows // (per_member * per_claim))
    # From the pattern store alone: its members and which elements a claim
    # carries once or per line. No target is involved, onboarded or not.
    shape = _store_shape()
    members = _pattern_members(request, shape, claims_pattern, needed)
    claim_level, line_level = elements.store_claim_elements(shape)
    claims_of, lines_of = _how_many(request, per_member, per_claim, always=False)
    return elements.claims_for(
        members, rules,
        claim_level=claim_level, line_level=line_level,
        claims_per_member=per_member,
        lines_per_claim=per_claim,
        seed=_file_seed(), as_of=request.as_of, limit=request.rows,
        claims_of=claims_of, lines_of=lines_of,
    ), rules


def _store_shape():
    """The pattern store's description, which a file from a pattern is built on."""
    shape = service.landscape()
    if shape is None:
        raise HTTPException(
            status_code=409,
            detail="The pattern store has not been described yet. Run "
                   "`sdg landscape` (first-run does) and try again.",
        )
    return shape


def _file_seed() -> str:
    """The seed for a file from a pattern: its request's, else the installation's."""
    return _REQUEST_SEED.get() or _base_seed()


def _base_seed() -> str:
    return str(service.read_settings().get("seed") or "demo-seed-001")


# The seed of the request being written, while its export is being set up.
_REQUEST_SEED: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "request_seed", default=None)


class _Sources:
    """What an export is written through -- the rules, a layout, a sample
    config, a saved pattern -- read live and kept, or, writing a file again,
    read from what was kept.

    Live values are passed through the same plain form a snapshot is kept in,
    so a file and its replay are made from identical values."""

    def __init__(self, replay: dict[str, Any] | None = None,
                 preset: dict[str, Any] | None = None):
        self.replay = replay
        # A new file for an earlier request: its people are made from what the
        # request's first file was written through, not from today's.
        self.preset = preset or {}
        self.used: dict[str, Any] = {}
        # What is read live even writing a file again -- the store's members a
        # claims file is for -- by digest: a file written again that differs
        # can say it was them.
        self.digests: dict[str, str] = {}
        # What a file written again was read live for, because it was written
        # before SDM kept it.
        self.unkept: list[str] = []

    def get(self, kind: str, live):
        from sdg.analyzer.services import generation_requests as gr

        if kind in self.preset:
            self.used[kind] = self.preset[kind]
            return self.preset[kind]
        if self.replay is not None:
            if kind not in self.replay and kind in _KEPT_SINCE:
                # Written before SDM kept it: written again from today's.
                self.unkept.append(kind)
                return gr.loads(gr.dumps(live()))
            if kind not in self.replay:
                raise HTTPException(
                    status_code=409,
                    detail=f"This file was not written through {kind}, so it cannot be "
                           "written again the same way.")
            return self.replay[kind]
        value = live()
        if value is not None:
            value = gr.loads(gr.dumps(value))
        self.used[kind] = value
        return value


# Kept with every file since the date given; a file written before it is
# written again from what is there today, and says so.
_KEPT_SINCE = {"plan": "2026-11-06"}

_SOURCES: "contextvars.ContextVar[_Sources | None]" = contextvars.ContextVar(
    "export_sources", default=None)


def _source(kind: str, live):
    sources = _SOURCES.get()
    return live() if sources is None else sources.get(kind, live)


def _pattern_row(pattern_id: int) -> dict[str, Any] | None:
    from sdg.analyzer import db

    return _source(f"pattern:{pattern_id}", lambda: db.get_pattern(pattern_id))


def _listed(value: Any) -> list[str]:
    """A stored scope value -- a JSON list, a sentinel, or nothing -- as a list."""
    import json

    if value in (None, "", "null", "ALL"):
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        parsed = [value]
    if not isinstance(parsed, list):
        parsed = [parsed]
    return [str(v) for v in parsed if v not in (None, "") and not str(v).startswith("__")]


# What a pattern's scope names, as the elements it narrows.
SCOPE_ELEMENTS = packs.held("pack:files.scope_elements")
SCOPE_NARROWS: list[dict[str, str]] = packs.held("pack:files.scope_narrows")
# Their table and columns go into SQL by name: plain names only, or the pack
# is refused before anything is read.
for _narrow in SCOPE_NARROWS:
    for _part in ("table", "column", "gives"):
        packs.plain_name(_narrow.get(_part), f"pack.json files.scope_narrows {_part}")


def _addresses() -> list[dict[str, Any]]:
    """The store's address list: the one every address rule draws from.

    A store without the list has no address rules. Any other failure to read
    it is said: a file written without its addresses because of a moment's
    connection trouble would be recorded as if nothing were wrong."""
    from sdg.analyzer.generation_rules import address_records, missing_list

    try:
        return address_records()
    except Exception as e:
        if missing_list(e):
            return []
        raise


def _lists_digest() -> dict[str, str]:
    """A digest of each list names, streets and addresses are drawn from."""
    from sdg.analyzer import generation_rules as lists
    from sdg.analyzer.services import generation_requests as gr

    held: dict[str, str] = {}
    for name, read in (("first names", lambda: lists.name_list("first_names")),
                       ("last names", lambda: lists.name_list("last_names")),
                       ("streets", lists.street_list),
                       ("addresses", lists.address_records)):
        try:
            held[name] = gr.digest(read())
        except Exception as e:
            if not lists.missing_list(e):
                raise
            held[name] = "missing"
    return held


def _address_rules() -> dict[str, dict]:
    """ZIP, city and state as whole records from the store's address list."""
    if not _addresses():
        return {}
    return {
        element: {"element": element, "gen_method": "lookup", "values": _addresses(),
                  "record_key": "pattern_address", "field": element}
        for element in ("ADDRESS_ZIP", "ADDRESS_CITY", "ADDRESS_STATE", "ADDRESS_COUNTY")
    }


def _kind(entity: str) -> str:
    """The plan's word for a record kind (plan.kind_anchors: the glossary's one,
    health's member or claim, a bank's customer or account)."""
    return packs.active().term(entity, "one")


def _scope_rules(scope: dict[str, Any], rules: dict[str, dict]) -> dict[str, dict]:
    """The pattern's scope, as rules (pack.json files.scope_elements).

    A pattern found in one part of the book describes the records there, so
    new records drawn from it belong to the same scope (health: one
    employer's group, plans and products). A level the files carry only
    through another narrows that one instead (files.scope_narrows; health: a
    plan category narrows the plans to the ones in that category).
    """
    out: dict[str, dict] = {}
    for key, element in SCOPE_ELEMENTS.items():
        values = _listed(scope.get(key))
        if values:
            out[element] = {"element": element, "gen_method": "lookup", "values": values}
    for narrow in SCOPE_NARROWS:
        chosen = set(_listed(scope.get(narrow["key"])))
        element = narrow["element"]
        if not chosen or element in out:
            continue
        # The values those choices hold, as the pattern store has them.
        from sdg.config import get_settings

        marks = ", ".join("?" for _ in chosen)
        gives = narrow["gives"]
        found = [str(r[gives]).strip() for r in service.registry.query(
            "denorm",
            f"SELECT DISTINCT {gives} FROM {get_settings().denorm_schema}.{narrow['table']} "
            f"WHERE {narrow['column']} IN ({marks})",
            sorted(chosen),
        ) if r.get(gives)]
        if found:
            out[element] = {"element": element, "gen_method": "lookup", "values": sorted(found)}
    return out


def _pattern_members(request: ExportRequest, shape, claims_pattern: bool,
                     needed: int | None = None):
    """The members a pattern holds, read from the pattern store."""
    from sdg.analyzer.services import pattern_data
    from sdg.writers import elements

    try:
        if request.pattern is not None:
            live = request.pattern
            from sdg.analyzer.services import filter_models

            if claims_pattern:
                from sdg.api.routers.claims import ChildAnalyzeRequest

                filters = filter_models.of(ChildAnalyzeRequest, live.filters,
                                           live.claim_type, k=live.k)
            else:
                from sdg.analyzer.services.schemas import Filters

                filters = filter_models.of(Filters, live.filters)
            frame = pattern_data.rows_for_live(
                packs.active().child if claims_pattern else packs.active().primary,
                filters, live.k, live.cluster_id,
            )["df"]
        else:
            # A saved pattern recorded the members it holds; a claim file is
            # for exactly those, without rebuilding anything.
            import json

            from sdg.analyzer import db
            from sdg.analyzer.services.saved_patterns import member_keys

            row = _pattern_row(request.pattern_id) or {}
            recorded = member_keys(json.loads(row.get("profile_json") or "{}"))
            if recorded:
                keys = recorded[:needed] if needed is not None else recorded
                # A pattern's members are people in the landscape it was found
                # in, held in the store, whichever database is being worked on.
                members = _store_members(keys, shape)
                if not members:
                    raise HTTPException(status_code=404,
                                        detail="None of this pattern's members were found.")
                return members
            frame = pattern_data.rows_for(request.pattern_id)["df"]
    except pattern_data.StalePattern as e:
        raise HTTPException(status_code=409, detail=str(e))

    # The pattern's rows are store rows, carrying the store's key of their
    # primary record (features.json store.member_key: health's MEME_CK).
    key = packs.held("features:store.member_key")
    if key not in frame.columns:
        primary = packs.active().term(packs.active().primary, "many")
        raise HTTPException(
            status_code=422,
            detail=f"The pattern's rows do not say which {primary} they are.",
        )
    keys = [k for k in dict.fromkeys(frame[key].tolist()) if k is not None]
    if needed is not None:
        keys = keys[:needed]
    members = _store_members(keys, shape)
    if not members:
        raise HTTPException(status_code=404,
                            detail="None of this pattern's members were found.")
    return members


def _store_members(keys: list, shape) -> list[dict[str, Any]]:
    """The store's members for `keys`, as it holds them now, noted by digest."""
    from sdg.analyzer.services import generation_requests as gr
    from sdg.writers import elements

    members = elements.store_members(keys, shape, service.registry)
    sources = _SOURCES.get()
    if sources is not None:
        sources.digests["store members"] = gr.digest(members)
    return members


def _claim_shape(profile: dict[str, Any], claims_pattern: bool) -> tuple[int, int]:
    """Claims per member and lines per claim, as the pattern measured them."""
    if not claims_pattern:
        return 1, 1
    members = float(profile.get("n_members") or 0)
    claims = float(profile.get("n_claims") or 0)
    lines = float(profile.get("size") or 0)
    per_member = round(claims / members) if members else 1
    per_claim = round(lines / claims) if claims else 1
    return max(1, min(per_member, 50)), max(1, min(per_claim, 10))


# Streaming rather than building a response body: an extract is the one thing
# here whose size is not bounded by the screen, and the formatter holds a block
# at a time however many rows go through it.
def _attachment(name: str) -> str:
    """A download's header: the file's own name, never a path, quotes or
    control characters. `filename` is plain ASCII (any other character an
    underscore), so every server and browser can carry it; `filename*`
    (RFC 5987) holds the real name in UTF-8."""
    from pathlib import PurePath
    from urllib.parse import quote

    plain_name = PurePath(str(name).replace("\\", "/")).name
    plain_name = "".join(c for c in plain_name
                         if c != '"' and ord(c) >= 32 and ord(c) != 127) or "sdm-file"
    ascii_name = "".join(c if ord(c) < 127 and c not in "\\;" else "_" for c in plain_name)
    if ascii_name == plain_name:
        return f'attachment; filename="{plain_name}"'
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(plain_name, safe='')}")


@app.post("/api/export")
def export(request: ExportRequest) -> StreamingResponse:
    """Write records in the shape another system reads.

    Every record is keyed by element and every format names elements -- an EDI
    position, a layout field -- so nothing here knows a column. Where a record
    lacks an element, its rule makes one; the same rules, seed and position
    give the same value, so the same request produces byte-identical output.
    """
    stream, media, filename, request_id = write_file(request)
    headers = {"Content-Disposition": _attachment(filename)}
    if request_id is not None:
        headers["X-SDM-Request"] = str(request_id)
    return StreamingResponse(stream, media_type=media, headers=headers)


def write_file(request: ExportRequest):
    """A file as the screens write it -- its parts, type, name and the request
    it is kept under, if it is -- for the screens and the command line alike."""
    stream, media, filename, kept = _request_stream(request)
    if kept is not None:
        stream = kept.recorded(stream, filename)
    return _primed(stream), media, filename, (kept.request["id"] if kept is not None else None)


def _primed(stream):
    """The stream with its first part already made.

    Once a response has begun it can only be cut off, so a file that cannot
    be written -- a pattern gone, a rule that fails on the first record --
    fails here, before it begins, as a sentence and an error status. Failing
    later, the connection is cut, and the screens say the file stopped part
    way; nothing is recorded for it, since a file is recorded once complete.
    """
    import itertools

    parts = iter(stream)
    try:
        first = next(parts)
    except StopIteration:
        return iter(())
    return itertools.chain([first], parts)


def _pattern_key(request: ExportRequest) -> str:
    from sdg.analyzer.services import generation_requests as gr

    if request.of_request is not None:
        # Claims for an earlier request's members: their own request, of the
        # claims pattern and the request it supersedes.
        return f"{request.pattern_id}@r{request.of_request}"
    return gr.pattern_key(
        pattern_id=request.pattern_id,
        live=request.pattern.model_dump() if request.pattern is not None else None,
        scenarios=[p.model_dump() for p in request.scenarios] if request.scenarios else None,
    )


def _request_seed(request: ExportRequest) -> tuple[str | None, dict[str, Any] | None]:
    """The seed a request writes with, and the request when it is a stored one.

    A file for the plan's run keeps the plan's seed: it describes the people a
    database load writes, whichever request it is listed under."""
    from sdg.analyzer.services import generation_requests as gr

    if request.request_id is not None:
        stored = gr.get(request.request_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"No run {request.request_id}.")
        return (None if stored["pattern_key"] == "plan" else stored["seed"]), stored
    if request.request_started is None:
        return None, None
    try:
        started = gr.checked_started(request.request_started)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    key = _pattern_key(request)
    return (None if key == "plan" else gr.seed_for(_base_seed(), key, started)), None


# What a file is read from live even when it is written again, as a person
# reads it in "it differs because ... changed".
_LIVE_INPUTS = {
    "store members": "the store's members",
    "reused keys": "the keys the plan reuses",
    "combinations": "the combinations measured in the store",
}


class _Kept:
    """A file being written under a request: kept in its record once all of it
    has been written, with its fingerprint and what it was written through."""

    def __init__(self, request: dict[str, Any], export: ExportRequest, sources: _Sources,
                 replay_of: dict[str, Any] | None = None,
                 versions: dict[str, int] | None = None):
        self.request = request
        self.export = export
        self.sources = sources
        self.replay_of = replay_of
        # The versions the file was pinned to, when it was: what made it.
        self.versions = versions

    def recorded(self, stream, filename: str):
        import hashlib

        from sdg.analyzer.services import generation_requests as gr

        fingerprint, size = hashlib.sha256(), 0
        for chunk in stream:
            data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            fingerprint.update(data)
            size += len(data)
            yield chunk
        if self.replay_of is not None:
            snapshots = dict(self.replay_of["snapshots"])
            changed = self._changed_since(snapshots)
            if changed:
                snapshots["changed"] = gr.keep_snapshot("changed", changed)
        else:
            from sdg.engine.generators import algorithm_versions

            snapshots = {kind: gr.keep_snapshot(
                             kind.split(":", 1)[0],
                             _unsaved_kept(body) if kind.startswith("unsaved:") else body)
                         for kind, body in self.sources.used.items() if body is not None}
            # Which version of each algorithm made the values: the ones it was
            # pinned to (an earlier request's), else today's latest. Pinned, an
            # algorithm the pins do not name made them at version 1 (it came
            # after them), and is recorded so -- else the file, written again,
            # would be made with its latest and differ.
            made_by = algorithm_versions()
            if self.versions is not None:
                made_by = {name: self.versions.get(name, 1) for name in made_by}
            snapshots["algorithms"] = gr.keep_snapshot("algorithms", made_by)
            # The store's lists every name, street and address is drawn from,
            # by digest: a file written again that differs can say they changed.
            snapshots["lists"] = gr.keep_snapshot("lists", _lists_digest())
            for kind, held in self.sources.digests.items():
                snapshots[kind] = gr.keep_snapshot(kind, held)
        gr.record_file(
            request_id=self.request["id"], file_name=self.export.file_name or filename,
            format=self.export.format,
            subject=self.export.subject, params=_export_params(self.export),
            snapshots=snapshots, sdm_version=__version__, sdm_build=_sdm_build(),
            fingerprint=fingerprint.hexdigest(), size=size, written_by=_who(),
            replay_of=self.replay_of["id"] if self.replay_of is not None else None,
        )

    def _changed_since(self, snapshots: dict[str, str]) -> list[str]:
        """What the store holds differently from when the file was first
        written: what a file written again was read from live."""
        from sdg.analyzer.services import generation_requests as gr

        changed = []
        for kind, now in self.sources.digests.items():
            held = snapshots.get(kind)
            if held and now and gr.snapshot(held) != now:
                changed.append(_LIVE_INPUTS.get(kind, kind))
        for kind in self.sources.unkept:
            changed.append(f"the {kind} (files written before {_KEPT_SINCE[kind]} did not keep it)")
        if "lists" in snapshots and gr.snapshot(snapshots["lists"]) != _lists_digest():
            changed.append("the name and address lists")
        return changed


def _export_params(request: ExportRequest) -> dict[str, Any]:
    """The export as it was asked for, without which request it belongs to."""
    # An unsaved sample configuration is kept as a snapshot, by its digest in
    # the format, not repeated in every file's parameters.
    return request.model_dump(exclude={"record", "request_started", "request_id", "file_name",
                                       "sample_config"}, mode="json")


def _request_stream(request: ExportRequest, replay: dict[str, Any] | None = None):
    """The file an export writes, set up with its request's seed and sources.

    A download names its request in `kept`, opening the request with its first
    file; a preview or a check writes with the same seed and keeps nothing."""
    from sdg.analyzer.services import generation_requests as gr

    # A file from a pattern, where the pack offers writing one: refused here,
    # where every file is set up -- a download, a check, a write again.
    if request.pattern_id is not None or request.pattern is not None or request.scenarios:
        packs.require("pattern_generation", "Generating from patterns")

    from sdg.engine.generators import pinned

    if request.format == "unsaved":
        # Named in the run by what was accepted: the same rules on the same
        # sample are the same configuration.
        request.format = f"unsaved:{_unsaved_key(request.sample_config)}"
    if replay is None and request.request_id is not None and request.of_request is None:
        # An earlier request of claims for a request's members: the same
        # claims, in another format, asked for as they were.
        chosen = gr.get(request.request_id)
        if chosen is not None and chosen.get("parent_id"):
            first = gr.first_file(chosen["id"])
            for key in ("of_request", "pattern_id", "claimed_share", "claims_min", "claims_max",
                        "claims_mix", "lines_per_claim", "lines_min", "lines_max", "lines_mix",
                        "as_of"):
                if first is not None and key in first["params"]:
                    setattr(request, key, first["params"][key])
    if replay is None and request.of_request is not None:
        # An earlier request's members: their claims are a request of their
        # own; another file of the members themselves is the request's.
        if packs.is_child(_file_subject(request)):
            if request.pattern_id is None:
                pack = packs.active()
                raise HTTPException(status_code=422, detail=(
                    f"Choose a pattern of {pack.term(pack.child, 'many')} for the "
                    f"{pack.term(pack.primary, 'many')} of run {request.of_request}."))
            chosen = gr.get(request.request_id) if request.request_id is not None else None
            if not (chosen and chosen.get("parent_id") == request.of_request
                    and chosen.get("pattern_id") == request.pattern_id):
                if request.request_started is None:
                    raise HTTPException(status_code=422, detail=(
                        f"The {packs.active().term(packs.active().child, 'one')} run's start "
                        "time is required."))
                request.request_id = None
        else:
            members_of = gr.get(request.request_id or request.of_request)
            request.request_id = ((members_of or {}).get("parent_id")
                                  or request.request_id or request.of_request)
            request.of_request = None
    claims_mode = request.of_request is not None
    if claims_mode:
        request.subject = packs.active().child

    seed, stored = _request_seed(request)
    sources = _Sources(replay=None if replay is None else {
        kind: gr.snapshot(key) for kind, key in replay["snapshots"].items()})
    # Written again: with the versions of the algorithms it was made with. A
    # file kept before versions were recorded is made with the first ones.
    versions = None
    if replay is not None:
        versions = (sources.replay or {}).get("algorithms") or _FIRST_VERSIONS
    earlier = stored is not None and replay is None and not claims_mode
    if earlier:
        versions, kept_subject = _for_earlier_request(request, stored, sources)
    if claims_mode and replay is None:
        _preset_members(request.of_request, sources)
        if stored is not None:
            # More of an earlier claims request's claims: made by what its
            # first file was made with -- the rules, the claims pattern, the
            # members as they were and the algorithm versions -- not today's.
            versions = _as_first_claims_file(stored, sources) or versions
    recording = request.record and (stored is not None or request.request_started is not None)
    if recording or replay is not None:
        # Measured in the store live, kept by digest: a file written again
        # that differs can say the combinations changed or could not be read.
        from sdg.plan import combinations

        sources.digests["combinations"] = gr.digest(combinations.signature())
    seed_token = _REQUEST_SEED.set(seed)
    sources_token = _SOURCES.set(
        sources if (recording or replay is not None or earlier or claims_mode) else None)
    try:
        with pinned(versions):
            stream, media, filename = _export_stream(request)
    finally:
        _REQUEST_SEED.reset(seed_token)
        _SOURCES.reset(sources_token)
    if earlier and request.subject != kept_subject:
        _refuse_other_subject(stored, kept_subject, request.subject)
    if versions is not None:
        stream = _pinned_stream(stream, versions)
    if not recording:
        return stream, media, filename, None
    if stored is None:
        stored = gr.open_request(
            pattern_key=_pattern_key(request), started=request.request_started,
            seed=seed or service.require_plan().meta.seed,
            pattern_id=request.pattern_id, pattern_name=_request_pattern_name(request),
            subject=request.subject, pattern_body=_request_pattern(request, sources),
            created_by=_who(), parent_id=request.of_request,
        )
    return stream, media, filename, _Kept(stored, request, sources, replay, versions)


def _as_first_claims_file(stored: dict[str, Any], sources: _Sources) -> dict[str, int] | None:
    """Preset what a claims request's first file was written through; its versions."""
    from sdg.analyzer.services import generation_requests as gr

    first = gr.first_file(stored["id"])
    if first is None:
        return None
    for kind, key in first["snapshots"].items():
        if kind in ("rules", "member rules", "member algorithms") or kind.startswith("pattern:"):
            sources.preset[kind] = gr.snapshot(key)
    held = first["snapshots"].get("algorithms")
    return gr.snapshot(held) if held else _FIRST_VERSIONS


def _for_earlier_request(request: ExportRequest, stored: dict[str, Any],
                         sources: _Sources) -> tuple[dict[str, int], str]:
    """A new file for the people of an earlier request.

    Its pattern is the request's, as it was; its people are made by the rules
    and algorithm versions its first file was made with, so a FHIR file on day
    25 names the people of the 834 written on day 1. What only this file type
    reads -- its layout or sample config -- is today's. Returns the versions
    to pin and what the request's people are: members, or claims."""
    from sdg.analyzer.services import generation_requests as gr

    first = gr.first_file(stored["id"])
    if first is None:
        raise HTTPException(status_code=409,
                            detail=f"Run {stored['id']} has no file to start from.")
    key = stored["pattern_key"]
    pattern = gr.snapshot(stored["pattern_hash"]) if stored.get("pattern_hash") else None
    if key.isdigit():
        request.pattern_id, request.pattern, request.scenarios = int(key), None, None
    elif key.startswith("live:") and pattern is not None:
        request.pattern_id, request.scenarios = None, None
        request.pattern = LivePattern(**pattern)
    elif key.startswith("scenarios:") and pattern is not None:
        request.pattern_id, request.pattern = None, None
        request.scenarios = [ScenarioPart(**p) for p in pattern]

    for kind, digest_key in first["snapshots"].items():
        if kind == "rules" or kind.startswith("pattern:"):
            sources.preset[kind] = gr.snapshot(digest_key)
    held = first["snapshots"].get("algorithms")
    return (gr.snapshot(held) if held else _FIRST_VERSIONS), first["subject"]


def _file_subject(request: ExportRequest) -> str:
    """Whether the file asked for is of members or of claims, before writing it."""
    if _modeled(request.format):
        from sdg.writers import samples

        modeled = _modeled_config(request.format, request.sample_config)
        return samples.module(modeled["format"]).subject(modeled["kind"])
    if request.format.startswith("edi"):
        from sdg.writers.edi_formats import load_format

        legacy = {"edi834": "834", "edi837": "837P"}
        return load_format(legacy.get(request.format) or request.format.split(":", 1)[-1]).subject
    return request.subject


def _parent_members(parent_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """An earlier request of members, and its first file: what its people were made with."""
    from sdg.analyzer.services import generation_requests as gr

    parent = gr.get(parent_id)
    first = gr.first_file(parent_id) if parent is not None else None
    if parent is None or first is None:
        raise HTTPException(status_code=404, detail=f"No run {parent_id} with a file.")
    if not packs.is_primary(first["subject"]):
        raise HTTPException(status_code=409, detail=(
            f"Run {parent_id} is claims: claims are written for a run of members."))
    return parent, first


def _preset_members(parent_id: int, sources: _Sources) -> None:
    """The members' pattern as their request found it."""
    from sdg.analyzer.services import generation_requests as gr

    _parent, first = _parent_members(parent_id)
    for kind, key in first["snapshots"].items():
        if kind.startswith("pattern:"):
            sources.preset[kind] = gr.snapshot(key)


def _request_members(parent_id: int) -> list[dict[str, Any]]:
    """The members of an earlier request, every element made as its first file
    made them: its seed, its rules and its algorithm versions. A claim copies
    its member, so each claim names the member exactly as the 834 did."""
    from sdg.analyzer.services import generation_requests as gr
    from sdg.engine.generators import algorithm_versions, pinned
    from sdg.writers import elements

    parent, first = _parent_members(parent_id)
    member_rules = _source("member rules", lambda: gr.snapshot(first["snapshots"]["rules"]))
    versions = _source("member algorithms", lambda: (
        gr.snapshot(first["snapshots"]["algorithms"]) if "algorithms" in first["snapshots"]
        else {name: 1 for name in algorithm_versions()}))
    asked = first["params"]
    members = ExportRequest(**{k: asked[k] for k in (
        "members", "rows", "pattern_id", "pattern", "scenarios", "as_of") if k in asked})
    members.subject = packs.active().primary
    shape = _store_shape()
    claim_level, line_level = elements.store_claim_elements(shape)
    token = _REQUEST_SEED.set(parent["seed"])
    try:
        with pinned(versions):
            if members.scenarios:
                records, rules = _scenario_records(members, dict(member_rules))
            else:
                records, rules = _export_records(members, dict(member_rules))
            leading = DEFAULT_ELEMENTS[packs.active().primary]
            skip = {*claim_level, *line_level,
                    *(a for a in packs.active().child_anchors() if a)}
            wanted = [e for e in dict.fromkeys([*leading, *sorted(rules)])
                      if e in rules and e not in skip]
            return list(elements.complete(records, wanted, rules, seed=parent["seed"],
                                          as_of=members.as_of))
    finally:
        _REQUEST_SEED.reset(token)


def _claims_for_request(request: ExportRequest, rules: dict[str, dict]):
    """Claims of a claims pattern for the members of an earlier request.

    The members are the request's, made as it made them; whatever the claims
    pattern says of a member -- gender, age, plan -- gives way to the member,
    who holds it already. What it says of a claim is the claim's."""
    from sdg.writers import elements

    members = _request_members(request.of_request)
    row = _pattern_row(request.pattern_id)
    if row is None or not row.get("profile_json"):
        raise HTTPException(status_code=404, detail=f"No pattern {request.pattern_id}.")
    profile = json.loads(row["profile_json"])
    if not profile.get("claims_pattern"):
        child = packs.active().term(packs.active().child, "many")
        article = "an" if child[:1].lower() in "aeiou" else "a"
        raise HTTPException(status_code=422, detail=(
            f"{row.get('contextual_name') or request.pattern_id} is not {article} "
            f"{child} pattern."))
    rules = elements.with_pattern(rules, elements.pattern_rules(
        profile, service.catalog, kind=_kind(packs.active().child), as_of=request.as_of))
    per_member, per_claim = _claim_shape(profile, True)
    per_claim = request.lines_per_claim or per_claim
    claim_level, line_level = elements.store_claim_elements(_store_shape())
    claims_of, lines_of = _how_many(request, per_member, per_claim, always=True)
    return elements.claims_for(
        members, rules, claim_level=claim_level, line_level=line_level,
        lines_per_claim=per_claim, seed=_file_seed(), as_of=request.as_of,
        limit=request.rows, claims_of=claims_of, lines_of=lines_of,
    ), rules


def _how_many(request: ExportRequest, per_member: int, per_claim: int, *, always: bool):
    """How many claims each member has and how many lines each claim, as the
    request asks: exactly, from one count to another, or a mix; else as the
    pattern measured. Without `always`, None where nothing was asked."""
    from sdg.writers import elements
    from sdg.writers.samples import SampleError
    from sdg.writers.samples.engine import read_mix

    try:
        claims_mix = read_mix(request.claims_mix, "claims each")
        lines_mix = read_mix(request.lines_mix, "lines each")
    except SampleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    asked_claims = (always or claims_mix or request.claims_min or request.claims_max
                    or request.claimed_share < 100)
    claims_of = None
    if asked_claims:
        low = request.claims_min or per_member
        high = max(low, request.claims_max or low)
        claims_of = elements.claims_of(_file_seed(), request.claimed_share, low, high, claims_mix)
    lines_of = None
    if lines_mix or request.lines_min or request.lines_max:
        low = request.lines_min or per_claim
        high = max(low, request.lines_max or low)
        lines_of = elements.lines_of(_file_seed(), low, high, lines_mix)
    return claims_of, lines_of


@app.get("/api/patterns/{pattern_id}/claim-shape", dependencies=[_ANALYSIS, _GENERATION])
def claim_shape(pattern_id: int) -> dict[str, Any]:
    """How many claims its members have and how many lines its claims, as the
    pattern's own claims have them: mixes a file can be written with, so the
    request keeps the shares and never needs the claims again."""
    from sdg.analyzer.services import pattern_data

    try:
        frame = pattern_data.rows_for(pattern_id)["df"]
    except pattern_data.StalePattern as e:
        raise HTTPException(status_code=409, detail=str(e))
    # The child records' key and their primary record's (child_analysis:
    # health's CLCL_ID and MEME_CK, a bank's ACCOUNT_ID and CUSTOMER_ID).
    from sdg.analyzer.services.claims_service import PRIMARY_KEY, RECORD_KEY

    if not {RECORD_KEY, PRIMARY_KEY} <= set(frame.columns):
        child = packs.active().term(packs.active().child, "many")
        raise HTTPException(status_code=422,
                            detail=f"This pattern's rows are not {child}: nothing to measure.")
    claims = frame.groupby(PRIMARY_KEY)[RECORD_KEY].nunique()
    lines = frame.groupby(RECORD_KEY).size()
    return {"claims_each": _measured_mix(claims), "lines_each": _measured_mix(lines),
            "members": int(len(claims)), "claims": int(len(lines))}


def _measured_mix(counts) -> list[list[float]]:
    """Each count with its share, in percent: a count of more than 50 is
    written as 50, and a count in fewer than 1 in 1,000 is left out."""
    shares = (counts.clip(upper=50).value_counts(normalize=True) * 100).sort_index()
    return [[int(n), round(float(s), 1)] for n, s in shares.items() if s >= 0.1]


class ClaimsCheck(BaseModel):
    pattern_id: int
    as_of: str = "2026-06-30"


@app.post("/api/generation-requests/{request_id}/claims-check",
          dependencies=[_ANALYSIS, _GENERATION])
def claims_check(request_id: int, asked: ClaimsCheck) -> dict[str, Any]:
    """What a claims pattern says of a member that this request's members
    supersede, and how many members fall outside what it was found on: a
    pregnancy pattern for men, a pediatric one for adults."""
    from collections import Counter

    from sdg.writers import elements

    sources = _Sources()
    _preset_members(request_id, sources)
    token = _SOURCES.set(sources)
    try:
        members = _request_members(request_id)
    finally:
        _SOURCES.reset(token)
    row = _pattern_row(asked.pattern_id)
    if row is None or not row.get("profile_json"):
        raise HTTPException(status_code=404, detail=f"No pattern {asked.pattern_id}.")
    claim_rules = elements.pattern_rules(json.loads(row["profile_json"]), service.catalog,
                                         kind=_kind(packs.active().child), as_of=asked.as_of)
    held = {e for m in members[:1] for e, v in m.items() if v not in (None, "")}
    features = []
    for element, rule in claim_rules.items():
        if element not in held:
            continue
        values = [str(m.get(element) or "") for m in members]
        inside = _within(rule, asked.as_of)
        if inside is None:
            continue
        outside = sum(1 for v in values if not inside(v))
        features.append({
            "element": element,
            "pattern": _says(rule, asked.as_of),
            "request": _shares(Counter(_as_age(v, asked.as_of) if element == "DOB" else v
                                       for v in values), len(values),
                               ages=element == "DOB"),
            "outside": outside,
        })
    return {"members": len(members), "features": features,
            "outside": max((f["outside"] for f in features), default=0)}


def _window_days(rule: dict[str, Any], as_of: str) -> tuple[Any, Any] | None:
    """The first and last day a date rule draws from."""
    from datetime import date, timedelta

    anchor_text = str(rule.get("format_pattern") or "").strip()[:10] or as_of[:10]
    try:
        anchor = date.fromisoformat(anchor_text)
    except ValueError:
        return None
    if rule.get("min_value") not in (None, "") and rule.get("max_value") not in (None, ""):
        low, high = sorted((int(float(rule["min_value"])), int(float(rule["max_value"]))))
    else:
        span = int(float(rule.get("start_value") or 3650))
        low, high = -max(span - 1, 0), 0
    return anchor + timedelta(days=low), anchor + timedelta(days=high)


def _within(rule: dict[str, Any], as_of: str):
    """Whether a value is one the rule could make, or None where that cannot be said."""
    from datetime import date

    method = str(rule.get("gen_method") or "").lower()
    if method == "constant":
        # A pattern all of one value: female, every one of them.
        only = str(rule.get("domain") or "").strip()
        return lambda v: v.strip() == only
    if method == "lookup" and isinstance(rule.get("values"), list) \
            and all(not isinstance(v, dict) for v in rule["values"]):
        allowed = {str(v).strip() for v in rule["values"]}
        return lambda v: v.strip() in allowed
    if method == "date":
        window = _window_days(rule, as_of)
        if window is None:
            return None
        first, last = window

        def inside(v: str) -> bool:
            try:
                return first <= date.fromisoformat(v[:10]) <= last
            except ValueError:
                return True
        return inside
    return None


def _as_age(value: str, as_of: str) -> str:
    from datetime import date

    try:
        born, on = date.fromisoformat(value[:10]), date.fromisoformat(as_of[:10])
    except ValueError:
        return value
    return str(on.year - born.year - ((on.month, on.day) < (born.month, born.day)))


def _says(rule: dict[str, Any], as_of: str) -> str:
    """What a claims pattern's rule says of a member, in a person's words."""
    method = str(rule.get("gen_method") or "").lower()
    if method == "date":
        first, last = _window_days(rule, as_of) or (None, None)
        if first is None:
            return "a date"
        return f"aged {_as_age(last.isoformat(), as_of)} to {_as_age(first.isoformat(), as_of)}"
    if method == "constant":
        return f"{rule.get('domain') or 'blank'} 100%"
    values = [str(v) for v in rule.get("values") or []]
    weights = rule.get("weights") or []
    if len(weights) == len(values) and values:
        total = sum(float(w) for w in weights) or 1
        return ", ".join(f"{v} {float(w) / total:.0%}" for v, w in zip(values, weights))
    return ", ".join(values)


def _shares(counts, total: int, ages: bool = False) -> str:
    if not total:
        return "no members"
    if ages:
        known = sorted(int(a) for a in counts if str(a).isdigit())
        return f"aged {known[0]} to {known[-1]}" if known else "ages unknown"
    return ", ".join(f"{v or 'blank'} {n / total:.0%}" for v, n in counts.most_common())


def _refuse_other_subject(stored: dict[str, Any], kept: str, wanted: str) -> None:
    raise HTTPException(
        status_code=409,
        detail=f"Run {stored['id']} is {kept}: write its {kept} in any format. "
               f"{wanted.capitalize()} for its people are not written from a run yet.")


class _FirstVersions(dict):
    """Version 1 of every algorithm: what a file kept before versions were
    recorded was made with."""

    def get(self, key, default=None):
        return 1


_FIRST_VERSIONS = _FirstVersions()


def _pinned_stream(stream, versions: dict[str, int]):
    """A file's values are made while it streams: each part of it is made with
    the pinned versions, whichever thread the response reads it on."""
    from sdg.engine.generators import pinned

    parts = iter(stream)
    while True:
        with pinned(versions):
            try:
                part = next(parts)
            except StopIteration:
                return
        yield part


def _request_pattern(request: ExportRequest, sources: _Sources) -> Any:
    """The pattern as the request found it, kept with the request."""
    if request.pattern_id is not None:
        return sources.used.get(f"pattern:{request.pattern_id}")
    if request.pattern is not None:
        return request.pattern.model_dump()
    if request.scenarios:
        return [p.model_dump() for p in request.scenarios]
    return None


def _request_pattern_name(request: ExportRequest) -> str | None:
    if request.pattern_id is not None:
        row = _pattern_row(request.pattern_id) or {}
        name = row.get("contextual_name") or f"Pattern {request.pattern_id}"
        if request.of_request is not None:
            return f"{name} · claims for run {request.of_request}"
        return name
    if request.pattern is not None:
        return str(request.pattern.profile.get("name")
                   or request.pattern.profile.get("contextual_name") or "A pattern just analyzed")
    if request.scenarios:
        return " + ".join(p.label for p in request.scenarios)
    return "The plan's run"


def _who() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return "user"


_BUILD: str | None = None

# What a build is: SDM's code and the profiles, layouts, samples and SQL it
# ships. Not what it writes while running -- the analyzer's cache of frames,
# compiled bytecode -- which changes every session without the code changing.
_BUILD_SUFFIXES = {".py", ".json", ".txt", ".hl7", ".xml", ".sql", ".md", ".j2", ".csv"}
_NOT_BUILD = {"__pycache__", "cache"}


def _build_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in _BUILD_SUFFIXES
                  and not _NOT_BUILD & set(p.relative_to(root).parts[:-1]))


def _sdm_build() -> str:
    """A digest of SDM's code and the profiles, layouts and samples it ships:
    two files from the same build were made by the same algorithms."""
    global _BUILD
    if _BUILD is None:
        import hashlib

        import sdg

        root = Path(sdg.__file__).resolve().parent
        digest = hashlib.sha256()
        for path in _build_files(root):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        _BUILD = digest.hexdigest()[:16]
    return _BUILD


@app.get("/api/generation-requests")
def generation_request_list(day: str | None = None, pattern_id: int | None = None,
                            user: str | None = None) -> dict[str, Any]:
    """Requests with the files written under them, newest first."""
    from sdg.analyzer.services import generation_requests as gr

    try:
        requests = gr.list_requests(day=day, pattern_id=pattern_id, user=user)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    for request in requests:
        for file in request["files"]:
            file["rerun_refusal"] = _rerun_refusal(file)
            file["rerunnable"] = file["rerun_refusal"] is None
    return {"requests": requests, "build": _sdm_build(), "version": __version__}


def _rerun_refusal(file: dict[str, Any]) -> str | None:
    """Why Rerun can't write this file again, said before it is pressed: only
    a run of an unsaved configuration can be refused for what it kept."""
    from sdg.analyzer.services import generation_requests as gr

    if not str(file.get("format") or "").startswith("unsaved:"):
        return None
    try:
        kept = gr.get_file(int(file["id"]))
        key = kept["snapshots"].get(kept["format"]) if kept else None
        if key is None:
            return None  # Rerun says what is missing
        return unsaved_rerun_refusal(gr.snapshot(key))
    except LookupError:
        return None  # Rerun says the snapshot is missing


@app.post("/api/generation-requests/files/{file_id}/again")
def generation_file_again(file_id: int) -> StreamingResponse:
    """A file written again from its request's record alone: the same seed,
    the pattern and everything it was written through as they were then."""
    original = original_file(file_id)
    if original["format"] == "database":
        return _regenerated_load(original)
    stream, media = write_again(original)
    headers = {"Content-Disposition": _attachment(original["file_name"]),
               "X-SDM-Request": str(original["request_id"])}
    return StreamingResponse(stream, media_type=media, headers=headers)


def original_file(file_id: int) -> dict[str, Any]:
    """The file a request kept, as first written: a file written again names it."""
    from sdg.analyzer.services import generation_requests as gr

    original = gr.get_file(file_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"No file {file_id} in any run.")
    if original.get("replay_of") is not None:
        original = gr.get_file(original["replay_of"]) or original
    return original


def write_again(original: dict[str, Any]):
    """A kept file written again from its request's record alone: its parts and type."""
    export_request = ExportRequest(**original["params"], request_id=original["request_id"],
                                   record=True)
    try:
        stream, media, _filename, kept = _request_stream(export_request, replay=original)
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _primed(kept.recorded(stream, original["file_name"])), media


def _regenerated_load(original: dict[str, Any]) -> Response:
    """A load's rows made again as evidence, as CSVs; no database is written."""
    from sdg.api import load_requests
    from sdg.engine.execute import NeverGenerated

    try:
        body, name, evidence = load_requests.regenerate(
            original, load_references=service.reference_sets_with_sources,
            who=_who(), version=__version__, build=_sdm_build())
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except NeverGenerated as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # said, rather than a stack trace
        raise HTTPException(status_code=500, detail=plain(e)) from e
    return Response(body, media_type="application/zip", headers={
        "Content-Disposition": _attachment(name),
        "X-SDM-Request": str(original["request_id"]),
        "X-SDM-Matched": "1" if evidence["matched"] else "0",
    })


def _export_stream(request: ExportRequest):
    """The file an export writes, as a stream of text, with its type and name.

    Separate from the response so the SNIP checks read exactly the same file
    the download sends.
    """
    from sdg.analyzer.services.layout_service import rules_by_element
    from sdg.writers import elements
    from sdg.writers import export as export_writer

    rules = _source("rules", rules_by_element)
    modeled = None
    if _modeled(request.format):
        # A file modeled on a sample: its kind says what is written -- new
        # members for an 834, the pattern's members' claims for an 837. A
        # standard file is written as its tab proposes it, nothing saved.
        from sdg.writers import samples

        modeled = _modeled_config(request.format, request.sample_config)
        # A run of an unsaved configuration written again needs its sample:
        # refused here, before any member is made, when it can't have it.
        sample_text = _kept_sample_text(modeled)
        try:
            sample_format = samples.module(modeled["format"])
        except samples.SampleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        request.subject = sample_format.subject(modeled["kind"])
    if request.scenarios:
        records, rules = _scenario_records(request, rules)
    else:
        records, rules = _export_records(request, rules)
    seed = (_file_seed() if (request.pattern_id is not None or request.pattern is not None
                             or request.scenarios)
            else _source("plan", lambda: service.require_plan().to_dict())["meta"]["seed"])

    if modeled is not None:
        from sdg.writers.samples import engine as sample_engine

        try:
            sample = sample_format.read_sample(sample_text)
            entries = sample_format.read_config(modeled["config"])
            vary = sample_engine.read_vary(modeled["config"])
        except samples.SampleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        all_rules = samples.algorithm_rules(rules)
        wanted = sorted(name for name in sample_format.used(entries) if name in all_rules)
        stream = sample_format.stream(
            sample, entries,
            elements.complete(records, wanted, all_rules, seed=seed, as_of=request.as_of),
            all_rules, seed=seed, as_of=request.as_of,
            **({"vary": vary} if vary and hasattr(sample_format, "structure") else {}),
        )
        media, extension = sample_format.MEDIA, sample_format.EXTENSION
        if hasattr(sample_format, "package"):
            if request.packaged:
                stream = sample_format.package(stream, modeled["kind"])
            else:
                media, extension = sample_format.TEXT_MEDIA, ".xml"
        filename = f"{modeled['kind']}-{modeled['name']}{extension}".replace(" ", "_")
    elif request.format.startswith("edi"):
        from sdg.writers.edi_formats import load_format, stream_format

        legacy = {"edi834": "834", "edi837": "837P"}
        wanted = legacy.get(request.format) or request.format.split(":", 1)[-1]
        chosen = load_format(wanted)
        stream = stream_format(
            chosen, records, as_of=request.as_of,
            per_transaction=request.per_transaction, rules=rules, seed=seed,
        )
        media = "application/edi-x12"
        filename = f"{chosen.id.lower()}.txt"
    elif request.format == "fixed":
        fields = _layout_fields(request.layout_id)
        names = _layout_elements(fields)
        rows = elements.complete(records, _named(names), rules,
                                 seed=seed, as_of=request.as_of)
        stream = export_writer.stream_fixed_width(
            (_layout_row(r, fields, names) for r in rows), fields,
        )
        media, filename = "text/plain", "extract.txt"
    else:
        delimiter = ","
        if request.layout_id is not None:
            fields = _layout_fields(request.layout_id)
            names = _layout_elements(fields)
            columns = [f["name"] for f in fields]
            # The layout says what separates its fields; a pipe-delimited
            # layout written with commas is a file its reader cannot split.
            delimiter = _layout_delimiter(request.layout_id)
        else:
            columns = DEFAULT_ELEMENTS.get(request.subject, DEFAULT_ELEMENTS[packs.active().primary])
            if request.scenarios:
                # Which scenario each row belongs to.
                columns = ["SCENARIO", *columns]
            fields = [{"name": c} for c in columns]
            names = {c: c for c in columns}
        rows = elements.complete(records, _named(names), rules,
                                 seed=seed, as_of=request.as_of)
        stream = export_writer.stream_delimited(
            (_layout_row(r, fields, names) for r in rows), columns,
            delimiter=delimiter,
        )
        media = "text/csv" if delimiter == "," else "text/plain"
        filename = f"{request.subject}.{'csv' if delimiter == ',' else 'txt'}"

    return stream, media, filename


def _scenario_records(request: ExportRequest, base: dict[str, dict]):
    """Every chosen scenario's records, in one stream, each tagged with its scenario.

    Each scenario is a pattern of its own rows; its records carry its own
    values, completed here from its own rules, and the rest is filled from
    the shared ones afterwards -- across the whole file, so the people in one
    scenario are never the people in another.
    """
    from sdg.writers import elements

    out: list[dict[str, Any]] = []
    for part in request.scenarios or []:
        sub = request.model_copy(update={
            "scenarios": None, "pattern_id": part.pattern_id, "pattern": None,
            "members": max(1, part.count),
            "rows": max(1, part.count) if packs.is_child(request.subject) else None,
        })
        # Each scenario's families from a seed of its own: the same seed at
        # the same places would give two scenarios the same subscribers.
        records, rules = _export_records(
            sub, dict(base), members_seed=f"{_file_seed()}:{part.pattern_id}:{part.label}")
        own = [e for e, rule in rules.items() if base.get(e) is not rule]
        for record in elements.complete(records, own, rules,
                                        seed=f"{_file_seed()}:{part.pattern_id}",
                                        as_of=request.as_of):
            record["SCENARIO"] = part.label
            out.append(record)
    return out, base


@app.get("/api/patterns/scenario-dimensions", dependencies=[_ANALYSIS])
def scenario_dimensions(entity: str = packs.active().primary) -> dict[str, Any]:
    """The features a pattern can be split into scenarios on, and the split
    used until somebody chooses one."""
    from sdg.analyzer.services import scenarios

    return {"dimensions": scenarios.dimensions(entity),
            "defaults": scenarios.DEFAULT_SPLIT.get(entity, [])}


class ScenarioRequest(BaseModel):
    """A pattern, addressed as its analysis or by its id, and what to split it on."""

    domain: str = packs.active().primary
    filters: dict[str, Any] = {}
    k: int | None = None
    cluster_id: int = 0
    claim_type: str | None = None
    pattern_id: int | None = None
    dimensions: list[str] = []


@app.post("/api/patterns/scenarios", dependencies=[_ANALYSIS])
def pattern_scenarios(request: ScenarioRequest) -> dict[str, Any]:
    """The combinations of the chosen features the pattern's own rows hold."""
    from sdg.analyzer.services import scenarios

    try:
        frame = scenarios.rows_of(request.domain, request.filters, request.k,
                                  request.cluster_id, request.claim_type, request.pattern_id)
        return scenarios.list_scenarios(request.domain, request.dimensions, frame)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


class KeepScenarios(ScenarioRequest):
    name: str
    scenarios: list[dict[str, str]]


@app.post("/api/patterns/scenarios/keep", dependencies=[_ANALYSIS])
def keep_scenarios(request: KeepScenarios) -> dict[str, Any]:
    """Keep each chosen scenario as a pattern of exactly its rows."""
    from sdg.analyzer.services import scenarios

    if not request.scenarios:
        raise HTTPException(status_code=422, detail="Choose at least one scenario.")
    try:
        kept = scenarios.keep_scenarios(
            request.domain, request.dimensions, request.scenarios, request.filters,
            request.k, request.cluster_id, request.claim_type, request.name,
            request.pattern_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"kept": kept}


def _layout_fields(layout_id: int | None) -> list[dict[str, Any]]:
    """The fields of a layout that may write files: a saved one, or, where a
    second approver is configured, an approved one. A Rerun writes through
    the layout as it was kept, whatever its status now."""
    if layout_id is None:
        raise HTTPException(status_code=422, detail="This format needs a layout.")
    from sdg.analyzer.services import layout_service
    from sdg.api.routers.layouts import approval_required

    layout = _source(f"layout:{layout_id}", lambda: layout_service.get_layout(layout_id))
    if layout is None:
        raise HTTPException(status_code=404, detail=f"No layout {layout_id}.")
    sources = _SOURCES.get()
    if sources is None or sources.replay is None:
        status = layout.get("status")
        if status == "archived":
            raise HTTPException(status_code=409,
                                detail=f"Layout {layout.get('name')} is deleted.")
        if status != "approved" and approval_required():
            raise HTTPException(
                status_code=409,
                detail=f"Layout {layout.get('name')} is pending approval. Approve it first.",
            )
    return layout.get("fields") or []


def _layout_delimiter(layout_id: int) -> str:
    """What separates a delimited layout's fields; a comma if it never said."""
    from sdg.analyzer.services import layout_service

    layout = _source(f"layout:{layout_id}", lambda: layout_service.get_layout(layout_id)) or {}
    return layout.get("delimiter") or ","


def _layout_elements(fields: list[dict[str, Any]]) -> dict[str, str | None]:
    """Each layout field's element: the rule it is bound to, or its own name.

    A field names what goes in it the same way an EDI position does -- by the
    rule. A field bound to none is read as an element of its own name, which
    is what a layout defined from the rule list already is. A field somebody
    chose to leave blank has no element at all, whatever it is called.
    """
    from sdg.analyzer.services import layout_service

    by_id = {int(r["id"]): str(r["field_name"]).strip().upper()
             for r in _source("rule names", layout_service.get_all_rules)}
    out: dict[str, str | None] = {}
    for f in fields:
        rule_id = f.get("rule_id")
        if f.get("blank"):
            out[f["name"]] = None
        elif rule_id is not None and int(rule_id) in by_id:
            out[f["name"]] = by_id[int(rule_id)]
        else:
            out[f["name"]] = str(f["name"]).strip().upper()
    return out


def _named(names: dict[str, str | None]) -> list[str]:
    """The elements a layout's fields name, once each, blanks left out."""
    return list(dict.fromkeys(e for e in names.values() if e))


def _layout_row(record: dict[str, Any], fields: list[dict[str, Any]],
                names: dict[str, str | None]) -> dict[str, Any]:
    """One record as a layout's fields read it, each in the field's own form."""
    from sdg.writers.export import layout_value

    return {
        f["name"]: (layout_value(record.get(names[f["name"]]), f)
                    if names[f["name"]] else None)
        for f in fields
    }


class StoryToPlanRequest(BaseModel):
    """Saved patterns a requirement matched, and how much of each is wanted."""

    pattern_ids: list[int]
    total_volume: int = 1000
    as_of: str = "2026-06-30"
    apply: bool = False


@app.post("/api/story/to-plan", dependencies=[_ANALYSIS, _GENERATION])
def story_to_plan(request: StoryToPlanRequest) -> dict[str, Any]:
    """Turn a requirement's matched patterns into cohorts on the plan.

    Story to Data used to run a generator of its own. It does not need one: a
    requirement that names three cohorts and how much of each it wants is a
    description of a population, and a population is what the plan engine makes.
    The volume asked for becomes each pattern's share of the run, so a request
    for a thousand rows with six hundred of one kind generates a plan where that
    kind is sixty per cent -- rather than a separate dataset nobody can validate,
    clean up, or export alongside the rest.
    """
    import json

    from sdg.analyzer import db
    from sdg.analyzer.bridge import to_pattern
    from sdg.analyzer.services import story_service
    from sdg.plan.patterns import apply_to_decisions, resolve_pattern

    if not request.pattern_ids:
        raise HTTPException(status_code=422, detail="No patterns selected.")

    allocation = story_service.suggest_allocation(
        request.pattern_ids, request.total_volume
    )
    total = sum(allocation.values()) or 1

    resolutions = []
    missing: list[int] = []
    for pattern_id in request.pattern_ids:
        row = db.get_pattern(pattern_id)
        if row is None or not row.get("profile_json"):
            missing.append(pattern_id)
            continue
        profile = json.loads(row["profile_json"])
        # The share is what the allocation asked for, not what the pattern
        # covers in the source: the requirement is the thing being satisfied.
        profile["pct_of_total"] = allocation.get(pattern_id, 0) / total
        analyzer_pattern = to_pattern(
            profile,
            kind=_kind(packs.active().child if packs.is_child(pattern_scope.entity_of(row))
                       else packs.active().primary),
            as_of=request.as_of,
            pattern_id=str(row.get("contextual_name") or f"pattern-{pattern_id}"),
        )
        resolutions.append((
            pattern_id,
            _guard(
                resolve_pattern,
                analyzer_pattern,
                catalog=service.catalog,
                mappings=service.require_mappings(),
                plan=service.require_plan(),
                model=service.model,
            ),
        ))

    usable = [r for _id, r in resolutions if r.targets]
    written: list[str] = []
    if request.apply and usable:
        document, written = _guard(
            apply_to_decisions, usable, service.read_decisions(), as_cohort=True,
        )
        service.write_decisions(document)

    return {
        "allocation": {str(k): v for k, v in allocation.items()},
        "total_volume": request.total_volume,
        "patterns": [
            {
                # The id is carried back so the caller can join a row to its
                # allocation without relying on the order they came out in.
                "pattern_id": pattern_id,
                "rows": allocation.get(pattern_id, 0),
                "pattern": r.pattern,
                "subject": r.subject,
                "share": r.support,
                "targets": [t.model_dump(mode="json") for t in r.targets],
                "rejections": [x.model_dump(mode="json") for x in r.rejections],
            }
            for pattern_id, r in resolutions
        ],
        "unreadable": missing,
        "applied": written,
    }


@app.get("/api/analyzer/health")
def analyzer_health() -> dict[str, Any]:
    """Whether the analyzer can reach its schema, and which one it is reading."""
    from sdg.analyzer import config as analyzer_config

    detail = analyzer_config.describe_connection()
    detail["build"] = __version__
    try:
        import pyodbc

        with pyodbc.connect(analyzer_config.get_connection_string(), timeout=5) as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ?",
                analyzer_config.SQL_SCHEMA,
            )
            detail["tables"] = cursor.fetchone()[0]
            detail["ok"] = True
    except Exception as exc:  # reported, not raised: the screen says what is wrong
        detail["ok"] = False
        detail["error"] = f"{plain(exc)}"
    return detail


# ---------------------------------------------------------------------------
# The built interface
# ---------------------------------------------------------------------------

if UI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    # The asset filenames carry a content hash, so they may be cached forever.
    # index.html names them, and must not be: a cached copy keeps pointing at
    # the previous bundle after a rebuild, and the screens added since come up
    # blank with nothing in the console to say why.
    NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}

    def _page() -> Response:
        """index.html, carrying the token the screens send with each call."""
        from sdg.api import guard

        html = (UI_DIST / "index.html").read_text(encoding="utf-8")
        return Response(guard.with_token(html), media_type="text/html", headers=NO_CACHE)

    @app.get("/")
    def index() -> Response:
        return _page()

    @app.get("/{path:path}")
    def spa(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"No such API route: /{path}")
        candidate = (UI_DIST / path).resolve()
        # Only the built interface's own files: never a path out of it
        # ("../../.env").
        inside = candidate.is_relative_to(UI_DIST.resolve())
        if inside and candidate.is_file() and candidate.name != "index.html":
            return FileResponse(candidate)
        return _page()

"""Compute and serve the stored pattern baseline.

Enumeration is deliberately a separate, explicit job. It clusters each group in
turn, records how many patterns fall out and how many members or claims they
cover, and writes the result to SQL Server. Nothing recomputes it on its own —
a denominator that drifts is the problem this exists to solve.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from pydantic import BaseModel

from sdg import packs
from sdg.analyzer.services import baseline
from sdg.api import jobs
from sdg.packs import active, is_child, is_primary

log = logging.getLogger(__name__)

# Pattern analysis, where the business pack offers it (phase 6b: every pack).
router = APIRouter(prefix="/baseline", tags=["baseline"],
                   dependencies=[Depends(packs.offered("pattern_analysis", "Pattern analysis"))])


class ComputeRequest(BaseModel):
    entity: str = active().primary
    # How deep to walk: 1 = the first scope level (health: groups), each more
    # adds the next (plan category, plan, product).
    max_depth: int = 4
    # Safety cap. A wide hierarchy produces thousands of combinations and each
    # one runs a clustering pass.
    max_combinations: int = 2000
    # Limit the walk to the first N groups, for a quick trial run.
    limit_groups: int | None = None


@router.get("")
def read(entity: str | None = Query(None)):
    """Every stored baseline, newest computation timestamps included."""
    rows = baseline.get_all(entity)
    return {
        "baselines": rows,
        "computed": len(rows) > 0,
        "note": (
            "Weights divide by these stored totals. Recompute only when the "
            "source data has genuinely changed."
            if rows else
            "No baseline stored yet. POST /api/baseline/compute to build it."
        ),
    }


@router.post("/compute")
def compute(req: ComputeRequest):
    """Enumerate every pattern across every combination and store the total.

    Synchronous, and only sensible at shallow depth: a full walk is minutes to
    the better part of an hour. `POST /api/baseline/jobs` is the same work on a
    thread, which is what the screen uses.
    """
    return _run(req, None)


def _run(req: ComputeRequest, progress) -> dict:
    if not (is_primary(req.entity) or is_child(req.entity)):
        raise HTTPException(status_code=422, detail=_ENTITY_MUST)
    return _compute(req, progress)


# ── The same walk, on a thread ───────────────────────────────────────────────
#
# A full-depth walk is 340 combinations for members and 329 for claims, each a
# clustering pass. Nothing holds an HTTP request open for that, so the walk runs
# in the background and the caller polls. See sdg/api/jobs.py.

KIND = "baseline"
_ENTITY_MUST = f"entity must be {active().primary!r} or {active().child!r}."


@router.get("/estimate")
def estimate(entity: str = Query(active().primary), max_depth: int = Query(4)):
    """How many combinations a walk would visit, before committing to one.

    Enumeration itself is only hierarchy queries, so this is cheap relative to
    the walk it describes. The per-combination seconds are measured on the demo
    database and are a guide, not a promise -- the job reports a real estimate
    from its own rate once it is moving.
    """
    try:
        if is_primary(entity) or is_child(entity):
            combos = _combinations(entity, max_depth, None, 10_000)
            per_combination = 1.8 if is_primary(entity) else 8.4
        else:
            raise HTTPException(status_code=422, detail=_ENTITY_MUST)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not enumerate: {e}")

    return {
        "entity": entity,
        "max_depth": max_depth,
        "combinations": len(combos),
        "seconds_per_combination": per_combination,
        "estimated_seconds": round(len(combos) * per_combination),
    }


@router.post("/jobs")
def start_job(req: ComputeRequest):
    """Start a walk in the background. Returns immediately with the job."""
    if not (is_primary(req.entity) or is_child(req.entity)):
        raise HTTPException(status_code=422, detail=_ENTITY_MUST)

    # One at a time. Two walks would compete for the same connection pool and
    # the second would make the first slower rather than finishing sooner.
    if jobs.running(KIND):
        raise HTTPException(
            status_code=409,
            detail="A baseline walk is already running. Wait for it or cancel it.",
        )

    job = jobs.start(
        KIND,
        f"{req.entity} baseline, depth {req.max_depth}",
        lambda progress: _run(req, progress),
    )
    return job.as_dict()


@router.get("/jobs/latest")
def latest_job():
    """Whatever a screen should attach to on load: in flight, or last outcome."""
    job = jobs.latest(KIND)
    return job.as_dict() if job else {"state": "none"}


@router.get("/jobs/{job_id}")
def read_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        # A job this process has never heard of, most likely because it was
        # restarted. Saying so beats a bare 404 the screen has to guess at.
        raise HTTPException(
            status_code=404,
            detail="restart_lost: no such job. The server may have restarted; "
                   "any walk in flight was lost and nothing was stored.",
        )
    return job.as_dict()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Ask a walk to stop after the combination it is on.

    Nothing is stored until a walk finishes, so canceling discards the work
    rather than leaving a half-built baseline. That is the safer of the two:
    a denominator summed over part of the hierarchy is wrong in a way nothing
    downstream could detect.
    """
    if not jobs.cancel(job_id):
        raise HTTPException(status_code=409, detail="That job is not running.")
    return {"canceling": job_id}




def _percentiles(sizes: list[int]) -> dict:
    """Cut-offs over every pattern size the walk observed.

    Bands then mean something concrete — "very high" is the top 5% of patterns
    that actually exist in this data, rather than a number someone guessed
    before seeing it.
    """
    if not sizes:
        return {}
    import numpy as np

    arr = np.asarray(sizes, dtype=float)
    return {
        "n_patterns": int(arr.size),
        "p20": float(np.percentile(arr, 20)),
        "p50": float(np.percentile(arr, 50)),
        "p80": float(np.percentile(arr, 80)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(arr.max()),
    }


def _combinations(entity: str, max_depth: int, limit_groups: int | None,
                  cap: int) -> list[dict]:
    """Every scope combination the user could select, and the unfiltered one.

    Patterns from different combinations overlap -- one record belongs to a
    first-level pattern, a pattern of that and the next level, and so on.
    That overlap is the whole point: the baseline is the sum of counts across
    every pattern that exists anywhere, so a single combination's patterns
    are a small share of it rather than 100%. The levels are the pack's
    (scope_walk); limit_groups takes the first N values of the first level.
    """
    from sdg.analyzer import scope_walk

    return ([{}] + scope_walk.every(entity, max_depth, limit_groups, cap - 1))[:cap]


# What a first-level roll-up is stored and shown as (health: "GRGR:10042",
# "group 10042"), and the filter it rolls up by.
_ROLL_UP = packs.held("features:store.baseline_scope")


def _compute(req: ComputeRequest, progress: "jobs.Progress | None" = None) -> dict:
    """Walk every combination, sum every pattern's count, persist the total."""
    from sdg.analyzer import scope_walk
    from sdg.analyzer.services.analysis import for_entity

    found = for_entity(req.entity)
    entity = found.entity
    try:
        combos = _combinations(entity, req.max_depth, req.limit_groups, req.max_combinations)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not enumerate: {e}")

    if progress is not None:
        progress.set_total(len(combos), f"{len(combos)} combinations to walk")

    first = scope_walk.first_filter(entity)
    grand_patterns = grand_count = 0
    per_first: dict[str, dict] = {}
    processed = failed = skipped = 0
    failures: list[dict] = []
    skipped_examples: list[dict] = []
    sizes: list[int] = []   # every pattern seen, for percentile banding

    for combo in combos:
        if progress is not None:
            progress.raise_if_canceled()
        try:
            result = found.analyze(combo, None)
        except ValueError as e:
            # The loader raises this when a combination matches nothing.
            # Empty slices are expected in a sparse scope.
            if progress is not None:
                progress.step()
            skipped += 1
            if len(skipped_examples) < 5:
                skipped_examples.append({"combo": combo, "reason": str(e)[:160]})
            continue
        except Exception as e:
            if progress is not None:
                progress.step()
            log.warning("Baseline combination %s failed", combo, exc_info=True)
            failed += 1
            if len(failures) < 25:
                failures.append({
                    "combo": combo,
                    "error_type": type(e).__name__,
                    "error": str(e)[:400],
                })
            continue

        if found.subset(result) == 0 or not result.get("patterns"):
            if progress is not None:
                progress.step()
            skipped += 1
            if len(skipped_examples) < 5:
                skipped_examples.append({"combo": combo, "reason": "no rows in this combination"})
            continue

        # Sum the patterns themselves, not the subset size. They are the same
        # for a full partition, but summing patterns is what the definition
        # says and stays correct if clustering ever drops rows.
        count = sum(found.size(p) for p in result["patterns"])
        sizes.extend(found.size(p) for p in result["patterns"])
        grand_patterns += result["n_patterns"]
        grand_count += count
        processed += 1
        if progress is not None:
            progress.step(note=f"{processed} walked, {skipped} empty")

        for value in (combo.get(first) or []) if first else []:
            slot = per_first.setdefault(str(value), {"n_patterns": 0, "total_count": 0})
            slot["n_patterns"] += result["n_patterns"]
            slot["total_count"] += count

    # A total summed over part of the hierarchy is wrong in a way nothing
    # downstream could detect, so the stored rows say whether the walk was
    # whole: no combination failed, the cap was not reached, no group limit.
    capped = len(combos) >= req.max_combinations
    complete = failed == 0 and not capped and req.limit_groups is None
    if not complete:
        log.warning(
            "Baseline for %s stored from a partial walk: %d failed, capped=%s, "
            "limit_groups=%s", entity, failed, capped, req.limit_groups)

    for value, agg in per_first.items():
        baseline.upsert(
            entity, f"{_ROLL_UP['prefix']}:{value}", f"{_ROLL_UP['label']} {value}",
            agg["n_patterns"], agg["total_count"],
            detail={"note": f"sum across every combination rooted at this {_ROLL_UP['label']}",
                    "complete": complete},
        )

    baseline.upsert(
        entity, "ALL", "all patterns, all combinations",
        grand_patterns, grand_count,
        detail={
            "combinations_processed": processed,
            "combinations_failed": failed,
            "combinations_skipped": skipped,
            "capped": capped,
            "limit_groups": req.limit_groups,
            "complete": complete,
            "max_depth": req.max_depth,
            "thresholds": _percentiles(sizes),
        },
    )

    return {
        "entity": entity,
        "combinations_enumerated": len(combos),
        "combinations_processed": processed,
        "combinations_skipped": skipped,
        "combinations_failed": failed,
        "skipped_examples": skipped_examples,
        "total_patterns": grand_patterns,
        "total_count": grand_count,
        "thresholds": _percentiles(sizes),
        "capped": capped,
        "failures": failures,
    }

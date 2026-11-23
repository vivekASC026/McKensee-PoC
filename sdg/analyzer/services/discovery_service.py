"""System discovery and saved patterns.

Two things the earlier build had that the rewrite dropped:

  1. System discovery — a naive user opens the portal and, without selecting
     anything, sees the largest patterns across the whole book. This is the
     enumeration walk from discovery.py, ranked by size and returned as a
     landing page. Every pattern is persisted so it can be saved and reused.

  2. Save / retrieve — mark a discovered pattern as saved, list saved patterns,
     and generate from one without re-running the search.

The denominator here is the whole member (or claim) population — the same
`size / total_pop` the original used — so a pattern covering 40% of the book
reads 40%, not a fraction of a percent. That is separate from the enumeration
baseline: discovery ranks patterns against the population; the baseline sums
overlapping patterns for weight banding. Two different questions, two
denominators, and conflating them was the bug.
"""

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext as _nullcontext
from typing import Optional

import numpy as np

try:
    from threadpoolctl import threadpool_limits as _threadpool_limits
except ImportError:  # ships with scikit-learn, but never fail the walk over it
    _threadpool_limits = None

from sdg.analyzer import db
from sdg.analyzer import pattern_scope, scope_walk
from sdg.analyzer.clustering import discover_clusters
from sdg.analyzer.config import DEFAULT_REFERENCE_DATE
from sdg.analyzer import entity_loader
from sdg.analyzer.naming import build_contextual_name
from sdg.analyzer.profiler import build_subset_summary, profile_cluster
from sdg import packs
from sdg.packs import is_child

import time

from .cache import ttl_cache
from .disk_cache import two_tier
from .service import book_size, overview, scope_labels, to_py

log = logging.getLogger(__name__)


@contextmanager
def _persist_connection(label: str):
    """A shared connection for one walk's writes, always released.

    pyodbc opens a transaction on the first write and holds it until commit or
    close. Cleanup used to sit after the walk loop with nothing guarding it, so
    a walk that raised — or a request the browser canceled when the user
    navigated away mid-walk — left this connection idle holding exclusive locks
    on sdm.patterns. Every later read of that table blocked behind it until
    uvicorn was restarted, which presents as the whole application hanging with
    nothing pointing at discovery.

    Yields None when no connection can be opened, which makes upsert_pattern
    fall back to opening its own per write.
    """
    conn = None
    try:
        conn = db.get_raw_connection()
    except Exception as e:
        print(f"[{label}] persisting per-pattern (no shared connection): "
              f"{type(e).__name__}: {e}")

    if conn is None:
        yield None
        return

    try:
        yield conn
        conn.commit()
    except BaseException:
        # BaseException rather than Exception on purpose: a canceled request
        # raises CancelledError, which does not inherit from Exception, and
        # that is precisely the navigate-away case this exists to survive.
        try:
            conn.rollback()
        except Exception:
            # The original error still propagates; this one is said, because
            # a rollback that did not happen can leave locks on sdm.patterns
            # until the close below ends the session.
            log.error("[%s] could not roll back the pattern writes; closing the "
                      "connection to release its locks.", label, exc_info=True)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            log.error("[%s] could not close the pattern connection. If pattern "
                      "pages stop responding, restart the service to release "
                      "its locks.", label, exc_info=True)


def canonical_combo(combo: dict, entity: str | None = None) -> dict:
    """Normalize a filter combo so the same pattern always yields one key --
    and so two record kinds over one scope never collide.

    upsert_pattern keys on the pack's scope levels (store.combo_key) and the
    cluster id. Two problems this fixes:

      1. Representation drift -- [] vs None, ints vs strings, unsorted lists --
         made the same pattern store twice. Everything is normalized.

      2. Record-kind collision -- a primary and a child pattern from the same
         scope and cluster produced an identical key, so they became one row
         and unsaving one unsaved the other. Where the pack names a level to
         fold a tag into (store.entity_tag; health: the plan category), the
         tag goes there, without disturbing a real selection of that level;
         elsewhere the record key goes in front of the key (pattern_scope).

    The names the scope was shown under come in as `_labels` ({level: name})
    or under the pack's label columns, and go out under both.
    """
    from sdg.analyzer import pattern_scope as scope

    entity = scope._known(entity)

    def norm(v):
        if not v:
            return None
        return sorted(str(x) for x in v) or None

    out: dict = {key: norm(combo.get(key)) for key in scope.SCOPE_KEYS}
    tag = scope.tag_of(entity)
    if tag:
        out[scope.TAG_COLUMN] = (out.get(scope.TAG_COLUMN) or []) + [tag]
    given = combo.get("_labels") or {}
    labels = {key: given.get(key) or combo.get(column, "")
              for key, column in scope.LABEL_COLUMNS.items()}
    labels.update({k: v for k, v in given.items() if k not in labels})
    for key, column in scope.LABEL_COLUMNS.items():
        out[column] = labels.get(key, "")
    out["_labels"] = labels
    out["_entity"] = entity
    return out


# How many combinations are analyzed at once.
#
# Measured on this book — 42 combinations, 16 logical cores, load + cluster:
#
#     serial      178.7s      threads x8    106.8s
#     threads x4   87.5s      threads x16   111.6s
#
# Past four it gets slower again. Each combination runs ~37 KMeans fits (a
# 9-value k sweep at n_init=3, then a final fit at n_init=10) and the
# orchestration around each fit is Python, so the extra threads spend their time
# queueing for the GIL rather than clustering.
#
# A process pool reached 66.0s with a warm pool, but inside uvicorn it would pay
# a 6-18s spawn on Windows — every worker re-imports pandas and sklearn — and
# add a pool lifecycle to get wrong on a canceled request, which is a bug this
# module has already had once. Threads are the better trade given the walk's
# result is cached for a day and a half either way.
#
# Override with SDM_DISCOVERY_WORKERS; 1 restores the serial walk exactly.
try:
    DISCOVERY_WORKERS = max(1, int(os.environ.get("SDM_DISCOVERY_WORKERS") or 4))
except ValueError:
    DISCOVERY_WORKERS = 4


# Only one worker queries at a time.
#
# The clustering parallelizes; the queries do not. SQL Server Express is capped
# at four cores and one instance serves every worker, so four concurrent loads
# do not overlap — they queue, and badly: the same 42 loads cost 9.1s of
# aggregate time run serially and 35.1s run four ways. That is 26s of pure
# contention, paid on every cold walk.
#
# Holding the lock across the query alone still lets the other three workers
# cluster while one loads, so the loads end up hidden behind CPU work rather
# than competing with each other. It costs nothing when DISCOVERY_WORKERS is 1.
_LOAD_LOCK = threading.Lock()


def _analyze_combo(combo: dict) -> Optional[dict]:
    """Load and cluster one combination. Pure analysis — writes nothing.

    Runs on a worker thread, so it touches no shared state: entity_loader.load
    opens its own connection per call, and the profiler and clustering helpers
    work only on the frame handed to them. Persisting is left to the caller,
    which does it serially on the one shared connection.

    Returns None when the combination has no rows, so the caller can count it as
    empty without unpacking anything.
    """
    # Each worker gets one BLAS thread. sklearn would otherwise start its own
    # OpenMP pool per fit — 4 workers x 16 threads on 16 cores is oversubscribed,
    # and the combinations here are small enough (a few thousand rows) that the
    # inner parallelism earns nothing anyway.
    limits = _threadpool_limits(1) if _threadpool_limits else _nullcontext()

    # Waiting for the lock is not query time. Timed apart, or every extra worker
    # appears to make the database slower — at twelve workers the wait alone
    # summed to 437s and buried the 9s of actual querying underneath it.
    t_wait = time.perf_counter()
    try:
        with _LOAD_LOCK:
            wait_ms = (time.perf_counter() - t_wait) * 1000
            t0 = time.perf_counter()
            got = entity_loader.load(packs.active().primary, combo,
                                     reference_date=DEFAULT_REFERENCE_DATE)
            members, labels, family, filters_used = got.rows, got.labels, got.family, got.filters
            load_ms = (time.perf_counter() - t0) * 1000
    except ValueError as exc:
        if entity_loader.is_no_rows(exc):
            # No rows is an empty combination, not a failure.
            return {"combo": combo, "load_ms": (time.perf_counter() - t_wait) * 1000,
                    "wait_ms": 0.0, "cluster_ms": 0.0, "empty": True,
                    "failed": False, "clusters": []}
        # A filter or configuration the loader refused: said, not hidden.
        log.warning("Discovery skipped the combination %s: %s", combo, exc)
        return {"combo": combo, "load_ms": (time.perf_counter() - t_wait) * 1000,
                "wait_ms": 0.0, "cluster_ms": 0.0, "empty": False,
                "failed": True, "clusters": []}

    if members is None or len(members) == 0:
        return {"combo": combo, "load_ms": load_ms, "wait_ms": wait_ms,
                "cluster_ms": 0.0, "empty": True, "failed": False,
                "clusters": []}

    t0 = time.perf_counter()
    with limits:
        assignments, metrics = discover_clusters(
            members, labels, k=None, filters_used=filters_used,
        )
    cluster_ms = (time.perf_counter() - t0) * 1000

    summary = build_subset_summary(members, filters_used)
    labels = scope_labels(summary)

    clusters = []
    for cid in sorted({int(a) for a in assignments if a >= 0}):
        mask = np.asarray(assignments) == cid
        profile = profile_cluster(members, family, mask, cid, len(members))
        clusters.append({
            "cluster_id": cid,
            "profile": profile,
            "size": int(profile["size"]),
            "name": build_contextual_name(labels, cluster_id=cid, profile=profile),
            "silhouette": float(metrics.get("silhouette") or 0.0),
        })

    return {"combo": combo, "load_ms": load_ms, "wait_ms": wait_ms,
            "cluster_ms": cluster_ms, "empty": False, "failed": False,
            "rows": len(members), "labels": labels,
            "clusters": clusters}


def _analyze_claims_combo(combo: dict) -> dict:
    """Load, cluster and profile one claims combination. Writes nothing.

    Splits the load from the clustering. They used to arrive as one number that
    _timing then printed under "load", which read as a walk bottlenecked on SQL.
    It is not: measured across six combinations, the load is 1.7s and the
    clustering 46.7s — 4% against 96%. Reporting them apart is what makes that
    visible instead of misleading.
    """
    from . import claims_service as cs

    limits = _threadpool_limits(1) if _threadpool_limits else _nullcontext()

    t0 = time.perf_counter()
    try:
        df, _filters_used = cs._load(combo)
    except Exception as exc:
        if entity_loader.is_no_rows(exc):
            # No child records in this scope: counted as empty by the caller, not failed.
            return {"combo": combo, "load_ms": (time.perf_counter() - t0) * 1000,
                    "cluster_ms": 0.0, "failed": False, "result": {"patterns": []}}
        log.warning("Discovery could not load the child records for the combination %s.",
                    combo, exc_info=True)
        return {"combo": combo, "load_ms": (time.perf_counter() - t0) * 1000,
                "cluster_ms": 0.0, "failed": True, "result": None}
    load_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    try:
        with limits:
            result = cs.analyze(combo, k=None)
    except Exception:
        log.warning("Discovery could not analyze the child records for the combination %s.",
                    combo, exc_info=True)
        return {"combo": combo, "load_ms": load_ms,
                "cluster_ms": (time.perf_counter() - t0) * 1000,
                "failed": True, "result": None}

    return {"combo": combo, "load_ms": load_ms,
            "cluster_ms": (time.perf_counter() - t0) * 1000,
            "failed": False, "result": result}


def _analyze_all_claims(combos: list[dict]) -> list[dict]:
    """Every claims combination, in order. Serial on purpose.

    The member walk parallelizes well; this one does not, and it was worth
    measuring rather than assuming the same answer twice. Across 63
    combinations at four threads:

        serial      94.6s, 202 patterns
        4 threads   83.1s, 197 patterns   — 1.14x, and not the same result

    Two reasons. The queries contend: SQL Server Express is capped at four
    cores, and running the loads concurrently took them from 5.8s to 69.3s in
    aggregate. And the walk reaches its data through cached layers, so
    concurrent runs duplicate work the serial walk does once — 3.6x the total
    work for a 14% saving. The differing pattern count is the disqualifier on
    its own: a walk that finds five fewer patterns is not faster, it is wrong.

    Batching the loads into one read was tried too. It removes the queries but
    changes their row order, and KMeans on a reordered frame lands on different
    clusters — the same combination came back as "Adult · dx E11.9" one way and
    "Adult · Type 2 diabetes mellitus" the other. There is no unique row key to
    sort on (50,000 rows, 14,321 distinct CLCL_ID), so the order cannot be made
    to match. It saved 5% and cost determinism.

    What would actually move this: the k sweep. Each combination fits KMeans 37
    times, and that is 96% of the walk. Narrowing the sweep changes which k is
    chosen, so it is an analytics decision rather than a mechanical one.
    """
    return [_analyze_claims_combo(c) for c in combos]


def _analyze_all(combos: list[dict]) -> list[Optional[dict]]:
    """Analyze every combination, in parallel, results in the order given.

    executor.map preserves input order, so the walk produces the same patterns in
    the same sequence as the serial version — the output must not depend on which
    thread happened to finish first.
    """
    if DISCOVERY_WORKERS <= 1 or len(combos) <= 1:
        return [_analyze_combo(c) for c in combos]

    with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS,
                            thread_name_prefix="discovery") as pool:
        return list(pool.map(_analyze_combo, combos))


# A combination smaller than this yields no pattern worth ranking, and the
# clustering on it is noise. Skipping them before the load is the single
# largest saving in the walk, because most combinations are small.
MIN_COMBO_MEMBERS = 30

# Claims are far denser than members — one member yields many claim lines — so
# the floor is higher for the same intent: skip combinations too small to
# produce a pattern worth ranking.
MIN_COMBO_CLAIMS = 100


def _enumerate_by_size(max_depth: int, limit_groups: Optional[int],
                       cap: int, min_members: int) -> Optional[list[dict]]:
    """The primary records' combinations, largest first, from one aggregate
    query per depth (scope_walk.by_size); None where it cannot be read, so the
    walk can go unranked instead. limit_groups caps the first level's values."""
    return scope_walk.by_size(packs.active().primary, max_depth, limit_groups, cap,
                              min_members)


def _enumerate_claims_by_size(max_depth: int, limit_groups: Optional[int],
                              cap: int, min_claims: int) -> Optional[list[dict]]:
    """The child records' combinations, largest first (scope_walk.by_size)."""
    return scope_walk.by_size(packs.active().child, max_depth, limit_groups, cap, min_claims)


def _enumerate(max_depth: int, limit_groups: Optional[int], cap: int,
               entity: Optional[str] = None) -> list[dict]:
    """Every combination, shallowest first under each first-level value, no
    deeper than the aggregate goes: the walk when the aggregate cannot run."""
    entity = entity or packs.active().primary
    return scope_walk.every(entity, max_depth, limit_groups, cap,
                            deepest=scope_walk.size_depth(entity))


@two_tier("discovery.top", ttl=3600)
def discover(top_n: int = 50, max_depth: int = 2,
             limit_groups: Optional[int] = None, cap: int = 500) -> dict:
    # refresh=True is intercepted by the two_tier wrapper before this body runs.
    """Rank the largest patterns across every combination. The landing view.

    Cached on disk, so the naive user who opens the portal gets an instant
    result after the first run rather than waiting on the walk every time.
    """
    # A store that cannot keep this pack's patterns is said at once.
    pattern_scope.require_store()
    t_start = time.perf_counter()
    book = book_size(overview()) or 1

    t0 = time.perf_counter()
    combos = _enumerate_by_size(max_depth, limit_groups, cap, MIN_COMBO_MEMBERS)
    if combos is None:
        combos = _enumerate(max_depth, limit_groups, cap)
    enumerate_ms = (time.perf_counter() - t0) * 1000
    # (overview is cheap and uncached; book is always current here.)

    found: list[dict] = []
    processed = 0
    skipped_empty = 0
    load_ms = cluster_ms = persist_ms = wait_ms = 0.0
    rows_loaded = 0
    persist_failures: list[str] = []

    # Analysis first, across DISCOVERY_WORKERS threads. Loading and clustering
    # is 97% of the walk and each combination is independent of the others.
    analyzed = _analyze_all(combos)

    # Persisting stays serial on the one shared connection: pyodbc connections
    # are not safe to share between threads, and the writes are 1% of the walk,
    # so there is nothing to win by parallelizing them.
    with _persist_connection("discovery") as persist_conn:
        for result in analyzed:
            if result is None:
                continue
            # load_ms and cluster_ms are aggregate work across the pool, so they
            # can exceed the wall clock — see _timing.
            load_ms += result["load_ms"]
            wait_ms += result.get("wait_ms", 0.0)
            cluster_ms += result["cluster_ms"]
            if result.get("failed"):
                continue
            if result["empty"]:
                skipped_empty += 1
                continue

            combo = result["combo"]
            labels = result["labels"]
            rows_loaded += result["rows"]
            processed += 1

            for c in result["clusters"]:
                cid, profile, size = c["cluster_id"], c["profile"], c["size"]
                name = c["name"]

                # Persist so it can be saved and generated from later.
                pattern_id = None
                t_persist = time.perf_counter()
                try:
                    pattern_id = db.upsert_pattern(
                        combo=canonical_combo({**combo, "_labels": labels}),
                        cluster_id=cid, contextual_name=name,
                        member_count=size, pct_of_pop=size / book,
                        silhouette=c["silhouette"],
                        profile=profile, conn=persist_conn,
                    )
                except Exception as e:
                    # One unusable pattern must not lose a walk that took
                    # minutes, so the run continues — but it is counted and
                    # reported. Swallowing it silently is how a broken write
                    # stays invisible: a column added to this INSERT and got
                    # wrong would look exactly like a successful walk, with the
                    # patterns simply not there.
                    persist_failures.append(f"{name}: {type(e).__name__}: {e}")
                persist_ms += (time.perf_counter() - t_persist) * 1000

                found.append({
                    "pattern_id": pattern_id,
                    "entity": packs.active().primary,
                    "cluster_id": cid,
                    "name": name,
                    "size": size,
                    # Share of the whole book; capped at 1 as a guard against a
                    # stale denominator ever showing an impossible >100%.
                    "pct_of_population": round(min(1.0, size / book) if book else 0.0, 4),
                    "silhouette": round(c["silhouette"], 3),
                    "combo": to_py(combo),
                    **pattern_scope.named_labels(labels),
                    "scope": pattern_scope.path(labels),
                    "_first": pattern_scope.levels(labels)[0],
                })


    found = _dedupe_by_identity(found)
    found.sort(key=lambda p: p["size"], reverse=True)

    _report_persist_failures("discovery.members", persist_failures, len(found))

    timing = _timing(packs.active().primary, len(combos), processed, skipped_empty,
                     rows_loaded, enumerate_ms, load_ms, cluster_ms, t_start,
                     persist_ms, workers=DISCOVERY_WORKERS, wait_ms=wait_ms,
                     persist_failed=len(persist_failures))

    return {
        "total_population": book,
        "combinations_processed": processed,
        "n_discovered": len(found),
        "patterns": found[:top_n],
        "timing": timing,
    }


def _report_persist_failures(label: str, failures: list[str], n_found: int) -> None:
    """Say so, loudly, when patterns were discovered but could not be stored.

    A walk that finds 150 patterns and stores none still returns 150 patterns to
    the caller — they are in the response, just not in sdm.patterns, so nothing
    can be saved or generated from them later. That is worth a line in the
    console rather than a shrug.
    """
    if not failures:
        return
    print(f"[{label}] WARNING: {len(failures)} of {n_found} patterns failed to "
          f"persist and exist only in this response")
    for line in failures[:3]:
        print(f"    {line}")
    if len(failures) > 3:
        print(f"    ... and {len(failures) - 3} more")


def _timing(entity: str, n_combos: int, processed: int, skipped_empty: int,
            rows: int, enumerate_ms: float, load_ms: float, cluster_ms: float,
            t_start: float, persist_ms: float = 0.0, workers: int = 1,
            wait_ms: float = 0.0, persist_failed: int = 0) -> dict:
    """Where a discovery sweep actually spent its time.

    Recorded because the two candidate causes — SQL round trips per combination
    versus KMeans per combination — call for completely different fixes, and
    guessing between them would mean redesigning the wrong half.
    """
    total_ms = (time.perf_counter() - t_start) * 1000

    # load and cluster run across a thread pool, so their totals are aggregate
    # work and can exceed the wall clock. Shares are taken against that
    # aggregate rather than against total_ms, which would otherwise read past
    # 100% and look like a bug in the instrumentation.
    work_ms = enumerate_ms + load_ms + cluster_ms + persist_ms
    basis = max(work_ms, total_ms) or 1.0

    stats = {
        "entity": entity,
        "combinations": n_combos,
        "processed": processed,
        "skipped_empty": skipped_empty,
        "rows_loaded": rows,
        "enumerate_ms": round(enumerate_ms),
        "load_ms": round(load_ms),
        "cluster_ms": round(cluster_ms),
        "persist_ms": round(persist_ms),
        "other_ms": round(max(0.0, total_ms - work_ms)),
        # Wall clock. Divided by work_ms this is the speedup parallelism bought.
        "total_ms": round(total_ms),
        "work_ms": round(work_ms),
        # What this walk actually used, not the global setting: the claims walk
        # is serial whatever DISCOVERY_WORKERS says, and reporting 4 there read
        # as though threads were in play when they were not.
        "workers": workers,
        # Time workers spent queued for the load lock. Not query time and not
        # clustering — it is the cost of keeping the database to one caller.
        "load_wait_ms": round(wait_ms),
        # Patterns found but not stored. Zero on a healthy walk; anything else
        # means the response and the table disagree.
        "persist_failed": persist_failed,
    }

    share = lambda v: f"{100 * v / basis:.0f}%"
    parallel = (f" | {workers} workers, "
                f"{work_ms / total_ms:.1f}x work in wall clock"
                if workers > 1 and total_ms else "")
    print(
        f"[discovery.{entity}] {total_ms / 1000:.1f}s over {n_combos} combos "
        f"({processed} clustered, {skipped_empty} empty, {rows:,} rows) | "
        f"enumerate {enumerate_ms / 1000:.1f}s {share(enumerate_ms)} · "
        f"load {load_ms / 1000:.1f}s {share(load_ms)} · "
        f"cluster {cluster_ms / 1000:.1f}s {share(cluster_ms)} · "
        f"persist {persist_ms / 1000:.1f}s {share(persist_ms)}{parallel}"
    )
    return stats


def _combo_depth(combo: dict) -> int:
    """How many scope levels the combo pins down. Fewer = more general."""
    return sum(1 for k in pattern_scope.SCOPE_KEYS if combo.get(k))


def _identity(p: dict) -> tuple:
    """What makes two discovered patterns 'the same'.

    Same record kind, name, size, first scope level (health: the group) and
    cluster id means the walk found one pattern through two combinations. The
    record kind is first so a primary and a child pattern are never treated
    as the same even if the rest matches.
    """
    return (p.get("entity", packs.active().primary), p["name"], p["size"],
            p.get("_first"), p["cluster_id"])


def _dedupe_by_identity(patterns: list[dict]) -> list[dict]:
    """Keep one card per real pattern — the one from the shallowest combo.

    Discovery walks group, then group+category, then group+category+plan. A
    pattern that survives unchanged as the filter deepens is the same pattern
    surfacing repeatedly, and should appear once. The shallowest combo is the
    most general description of it, so that is the one kept.
    """
    best: dict[tuple, dict] = {}
    for p in patterns:
        sig = _identity(p)
        prior = best.get(sig)
        if prior is None or _combo_depth(p["combo"]) < _combo_depth(prior["combo"]):
            best[sig] = p
    # The first level's name was the walk's own, to tell patterns apart by.
    return [{k: v for k, v in p.items() if k != "_first"} for p in best.values()]


# ── Save / retrieve ──────────────────────────────────────────────────────────

def save(pattern_id: int, saved_by: str = "user") -> dict:
    db.mark_pattern_saved(pattern_id, saved_by=saved_by)
    return {"pattern_id": pattern_id, "saved": True, "saved_count": len(list_saved())}


def unsave(pattern_id: int) -> dict:
    db.mark_pattern_unsaved(pattern_id)
    return {"pattern_id": pattern_id, "saved": False, "saved_count": len(list_saved())}


def list_saved() -> list[dict]:
    out = []
    for p in db.get_saved_patterns() or []:
        try:
            profile = json.loads(p.get("profile_json") or "{}")
        except ValueError:
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        # The analysis it was saved from (not the records chosen, which can be
        # many): what the Find screen tells a saved found pattern by.
        source = profile.get("saved_from")
        saved_from = ({key: source.get(key) for key in
                       ("domain", "filters", "k", "cluster_id", "claim_type", "records")}
                      if isinstance(source, dict) else None)
        out.append({
            "pattern_id": int(p["id"]),
            "name": str(p.get("contextual_name") or f"Pattern {p['id']}"),
            # The record kind its scope names, or an older row's markers.
            "entity": pattern_scope.entity_of(p),
            "origin": p.get("origin") or "discovered",
            "member_count": int(p.get("member_count") or 0),
            "pct_of_population": float(p.get("pct_of_pop") or 0.0),
            "silhouette": float(p.get("silhouette") or 0.0),
            **pattern_scope.named_labels(pattern_scope.labels_of(p)),
            "scope": pattern_scope.path(pattern_scope.labels_of(p)),
            "scope_levels": pattern_scope.levels(pattern_scope.labels_of(p)),
            "saved_at": str(p.get("saved_at")) if p.get("saved_at") else None,
            "saved_from": saved_from,
            # Found under an earlier build of a generated pack: read-only.
            "archived": p.get("archived_at") is not None,
            "plan_version": p.get("plan_version"),
        })
    return out


# ── Claims discovery ─────────────────────────────────────────────────────────

@two_tier("discovery.claims", ttl=3600)
def discover_claims(top_n: int = 50, max_depth: int = 2,
                    limit_groups: Optional[int] = None, cap: int = 500) -> dict:
    # refresh=True is intercepted by the two_tier wrapper before this body runs.
    """The claims counterpart to discover(): largest claim patterns, ranked."""
    from sdg.analyzer import db as _db

    from . import claims_service as cs

    # A store that cannot keep this pack's patterns is said at once.
    pattern_scope.require_store()
    t_start = time.perf_counter()
    book = cs.book_totals(refresh=True).get("claims", 0) or 1

    # Group → Plan → Product, largest first, from one aggregate query.
    t0 = time.perf_counter()
    combos = _enumerate_claims_by_size(max_depth, limit_groups, cap,
                                       MIN_COMBO_CLAIMS)
    if combos is None:
        combos = _enumerate(max_depth, limit_groups, cap, packs.active().child)
    enumerate_ms = (time.perf_counter() - t0) * 1000

    found: list[dict] = []
    processed = 0
    skipped_empty = 0
    load_ms = cluster_ms = persist_ms = 0.0
    persist_failures: list[str] = []

    # Same shape as the member walk: analyze across threads, persist serially on
    # the one shared connection. This walk is 96% clustering, so it parallelizes
    # for the same reason the member one does.
    analyzed = _analyze_all_claims(combos)

    # The names each combination is shown by: two patterns named alike from
    # different plans are told apart by where they were found.
    child = packs.active().child
    names = {level["filter"]: {str(o["key"]): o["label"]
                               for o in entity_loader.options(child, level["level"])}
             for level in scope_walk._levels(child)}
    # What the code of its day marked a kept child pattern with (health: a
    # "[Claims] " name, a "Claims" category), kept beside its scope.
    marks = cs.CHILD.get("marks") or {}
    prefix = marks.get("name_prefix") or ""

    def where(key: str) -> str | None:
        keys = combo.get(key) or []
        return ", ".join(names[key].get(str(k)) or str(k) for k in keys) or None

    with _persist_connection("discovery.claims") as persist_conn:
        for entry in analyzed:
            load_ms += entry["load_ms"]
            cluster_ms += entry["cluster_ms"]
            if entry["failed"]:
                continue
            combo, result = entry["combo"], entry["result"]
            labels = {key: where(key) for key in names}

            if not result.get("patterns"):
                skipped_empty += 1
                continue
            processed += 1

            for p in result["patterns"]:
                n_claims = int(p["n_claims"])
                pattern_id = None
                t_persist = time.perf_counter()
                try:
                    # Claims patterns persist to the same table, tagged by domain in
                    # the name so members and claims don't collide on retrieval.
                    pattern_id = _db.upsert_pattern(
                        combo=canonical_combo({**combo, "_labels": {
                            **{k: v or "" for k, v in labels.items()},
                            **(marks.get("labels") or {})}}, entity=child),
                        cluster_id=p["cluster_id"],
                        contextual_name=f"{prefix}{p['name']}",
                        member_count=n_claims,
                        pct_of_pop=n_claims / book,
                        silhouette=float(result.get("silhouette") or 0.0),
                        profile={"claims_pattern": True, **p},
                        conn=persist_conn,
                    )
                except Exception as e:
                    # Counted, not swallowed — see the member walk above.
                    persist_failures.append(
                        f"{prefix}{p['name']}: {type(e).__name__}: {e}")
                persist_ms += (time.perf_counter() - t_persist) * 1000

                # n_claims for one combination cannot exceed the whole book; if it
                # appears to, the denominator is stale and the ratio is capped at 1
                # so the UI never shows an impossible >100%.
                pct = min(1.0, n_claims / book) if book else 0.0
                found.append({
                    "pattern_id": pattern_id,
                    "entity": child,
                    "cluster_id": p["cluster_id"],
                    "name": p["name"],
                    "size": n_claims,
                    "pct_of_population": round(pct, 4),
                    "silhouette": round(float(result.get("silhouette") or 0.0), 3),
                    "combo": to_py(combo),
                    **pattern_scope.named_labels({k: v for k, v in labels.items() if v}),
                    "scope": pattern_scope.path(labels),
                    "_first": pattern_scope.levels(labels)[0],
                })

        found = _dedupe_by_identity(found)
        found.sort(key=lambda p: p["size"], reverse=True)

    # Load and cluster are timed apart now. Folding both into load_ms made this
    # walk look I/O-bound when it is 96% clustering, and that reading sent an
    # optimization at the queries instead of at the KMeans.
    _report_persist_failures("discovery.claims", persist_failures, len(found))

    timing = _timing(packs.active().child, len(combos), processed, skipped_empty,
                     0, enumerate_ms, load_ms, cluster_ms, t_start, persist_ms,
                     persist_failed=len(persist_failures))

    return {
        "total_population": book,
        "combinations_processed": processed,
        "n_discovered": len(found),
        "patterns": found[:top_n],
        "timing": timing,
    }

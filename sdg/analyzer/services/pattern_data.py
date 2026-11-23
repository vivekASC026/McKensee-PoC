"""The real rows a pattern was derived from.

A pattern is a claim about the book: "1,531 members look like this". This
answers the obvious next question — which ones — by rebuilding the exact subset
the pattern came from and handing back a page of it.

Nothing here generates. These are source rows out of member_denorm or
claims_denorm, which is the whole point: the generated data is a synthesis of
this, and being able to see the evidence underneath is what makes a pattern
reviewable rather than something to take on trust.

Reconstruction is not free — a pattern's cluster only exists as the output of a
load plus a clustering run, so producing it means redoing both. That work is
cached against the pattern and the state of the patterns table, and pagination
then slices the cached frame. Paging through 5,800 rows twenty-five at a time
must not re-cluster twenty-three times.
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd

from sdg.analyzer import db

from . import disk_cache
from sdg import packs
from sdg.packs import held, is_child

NAMESPACE = "pattern.rows"

# Rebuilt subsets are held longer than a query but well short of the discovery
# result they came from: a pattern's membership only changes when the source
# data does, and the cost is seconds rather than minutes.
TTL = 1800

# A pattern covering a large group runs to thousands of rows. Held whole so
# paging is instant, but capped so one enormous subset cannot fill the tier.
MAX_ROWS = 20_000


# Columns that identify a person, flagged rather than hidden.
#
# The point of this view is to see the real rows, so nothing is masked — but a
# reviewer should know which columns carry PHI before screen-sharing them or
# pasting them into a ticket. Named explicitly rather than matched on "NAME",
# because GRGR_NAME, SGSG_NAME and PLDS_DESC are the group, section and plan —
# organizational labels, not people.
_PHI_COLUMNS = frozenset(held("features:store.personal_columns_rows"))

# Anything ending this way is an address component wherever it appears.
_PHI_SUFFIXES = tuple(held("features:store.personal_suffixes_rows"))


def phi_columns(columns: list[str]) -> list[str]:
    """Which of these columns identify a person."""
    out = []
    for col in columns:
        upper = str(col).strip().upper()
        if upper in _PHI_COLUMNS or upper.endswith(_PHI_SUFFIXES):
            out.append(col)
    return out


def _filters_taken(entity: str | None) -> tuple[str, ...]:
    """The filters a record kind's loader takes, and a defined pattern's
    conditions, in the pack's order."""
    from sdg.analyzer.pattern_scope import _known

    from .filter_models import listed

    return (*(f["key"] for f in listed(_known(entity))), "conditions")


# The child records' (kept under its old name: tests/test_pack_constants.py
# holds it to the list the health pack kept by hand before).
_CLAIMS_FILTERS = _filters_taken(packs.active().child)


def _taken(entity: str, filters: dict) -> dict:
    """Only the filters the record kind's loader takes (and a defined
    pattern's conditions). Two kinds' scopes can differ -- health's claims have
    no plan category -- so one stored by a pattern of the other kind (a clone
    of a member pattern) is not passed through: the loader refuses a filter it
    does not take."""
    taken = set(_filters_taken(entity))
    return {k: v for k, v in filters.items() if k in taken}


def _filters(pattern: dict, entity: str | None = None) -> dict:
    """Scope filters from a stored pattern, the tag and blanks removed."""
    from .story_service import _filters_from_pattern

    return _taken(entity, _filters_from_pattern(pattern))


class StalePattern(ValueError):
    """The pattern's cluster no longer exists in a fresh clustering run."""


def _stale(cluster_id: int, pattern: dict) -> "StalePattern":
    """Explain a cluster id that has outlived the run that produced it.

    cluster_id is only meaningful against the clustering that assigned it. A
    pattern discovered weeks ago may have been cluster 8 of a run that chose
    k=9; if the data has shifted since, today's run of the same combination may
    produce seven clusters and there is no eighth to show. The stored pattern is
    not corrupt — its evidence has simply moved on.

    The underlying message is "Pattern 8 is not present in this subset", where 8
    is a cluster index. Surfaced raw next to a pattern id of 403, that reads as
    the wrong pattern being looked up.
    """
    when = str(pattern.get("created_at") or "")[:10]
    return StalePattern(
        f"This pattern was discovered on {when or 'an earlier run'} as cluster "
        f"{cluster_id}, and a fresh clustering of the same combination no longer "
        f"produces that cluster — the underlying data has changed since. Re-run "
        f"discovery to refresh it."
    )


def _rebuild(pattern: dict, entity: str) -> pd.DataFrame:
    """Load and cluster to recover this pattern's rows.

    A user-defined pattern has no cluster — it states a combination and nothing
    was ever fitted to it — so every member matching the combination is its
    data. USER_CLUSTER_ID marks that case.
    """
    from . import pattern_service as ps

    # A pattern somebody saved says where it came from -- the analysis, its k,
    # the records chosen -- which a discovery slot's hierarchy key cannot.
    try:
        import json

        from . import saved_patterns

        profile = json.loads(pattern.get("profile_json") or "{}")
        saved = saved_patterns.saved_rows(profile)
        if saved is not None:
            return saved
    except ValueError as e:
        raise _stale(int(pattern.get("cluster_id") or 0), pattern) from e

    filters = _filters(pattern, entity)
    cluster_id = int(pattern.get("cluster_id") if pattern.get("cluster_id")
                     is not None else -1)

    if is_child(entity):
        from . import claims_service as cs

        if cluster_id == ps.USER_CLUSTER_ID:
            df, _used = cs._load(filters)
            return df
        try:
            return cs.pattern_rows(filters, None, cluster_id)
        except ValueError as e:
            raise _stale(cluster_id, pattern) from e

    from sdg.analyzer.config import DEFAULT_REFERENCE_DATE

    from . import service

    if cluster_id == ps.USER_CLUSTER_ID:
        members, _labels, _family, _used = service._load(
            filters, DEFAULT_REFERENCE_DATE)
        return members

    try:
        _profile, _used, rows, _name = service.pattern_context(
            filters, None, cluster_id, DEFAULT_REFERENCE_DATE)
    except ValueError as e:
        if "not present in this subset" in str(e):
            raise _stale(cluster_id, pattern) from e
        raise
    return rows


def _identity(pattern: dict) -> tuple:
    """What a pattern's row set actually depends on.

    Keyed on this rather than on the whole patterns table. The first version
    used db.patterns_signature(), which moves on any write to sdm.patterns — so
    saving an unrelated pattern threw away every rebuilt subset, and the next
    person to open one waited through a full reload and re-cluster for no
    reason. Measured: 242 ms warm, 991 ms after an unrelated save.

    A pattern's rows depend on the combination it selects and the cluster it
    took, plus when the pattern itself was last edited. Nothing another pattern
    does can change them. saved_at is deliberately absent: bookmarking a pattern
    does not alter which members are in it.
    """
    return (
        str(pattern.get("pattern_key") or ""),
        str(pattern.get("combo_key") or ""),
        pattern.get("cluster_id"),
        # An edit to this pattern's own combination changes its rows.
        str(pattern.get("updated_at") or ""),
    )


def rows_for(pattern_id: int) -> dict:
    """Every source row behind one pattern, with the context to describe it.

    Cached on the pattern id plus the patterns table's signature, so an edit to
    the pattern — a changed combination, say — produces a different key and the
    subset is rebuilt rather than served stale.
    """
    pattern = db.get_pattern(pattern_id)
    if not pattern:
        raise ValueError(f"Pattern {pattern_id} not found.")
    if pattern.get("archived_at") is not None:
        # The tables now hold another build's rows: none of them are its.
        raise StalePattern(
            f"“{pattern.get('contextual_name')}” was found under an earlier build of the pack; "
            f"the tables now hold another build's rows, so its rows are not shown. It is kept "
            f"to read.")

    from . import pattern_service as ps

    entity = ps.entity_of(pattern)
    name = str(pattern.get("contextual_name") or f"Pattern {pattern_id}")

    key = disk_cache.make_key(NAMESPACE, pattern_id, _identity(pattern))
    hit = disk_cache.get(key)
    if hit is None:
        started = time.perf_counter()
        df = _rebuild(pattern, entity)
        cost = time.perf_counter() - started

        truncated = len(df) > MAX_ROWS
        if truncated:
            df = df.head(MAX_ROWS)

        hit = {"df": df, "truncated": truncated, "rebuild_ms": round(cost * 1000)}
        try:
            disk_cache.put(key, hit, ttl=TTL, label=NAMESPACE,
                           namespace=NAMESPACE, cost_seconds=cost)
        except Exception:
            pass  # Serving the rows matters more than caching them.

    return {"pattern": pattern, "name": name, "entity": entity, **hit}


def every_row(entity: str, filters: dict, k: Optional[int], cluster_id: int,
              pattern_id: Optional[int] = None) -> pd.DataFrame:
    """All of a pattern's rows, uncapped: what counts are made from (the rows
    view holds at most MAX_ROWS; a count from those would be short)."""
    if pattern_id is not None:
        pattern = db.get_pattern(pattern_id)
        if not pattern:
            raise ValueError(f"Pattern {pattern_id} not found.")
        from . import pattern_service as ps

        return _rebuild(pattern, ps.entity_of(pattern))
    if is_child(entity):
        from . import claims_service as cs

        return cs.pattern_rows(filters, k, cluster_id)
    from sdg.analyzer.config import DEFAULT_REFERENCE_DATE

    from . import service

    return service.pattern_context(filters, k, cluster_id, DEFAULT_REFERENCE_DATE)[2]


def rows_for_live(entity: str, filters: dict, k: Optional[int],
                  cluster_id: int) -> dict:
    """Rows for a pattern that exists only as the result of an analysis.

    The profiler screens cluster on demand and show the result; those patterns
    have a cluster_id but no pattern_id, because nothing has been saved. They
    still have everything needed to rebuild the subset — the filters, the k and
    the cluster — so they get the same view as a stored pattern.

    Keyed on that same triple rather than on a pattern id.
    """
    key = disk_cache.make_key(NAMESPACE, "live", entity,
                              sorted(filters.items()), k, cluster_id)
    hit = disk_cache.get(key)
    if hit is None:
        started = time.perf_counter()
        if is_child(entity):
            from . import claims_service as cs

            df = cs.pattern_rows(filters, k, cluster_id)
            name = f"{cs.CHILD['name']['fallback']} {cluster_id}"
        else:
            from sdg.analyzer.config import DEFAULT_REFERENCE_DATE

            from . import service

            _profile, _used, df, name = service.pattern_context(
                filters, k, cluster_id, DEFAULT_REFERENCE_DATE)
        cost = time.perf_counter() - started

        truncated = len(df) > MAX_ROWS
        if truncated:
            df = df.head(MAX_ROWS)
        hit = {"df": df, "name": name, "entity": entity,
               "truncated": truncated, "rebuild_ms": round(cost * 1000)}
        try:
            disk_cache.put(key, hit, ttl=TTL, label=NAMESPACE,
                           namespace=NAMESPACE, cost_seconds=cost)
        except Exception:
            pass
    return hit


def _shape(found: dict, limit: int, offset: int, pattern_id=None) -> dict:
    """One page out of an already-rebuilt subset."""
    from .service import df_preview

    from .saved_patterns import row_keys

    df: pd.DataFrame = found["df"]
    total = int(len(df))

    window = df.iloc[offset: offset + limit] if limit else df.iloc[offset:]
    columns, rows = df_preview(window, len(window))

    return {
        "pattern_id": pattern_id,
        "name": found["name"],
        "entity": found["entity"],
        "columns": columns,
        # Shown, never hidden — but named, so the UI can mark them and whoever
        # is looking knows what is on their screen.
        "phi_columns": phi_columns(columns),
        "rows": rows,
        # Each row's name, aligned with `rows`, for choosing records: the same
        # on every page, so a choice survives paging.
        "keys": row_keys(window, found["entity"]),
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "has_more": offset + len(rows) < total,
        # True when the subset was larger than MAX_ROWS and the tail was
        # dropped, so a caller can say "first 20,000 of many" honestly rather
        # than reporting a total it has quietly capped.
        "truncated": bool(found["truncated"]),
        "rebuild_ms": found["rebuild_ms"],
    }


def page(pattern_id: int, limit: int, offset: int) -> dict:
    """One page of a stored pattern's source rows."""
    return _shape(rows_for(pattern_id), limit, offset, pattern_id)


def page_live(entity: str, filters: dict, k: Optional[int], cluster_id: int,
              limit: int, offset: int) -> dict:
    """One page of an analyzed-but-unsaved pattern's source rows."""
    filters = _taken(entity, filters)
    try:
        found = rows_for_live(entity, filters, k, cluster_id)
    except ValueError as e:
        if "not present in this subset" in str(e):
            raise StalePattern(
                f"Cluster {cluster_id} is not in this analysis any more — "
                f"re-run it to see the current clusters.") from e
        raise
    return _shape(found, limit, offset)

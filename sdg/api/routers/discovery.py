"""System discovery (the landing view) and saved patterns."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel, Field

from sdg import packs
from sdg.analyzer.db import PatternArchived
from sdg.analyzer.services import discovery_service as ds
from sdg.discovery.errors import plain
from sdg.packs import is_child, is_primary

# Pattern analysis, where the business pack offers it (phase 6b: every pack).
router = APIRouter(prefix="/discovery", tags=["discovery"],
                   dependencies=[Depends(packs.offered("pattern_analysis", "Pattern analysis"))])

log = logging.getLogger(__name__)


# Discovery reads from these caches, so a real refresh must drop the whole
# chain — otherwise the walk recomputes from stale loads and shows no change.
# (By role: the cache names are the services' own, whatever the pack calls
# its records.)
_REFRESH_NAMESPACES = {
    "primary": ["discovery.top", "members.load", "members.cluster", "funnel"],
    "child":  ["discovery.claims", "claims.load", "claims.cluster",
               "claims.booktotal"],
}


@router.get("")
def discover(entity: str = packs.active().primary, top_n: int = 50,
             max_depth: int = 2, limit_groups: int | None = None,
             refresh: bool = False):
    """Largest patterns across the book, for members or claims.

    Cached, so instant after the first walk. Pass refresh=true to re-walk after
    the underlying data has changed. Refresh clears not just the discovery
    cache but everything it reads from — the per-combination loads, the
    clustering, and the book totals — so the new data actually surfaces.
    """
    from sdg.analyzer.services import disk_cache

    if refresh:
        role = "child" if is_child(entity) else "primary" if is_primary(entity) else ""
        for ns in _REFRESH_NAMESPACES.get(role, []):
            disk_cache.clear_namespace(ns)

    fn = ds.discover_claims if is_child(entity) else ds.discover
    try:
        return fn(top_n=top_n, max_depth=max_depth,
                  limit_groups=limit_groups, refresh=refresh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


@router.get("/status")
def status(entity: str = packs.active().primary, top_n: int = 50,
           max_depth: int = 2, limit_groups: int | None = None):
    """Whether a discovery result is already cached — without computing one.

    The landing page calls this first. A cold walk is minutes of work, and
    firing it automatically on page load meant the first thing anyone saw was a
    spinner with no indication of how long it would take or that it had even
    started. This answers in milliseconds so the page can render immediately
    and let the user start the walk deliberately.
    """
    from sdg.analyzer.services import disk_cache

    namespace = "discovery.claims" if is_child(entity) else "discovery.top"
    # Must mirror how two_tier builds its key, or the probe misses a live entry.
    kwargs = sorted({"top_n": top_n, "max_depth": max_depth,
                     "limit_groups": limit_groups}.items())
    key = disk_cache.make_key(namespace, (), kwargs)

    cached = False
    try:
        cached = disk_cache.get(key) is not None
    except Exception:
        # Only a hint for the page: "not cached" means the walk runs when
        # asked. Logged, so a cache that cannot be read is not invisible.
        log.warning("The discovery cache could not be read", exc_info=True)
        cached = False

    return {"entity": entity, "cached": cached}


# ── Save / retrieve ──────────────────────────────────────────────────────────

class SaveRequest(BaseModel):
    pattern_id: int = Field(..., description="ID from a discovered or analyzed pattern.")


@router.post("/save")
def save(req: SaveRequest):
    try:
        return ds.save(req.pattern_id)
    except PatternArchived as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


@router.post("/unsave")
def unsave(req: SaveRequest):
    try:
        return ds.unsave(req.pattern_id)
    except PatternArchived as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


@router.get("/saved")
def saved():
    """Every saved pattern, so a user retrieves rather than re-searches."""
    try:
        return {"patterns": ds.list_saved()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")

"""The neutral routes: an entity by its pack key, a scope level by its name.

    /api/entities/{key}/overview | analyze | rows | save | highlights
    /api/entities/{child}/available  whether the store holds any
    /api/scope?entity=...           the entity's scope levels, broadest first
    /api/scope/{level}?entity=...   one level's choices, narrowed by the levels above

The business pack names the entities; what each does follows its role. An
entity's routes are the routes its role already has -- the same function,
request model and response model -- so /api/entities/claims/analyze and
/api/claims/analyze are one route under two paths, and the old paths stay
as permanent aliases. Routes are registered for the active pack's entities
when the app starts: the pack is chosen per installation.

A scope level is not an entity verb: the types the plan once listed as an
entity's own route are /api/scope/types, beside the other levels. An entity's
highlights (its most frequent codes) are what its pack names; an entity
with none has none.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.routing import APIRoute

from sdg import packs
from sdg.analyzer import entity_loader
from sdg.api.routers import claims, hierarchy, patterns
from sdg.discovery.errors import plain

# Pattern analysis, where the business pack offers it (phase 6b: every pack).
router = APIRouter(tags=["entities"],
                   dependencies=[Depends(packs.offered("pattern_analysis", "Pattern analysis"))])

# An id as the old routes' list[int] took it: a whole number, nothing else.
_WHOLE = re.compile(r"\s*[-+]?\d+\s*")

# What an entity can do, by role: each verb is the route that role already has.
BY_ROLE = {
    "primary": {"overview": hierarchy.overview, "analyze": patterns.analyze,
                "rows": patterns.analyzed_pattern_rows, "save": patterns.save_pattern},
    "child": {"overview": claims.overview, "available": claims.available,
              "analyze": claims.claims_analyze,
              "rows": claims.claims_pattern_rows, "save": claims.claims_save},
}
VERBS = ("overview", "analyze", "rows", "save", "highlights")


def _route_of(endpoint) -> APIRoute:
    for source in (hierarchy.router, patterns.router, claims.router):
        for route in source.routes:
            if isinstance(route, APIRoute) and route.endpoint is endpoint:
                return route
    raise LookupError(f"{endpoint.__name__} is not a route.")


def _register() -> None:
    pack = packs.active()
    for key, entity in pack.entities.items():
        for verb, endpoint in BY_ROLE.get(entity["role"], {}).items():
            old = _route_of(endpoint)
            router.add_api_route(
                f"/entities/{key}/{verb}", endpoint, methods=sorted(old.methods),
                response_model=old.response_model, name=f"{key}_{verb}",
                summary=f"{pack.term(key, 'Many')}: {verb}")


_register()


@router.get("/entities/{key}/highlights")
def highlights(key: str, n: int = Query(30, ge=1, le=100)):
    """The entity's most frequent values, as the pack names them, each
    [{code, description, n}]; an empty list where the pack names none, and
    the screens show no panel."""
    _entity(key)
    out = []
    for spec in entity_loader.highlights(key):
        try:
            codes = entity_loader.highlight(key, spec["key"], n)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{spec['label']}: {plain(e)}")
        out.append({"key": spec["key"], "label": spec["label"], "codes": codes})
    return out


@router.api_route("/entities/{key}/{verb}", methods=["GET", "POST"], include_in_schema=False)
def _not_offered(key: str, verb: str):
    """Whatever the routes above do not answer, said plainly."""
    pack = packs.active()
    if pack.role(key) is None:
        raise HTTPException(status_code=404, detail=f"The {pack.name} pack has no {key!r}.")
    if verb in BY_ROLE.get(pack.role(key), {}):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    if verb in VERBS:
        raise HTTPException(status_code=404,
                            detail=f"{pack.term(key, 'Many')} have no {verb} of their own.")
    if any(s["level"] == verb for s in entity_loader.scope_levels(key)):
        raise HTTPException(status_code=404, detail=(
            f"{verb} is a scope level: /api/scope/{verb}?entity={key}"))
    raise HTTPException(status_code=404, detail=f"No {verb!r}; an entity has {', '.join(VERBS)}.")


def _entity(entity: str | None) -> str:
    pack = packs.active()
    key = entity or pack.primary
    if pack.role(key) is None:
        raise HTTPException(status_code=404, detail=f"The {pack.name} pack has no {key!r}.")
    return key


@router.get("/scope")
def scope_levels(entity: str | None = None):
    """The entity's scope levels, broadest first, and what each narrows by."""
    return entity_loader.scope_levels(_entity(entity))


@router.get("/scope/{level}")
def scope_options(level: str, request: Request, entity: str | None = None):
    """One level's choices, {key, label}. The levels above it narrow it, sent
    as their filter keys, repeated per value: ?grgr_ck=1&grgr_ck=2."""
    key = _entity(entity)
    takes = next((s["takes"] for s in entity_loader.scope_levels(key) if s["level"] == level), None)
    if takes is None:
        raise HTTPException(status_code=404, detail=(
            f"{packs.active().term(key, 'Many')} have no scope level {level!r}."))
    chosen = {k: request.query_params.getlist(k) for k in takes}
    # Ids are numbers, as the old routes' list[int] said: one that is not would
    # be dropped, and the level read unnarrowed, as if it were that choice's.
    ids = {f["key"] for f in entity_loader.config(key)["filters"] if f["kind"] == "ids"}
    for k in ids & set(chosen):
        bad = [v for v in chosen[k] if not _WHOLE.fullmatch(v)]
        if bad:
            raise HTTPException(status_code=422, detail=f"{k} takes numbers, not {bad[0]!r}.")
    try:
        return entity_loader.options(key, level, entity_loader.given(**chosen))
    except Exception as e:
        # A failed lookup is not an empty list: the screen must be able to tell.
        raise HTTPException(status_code=500, detail=f"Scope lookup failed: {plain(e)}")

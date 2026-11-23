"""Claims profiling over sdm.claims_denorm.

claims_data_loader still carries @st.cache_data decorators. They function
outside a Streamlit runtime (you'll see a ScriptRunContext warning on first
call and nothing else), so this router works as-is. Swapping them for
api.cache.ttl_cache removes the warning and is a five-minute job — see the
README.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from sdg import packs
from sdg.analyzer.services import errors, filter_models

from sdg.analyzer.services.schemas import ClaimsOverview, CodeCount
from sdg.discovery.errors import plain

# Pattern analysis, where the business pack offers it (phase 6b: every pack).
router = APIRouter(prefix="/claims", tags=["claims"],
                   dependencies=[Depends(packs.offered("pattern_analysis", "Pattern analysis"))])

log = logging.getLogger(__name__)


def _loader():
    try:
        from sdg.analyzer import claims_data_loader as cdl
        return cdl
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"{packs.active().term(packs.active().child, 'Many')} could not be read: "
                   f"{plain(e)}",
        )


@router.get("/available")
def available():
    cdl = _loader()
    # The loader answers False for a store with none, and raises when it cannot
    # be reached: an outage is said, not reported as having none.
    try:
        return {"available": bool(cdl.claims_available())}
    except Exception as e:
        log.exception("Could not tell whether the store holds child records")
        raise HTTPException(
            status_code=503,
            detail=f"{packs.active().term(packs.active().child, 'Many')} could not be read: "
                   f"{plain(e)}",
        ) from e


@router.get("/overview", response_model=ClaimsOverview)
def overview():
    cdl = _loader()
    if not cdl.claims_available():
        raise HTTPException(
            status_code=404,
            detail=(f"Pattern data has no {packs.active().term(packs.active().child, 'many')}. "
                    "Load them with the customer extract."),
        )
    raw = cdl.get_claims_overview() or {}
    return {k: int(raw.get(k, 0) or 0) for k in ClaimsOverview.model_fields}


# Two of the health pack's highlights by their old paths, kept as aliases of
# /api/entities/{child}/highlights.
@router.get("/diagnoses", response_model=list[CodeCount])
def diagnoses(n: int = 30):
    return _highlight("diagnoses", n)


@router.get("/procedures", response_model=list[CodeCount])
def procedures(n: int = 30):
    return _highlight("procedures", n)


def _highlight(key: str, n: int) -> list[dict]:
    from sdg import packs
    from sdg.analyzer import entity_loader

    try:
        return entity_loader.highlight(packs.active().child, key, n)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Hierarchy cascade ────────────────────────────────────────────────────────

def _t(v):
    return tuple(v) if v else None


def _guard(fn, *args):
    """Run a hierarchy lookup, turning any failure into a readable message.

    Without this an exception surfaces as a bare 500 and the filter rail shows
    an empty list — indistinguishable from a table that legitimately has no
    rows. The distinction matters: one is a bug, the other is a valid answer.
    """
    from sdg.analyzer.services import claims_service as cs
    try:
        return fn(cs, *args)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"The {packs.active().term(packs.active().child, 'many')}' scope could not "
                   f"be read: {plain(e)}",
        )


@router.get("/groups")
def claim_groups():
    return _guard(lambda cs: cs.groups())


@router.get("/plans")
def claim_plans(grgr_ck: list[int] | None = Query(None)):
    return _guard(lambda cs, g: cs.plans(g), _t(grgr_ck))


@router.get("/products")
def claim_products(grgr_ck: list[int] | None = Query(None),
                   cspi_id: list[str] | None = Query(None)):
    return _guard(lambda cs, g, p: cs.products(g, p), _t(grgr_ck), _t(cspi_id))


@router.get("/types")
def claim_types(grgr_ck: list[int] | None = Query(None),
                cspi_id: list[str] | None = Query(None),
                pdpd_id: list[str] | None = Query(None)):
    return _guard(lambda cs, g, p, d: cs.claim_types(g, p, d),
                  _t(grgr_ck), _t(cspi_id), _t(pdpd_id))


# ── Pattern analysis ─────────────────────────────────────────────────────────

# The child records' filters, as the business pack's loader lists them
# (filter_models), a defined pattern's rules beyond them, and k (1 is the
# whole scope as one pattern).
ChildAnalyzeRequest = filter_models.model(
    "ChildAnalyzeRequest", packs.active().child, k=filter_models.K_FIELD)
ClaimsAnalyzeRequest = ChildAnalyzeRequest


@router.post("/analyze")
def claims_analyze(req: ClaimsAnalyzeRequest):
    from sdg.analyzer.services import claims_service as cs
    try:
        return cs.analyze(req.as_kwargs(), req.k)
    except ValueError as e:
        # load_filtered_claims raises this when a filter combination is empty.
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


# ── Generation ───────────────────────────────────────────────────────────────

class ClaimsRowsRequest(ClaimsAnalyzeRequest):
    """Source rows for one cluster of a claims analysis."""
    cluster_id: int
    limit: int = Field(25, ge=1, le=500)
    offset: int = Field(0, ge=0)


@router.post("/rows")
def claims_pattern_rows(req: ClaimsRowsRequest):
    """The real claim rows behind one analyzed claims pattern.

    The claims profiler's patterns have no pattern_id until they are saved, so
    they are addressed by the analysis that produced them.
    """
    from sdg.analyzer.services import pattern_data

    try:
        return pattern_data.page_live(
            packs.active().child, req.as_kwargs(), req.k, req.cluster_id,
            req.limit, req.offset)
    except pattern_data.StalePattern as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))


class ClaimsGenerateRequest(ClaimsAnalyzeRequest):
    cluster_id: int
    n_records: int = Field(100, ge=1, le=100_000)
    member_pattern_id: int = Field(
        ..., description="Member pattern the generated claims attach to.",
    )
    seed: int | None = None
    row_indices: list[int] | None = None


@router.get("/member-patterns")
def member_patterns():
    """Member patterns available to map claims onto."""
    from sdg.analyzer.services import claims_service as cs
    try:
        return cs.saved_member_patterns()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


class ClaimsSaveRequest(ClaimsAnalyzeRequest):
    cluster_id: int


@router.post("/save")
def claims_save(req: ClaimsSaveRequest):
    """Persist and mark-saved a claims pattern, so it's retrievable later."""
    from sdg.analyzer import db

    from sdg.analyzer.services import claims_service as cs

    try:
        result = cs.analyze(req.as_kwargs(), req.k)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")

    pattern = next(
        (p for p in result["patterns"] if p["cluster_id"] == req.cluster_id), None
    )
    if pattern is None:
        raise HTTPException(status_code=404, detail=f"Pattern {req.cluster_id} not in this view.")

    try:
        book = cs.book_totals().get("claims", 0) or 1
        from sdg.analyzer.services.discovery_service import canonical_combo
        # What the code of its day marked a kept child pattern with (health:
        # a "[Claims] " name, a "Claims" category), kept beside its scope.
        marks = cs.CHILD.get("marks") or {}
        pattern_id = db.save_pattern_from_profiler(
            combo=canonical_combo({**req.as_kwargs(), "_labels": dict(marks.get("labels") or {})},
                                  entity=packs.active().child),
            cluster_id=req.cluster_id,
            contextual_name=f"{marks.get('name_prefix') or ''}{pattern['name']}",
            member_count=int(pattern["n_claims"]),
            pct_of_pop=int(pattern["n_claims"]) / book,
            silhouette=float(result.get("silhouette") or 0.0),
            profile={"claims_pattern": True, **pattern},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save failed: {plain(e)}")

    return {"pattern_id": pattern_id, "saved": True, "name": pattern["name"]}

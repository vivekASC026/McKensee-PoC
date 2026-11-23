"""Cascading hierarchy options: Group → Plan Category → Plan → Product.

Each level accepts the selections above it, which is the SQL pushdown the
Streamlit sidebar was already doing — just over HTTP now.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from sdg import packs
from sdg.analyzer.services import service
from sdg.analyzer.services.schemas import Option, OverviewMetrics
from sdg.discovery.errors import plain

# Pattern analysis, where the business pack offers it (phase 6b: every pack).
router = APIRouter(prefix="/hierarchy", tags=["hierarchy"],
                   dependencies=[Depends(packs.offered("pattern_analysis", "Pattern analysis"))])


log = logging.getLogger(__name__)


def _t(v: Optional[list]) -> Optional[tuple]:
    """Tuples so the values are hashable for the cache."""
    return tuple(v) if v else None


def _guard(fn, *args):
    """Run a hierarchy lookup, turning any failure into a readable message.

    As in the other routers' _guard: a failure must not read on the filter rail
    as a level that legitimately has no rows.
    """
    try:
        return fn(*args)
    except Exception as e:
        log.exception("Hierarchy lookup %s failed", getattr(fn, "__name__", fn))
        raise HTTPException(
            status_code=500,
            detail=f"The {packs.active().term(packs.active().primary, 'many')}' scope "
                   f"could not be read: {plain(e)}",
        ) from e


@router.get("/overview", response_model=OverviewMetrics)
def overview():
    return _guard(service.overview)


@router.get("/groups", response_model=list[Option])
def groups():
    return _guard(service.groups)


@router.get("/plan-categories", response_model=list[Option])
def plan_categories(
    grgr_ck: Optional[list[int]] = Query(None),
):
    return _guard(service.plan_categories, _t(grgr_ck))


@router.get("/plans", response_model=list[Option])
def plans(
    grgr_ck: Optional[list[int]] = Query(None),
    cspd_cat: Optional[list[str]] = Query(None),
):
    return _guard(service.plans, _t(grgr_ck), _t(cspd_cat))


@router.get("/products", response_model=list[Option])
def products(
    grgr_ck: Optional[list[int]] = Query(None),
    cspd_cat: Optional[list[str]] = Query(None),
    cspi_id: Optional[list[str]] = Query(None),
):
    return _guard(service.products, _t(grgr_ck), _t(cspd_cat), _t(cspi_id))


@router.get("/counts")
def counts(
    grgr_ck: Optional[list[int]] = Query(None),
    cspd_cat: Optional[list[str]] = Query(None),
    cspi_id: Optional[list[str]] = Query(None),
    pdpd_id: Optional[list[str]] = Query(None),
):
    """Member counts at each level of the funnel."""
    return _guard(
        service.funnel_counts,
        tuple(grgr_ck or ()), tuple(cspd_cat or ()),
        tuple(cspi_id or ()), tuple(pdpd_id or ()),
    )

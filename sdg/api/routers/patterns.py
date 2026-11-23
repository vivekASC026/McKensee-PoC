"""Pattern analysis.

Clustering over a large subset is slow enough that this is a POST returning a
whole result rather than a chatty set of GETs. If a filter combination ever
pushes past the proxy timeout, this is the endpoint to move behind a job
queue — the response shape stays the same either way.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from sdg import packs
from sdg.analyzer import db
from sdg.analyzer.services import errors
from sdg.analyzer.services import pattern_service as ps

from sdg.analyzer.services import service
from pydantic import BaseModel, Field

from sdg.analyzer.services.schemas import AnalyzeRequest, AnalyzeResponse, Filters
from sdg.discovery.errors import plain
from sdg.packs import active, is_child, is_primary

# Pattern analysis, where the business pack offers it (phase 6b: every pack).
router = APIRouter(prefix="/patterns", tags=["patterns"],
                   dependencies=[Depends(packs.offered("pattern_analysis", "Pattern analysis"))])
_DOMAIN_IS = f"domain is {active().primary} or {active().child}."


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    try:
        return service.analyze(
            filters=req.filters.as_kwargs(),
            k=req.k,
            reference_date=req.reference_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # surface the cause instead of a bare 500
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


class SavePatternRequest(BaseModel):
    filters: Filters = Field(default_factory=Filters)
    k: int | None = Field(None, ge=1, le=20)
    cluster_id: int


@router.post("/save")
def save_pattern(req: SavePatternRequest):
    """Persist a profiler pattern and mark it saved, so it's retrievable later."""
    from sdg.analyzer import db

    from sdg.analyzer.services import service

    try:
        profile, filters_used, _, name = service.pattern_context(
            req.filters.as_kwargs(), req.k, req.cluster_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    from sdg.analyzer.config import DEFAULT_REFERENCE_DATE
    try:
        members = service._load(req.filters.as_kwargs(), DEFAULT_REFERENCE_DATE)[0]
        labels = service.scope_labels(service.build_subset_summary(members, filters_used))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save failed: {plain(e)}")

    try:
        book = service.book_size() or 1
        from sdg.analyzer.services.discovery_service import canonical_combo
        pattern_id = db.save_pattern_from_profiler(
            combo=canonical_combo({**req.filters.as_kwargs(), "_labels": labels}),
            cluster_id=req.cluster_id,
            contextual_name=name,
            member_count=int(profile["size"]),
            pct_of_pop=int(profile["size"]) / book,
            silhouette=0.0,
            profile=profile,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save failed: {plain(e)}")

    return {"pattern_id": pattern_id, "saved": True, "name": name}


# ── User-defined patterns, cloning and certification ─────────────────────────

class UserPatternBody(BaseModel):
    name: str
    features: dict
    entity: str = packs.active().primary
    labels: dict | None = None


def _search(items: list[dict], q: str | None) -> list[dict]:
    """Name or any feature value, case-insensitive substring.

    Mirrors what the screen's featureSummary() put in front of the user, so a
    query that used to match still matches.
    """
    text = (q or "").strip().lower()
    if not text:
        return items

    def matches(p: dict) -> bool:
        if text in str(p.get("name") or "").lower():
            return True
        for values in (p.get("features") or {}).values():
            for v in values or []:
                if text in str(v).lower():
                    return True
        return False

    return [p for p in items if matches(p)]


@router.get("/catalog")
def catalog(origin: str | None = None,
            q: str | None = Query(None, description="Match name or feature value."),
            limit: int | None = Query(None, ge=1, le=1000,
                                      description="Page size. Omit for every pattern."),
            offset: int = Query(0, ge=0, description="Rows to skip.")):
    """Every pattern with its provenance — discovered, user-defined, certified.

    Pagination is opt-in: without `limit` the whole catalog comes back, which is
    what the blend picker needs — it builds a mix from the full set and cannot
    choose from a page. Passing `limit` returns one page plus the totals needed
    to render a pager.

    Ordering is member_count descending, then id. The tiebreaker matters here:
    without it, patterns sharing a member_count are ordered arbitrarily by SQL
    Server, and a row could appear on two pages or on none.

    The counts describe the whole filtered catalog, not the page, so the summary
    figures stay still while the user pages through.

    `q` searches here rather than in the browser. The screen used to filter the
    full list client-side, which only works while the client holds all of it —
    paginate without moving the search to the server and a search silently
    becomes "find matches among the 25 rows on screen".
    """
    try:
        items = ps.list_patterns(origin)
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))

    items = _search(items, q)
    total = len(items)
    counts = {
        "n_discovered": sum(1 for p in items if p["origin"] == ps.DISCOVERED),
        "n_user_defined": sum(1 for p in items if p["origin"] == ps.USER_DEFINED),
        "n_certified": sum(1 for p in items if p["certified"]),
    }

    page = items[offset: offset + limit] if limit is not None else items[offset:]

    return {
        "patterns": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        # True while another page exists, so the client never has to compare
        # offset + limit against total itself and get the edge case wrong.
        "has_more": offset + len(page) < total,
        **counts,
        "features": list(ps.FEATURES),
    }


@router.post("/define")
def define(req: UserPatternBody):
    """Define a new pattern from a combination of feature values.

    Values are not validated against reference data — a new line of business
    has codes that are configured nowhere yet. The pattern is flagged
    user-defined instead, because the tool cannot vouch for it.
    """
    try:
        return ps.create_user_pattern(
            req.name, req.features, req.entity, labels=req.labels)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))


class ClonePatternBody(BaseModel):
    name: str | None = None
    features: dict | None = None


@router.post("/{pattern_id}/clone")
def clone(pattern_id: int, req: ClonePatternBody):
    """Copy a pattern and change feature values. The copy is always
    user-defined — a copy of evidence is not itself evidence."""
    try:
        return ps.clone_pattern(pattern_id, req.name or "", req.features)
    except db.PatternArchived as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))


@router.put("/{pattern_id}")
def edit(pattern_id: int, req: ClonePatternBody):
    """Edit a user-defined pattern. Discovered patterns are immutable."""
    try:
        return ps.update_user_pattern(pattern_id, req.name, req.features)
    except db.PatternArchived as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))


@router.get("/review-queue")
def queue():
    """User-defined patterns that discovery has since found in real data,
    waiting for a reviewer to approve the merge."""
    try:
        items = ps.review_queue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))
    return {"queue": items, "n_pending": len(items)}


@router.get("/{pattern_id}/rows")
def pattern_rows(pattern_id: int,
                 limit: int = Query(25, ge=1, le=500,
                                    description="Page size."),
                 offset: int = Query(0, ge=0, description="Rows to skip.")):
    """The real source rows this pattern was derived from.

    Not generated data — these are the member_denorm or claims_denorm rows the
    pattern describes, so the claim it makes about the book can be checked
    against the book.

    Always paginated. Rebuilding a pattern's subset means loading and clustering
    again, and the largest here runs to a few thousand rows across 59 columns;
    sending that whole thing to a browser to show twenty-five of them would make
    the feature slower than the analysis it explains. The subset is rebuilt once,
    cached, and paged from memory.
    """
    from sdg.analyzer.services import pattern_data

    try:
        return pattern_data.page(pattern_id, limit, offset)
    except pattern_data.StalePattern as e:
        # 409, not 404: the pattern exists and is a valid record. What it points
        # at has moved, which is a different thing from "no such pattern" and
        # calls for a different message on screen.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))


class LiveRowsBody(BaseModel):
    """A pattern that exists only as the output of an analysis."""
    filters: Filters = Field(default_factory=Filters)
    k: int | None = Field(None, ge=1, le=20)
    cluster_id: int
    limit: int = Field(25, ge=1, le=500)
    offset: int = Field(0, ge=0)


@router.post("/rows")
def analyzed_pattern_rows(req: LiveRowsBody):
    """Source rows for a pattern from the profiler, which has no id yet.

    The profiler clusters on demand, so its patterns carry a cluster_id and no
    pattern_id — nothing has been saved. They still describe a real subset, and
    being able to look at it before deciding whether to keep the pattern is the
    point. POST rather than GET because the filter set is a structure, not a
    query string.
    """
    from sdg.analyzer.services import pattern_data

    try:
        return pattern_data.page_live(
            packs.active().primary, req.filters.as_kwargs(), req.k, req.cluster_id,
            req.limit, req.offset)
    except pattern_data.StalePattern as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))


class CertifyBody(BaseModel):
    discovered_id: int
    approved_by: str = "user"


@router.post("/{pattern_id}/certify")
def certify(pattern_id: int, req: CertifyBody):
    """Approve a merge: mark a user-defined pattern certified against the
    discovered pattern that matches its combination.

    Only ever called from a review action. Certification is never automatic —
    silently promoting a hypothesis to evidence would rewrite the provenance of
    data already generated under it.
    """
    try:
        return ps.certify(pattern_id, req.discovered_id, req.approved_by)
    except db.PatternArchived as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=errors.detail(e))


class PatternReportRequest(BaseModel):
    """A pattern of an analysis (members or claims), or a saved one by id."""
    domain: str = packs.active().primary
    filters: dict = Field(default_factory=dict)
    k: int | None = Field(None, ge=1, le=20)
    cluster_id: int | None = None
    claim_type: str | None = None
    pattern_id: int | None = None
    # The fields a scenario is a combination of; the default when omitted.
    scenario_keys: list[str] | None = None
    # A download: one pattern's name; saved patterns by id ([] is every one);
    # the file type.
    name: str | None = None
    pattern_ids: list[int] | None = None
    format: str = "xlsx"


def _scope(req: PatternReportRequest) -> dict:
    from sdg.analyzer.services import saved_patterns

    if is_child(req.domain):
        return saved_patterns._claims_filters(req.filters, req.claim_type)
    return saved_patterns._member_filters(req.filters)


@router.post("/report")
def pattern_report(req: PatternReportRequest):
    """One pattern's counts and shares: every field and value, every scenario."""
    from sdg.analyzer.services import pattern_data, pattern_report as pr

    if not (is_primary(req.domain) or is_child(req.domain)):
        raise HTTPException(status_code=422, detail=_DOMAIN_IS)
    if req.cluster_id is None and req.pattern_id is None:
        raise HTTPException(status_code=422, detail="Name a cluster_id or a pattern_id.")
    try:
        rows = pattern_data.every_row(req.domain, _scope(req), req.k,
                                      int(req.cluster_id or 0), req.pattern_id)
        return pr.report(req.domain, rows, req.scenario_keys)
    except pattern_data.StalePattern as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/report.xlsx")
def pattern_report_sheet(req: PatternReportRequest):
    """Patterns on one sheet: values with counts and %, then each scenario as
    one row. Saved patterns by id (`pattern_ids`, [] for all of them); one
    pattern of an analysis (`cluster_id`) or a saved one (`pattern_id`); or
    every pattern of an analysis. Excel, or CSV with `format`."""
    from fastapi.responses import Response

    from sdg.analyzer import db
    from sdg.analyzer.services import pattern_data, pattern_report as pr

    if not (is_primary(req.domain) or is_child(req.domain)):
        raise HTTPException(status_code=422, detail=_DOMAIN_IS)
    if req.format not in ("xlsx", "csv", "pdf"):
        raise HTTPException(status_code=422, detail="format is xlsx, csv or pdf.")
    try:
        if req.pattern_ids is not None:
            saved = [p for p in db.get_all_patterns() if not req.pattern_ids
                     or int(p["id"]) in set(req.pattern_ids)]
            if not saved:
                raise ValueError("No saved patterns to export.")
            patterns = pr.for_saved(saved, req.scenario_keys)
            stem = "saved-patterns"
        elif req.cluster_id is not None or req.pattern_id is not None:
            rows = pattern_data.every_row(req.domain, _scope(req), req.k,
                                          int(req.cluster_id or 0), req.pattern_id)
            patterns = [{"name": req.name or "Pattern", "share": None,
                         "report": pr.report(req.domain, rows, req.scenario_keys)}]
            stem = _file_stem(req.name or "pattern")
        else:
            patterns = pr.for_analysis(req.domain, _scope(req), req.k, req.scenario_keys)
            stem = f"{req.domain}-patterns"
    except pattern_data.StalePattern as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if req.format == "csv":
        body, media = pr.csv_text(patterns).encode("utf-8-sig"), "text/csv"
    elif req.format == "pdf":
        from sdg.analyzer.services import pattern_pdf

        body, media = pattern_pdf.document(patterns), "application/pdf"
    else:
        body = pr.workbook(patterns)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    name = f"{stem}.{req.format}"
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _file_stem(name: str) -> str:
    """A pattern's name as a file name: letters, digits and dashes."""
    import re

    stem = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return (stem or "pattern")[:80]

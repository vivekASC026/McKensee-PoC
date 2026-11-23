"""Request and response models.

Responses are deliberately flat and JSON-native — the React client shouldn't
have to know that any of this came out of a pandas DataFrame.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field

from sdg import packs
from sdg.analyzer.services import filter_models


# ── Hierarchy ────────────────────────────────────────────────────────────────

class Option(BaseModel):
    key: str
    label: str


def _overview_model(name: str, entity: str | None, required: bool) -> type[BaseModel]:
    """A record kind's headline counts, as its loader's overview names them."""
    from pydantic import create_model

    loader = (packs.active().features.get("loaders") or {}).get(entity or "") or {}
    # The counts its overview route answers with (loaders.<entity>.answers),
    # else every count its overview makes.
    counted = loader.get("answers") or [as_ for as_, _of in loader.get("overview") or []]
    return create_model(name, **{c: ((int, ...) if required else (int, 0)) for c in counted})


# The primary records' counts (health: members, groups, plan categories,
# plans, products) and the child records' (health: lines, claims, members,
# groups, plans, products, diagnoses, procedures).
OverviewMetrics = _overview_model("OverviewMetrics", packs.active().primary, required=True)


# ── Pattern analysis ─────────────────────────────────────────────────────────

# The primary records' filters, as the business pack's loader lists them
# (filter_models), and a defined pattern's rules beyond them.
Filters = filter_models.model("Filters", packs.active().primary)


class AnalyzeRequest(BaseModel):
    filters: Filters = Field(default_factory=Filters)
    k: Optional[int] = Field(None, ge=1, le=20,
                             description="None selects K by silhouette score; 1 is the whole scope")
    reference_date: Optional[str] = None


class ContinuousStats(BaseModel):
    mean: float
    std: float
    min: float
    max: float
    median: float
    # The eleven cut points of ten equal shares, which generation draws from.
    deciles: Optional[list[float]] = None


class CategoricalDist(BaseModel):
    counts: dict[str, int]
    pct: dict[str, float]


class Pattern(BaseModel):
    """Anything the client reads must be declared here.

    FastAPI serializes strictly against this model — a field the service
    computes but this class omits is silently dropped, and the client sees
    undefined. That failure is invisible server-side, so it is worth being
    deliberate about every addition.
    """
    cluster_id: int
    name: str
    size: int
    pct_of_subset: float
    # Share of the stored baseline for this scope. Drives the weight band.
    pct_of_total: float = 0.0
    rare: bool
    weight_band: str
    continuous: dict[str, ContinuousStats]
    categorical: dict[str, CategoricalDist]
    family: dict[str, Any]
    descriptions: dict[str, list[str]]
    # Counts per bin of the response's `distributions`, feature by feature:
    # the shape behind this pattern's mean and range.
    histograms: dict[str, list[int]] = {}
    # Where the pattern's members live: address records with their shares.
    geography: list[dict[str, Any]] = []
    # Its own combinations of each group of dependent features, drawn whole.
    combinations: list[dict[str, Any]] = []
    # The group, plan and product its members are enrolled in, with shares.
    enrollment: list[dict[str, Any]] = []


class AnalyzeResponse(BaseModel):
    total_members: int
    subset_members: int
    pct_of_total: float
    n_patterns: int

    # Denominator behind every weight on this response.
    scope_total: int = 0
    scope_label: str = ""
    scope_from_baseline: bool = False
    weight_explained: str = ""
    pct_outside_filter: float = 0.0

    method: str
    silhouette: Optional[float] = None
    filters_used: dict[str, Any]
    patterns: list[Pattern]
    cache_key: str

    # The percentile cut-offs the weight bands were read against, when a
    # baseline has been computed. Declared because a response model drops what
    # it does not name: the member profiler has always sent this and it has
    # always been stripped here, so the screen could not say where a band came
    # from while the claims profiler, which has no response model, could.
    band_thresholds: Optional[dict[str, Any]] = None

    # Each continuous feature over the whole subset, on the bins every
    # pattern's histogram is counted into.
    distributions: dict[str, Any] = {}


# ── Claims ───────────────────────────────────────────────────────────────────

ClaimsOverview = _overview_model("ClaimsOverview", packs.active().child, required=False)


class CodeCount(BaseModel):
    code: str
    description: Optional[str] = None
    n: int


# ── Story → Data ─────────────────────────────────────────────────────────────

class StoryParseResponse(BaseModel):
    stories: list[str]
    column_used: Optional[str] = None
    filename: Optional[str] = None


class StoryMatchRequest(BaseModel):
    stories: Optional[list[str]] = None
    text: Optional[str] = None
    top_n: int = Field(4, ge=1, le=10)


class PatternCandidate(BaseModel):
    pattern_id: int
    name: str
    member_count: int
    match_score: float
    match_count: int
    group: Optional[str] = None
    plan_category: Optional[str] = None
    plan: Optional[str] = None
    product: Optional[str] = None


class StoryMatch(BaseModel):
    index: int
    story: str
    data_type: str
    volume: Optional[int] = None
    keywords: list[str] = []
    raw_terms: list[str] = []
    confidence: float
    matched: bool
    candidates: list[PatternCandidate] = []


class StoryMatchResponse(BaseModel):
    stories: list[StoryMatch]
    warning: Optional[str] = None


class StoryAllocateRequest(BaseModel):
    pattern_ids: list[int]
    total_volume: int = Field(100, ge=1, le=100_000)

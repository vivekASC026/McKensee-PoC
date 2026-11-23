"""Story → Data endpoints.

The approval gate is structural, not cosmetic: /match never generates, and
/generate only accepts explicit pattern IDs the user selected.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from sdg import packs
from sdg.analyzer.services import story_service as svc
from sdg.analyzer.services.schemas import (
    StoryAllocateRequest, StoryMatchRequest, StoryMatchResponse, StoryParseResponse,
)
from sdg.discovery.errors import plain

# Pattern analysis, where the business pack offers it (phase 6b: every pack).
router = APIRouter(prefix="/story", tags=["story"],
                   dependencies=[Depends(packs.offered("pattern_analysis", "Pattern analysis"))])

log = logging.getLogger(__name__)

PREVIEW_ROWS = 50
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/parse", response_model=StoryParseResponse)
async def parse(file: UploadFile = File(...)):
    """Turn an uploaded file into a list of stories. Reads only — no matching."""
    # One byte past the limit is enough to know it is over: nothing larger is read.
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=(
            "That file is larger than 10 MB. Choose a smaller file."))
    try:
        stories, column = svc.parse_upload(file.filename or "", raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        # A file the reader could not open (a damaged spreadsheet, say).
        log.exception("A story upload could not be read")
        raise HTTPException(status_code=422, detail=(
            f"That file could not be read. Check it opens, then upload it "
            f"again: {plain(e)}"))

    if not stories:
        raise HTTPException(
            status_code=422,
            detail="No readable text in that file.",
        )
    return {"stories": stories, "column_used": column, "filename": file.filename}


class StoryReadRequest(BaseModel):
    story: str
    entity: str | None = None


@router.post("/read")
def read(req: StoryReadRequest):
    """A requirement read into the rules of a pattern, to analyze like one."""
    from sdg.analyzer.services import story_reading

    if not req.story.strip():
        raise HTTPException(status_code=422, detail="Write the requirement first.")
    try:
        return story_reading.read(req.story, req.entity)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/match", response_model=StoryMatchResponse)
def match(req: StoryMatchRequest):
    stories = req.stories or svc.split_text(req.text or "")
    if not stories:
        raise HTTPException(status_code=422, detail="No stories to match.")
    try:
        return svc.match_stories(stories, req.top_n)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


@router.post("/allocate")
def allocate(req: StoryAllocateRequest):
    """Split a total volume across the selected patterns by member count."""
    if not req.pattern_ids:
        raise HTTPException(status_code=422, detail="No patterns selected.")
    return {"allocation": svc.suggest_allocation(req.pattern_ids, req.total_volume)}





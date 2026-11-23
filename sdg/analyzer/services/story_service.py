"""Story → Data.

Two steps with a human gate between them:

    1. Match   — stories in, candidate patterns out, with a confidence score.
    2. Approve — the user picks what's right, then generation runs.

Nothing is written before approval. There is no LLM anywhere in this path:
matching is the deterministic keyword/tag scorer in keyword_search.py, and
generation is the same synthetic_* engines used everywhere else.
"""

import io
import json
from typing import Optional

import pandas as pd

from sdg import packs
from sdg.analyzer import db
from sdg.analyzer import pattern_scope as scope
from sdg.analyzer.keyword_search import allocate_volume, search

from .service import to_py

# Column names seen in qTest / ADO exports, used to pick the text column
# when a spreadsheet is uploaded.
TEXT_COL_HINTS = [
    "scenario", "description", "summary", "test case", "testcase",
    "test_case", "steps", "story", "user story", "requirement",
    "expected", "title", "name",
]


# ── Step 0: turn an upload into a list of stories ────────────────────────────

def split_text(text: str) -> list[str]:
    """Blank line separates stories; failing that, one story per line."""
    text = (text or "").strip()
    if not text:
        return []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    if len(blocks) > 1:
        return blocks
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return lines if len(lines) > 1 else [text]


def _auto_text_col(df: pd.DataFrame) -> Optional[str]:
    lowered = {str(c).strip().lower(): c for c in df.columns}
    for hint in TEXT_COL_HINTS:
        for low, original in lowered.items():
            if hint in low:
                return original
    # Fall back to the widest string column.
    best, best_len = None, -1
    for c in df.columns:
        if df[c].dtype == object:
            avg = df[c].astype(str).str.len().mean()
            if avg > best_len:
                best, best_len = c, avg
    return best


def _from_json(raw: bytes) -> list[str]:
    obj = json.loads(raw.decode("utf-8"))
    if isinstance(obj, list):
        out = []
        for item in obj:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                vals = [str(v) for v in item.values() if isinstance(v, str)]
                out.append(" — ".join(vals) if vals else json.dumps(item))
        return [s for s in out if s.strip()]
    if isinstance(obj, dict):
        return [str(v) for v in obj.values() if isinstance(v, str)]
    return []


# A .docx or .xlsx is a zip. The upload limit counts its compressed bytes, and
# a small zip can unpack to gigabytes, so what it unpacks to is capped as well,
# from the sizes its directory states, before anything is opened.
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000


def _check_zip(raw: bytes, kind: str) -> None:
    """Refuse a damaged zip, or one that unpacks to more than the cap."""
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as e:
        raise ValueError(f"That {kind} file is damaged or is not a {kind} file. "
                         f"Check it opens, then upload it again.") from e
    if len(entries) > MAX_ZIP_ENTRIES or \
            sum(e.file_size for e in entries) > MAX_UNPACKED_BYTES:
        raise ValueError(f"That {kind} file unpacks to more than "
                         f"{MAX_UNPACKED_BYTES // (1024 * 1024)} MB. Split it into "
                         f"smaller files, then upload them one at a time.")


def _from_docx(raw: bytes) -> list[str]:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise ValueError(
            "Reading .docx needs python-docx. Install it with: pip install python-docx"
        ) from e
    _check_zip(raw, ".docx")
    try:
        document = docx.Document(io.BytesIO(raw))
    except (KeyError, ValueError) as e:
        # A zip without the parts a document has (python-docx raises KeyError).
        raise ValueError("That .docx file could not be read as a Word document. "
                         "Check it opens, then upload it again.") from e
    return [p.text.strip() for p in document.paragraphs if p.text.strip()]


def parse_upload(filename: str, raw: bytes) -> tuple[list[str], Optional[str]]:
    """Return (stories, column_used). Raises ValueError with a fixable message."""
    name = (filename or "").lower()

    if name.endswith(".json"):
        try:
            return _from_json(raw), None
        except json.JSONDecodeError as e:
            raise ValueError(f"That file isn't valid JSON: {e}")

    if name.endswith(".docx"):
        return _from_docx(raw), None

    if name.endswith((".xlsx", ".xls", ".csv", ".tsv")):
        if name.endswith(".xlsx"):
            _check_zip(raw, ".xlsx")
        try:
            if name.endswith((".xlsx", ".xls")):
                df = pd.read_excel(io.BytesIO(raw))
            else:
                sep = "\t" if name.endswith(".tsv") else ","
                df = pd.read_csv(io.BytesIO(raw), sep=sep)
        except ImportError as e:
            raise ValueError(
                "Reading .xlsx needs openpyxl. Install it with: pip install openpyxl"
            ) from e
        except Exception as e:
            raise ValueError(f"Could not read that file: {e}")

        col = _auto_text_col(df)
        if col is None:
            raise ValueError("No text column found in that file.")
        stories = [s.strip() for s in df[col].astype(str).tolist() if s and s.strip()]
        return stories, str(col)

    # .txt, .md, anything else — treat as plain text
    try:
        return split_text(raw.decode("utf-8", errors="replace")), None
    except Exception as e:
        raise ValueError(f"Could not read that file as text: {e}")


# ── Step 1: match ────────────────────────────────────────────────────────────

def _pattern_summary(p: dict) -> dict:
    return {
        "pattern_id": int(p["id"]),
        "name": str(p.get("contextual_name") or f"Pattern {p['id']}"),
        "member_count": int(p.get("member_count") or 0),
        "match_score": float(p.get("match_score") or 0.0),
        "match_count": int(p.get("match_count") or 0),
        **scope.named_labels(scope.labels_of(p)),
        "scope": scope.path(scope.labels_of(p)),
        "scope_levels": scope.levels(scope.labels_of(p)),
    }


def match_stories(stories: list[str], top_n: int = 4) -> dict:
    """Score every saved pattern against each story."""
    if not db.get_all_patterns(include_archived=False):
        return {
            "stories": [],
            "warning": "No saved patterns yet. Save patterns from the profiler "
                       "before matching stories against them.",
        }

    results = []
    for idx, story in enumerate(stories):
        text = (story or "").strip()
        if not text:
            continue
        found = search(text)
        parsed = found.get("parsed", {})
        candidates = [_pattern_summary(p) for p in found.get("patterns", [])[:top_n]]
        best = candidates[0]["match_score"] if candidates else 0.0

        results.append({
            "index": idx,
            "story": text,
            "data_type": str(parsed.get("data_type") or packs.active().primary),
            "volume": parsed.get("volume"),
            "keywords": to_py(parsed.get("keywords") or []),
            "raw_terms": to_py(parsed.get("raw_terms") or []),
            "confidence": round(best, 3),
            # Nothing scored above zero means the matcher fell back to a broad
            # mix — say so rather than presenting noise as a match.
            "matched": best > 0.0,
            "candidates": candidates,
        })

    return {"stories": results, "warning": None}


def suggest_allocation(pattern_ids: list[int], total_volume: int) -> dict[int, int]:
    """Split a requested volume across patterns by member count."""
    patterns = {p["id"]: p for p in db.get_all_patterns(include_archived=False)}
    total = sum(int(patterns.get(pid, {}).get("member_count") or 0) for pid in pattern_ids)
    if total <= 0:
        even = max(1, total_volume // max(1, len(pattern_ids)))
        return {pid: even for pid in pattern_ids}
    weights = {
        pid: (int(patterns.get(pid, {}).get("member_count") or 0) / total)
        for pid in pattern_ids
    }
    return {int(k): int(v) for k, v in allocate_volume(weights, total_volume).items()}


# ── A pattern's stored filters ───────────────────────────────────────────────

def _clean_filter_values(key: str, values, numeric: set[str]) -> list:
    """Drop values that cannot be used as a filter.

    A stored scope can carry an empty string, or a blank left by an older
    save. The loaders build `IN (...)` lists of numbers with int() for the
    filters they read as ids (`numeric`), so a blank one surfaced as
    `invalid literal for int() with base 10: ''` instead of anything a user
    could act on.
    """
    if not isinstance(values, (list, tuple, set)):
        values = [values]

    out = []
    for v in values:
        if v is None:
            continue
        text = str(v).strip()
        if not text:
            continue
        if key in numeric:
            try:
                out.append(int(float(text)))
            except (TypeError, ValueError):
                continue
        else:
            out.append(text)
    return out


def _filters_from_pattern(pattern: dict) -> dict:
    """The scope a stored pattern was found in, as filters its loader takes.

    Read by pattern_scope: the row's scope_json, or on an older row the pack's
    scope columns without the record-kind tag (bookkeeping: passed back as a
    filter it matches no rows at all)."""
    numeric = scope.numeric_keys(scope.entity_of(pattern))
    out = {}
    for k, values in scope.filters_of(pattern).items():
        cleaned = _clean_filter_values(k, values, numeric)
        if cleaned:
            out[k] = cleaned
    return out

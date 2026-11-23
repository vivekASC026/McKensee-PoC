"""Keyword-driven pattern search.

Resolves user queries like "500 Ohio Medicaid dental claims for adults"
into matching patterns with relevance scores.

Matching is hybrid and deterministic (no LLM):
  1. Parse the query into terms + volume + data type.
  2. Expand terms with state / LOB / demographic aliases and simple
     singularization, plus synonym resolution from the tag vocabulary.
  3. Score every pattern by how well those terms hit its curated tags AND
     its real text fields (contextual name, group, plan category, LOB,
     product, AI summary). Substring/word-boundary matching means a term
     no longer has to equal a stored tag exactly to count.
  4. If nothing scores, fall back to a broad mix of all patterns.
"""

import re
from typing import Optional

from . import db
from sdg.packs import held


# ── Alias / normalization tables ─────────────────────────────────────────────
# Expands abbreviations and common phrasings to the words that actually appear
# in pattern text or tags. Keeps recall high without an LLM.
_ALIASES = held("story:keyword_aliases")

_SHORT_OK = {"oh", "ky", "in", "ga", "wv", "ar", "nc", "ma"}  # 2-letter tokens we still honor


def parse_query(query: str) -> dict:
    """Parse a user query into structured components.

    Returns dict with: keywords (resolved tags), volume, data_type, raw_terms.
    """
    text = query.lower().strip()

    # Extract volume (e.g., "500", "10000", "10k", "200K")
    volume = None
    vol_match = re.search(r'\b(\d+)\s*k\b', text)
    if vol_match:
        volume = int(vol_match.group(1)) * 1000
        text = text[:vol_match.start()] + text[vol_match.end():]
    else:
        vol_match = re.search(r'\b(\d{2,})\b', text)
        if vol_match:
            volume = int(vol_match.group(1))
            text = text[:vol_match.start()] + text[vol_match.end():]

    # Extract data type, in the pack's words (story.json reading.data_types)
    from sdg import packs
    from sdg.packs import held

    data_type = None
    for dtype, pattern in held("story:reading.data_types"):
        if re.search(pattern, text):
            data_type = dtype
            text = re.sub(pattern, '', text)
            break

    # Remove common stop words
    stop_words = {
        'give', 'me', 'get', 'create', 'generate', 'make', 'need',
        'want', 'for', 'with', 'the', 'a', 'an', 'of', 'in', 'and',
        'some', 'data', 'test', 'please', 'can', 'you', 'i',
    }
    raw_terms = [
        w for w in re.findall(r'[\w\-\+]+', text)
        if w not in stop_words and len(w) > 1
    ]

    # Resolve synonyms via the tag vocabulary
    keywords = db.resolve_synonyms(raw_terms) if raw_terms else []

    return {
        'keywords': keywords,
        'volume': volume,
        'data_type': data_type or packs.active().primary,
        'raw_terms': raw_terms,
    }


# ── Token expansion + scoring ────────────────────────────────────────────────

def _expand_tokens(raw_terms, keywords):
    """Build the set of query tokens used for scoring.

    Combines raw terms + resolved keywords, applies alias expansion and naive
    singularization. 2-letter tokens are dropped from free-text matching
    (to avoid false positives) unless they are known abbreviations.
    """
    base = set(raw_terms) | set(keywords)
    expanded = set()
    for t in base:
        t = t.strip().lower()
        if not t:
            continue
        expanded.add(t)
        if t in _ALIASES:
            expanded.add(_ALIASES[t])
        # naive singularization
        if len(t) > 3 and t.endswith("s"):
            expanded.add(t[:-1])
    # keep only useful tokens: length > 2, or a known short abbreviation
    return {t for t in expanded if len(t) > 2 or t in _SHORT_OK}


def _variants(tok):
    """Hyphen/space variants of a token for matching across formats."""
    return {tok, tok.replace("-", " "), tok.replace(" ", "-")}


def _wb(needle, haystack):
    """Whole-word/phrase match."""
    if not needle or not haystack:
        return False
    return re.search(r'\b' + re.escape(needle) + r'\b', haystack) is not None


def _pattern_text(p):
    """Concatenated, lowercased text fields of a pattern for matching."""
    from sdg.analyzer import pattern_scope

    hier = " ".join(pattern_scope.labels_of(p).values())
    return {
        "hier": hier.lower(),
        "name": str(p.get("contextual_name", "") or "").lower(),
        "summary": str(p.get("ai_summary", "") or "").lower(),
    }


def _score_pattern(p, tokens, tags):
    """Score a pattern against query tokens. Returns (score 0..1, n_hits).

    Weights: tag or hierarchy hit = 3, name hit = 2, summary hit = 1.
    Each token counts once at its highest-weight hit.
    """
    if not tokens:
        return 0.0, 0

    txt = _pattern_text(p)
    tagset = {str(t["tag"]).lower() for t in tags} if tags else set()

    matched = set()
    score = 0
    for tok in tokens:
        vs = _variants(tok)
        weight = 0
        if tagset & vs:
            weight = 3
        elif any(_wb(v, txt["hier"]) for v in vs):
            weight = 3
        elif any(_wb(v, txt["name"]) for v in vs):
            weight = 2
        elif any(_wb(v, txt["summary"]) for v in vs):
            weight = 1
        if weight:
            matched.add(tok)
            score += weight

    denom = max(1, len(tokens)) * 3
    return min(1.0, score / denom), len(matched)


def search(query: str) -> dict:
    """Search for patterns matching a query, ranked by relevance.

    Returns dict with:
        patterns: list of matching patterns, each with 'match_score' (0..1)
                  and 'match_count' (# tokens hit), sorted by relevance
        parsed: the parsed query structure
        total_members: sum of members across returned patterns
        weights: dict of pattern_id → weight (member-count proportional)
    """
    parsed = parse_query(query)
    tokens = _expand_tokens(parsed['raw_terms'], parsed['keywords'])

    all_patterns = db.get_all_patterns(include_archived=False)
    if not all_patterns:
        return {'patterns': [], 'parsed': parsed, 'total_members': 0, 'weights': {}}

    tags_by_id = db.get_tags_batch([p['id'] for p in all_patterns])

    scored = []
    for p in all_patterns:
        s, hits = _score_pattern(p, tokens, tags_by_id.get(p['id'], []))
        if s > 0:
            q = dict(p)
            q['match_score'] = round(s, 3)
            q['match_count'] = hits
            scored.append(q)

    if scored:
        scored.sort(key=lambda x: (x['match_score'], x['member_count']), reverse=True)
        patterns = scored
    else:
        # No signal (empty/unmatched query) → broad mix of all patterns
        patterns = []
        for p in all_patterns:
            q = dict(p)
            q['match_score'] = 0.0
            q['match_count'] = 0
            patterns.append(q)

    total = sum(p['member_count'] for p in patterns)
    weights = {p['id']: round(p['member_count'] / total, 4) if total > 0 else 0
               for p in patterns}

    return {
        'patterns': patterns,
        'parsed': parsed,
        'total_members': total,
        'weights': weights,
    }


def allocate_volume(weights: dict, total_volume: int) -> dict:
    """Distribute requested volume across patterns by weight.

    Returns dict of pattern_id → count, ensuring sum = total_volume.
    """
    if not weights:
        return {}

    allocation = {}
    remaining = total_volume

    sorted_ids = sorted(weights.keys(), key=lambda k: weights[k], reverse=True)
    for pid in sorted_ids[:-1]:
        count = max(1, round(weights[pid] * total_volume))
        allocation[pid] = count
        remaining -= count

    # Last pattern gets the remainder
    allocation[sorted_ids[-1]] = max(1, remaining)

    return allocation

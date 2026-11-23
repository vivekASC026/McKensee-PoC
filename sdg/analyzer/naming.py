"""Contextual pattern names, from the scope a pattern was found in and the
records in it.

Instead of "Pattern 0", "Pattern 1", a name is each scope level's name made
short, then the age band and the family shape of the pattern's records, then
its cluster id, e.g. (health):
  "OH-Medicaid-Dental-Adult-P0"
  "KY-MA-Medical-Senior-Family-P3"
How each level is shortened and what the age bands are called are the
pack's (features.json primary_analysis.naming).
"""

import os
from typing import Optional

from sdg.packs import held

NAMING = held("features:primary_analysis.naming")
# The state words a group's name is shortened by, where the pack names them
# (health; a level's "words_from" reads them). Kept by this name:
# tests/test_pack_constants.py holds it to the words kept before.
_STATE_ABBREVS = held("story:naming_states")


def _words(spec: dict) -> dict[str, str]:
    """The level's words: a part of its name, and what that part is short for."""
    if spec.get("words_from"):
        return dict(held(spec["words_from"]))
    return dict(spec.get("words") or {})


def _drop_words() -> list[str]:
    """Words taken out of a scope level's name, from .env (comma separated)."""
    from sdg.config import load_env

    load_env()
    return [w.strip() for w in os.getenv("SDG_NAMING_DROP_WORDS", "").split(",") if w.strip()]


def shorten(name: str, spec: dict) -> str:
    """One scope level's name, made short as the pack says.

    The first of its words found in the name (case aside) stands for it;
    otherwise, with the words to `drop` taken out (the pack's, and the
    installation's SDG_NAMING_DROP_WORDS: a customer's own name, which its
    group names carry and no pattern name needs), its first word, or its
    first `length` characters.
    """
    lower = name.lower()
    for part, short in _words(spec).items():
        if part in lower:
            return short
    length = int(spec.get("length", 12))
    fallback = spec.get("fallback", "prefix")
    if fallback == "first_word":
        rest = name
        for word in [*spec.get("drop", []), *_drop_words()]:
            rest = rest.replace(word, "")
        parts = rest.strip().split()
        return parts[0][:length] if parts else name[:int(spec.get("empty_length", length))]
    if fallback == "stripped":
        return name.strip()[:length]
    return name[:length]


def _age_label(profile: Optional[dict]) -> Optional[str]:
    """The age band of a pattern's records, from its continuous stats."""
    if not profile or not NAMING.get("age_feature"):
        return None
    age_stats = profile.get("continuous", {}).get(NAMING["age_feature"], {})
    if not age_stats:
        return None
    mean_age = age_stats.get("mean", 0)
    return next(word for below, word in NAMING["ages"] if below is None or mean_age < below)


def _family_label(profile: Optional[dict]) -> Optional[str]:
    """A family-structure label, where the pack counts households."""
    if not profile:
        return None
    family = profile.get("family", {})
    avg_dep = family.get("avg_dependents", 0)
    spouse_rate = family.get("spouse_rate", 0)
    if avg_dep >= 1.0 or spouse_rate >= 0.5:
        return "Family"
    if family and avg_dep < 0.3 and spouse_rate < 0.2:
        return "Single"
    return None


def build_contextual_name(labels: Optional[dict] = None, cluster_id: int = 0,
                          profile: Optional[dict] = None) -> str:
    """A readable pattern name: each scope level's name ({filter key: name},
    "All" where the pattern spans the level) made short, in the pack's
    order, then the age band, the family shape and the cluster id."""
    labels = labels or {}
    parts: list[str] = []
    for key, spec in NAMING.get("levels", {}).items():
        name = labels.get(key)
        if not name or name == "All":
            continue
        short = shorten(name, spec)
        if spec.get("unique") and short.lower() in [p.lower() for p in parts]:
            continue
        parts.append(short)

    age_lbl = _age_label(profile)
    if age_lbl:
        parts.append(age_lbl)

    fam_lbl = _family_label(profile)
    if fam_lbl:
        parts.append(fam_lbl)

    # Cluster ID suffix for disambiguation
    parts.append(f"P{cluster_id}")

    return "-".join(parts)

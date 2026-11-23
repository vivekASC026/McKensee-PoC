"""Reading a requirement into the rules of a pattern.

A tester writes "500 professional claims for female members aged 40 to 60
with type 2 diabetes on a PPO plan". Matching that against kept patterns finds
one only if somebody already kept it. Reading it instead gives the rules of a
pattern -- claim type, gender, age, diagnosis, plan -- which is then analyzed
like any user defined pattern: measured against the book, and looked inside.

The reading is local and plain. A feature's values are matched by their own
names in the catalog -- plan names, group names, diagnosis descriptions --
and by the words people use for the rest: female, married, Texas, aged 40 to
60, over $1,000, in network. What was read is said, with the words it came
from, so a wrong reading is seen and corrected rather than trusted.
"""

from __future__ import annotations

import re
from typing import Any

from . import defined_patterns
from sdg import packs
from sdg.packs import held, held_or, is_child, is_primary

STOP = set(held("story:stop_words"))

GENDER = [tuple(p) for p in held_or("story:gender", [])]
MARITAL = [tuple(p) for p in held_or("story:marital", [])]
CLAIM_TYPES = [tuple(p) for p in held("story:claim_types")]
NETWORK = [tuple(p) for p in held("story:network")]
AGE_WORDS = [(r, tuple(a)) for r, a in held("story:age_words")]

STATES = held("story:states")

NUMBER = r"(\d[\d,]*(?:\.\d+)?)"

# Words read as ages, genders and the like, and so never as part of a name: a
# group called Senior Services is not what "seniors" asks for.
SPOKEN_FOR = set(held("story:spoken_for"))


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 3 and t not in STOP]


def _same(a: str, b: str) -> bool:
    """One word for the other: diabetes and diabetic, hypertension and hypertensive."""
    if a == b:
        return True
    return len(a) >= 6 and len(b) >= 6 and a[:6] == b[:6]


# How a story is read, in the pack's words (story.json reading).
READING = held("story:reading")
_TABLES = {"gender": GENDER, "marital": MARITAL, "claim_types": CLAIM_TYPES, "network": NETWORK}


def entity_of(text: str) -> str:
    """The child records when the story is about them (the pack's words for
    them: health's claims, diagnoses, charges), otherwise the primary ones."""
    pack = packs.active()
    if pack.child and re.search(READING["child_words"], text, re.I):
        return pack.child
    return pack.primary


def _named(feature: dict[str, Any], words: list[str]) -> tuple[list[str], list[str]]:
    """The options a story names by the words of their names.

    A word counts only if it is one of these names' words and tells them apart
    -- held by some options, not all of them. The options named are those
    whose names hold the most of the words the story uses for this feature:
    "the Metro Transit Authority group" names Metro Transit Authority, not
    every group with "Group" in its name, nor none for want of one holding all.
    """
    options = [o for o in feature.get("options") or [] if o.get("label")]
    if not options:
        return [], []
    names = {o["value"]: _tokens(o["label"]) for o in options}
    share = {}
    for tokens in names.values():
        for token in set(tokens):
            share[token] = share.get(token, 0) + 1
    telling = {t for t, n in share.items() if n < len(options)}
    used = sorted({w for w in words for t in telling if _same(w, t)})
    if not used:
        return [], []
    held = {value: {w for w in used if any(_same(w, t) for t in tokens)}
            for value, tokens in names.items()}
    most = max(len(h) for h in held.values())
    chosen = [value for value, h in held.items() if len(h) == most]
    return chosen, sorted(set().union(*(held[v] for v in chosen)))


def read(text: str, entity: str | None = None) -> dict[str, Any]:
    """The rules a requirement states, with the words each was read from."""
    entity = entity or entity_of(text)
    lowered = text.lower()
    words = [w for w in _tokens(text) if w not in SPOKEN_FOR]
    features = {f["key"]: f for f in defined_patterns.catalog(entity)}
    rules: dict[str, dict[str, Any]] = {}
    found: list[dict[str, Any]] = []

    def state(key: str, rule: dict[str, Any], said: str) -> None:
        if key not in features:
            return
        rules[key] = rule
        found.append({"feature": key, "label": features[key]["label"],
                      "rule": rule, "from": said})

    # Names (health: groups, plans, products, diagnoses, procedures).
    for key in READING["names"]:
        if key in READING["named_by"] and READING["named_by"][key] in rules:
            continue  # named already by the one it belongs to (a plan, its product)
        if key in features:
            chosen, used = _named(features[key], words)
            if chosen:
                state(key, {"in": chosen}, " ".join(used))

    # Codes written as codes (health: a diagnosis, a procedure, a plan).
    for key in READING["codes"]:
        options = {o["value"].upper() for o in (features.get(key) or {}).get("options") or []}
        codes = sorted({c for c in re.findall(r"\b[A-Z0-9]{4,10}\b", text.upper()) if c in options})
        if codes and key not in rules:
            state(key, {"in": codes}, ", ".join(codes))

    # Words for the rest (health: gender, marital status, claim type, network).
    for key, table_name, role in READING["words"]:
        table = ([] if role == "child" and not is_child(entity)
                 else _TABLES.get(table_name) or [tuple(p) for p in held(f"story:{table_name}")])
        values, said = [], []
        for pattern, value in table:
            hit = re.search(pattern, lowered)
            if hit and value not in values:
                values.append(value)
                said.append(hit.group(0))
        if values:
            state(key, {"in": values}, ", ".join(said))
    where = READING.get("state")
    if is_primary(entity) and where and where in features:
        named = [code for name, code in STATES.items() if re.search(rf"\b{name}\b", lowered)]
        if named:
            state(where, {"in": named}, ", ".join(named))
    # One word that names a category (health: dental), unless something read
    # already narrows further.
    word = READING.get("category_word")
    if is_primary(entity) and word and not any(k in rules for k in word["unless"]) \
            and re.search(rf"\b{word['word']}\b", lowered):
        cat = features.get(word["key"])
        named = next((o["value"] for o in (cat or {}).get("options") or []
                      if o["value"] in word["values"]), None)
        if cat and named:
            state(word["key"], {"in": [named]}, word["word"])

    # Ages, as ranges or as words.
    age_key = READING["ages"]["child" if is_child(entity) else "primary"]
    age = None
    patterns = [
        (rf"\b(?:aged?|ages)\s*(?:between\s*)?{NUMBER}\s*(?:-|to|and|–)\s*{NUMBER}", "range"),
        (rf"\bbetween\s*{NUMBER}\s*(?:and|to)\s*{NUMBER}\s*(?:years?|yrs?)", "range"),
        (rf"\b{NUMBER}\s*(?:-|to|–)\s*{NUMBER}\s*(?:years?|yrs?)", "range"),
        (rf"\b(?:over|above|older than|at least)\s*{NUMBER}\s*(?:years?|yrs?)?(?!\s*(?:dollars|\$))", "min"),
        (rf"\b{NUMBER}\s*\+", "min"),
        (rf"\b(?:under|below|younger than)\s*{NUMBER}\s*(?:years?|yrs?)?", "max"),
    ]
    for pattern, kind in patterns:
        hit = re.search(pattern, lowered)
        if not hit:
            continue
        before = lowered[max(0, hit.start() - 2): hit.start()]
        if "$" in before or "$" in hit.group(0):
            continue
        if kind == "range":
            age = {"min": _number(hit.group(1)), "max": _number(hit.group(2))}
        elif kind == "min":
            age = {"min": _number(hit.group(1))}
        else:
            age = {"max": _number(hit.group(1))}
        said = hit.group(0)
        break
    if age is None:
        for pattern, (low, high) in AGE_WORDS:
            hit = re.search(pattern, lowered)
            if hit:
                age = {"min": low, **({"max": high} if high is not None else {})}
                said = hit.group(0)
                break
    if age is not None and all(v <= 120 for v in age.values()):
        state(age_key, age, said.strip(" ,.;"))

    # Amounts, on the child records (health: charges).
    if is_child(entity) and READING.get("amount"):
        charge = None
        hit = re.search(rf"\$\s*{NUMBER}\s*(?:-|to|and)\s*\$?\s*{NUMBER}", lowered)
        if hit:
            charge = {"min": _number(hit.group(1)), "max": _number(hit.group(2))}
        else:
            hit = re.search(rf"(?:over|above|more than|at least|exceeding)\s*\$\s*{NUMBER}", lowered)
            if hit:
                charge = {"min": _number(hit.group(1))}
            else:
                hit = re.search(rf"(?:under|below|less than)\s*\$\s*{NUMBER}", lowered)
                if hit:
                    charge = {"max": _number(hit.group(1))}
        if charge:
            state(READING["amount"], charge, hit.group(0))

    # How many.
    count = None
    hit = re.search(rf"\b{NUMBER}\s+(?:\w+\s+){{0,3}}?(?:{READING['count_nouns']})\b", lowered)
    if hit:
        count = int(_number(hit.group(1)))

    return {"entity": entity, "rules": rules, "found": found, "count": count, "story": text}

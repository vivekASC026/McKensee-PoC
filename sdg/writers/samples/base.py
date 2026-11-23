"""What every sample format shares: its error, the algorithms a path can name, and
reading the masking tool's configs, which are JSON with // comments."""

from __future__ import annotations


class SampleError(ValueError):
    """The sample or the config cannot be used, said in a sentence."""


# Algorithms every file can use whether or not the repository has a rule for
# them yet: a FACETS plan has no phone column, so no rule is ever measured for
# one, and a sample's phone places would have nothing to choose. A repository
# rule of the same name (the Algorithms screen) replaces it.
BUILT_IN_RULES: dict[str, dict] = {
    "PHONE": {"field_name": "PHONE", "field_label": "Phone Number", "element": "PHONE",
              "field_category": "contact", "gen_method": "pattern", "data_type": "text",
              # A US number: area code and exchange never start with 0 or 1. The
              # file keeps the sample's way of writing one (fit in engine.py).
              "format_pattern": r"\([2-9]\d{2}\) [2-9]\d{2}-\d{4}"},
}


def algorithm_rules(rules: dict[str, dict] | None = None) -> dict[str, dict]:
    """Every algorithm a position can name: the rules by element, the rules
    known only by their field name (STREET_1), which no element claims, and
    the built-in ones no repository rule has replaced."""
    from sdg.analyzer.services.layout_service import get_rules, rules_by_element

    out = dict(rules if rules is not None else rules_by_element())
    for rule in get_rules():
        key = str(rule.get("element") or rule.get("field_name") or "").strip().upper()
        if key and key not in out:
            out[key] = rule
    for key, rule in BUILT_IN_RULES.items():
        out.setdefault(key, dict(rule))
    return out


def without_comments(text: str) -> str:
    """JSON with // comments (the masking tool's configs), as JSON."""
    out, i, n, in_string = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)

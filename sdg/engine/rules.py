"""A repository rule, read as one of the engine's generators.

There is one set of generation algorithms: the engine's generators. A database
load runs them from the plan; a file, an EDI transaction or a layout runs them
from the rule repository. The repository keeps its own vocabulary -- the
methods a person picks on the Rules screen -- and this module is the reading
of each method as a generator and its parameters:

    constant     -> constant         the rule's value
    sequential   -> sequence         start, prefix, zero-filled width
    random       -> pattern          that many digits, inside prefix and suffix
    lookup       -> lookup           the rule's list, a reference table, or the
                                     combinations the pattern store holds
                 -> street_address   for the street list
    range        -> number           low, high, places, measured spread
    pattern      -> pattern          the format
    date         -> date_relative    an anchor day and days either side of it
    formatted    -> template         other fields of the row in a fixed shape
    default      -> nothing          the field is left blank

So a format, a list or a date window means the same wherever it is used, and
an algorithm fixed once is fixed everywhere.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sdg.engine.generators import (GenerationError, RowContext, algorithm, get_generator,
                                   register_algorithm)
from sdg.packs import held_or

# A claim's total, as the plan found it: the sum of a column of its lines.
SUM_PREFIX = "sum:"
# Methods a person can choose, with what each does, for the Rules screen.
METHODS = {
    "pattern": "Made to a format, such as three letters and eight digits.",
    "lookup": "Drawn from a list: values you give, or a reference list "
              "(names, streets, addresses).",
    "sequential": "A counter, with a prefix and a zero-filled width if wanted.",
    "random": "A random number of so many digits, with a prefix if wanted.",
    "constant": "The same value on every row.",
    "formatted": "Other fields of the row in a fixed shape, such as an email "
                 "address, or a claim's total of its lines.",
    "date": "A day within so many days either side of a stated day. Never "
            "from the clock.",
    "range": "A number between a lowest and a highest, to a stated number of "
             "places.",
    "default": "No rule: the field is written blank.",
}

_ADDRESS_FIELDS = (("ZIP", "ADDRESS_ZIP"), ("POSTAL", "ADDRESS_ZIP"),
                   ("CITY", "ADDRESS_CITY"), ("COUNTY", "ADDRESS_COUNTY"),
                   ("STATE", "ADDRESS_STATE"))
_SEX_FIELDS = tuple(held_or("pack:plan.sex_fields", ()))


class RuleError(ValueError):
    """A rule that cannot be read as a generator: said, never guessed."""


def generator_for(rule: dict[str, Any], field: str,
                  context: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]] | None:
    """The engine generator and parameters a rule means, or None for no value.

    Versioned like the generators: reading a rule differently is a new version
    of `rule reading`, and the old reading stays for files written with it."""
    return algorithm("rule reading")(rule, field, context)


def _generator_for_v1(rule: dict[str, Any], field: str,
                      context: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]] | None:
    method = str(rule.get("gen_method") or "default").strip().lower()
    if method == "constant":
        return "constant", {"value": str(rule.get("domain") or "")}
    if method in ("sequential", "sequence"):
        return "sequence", {"start": int(_number(rule.get("start_value"), 1)),
                            "width": _digits(rule)}
    if method == "random":
        return "pattern", {"regex": rf"\d{{{_digits(rule) or 6}}}"}
    if method == "lookup":
        return _lookup(rule, field, context)
    if method == "range":
        return "number", _range(rule)
    if method == "pattern":
        regex = str(rule.get("format_pattern") or "").strip()
        if not regex:
            regex = f"[A-Z0-9]{{{_text_width(rule) or 8}}}"
        return "pattern", {"regex": regex}
    if method == "date":
        return "date_relative", _window(rule)
    if method == "formatted":
        shape = str(rule.get("format_pattern") or "").strip()
        if shape.lower().startswith(SUM_PREFIX):
            # A total of lines. On one line it is that line's own amount; a
            # claim's lines are totalled where they are made together.
            shape = "{" + shape[len(SUM_PREFIX):].strip().upper() + "}"
        domain = str(rule.get("domain") or "").strip().lstrip("@")
        if not shape and "." in domain:
            # An email address at the rule's domain, from the person's own name.
            return "template", {"template": "{FIRST_NAME}.{LAST_NAME}@" + domain,
                                "lower": True}
        if not shape:
            raise RuleError(f"{field}: a formatted rule needs a template, such as "
                            f"{{FIRST_NAME}}.{{LAST_NAME}}@example.com")
        return "template", {"template": shape}
    if method == "default":
        return None
    raise RuleError(f"{field}: no such method {method!r}; one of {sorted(METHODS)}")


def run(rule: dict[str, Any], *, seed: str, row_index: int, field: str,
        context: dict[str, Any] | None = None, scenario: str = "file") -> str:
    """One value of a rule, made by the engine's generator, as text."""
    found = generator_for(rule, field, context)
    if found is None:
        return ""
    name, params = found
    ctx = RowContext(seed=seed, scenario=scenario, table="rule", field_name=field,
                     params=params, row_index=row_index, row=dict(context or {}))
    value = get_generator(name)(ctx)
    return algorithm("rule finish")(rule, _text(value))


def summed(rule: dict[str, Any] | None) -> str | None:
    """The element a rule totals over a claim's lines, if it is a total."""
    shape = str((rule or {}).get("format_pattern") or "")
    if str((rule or {}).get("gen_method") or "").lower() == "formatted" \
            and shape.lower().startswith(SUM_PREFIX):
        return shape[len(SUM_PREFIX):].strip().upper() or None
    return None


# ---------------------------------------------------------------------------
# Reading each method
# ---------------------------------------------------------------------------

def _lookup(rule: dict[str, Any], field: str,
            context: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    values = rule.get("values")
    if isinstance(values, list) and values:
        params = {k: rule[k] for k in ("weights", "field", "record_key", "given", "within")
                  if rule.get(k) not in (None, "", [], {})}
        return "lookup", {**params, "values": values}
    source = str(rule.get("lookup_source") or "").strip()
    if source.lower().startswith(("combo:", "pattern_combo:")):
        # Drawn with what it depends on before any plain list: a provider is
        # one in the member's plan network, never just any provider.
        try:
            return _named_list(source, field, context)
        except RuleError:
            pass
    listed =[v.strip() for v in str(rule.get("domain") or "").split(",") if v.strip()]
    if listed:
        params: dict[str, Any] = {"values": listed}
        weights = [w.strip() for w in str(rule.get("weights") or "").split(",") if w.strip()]
        if len(weights) == len(listed):
            params["weights"] = [float(w) for w in weights]
        return "lookup", params
    source = str(rule.get("lookup_source") or "").strip()
    if not source:
        raise RuleError(f"{field}: a lookup needs values or a list to draw from")
    return _named_list(source, field, context)


def _named_list(source: str, field: str,
                context: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """A lookup whose list the rule names rather than holds."""
    name = source.lower()
    if name.startswith(("combo:", "pattern_combo:")):
        return "lookup", _combination(source, field)
    from sdg.analyzer import generation_rules as tables

    if name in ("first_names", "last_names"):
        sex = _sex(context) if name == "first_names" else None
        return "lookup", {"values": tables.name_list(name, sex)}
    if name == "street_names":
        return "street_address", {"streets": tables.street_list()}
    if name in ("zip_city_state", "address", "pattern_address"):
        part = next((p for token, p in _ADDRESS_FIELDS if token in field.upper()),
                    "ADDRESS_ZIP")
        # The same list and key as every other address field, so a ZIP, its
        # city and state come from one draw.
        return "lookup", {"values": tables.address_records(), "field": part,
                          "record_key": "pattern_address"}
    raise RuleError(f"{field}: no list named {source!r}")


# Keyed by the group's id, so each entry holds the group itself: while it is
# held its id cannot be reused by another group. Bounded, since the store's
# groups are re-read whenever it changes.
_COMBINATIONS: dict[tuple[int, str], tuple[Any, dict[str, Any]]] = {}
_COMBINATIONS_MOST = 64


def _combination(source: str, field: str) -> dict[str, Any]:
    """The combinations the pattern store holds, as a lookup's records.

    A rule seeded from a plan that draws a diagnosis with its procedure names
    the combination; its records are read from the store as the plan read
    them. Every field of the combination draws from the same records with the
    same key, and agrees with what the claim or line already holds.
    """
    from sdg.plan.combinations import for_patterns

    wanted = source.split(":", 1)[1].split("+")
    group = next((g for g in for_patterns() if set(wanted) <= set(g.elements)), None)
    if group is None or field not in group.elements:
        raise RuleError(f"{field}: the pattern store holds no combinations of "
                        f"{' + '.join(wanted)}")
    key = (id(group), "all")
    entry = _COMBINATIONS.get(key)
    if entry is None or entry[0] is not group:
        if len(_COMBINATIONS) >= _COMBINATIONS_MOST:
            _COMBINATIONS.clear()
        # The whole group, not only the elements the rule was drawn with: a
        # provider agrees with the member's plan as well as the claim's type.
        entry = (group, {
            "values": [dict(zip(group.elements, v)) for v, _ in group.combinations],
            "weights": [float(n) for _, n in group.combinations]})
        _COMBINATIONS[key] = entry
    found = entry[1]
    return {**found, "field": field, "record_key": "combo:" + "+".join(sorted(group.elements)),
            "given": {e: e for e in group.elements if e != field}}


def _range(rule: dict[str, Any]) -> dict[str, Any]:
    low = _number(rule.get("min_value", rule.get("min")), 0.0)
    high = _number(rule.get("max_value", rule.get("max")), low)
    if high < low:
        low, high = high, low
    places = rule.get("decimals", rule.get("places"))
    params: dict[str, Any] = {
        "distribution": "uniform", "min": low, "max": high,
        "decimals": int(_number(places, 0 if _integer(rule) else 2)),
    }
    shape = str(rule.get("distribution") or "uniform").lower()
    if shape in ("normal", "lognormal") and rule.get("mean_value") is not None \
            and rule.get("stddev_value") is not None:
        params.update(distribution=shape, mean=float(rule["mean_value"]),
                      stddev=float(rule["stddev_value"]))
    if rule.get("bands"):
        params["bands"] = _bands(rule["bands"])
    return params


def _bands(bands: Any) -> list[list[float]]:
    """[low, high, share] slices a value is drawn from: a slice by its share,
    then a value within it."""
    out = []
    for band in bands or []:
        low, high, share = (float(x) for x in list(band)[:3])
        if share > 0:
            out.append([min(low, high), max(low, high), share])
    return out


def _window(rule: dict[str, Any]) -> dict[str, Any]:
    """An anchor day and the days either side of it.

    min_value and max_value are days from the anchor, as the plan measured
    them. A rule with only a span (start_value) reaches that many days back.
    """
    anchor = str(rule.get("format_pattern") or "").strip()[:10]
    try:
        date.fromisoformat(anchor)
    except ValueError:
        anchor = str(rule.get("as_of") or "2026-06-30")[:10]
    if rule.get("min_value") not in (None, "") and rule.get("max_value") not in (None, ""):
        low, high = sorted((int(float(rule["min_value"])), int(float(rule["max_value"]))))
    else:
        span = int(_number(rule.get("start_value"), 3650))
        low, high = (-max(span - 1, 0), 0)
    window = {"anchor": anchor, "min_days": low, "max_days": high}
    if rule.get("bands"):
        window["bands"] = _bands(rule["bands"])
    return window


# ---------------------------------------------------------------------------
# Small readings
# ---------------------------------------------------------------------------

def _number(value: Any, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _integer(rule: dict[str, Any]) -> bool:
    kind = str(rule.get("data_type") or "").lower()
    return kind.startswith(("int", "bigint", "smallint", "tinyint"))


def _text_width(rule: dict[str, Any]) -> int | None:
    """How wide a text value may be: the rule's length, or its column's."""
    if rule.get("length") not in (None, "", 0, "0"):
        return int(rule["length"])
    found = re.match(r"\s*n?(?:var)?char\s*\(\s*(\d+)\s*\)", str(rule.get("data_type") or ""),
                     re.IGNORECASE)
    return int(found.group(1)) if found else None


def _digits(rule: dict[str, Any]) -> int:
    """How many digits a counter or random number has, inside its prefix and suffix."""
    if rule.get("length") in (None, "", 0, "0"):
        return 0
    taken = len(str(rule.get("prefix") or "")) + len(str(rule.get("postfix") or ""))
    return max(1, int(rule["length"]) - taken)


def _sex(context: dict[str, Any] | None) -> str | None:
    for key in _SEX_FIELDS:
        value = str((context or {}).get(key) or "").strip().upper()
        if value in ("F", "M"):
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _decorate(rule: dict[str, Any], value: str) -> str:
    """A rule's prefix and suffix, and the width it declared."""
    if not value:
        return value
    text = f"{rule.get('prefix') or ''}{value}{rule.get('postfix') or ''}"
    if not _integer(rule) and (width := _text_width(rule)):
        text = text[:width]
    return text


register_algorithm("rule reading", 1, _generator_for_v1)
register_algorithm("rule finish", 1, _decorate)


__all__ = ["METHODS", "RuleError", "SUM_PREFIX", "GenerationError", "generator_for",
           "run", "summed"]

"""Turning a described segment into a written one.

A description says: this segment, this position, this rule. It never says which
column holds the value, because a column is a fact about one database and a
description that named one would only work against that database.

So resolution has two halves. A record is keyed by element, and where it
holds one -- a member who exists, a value a database load already produced --
that is what is written. Where it does not, the rule repository says how a
value of this element is made: a format, a list, a counter, a constant. A file
and a load then describe the same people rather than two populations that
merely look alike.

Positions are stated, not counted. `{"position": 9}` is the ninth element of
the segment whatever is in the eight before it, and the gaps fill themselves.
The old shape counted array entries and used `{"skip": 2}` to step over the
gaps, which is a way of writing down the same thing and getting it wrong.
"""

from __future__ import annotations

from typing import Any

ELEMENT_SEP = "*"
COMPOSITE_SEP = ":"


def segment_text(
    spec: dict[str, Any],
    record: dict[str, Any],
    rules: dict[str, dict],
    *,
    seed: str,
    row_index: int,
    as_of: str,
    context: dict[str, Any] | None = None,
) -> str | None:
    """One segment, or nothing where the description says to omit it."""
    guard = spec.get("only_if")
    if guard and not _value_for(
        guard, record, rules, seed=seed, row_index=row_index, as_of=as_of
    ):
        return None
    # What the record itself holds, never what a rule would make: a claim is
    # for a dependent when its member names a subscriber who is someone else,
    # and a rule drawing a subscriber's name would make every claim one.
    held = spec.get("only_if_held")
    if held and record.get(str(held).strip().upper()) in (None, ""):
        return None
    unless = spec.get("unless_held")
    if unless and record.get(str(unless).strip().upper()) not in (None, ""):
        return None

    loop = spec.get("loop")
    if loop and context is not None:
        # A loop the group before already opened -- a subscriber's second
        # claim -- is not written again: its first segment says whether it
        # is shared, and the rest of the loop follows.
        shared = context.setdefault("_shared", {})
        if keys := spec.get("once_per"):
            from sdg.engine.generators import algorithm

            keys = [str(k).strip().upper() for k in keys]
            shared[loop] = bool(algorithm("shared loops")(
                context.get("_previous"), record, keys))
        if shared.get(loop):
            return None

    elements = spec.get("elements") or []
    if not elements:
        return spec["segment"]

    highest = max(int(e.get("position", 0)) for e in elements)
    slots = [""] * (highest + 1)

    for element in elements:
        position = int(element.get("position", 0))
        if position < 1:
            continue
        slots[position] = _one(
            element, record, rules, seed=seed, row_index=row_index,
            as_of=as_of, context=context or {},
        )

    parts = [spec["segment"], *slots[1:]]
    while len(parts) > 1 and parts[-1] == "" and not spec.get("keep_trailing"):
        parts.pop()
    return ELEMENT_SEP.join(parts)


def _one(
    element: dict[str, Any],
    record: dict[str, Any],
    rules: dict[str, dict],
    *,
    seed: str,
    row_index: int,
    as_of: str,
    context: dict[str, Any],
) -> str:
    """One element: a rule's value, or the format's own vocabulary."""
    held = element.get("only_if_held")
    if held and record.get(str(held).strip().upper()) in (None, ""):
        # Written only where the record holds it: CLP05, the patient's share,
        # is there only when a remittance worked one out.
        return ""
    if total := element.get("total"):
        # A transaction's total over an element its groups wrote -- the 835's
        # payment, the sum of every claim's paid amount. Worked out after the
        # groups are built, so it adds up what was actually written.
        totals = context.get("_totals") or {}
        return f"{totals.get((total['segment'], int(total['position'])), 0.0):.2f}"

    if counter := element.get("counter"):
        # HL numbering: each hierarchical level gets the next number in the
        # transaction. `starts_at` is for a counter the header already used --
        # the billing provider is HL 1, so the first subscriber is HL 2.
        counters = context.setdefault("_counters", {})
        parents = context.setdefault("_parents", {})
        named = context.setdefault("_named", {})
        if parent := element.get("parent"):
            # HL02: the level this one sits under. Named, the number of the
            # last HL that level took -- a patient's HL names their
            # subscriber's even after a sibling's; else the number the
            # counter had before this level took the next.
            if isinstance(parent, str):
                return str(named.get(parent, ""))
            return str(parents.get(counter, ""))
        parents[counter] = counters.get(counter, int(element.get("starts_at", 0)))
        counters[counter] = parents[counter] + 1
        if names := element.get("names"):
            named[names] = counters[counter]
        return str(counters[counter])

    if "composite" in element:
        return COMPOSITE_SEP.join(
            _one(part, record, rules, seed=seed, row_index=row_index,
                 as_of=as_of, context=context)
            if isinstance(part, dict) else str(part)
            for part in element["composite"]
        )

    if "rule" in element:
        if element.get("sum"):
            # A claim's total is its lines added up. Stated separately it can
            # disagree with them, which is the one thing a total must not do.
            value = _summed(element["rule"], context.get("_group") or [record],
                            rules, seed=seed,
                            row_index=int(context.get("_group_start", row_index)),
                            as_of=as_of)
        else:
            value = _value_for(
                element["rule"], record, rules,
                seed=seed, row_index=row_index, as_of=as_of,
            )
        if value and element.get("date"):
            value = _compact(value)
        if value and element.get("digits"):
            # An identifier as X12 wants it: an SSN is nine digits, not the
            # dashed form a person types.
            value = "".join(ch for ch in value if ch.isdigit())
        if value and (mapping := element.get("map")):
            value = str(mapping.get(value.strip(), element.get("const", value)))
        if not value and "const" in element:
            return str(element["const"])
        return value

    const = element.get("const", "")
    # A context name rather than a literal: the interchange's own date and
    # reference, which belong to the envelope rather than to any rule.
    if isinstance(const, str) and const.startswith("_"):
        return str(context.get(const, ""))
    return str(const)


def _value_for(
    name: str,
    record: dict[str, Any],
    rules: dict[str, dict],
    *,
    seed: str,
    row_index: int,
    as_of: str,
) -> str:
    """What this element holds, for this row."""
    from sdg.writers import rule_values

    key = str(name).strip().upper()
    held = record.get(key)
    if held not in (None, ""):
        return rule_values._as_text(held)
    rule = rules.get(key)
    if rule is None:
        # A description naming a rule nobody has made writes nothing there.
        return ""
    return rule_values.produce(
        {**rule, "element": key}, seed=seed, row_index=row_index,
        record=record, as_of=as_of,
    )


def _summed(name, records, rules, *, seed, row_index, as_of) -> str:
    """An element added up over a group's records."""
    total = 0.0
    for offset, record in enumerate(records):
        text = _value_for(name, record, rules, seed=seed,
                          row_index=row_index + offset, as_of=as_of)
        try:
            total += float(text)
        except ValueError:
            continue
    return f"{total:.2f}"


def _compact(value: str) -> str:
    """A date as X12 writes one."""
    text = str(value)
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10].replace("-", "")
    return text

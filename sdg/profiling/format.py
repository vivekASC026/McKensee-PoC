"""Fit a regular expression to a column's values.

Values are tokenized into character classes, runs of the same class collapse
into counts, and the result is a signature: MBR00123456 becomes [A-Z]{3} then
\\d{8}. Signatures are then offered as candidate patterns from the most
specific to the most general, and the first one covering the required share of
the sample wins. Smallest space first means the pattern says as much as the
data supports and no more.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import groupby
from typing import Iterable, Sequence

COVERAGE_TARGET = 0.95

# A literal is only emitted when enough values agree on the character, so a
# small sample cannot turn a coincidence into a rule.
MIN_VALUES_FOR_LITERAL = 20

UPPER, LOWER, DIGIT, SPACE = "U", "L", "D", "S"

CLASS_PATTERN = {
    UPPER: "[A-Z]",
    LOWER: "[a-z]",
    DIGIT: r"\d",
    SPACE: r"\s",
}
CLASS_SIZE = {UPPER: 26, LOWER: 26, DIGIT: 10, SPACE: 1}


def char_class(char: str) -> str:
    if char.isdigit():
        return DIGIT
    if char.isalpha():
        return UPPER if char.isupper() else LOWER
    if char.isspace():
        return SPACE
    return char  # punctuation stays literal


_ASCII_CLASS = {chr(code): char_class(chr(code)) for code in range(128)}


def signature(value: str) -> tuple[tuple[str, int], ...]:
    """Collapse a value into (class, run length) pairs."""
    classes = [_ASCII_CLASS.get(char) or char_class(char) for char in value]
    return tuple((cls, len(list(run))) for cls, run in groupby(classes))


def _quantify(count_low: int, count_high: int) -> str:
    if count_low == count_high:
        return "" if count_low == 1 else f"{{{count_low}}}"
    return f"{{{count_low},{count_high}}}"


def _render_part(cls: str, low: int, high: int) -> str:
    body = CLASS_PATTERN.get(cls) or re.escape(cls)
    return body + _quantify(low, high)


def signature_pattern(sig: Sequence[tuple[str, int]]) -> str:
    return "".join(_render_part(cls, count, count) for cls, count in sig)


def _space_of(parts: Sequence[tuple[str, int, int]]) -> int:
    """Roughly how many strings a candidate can produce. Used to rank candidates."""
    total = 1
    for cls, low, high in parts:
        size = CLASS_SIZE.get(cls, 1)
        total *= sum(size**k for k in range(low, high + 1)) or 1
    return total


@dataclass
class FittedFormat:
    regex: str
    coverage: float
    sample_size: int
    matched: int
    kind: str  # exact | literal | ranged | merged | fallback

    def as_dict(self) -> dict:
        return {
            "regex": self.regex,
            "coverage": round(self.coverage, 4),
            "sample_size": self.sample_size,
            "matched": self.matched,
            "kind": self.kind,
        }


def _with_literals(values: Sequence[str], sig: Sequence[tuple[str, int]],
                   count: int | None = None) -> str:
    """Replace positions where every value agrees on the character with a literal.

    PD000001 and PD000002 share the "PD" prefix, so the fitted format becomes
    PD\\d{6} rather than [A-Z]{2}\\d{6}. `values` may be the distinct values,
    with `count` how many there were in all.
    """
    if (len(values) if count is None else count) < MIN_VALUES_FOR_LITERAL:
        return signature_pattern(sig)

    length = sum(count for _, count in sig)
    if any(len(v) != length for v in values):
        return signature_pattern(sig)

    constant: list[str | None] = []
    for position in range(length):
        chars = {v[position] for v in values}
        constant.append(chars.pop() if len(chars) == 1 else None)

    parts: list[str] = []
    position = 0
    for cls, count in sig:
        if cls == DIGIT:
            parts.append(_render_part(cls, count, count))
            position += count
            continue
        run = constant[position : position + count]
        # Split the run into literal and free stretches.
        index = 0
        while index < count:
            if run[index] is not None:
                start = index
                while index < count and run[index] is not None:
                    index += 1
                parts.append(re.escape("".join(run[start:index])))
            else:
                start = index
                while index < count and run[index] is None:
                    index += 1
                parts.append(_render_part(cls, index - start, index - start))
        position += count
    return "".join(parts)


def _ranged_pattern(sigs: Iterable[tuple[tuple[str, int], ...]]) -> str | None:
    """Merge signatures that share a class sequence into one with count ranges."""
    sigs = list(sigs)
    classes = {tuple(cls for cls, _ in sig) for sig in sigs}
    if len(classes) != 1:
        return None
    sequence = classes.pop()
    parts = []
    for position, cls in enumerate(sequence):
        counts = [sig[position][1] for sig in sigs]
        parts.append(_render_part(cls, min(counts), max(counts)))
    return "".join(parts)


def _fallback_pattern(values: Sequence[str]) -> str:
    """The widest useful statement: which classes appear, and how long values are."""
    classes = {char_class(c) for c in set().union(*map(set, values))}
    body_parts = []
    if UPPER in classes:
        body_parts.append("A-Z")
    if LOWER in classes:
        body_parts.append("a-z")
    if DIGIT in classes:
        body_parts.append("0-9")
    if SPACE in classes:
        body_parts.append(" ")
    literals = sorted(c for c in classes if c not in CLASS_PATTERN)
    body_parts.extend(re.escape(c) for c in literals)
    body = "[" + "".join(body_parts) + "]" if body_parts else "."
    lengths = [len(v) for v in values]
    return body + _quantify(min(lengths), max(lengths))


def _coverage(regex: str, values: Sequence[str] | Counter) -> tuple[int, float]:
    """How many values the pattern matches, and what share. Given a Counter,
    each distinct value is matched once and counted as often as it occurs."""
    try:
        compiled = re.compile(f"^(?:{regex})$")
    except re.error:
        return 0, 0.0
    frequency = values if isinstance(values, Counter) else Counter(values)
    matched = sum(n for v, n in frequency.items() if compiled.match(v))
    return matched, matched / sum(frequency.values())


def fit_format(
    values: Iterable[str], *, target: float = COVERAGE_TARGET
) -> FittedFormat | None:
    """Return the smallest pattern covering at least `target` of the values."""
    # Each distinct value is looked at once and weighted by how often it
    # occurs: a code column of a hundred thousand rows holds a few dozen
    # values, and reading every character of every row was most of a read.
    # Values already counted may be passed as a Counter of value to count.
    if isinstance(values, Counter):
        frequency = Counter()
        for v, n in values.items():
            if v is not None and str(v) != "" and n > 0:
                frequency[str(v)] += n
    else:
        frequency = Counter(str(v) for v in values if v is not None and str(v) != "")
    if not frequency:
        return None
    total = sum(frequency.values())

    counts: Counter = Counter()
    by_signature: dict[tuple, list[str]] = defaultdict(list)
    for value, n in frequency.items():
        sig = signature(value)
        counts[sig] += n
        by_signature[sig].append(value)

    candidates: list[tuple[int, str, str]] = []  # (space, regex, kind)

    # 1. Each exact signature, with literals where the values all agree.
    for sig, n in counts.most_common(8):
        literal = _with_literals(by_signature[sig], sig, n)
        candidates.append((_space_of([(c, n, n) for c, n in sig]), literal, "literal"))
        plain = signature_pattern(sig)
        if plain != literal:
            candidates.append((_space_of([(c, n, n) for c, n in sig]) + 1, plain, "exact"))

    # 2. Signatures that share a class sequence, merged into count ranges.
    by_classes: dict[tuple, list[tuple]] = defaultdict(list)
    for sig in counts:
        by_classes[tuple(cls for cls, _ in sig)].append(sig)
    for group in by_classes.values():
        if len(group) > 1:
            merged = _ranged_pattern(group)
            if merged:
                candidates.append((_space_of(_parts_of(merged, group)), merged, "ranged"))

    # 3. Everything the data contains, stated as widely as necessary.
    candidates.append((10**30, _fallback_pattern(list(frequency)), "fallback"))

    seen: set[str] = set()
    for _space, regex, kind in sorted(candidates, key=lambda c: c[0]):
        if regex in seen:
            continue
        seen.add(regex)
        matched, coverage = _coverage(regex, frequency)
        if coverage >= target:
            return FittedFormat(
                regex=regex,
                coverage=coverage,
                sample_size=total,
                matched=matched,
                kind=kind,
            )
    return None


def _parts_of(_regex: str, group: list[tuple]) -> list[tuple[str, int, int]]:
    sequence = tuple(cls for cls, _ in group[0])
    return [
        (cls, min(sig[i][1] for sig in group), max(sig[i][1] for sig in group))
        for i, cls in enumerate(sequence)
    ]


def matches_format(values: Iterable[str], regex: str) -> float:
    """Share of values a pattern covers. Used by the mapping evidence matchers."""
    cleaned = [str(v) for v in values if v is not None and str(v) != ""]
    if not cleaned:
        return 0.0
    return _coverage(regex, cleaned)[1]

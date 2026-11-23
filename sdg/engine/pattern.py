"""Turn a regular expression into a value generator.

The pattern is parsed into a small tree whose every node knows how many strings
it can produce. That makes the pattern space addressable: value number *i* is a
well-defined string. Two things follow.

Unique columns come free. Walking the space with a stride that is coprime with
its size visits every value once before repeating, so a unique business key
needs no collision set and no retries.

The pattern space is a number the planner can check. A key with 10,000 possible
values cannot fill a million rows, and the validator can say so before the run
starts rather than after it fails.

Supported syntax: literals, escapes (\\d \\w \\s and their negations), character
classes with ranges and negation, dot, groups, alternation, and the quantifiers
{n}, {n,m}, ?, * and +. Unbounded quantifiers are capped so the space stays
finite.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

UNBOUNDED_CAP = 8
# Bounds that keep a typed format from hanging or exhausting a worker. The
# widest is SQL Server's widest non-max character column.
MAX_PATTERN = 1000   # characters in a pattern
MAX_REPEAT = 1000    # the largest count a quantifier may state
MAX_WIDTH = 8000     # characters in the longest value a pattern may produce
MASK64 = (1 << 64) - 1

DIGITS = string.digits
LETTERS = string.ascii_letters
WORD = LETTERS + DIGITS + "_"
WHITESPACE = " \t"
PRINTABLE = string.ascii_letters + string.digits + " "

# Strides tried in order when walking a pattern space; the first one coprime
# with the space size is used, which guarantees a full-period walk.
STRIDES = (
    982_451_653, 15_485_863, 32_452_843, 49_979_687, 67_867_979,
    86_028_121, 104_729, 7, 3, 2, 1,
)


class PatternError(ValueError):
    """A pattern this generator cannot handle."""


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


class Node:
    """A node that knows its cardinality and can render value number *i*."""

    cardinality: int

    def render(self, index: int) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def widest(self) -> int:  # pragma: no cover - interface
        """Characters the longest value this node can produce."""
        raise NotImplementedError


@dataclass
class Chars(Node):
    """One character drawn from a set."""

    options: str

    def __post_init__(self) -> None:
        if not self.options:
            raise PatternError("character class is empty")
        self.cardinality = len(self.options)

    def render(self, index: int) -> str:
        return self.options[index]

    def widest(self) -> int:
        return 1


@dataclass
class Concat(Node):
    parts: list[Node]

    def __post_init__(self) -> None:
        _check_width(sum(part.widest() for part in self.parts))
        self.cardinality = 1
        for part in self.parts:
            self.cardinality *= part.cardinality

    def render(self, index: int) -> str:
        # Mixed-radix decomposition; the last part is the least significant digit.
        pieces: list[str] = []
        for part in reversed(self.parts):
            size = part.cardinality
            pieces.append(part.render(index % size))
            index //= size
        pieces.reverse()
        return "".join(pieces)

    def widest(self) -> int:
        return sum(part.widest() for part in self.parts)


@dataclass
class Alternate(Node):
    branches: list[Node]

    def __post_init__(self) -> None:
        self.cardinality = sum(b.cardinality for b in self.branches)

    def render(self, index: int) -> str:
        for branch in self.branches:
            if index < branch.cardinality:
                return branch.render(index)
            index -= branch.cardinality
        raise PatternError("index outside the alternation space")

    def widest(self) -> int:
        return max(branch.widest() for branch in self.branches)


@dataclass
class Repeat(Node):
    node: Node
    low: int
    high: int

    def __post_init__(self) -> None:
        if self.low < 0 or self.high < self.low:
            raise PatternError(f"bad repeat bounds {{{self.low},{self.high}}}")
        if self.high > MAX_REPEAT:
            raise PatternError(
                f"a repeat of {self.high} is more than the {MAX_REPEAT} allowed"
            )
        _check_width(self.node.widest() * self.high)
        self._sizes = [self.node.cardinality**k for k in range(self.low, self.high + 1)]
        self.cardinality = sum(self._sizes)

    def render(self, index: int) -> str:
        unit = self.node.cardinality
        for offset, size in enumerate(self._sizes):
            if index < size:
                pieces: list[str] = []
                for _ in range(self.low + offset):
                    pieces.append(self.node.render(index % unit))
                    index //= unit
                pieces.reverse()
                return "".join(pieces)
            index -= size
        raise PatternError("index outside the repeat space")

    def widest(self) -> int:
        return self.node.widest() * self.high


def _check_width(width: int) -> None:
    if width > MAX_WIDTH:
        raise PatternError(
            f"this format can produce values {width} characters long; the most "
            f"allowed is {MAX_WIDTH}"
        )


@dataclass
class Empty(Node):
    def __post_init__(self) -> None:
        self.cardinality = 1

    def render(self, index: int) -> str:
        return ""

    def widest(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.pos = 0

    def parse(self) -> Node:
        node = self._alternation()
        if self.pos != len(self.pattern):
            raise PatternError(
                f"unexpected {self.pattern[self.pos]!r} at position {self.pos} in {self.pattern!r}"
            )
        return node

    def _alternation(self) -> Node:
        branches = [self._concat()]
        while self._peek() == "|":
            self.pos += 1
            branches.append(self._concat())
        return branches[0] if len(branches) == 1 else Alternate(branches)

    def _concat(self) -> Node:
        parts: list[Node] = []
        while self.pos < len(self.pattern) and self._peek() not in "|)":
            parts.append(self._quantified())
        if not parts:
            return Empty()
        return parts[0] if len(parts) == 1 else Concat(parts)

    def _quantified(self) -> Node:
        node = self._atom()
        while True:
            char = self._peek()
            if char == "{":
                low, high = self._braces()
            elif char == "?":
                self.pos += 1
                low, high = 0, 1
            elif char == "*":
                self.pos += 1
                low, high = 0, UNBOUNDED_CAP
            elif char == "+":
                self.pos += 1
                low, high = 1, UNBOUNDED_CAP
            else:
                return node
            node = Repeat(node, low, high)

    def _braces(self) -> tuple[int, int]:
        end = self.pattern.find("}", self.pos)
        if end < 0:
            raise PatternError(f"unclosed {{ at position {self.pos}")
        body = self.pattern[self.pos + 1 : end]
        self.pos = end + 1
        try:
            if "," in body:
                low_text, high_text = body.split(",", 1)
                low = int(low_text) if low_text else 0
                high = int(high_text) if high_text else low + UNBOUNDED_CAP
            else:
                low = high = int(body)
        except ValueError as exc:
            raise PatternError(f"bad repeat count {{{body}}}") from exc
        return low, high

    def _atom(self) -> Node:
        char = self._peek()
        if char is None:
            raise PatternError("pattern ended early")
        if char == "(":
            self.pos += 1
            # Non-capturing groups are accepted and treated the same way.
            if self.pattern.startswith("?:", self.pos):
                self.pos += 2
            node = self._alternation()
            if self._peek() != ")":
                raise PatternError("unclosed (")
            self.pos += 1
            return node
        if char == "[":
            return Chars(self._character_class())
        if char == "\\":
            self.pos += 1
            return Chars(self._escape(self._take()))
        if char == ".":
            self.pos += 1
            return Chars(PRINTABLE)
        if char in "^$":
            # Anchors carry no characters; the whole value is generated anyway.
            self.pos += 1
            return Empty()
        if char in "*+?{}|)":
            raise PatternError(f"unexpected {char!r} at position {self.pos}")
        self.pos += 1
        return Chars(char)

    def _character_class(self) -> str:
        self.pos += 1  # past [
        negated = self._peek() == "^"
        if negated:
            self.pos += 1
        options: list[str] = []
        first = True
        while True:
            char = self._peek()
            if char is None:
                raise PatternError("unclosed [")
            if char == "]" and not first:
                self.pos += 1
                break
            first = False
            self.pos += 1
            if char == "\\":
                options.extend(self._escape(self._take()))
                continue
            if self._peek() == "-" and self.pattern[self.pos + 1 : self.pos + 2] not in ("]", ""):
                self.pos += 1
                upper = self._take()
                if ord(upper) < ord(char):
                    raise PatternError(f"reversed range {char}-{upper}")
                options.extend(chr(c) for c in range(ord(char), ord(upper) + 1))
                continue
            options.append(char)
        seen = dict.fromkeys(options)
        if negated:
            return "".join(c for c in PRINTABLE if c not in seen) or PRINTABLE
        return "".join(seen)

    @staticmethod
    def _escape(char: str) -> str:
        table = {
            "d": DIGITS,
            "D": LETTERS + " ",
            "w": WORD,
            "W": " .,-",
            "s": WHITESPACE,
            "S": LETTERS + DIGITS,
            "n": "\n",
            "t": "\t",
        }
        return table.get(char, char)

    def _peek(self) -> str | None:
        return self.pattern[self.pos] if self.pos < len(self.pattern) else None

    def _take(self) -> str:
        if self.pos >= len(self.pattern):
            raise PatternError("pattern ended early")
        char = self.pattern[self.pos]
        self.pos += 1
        return char


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class CompiledPattern:
    """A parsed pattern, ready to produce values."""

    __slots__ = ("regex", "_root", "space", "_stride")


    def __init__(self, regex: str) -> None:
        self.regex = regex
        if len(regex) > MAX_PATTERN:
            raise PatternError(
                f"the format is {len(regex)} characters; the most allowed is {MAX_PATTERN}"
            )
        try:
            self._root = _Parser(regex).parse()
        except RecursionError as exc:
            raise PatternError("the format nests groups too deeply to read") from exc
        self.space = self._root.cardinality
        if self.space <= 0:
            raise PatternError(f"pattern {regex!r} can produce no values")
        self._stride = next(
            (s for s in STRIDES if _gcd(s, self.space) == 1), 1
        )

    def widest(self) -> int:
        """Characters in the longest value this pattern can produce."""
        return self._root.widest()

    def value_at(self, index: int) -> str:
        return self._root.render(index % self.space)

    def unique_value(self, row_index: int, offset: int) -> str:
        """The row_index-th value of a full-period walk starting at `offset`.

        Distinct row indices give distinct values until the space is exhausted.
        The walk is permuted so neighboring rows do not get neighboring values.
        """
        walked = (offset + row_index * self._stride) % self.space
        return self.value_at(permute_index(walked, self.space, offset))

    def random_value(self, draw: int) -> str:
        return self.value_at(draw % self.space)


# --- index permutation ------------------------------------------------------
#
# A stride walk covers the space exactly once, but consecutive row indices then
# land a constant distance apart, so the values come out looking like a counter.
# Running the index through a small Feistel network first fixes that: it is a
# bijection, so uniqueness is untouched, and neighboring indices land far apart.

_ROUNDS = 4


def _mix64(value: int) -> int:
    value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9 & MASK64
    value = (value ^ (value >> 27)) * 0x94D049BB133111EB & MASK64
    return (value ^ (value >> 31)) & MASK64


def permute_index(index: int, space: int, key: int) -> int:
    """A keyed bijection of [0, space) onto itself."""
    if space <= 2:
        return index % space
    bits = max(2, (space - 1).bit_length())
    half = (bits + 1) // 2
    mask = (1 << half) - 1
    value = index % space
    # Cycle walking: iterate the permutation until it lands back inside range.
    for _ in range(64):
        left, right = value >> half, value & mask
        for round_number in range(_ROUNDS):
            feistel = _mix64(((right + 1) * 0x9E3779B97F4A7C15 ^ key ^ (round_number * 0x2545F491)) & MASK64)
            left, right = right, (left ^ (feistel & mask)) & mask
        value = ((left << half) | right) & ((1 << (2 * half)) - 1)
        if value < space:
            return value
    return index % space


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


@lru_cache(maxsize=256)
def compile_pattern(regex: str) -> CompiledPattern:
    """Parse and cache a pattern. Plans reuse the same few patterns heavily."""
    return CompiledPattern(regex)


def pattern_space(regex: str) -> int:
    """How many distinct values a pattern can produce."""
    return compile_pattern(regex).space


def generate_many(regex: str, count: int, *, unique: bool = False, offset: int = 0) -> Sequence[str]:
    """Convenience used by tests and the preview grid."""
    compiled = compile_pattern(regex)
    if unique:
        return [compiled.unique_value(i, offset) for i in range(count)]
    return [compiled.value_at(offset + i) for i in range(count)]

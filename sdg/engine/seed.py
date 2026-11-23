"""Deterministic seed engine.

Every generated value is a function of the named seed, the scenario, the field,
and the row position. Nothing reads the clock or the process random state, so
the same plan and seed produce the same data on any machine.

The hash is BLAKE2b keyed by the scenario name, as the build brief specifies.
The 64-bit digest then drives a SplitMix64 stream, which is pure integer
arithmetic and therefore identical across platforms and Python builds.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from typing import Sequence

MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15
_MIX_A = 0xBF58476D1CE4E5B9
_MIX_B = 0x94D049BB133111EB

# BLAKE2b accepts a key of at most 64 bytes.
MAX_KEY_BYTES = 64


def value_seed(seed: str, scope: str, field: str, row_index: int) -> int:
    """Return a stable 64-bit integer for one value.

    seed   the named seed for the run
    scope  the scenario name; it keys the hash
    field  the field or element the value belongs to
    row_index  the row position inside its table
    """
    digest = hashlib.blake2b(
        key=scope.encode("utf-8")[:MAX_KEY_BYTES],
        digest_size=8,
    )
    digest.update(seed.encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(field.encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(str(row_index).encode("ascii"))
    return int.from_bytes(digest.digest(), "big")


class ValueRandom:
    """A SplitMix64 stream started from one 64-bit seed.

    Deliberately not Python's `random`: this must give the same numbers on any
    interpreter, and it must be cheap to create one stream per value.
    """

    __slots__ = ("_state",)

    def __init__(self, state: int) -> None:
        self._state = state & MASK64

    @classmethod
    def for_value(cls, seed: str, scope: str, field: str, row_index: int) -> "ValueRandom":
        return cls(value_seed(seed, scope, field, row_index))

    def next_u64(self) -> int:
        self._state = (self._state + _GOLDEN) & MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * _MIX_A) & MASK64
        z = ((z ^ (z >> 27)) * _MIX_B) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def random(self) -> float:
        """Uniform float in [0, 1). Uses 53 bits, as a double can hold."""
        return (self.next_u64() >> 11) / float(1 << 53)

    def below(self, n: int) -> int:
        """Uniform integer in [0, n). Rejection-sampled so it stays unbiased."""
        if n <= 0:
            raise ValueError("below() needs a positive bound")
        if n & (n - 1) == 0:
            return self.next_u64() & (n - 1)
        limit = MASK64 - (MASK64 % n)
        while True:
            value = self.next_u64()
            if value <= limit:
                return value % n

    def randint(self, low: int, high: int) -> int:
        """Uniform integer in [low, high], both ends included."""
        if high < low:
            raise ValueError(f"randint needs low <= high, got {low} > {high}")
        return low + self.below(high - low + 1)

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.random()

    def choice(self, values: Sequence):
        if not values:
            raise ValueError("choice() needs a non-empty sequence")
        return values[self.below(len(values))]

    def weighted_index(self, weights: Sequence[float]) -> int:
        """Index into `weights`, chosen in proportion to them."""
        total = 0.0
        for weight in weights:
            if weight < 0:
                raise ValueError("weights cannot be negative")
            total += weight
        if total <= 0:
            raise ValueError("weights sum to zero")
        target = self.random() * total
        running = 0.0
        for index, weight in enumerate(weights):
            running += weight
            if target < running:
                return index
        return len(weights) - 1

    def cumulative_index(self, running: Sequence[float]) -> int:
        """`weighted_index` over running totals made by `running_totals`.

        The same draw and the same pick: the totals are the sums
        `weighted_index` adds up, in the same order, so a list drawn from
        many times is summed once rather than on every draw.
        """
        target = self.random() * running[-1]
        return min(bisect_right(running, target), len(running) - 1)

    def gauss(self) -> float:
        """Standard normal by the Box-Muller transform."""
        import math

        u1 = self.random()
        while u1 <= 0.0:
            u1 = self.random()
        u2 = self.random()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def running_totals(weights: Sequence[float]) -> list[float]:
    """The running sums of `weights`, refused as `weighted_index` refuses them."""
    running: list[float] = []
    total = 0.0
    for weight in weights:
        if weight < 0:
            raise ValueError("weights cannot be negative")
        total += weight
        running.append(total)
    if total <= 0:
        raise ValueError("weights sum to zero")
    return running


def stream(seed: str, scope: str, field: str, row_index: int) -> ValueRandom:
    """Shorthand for the common case."""
    return ValueRandom.for_value(seed, scope, field, row_index)

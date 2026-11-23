"""Tests that recognize a column by the shape of its values.

A sequence test tells a surrogate key apart from a business number. A
check-digit test says a ten-digit column is an NPI rather than a phone number,
which is the strongest single piece of mapping evidence there is: no other
column will pass it by accident.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Sequence

# A National Provider Identifier is validated by running Luhn over this prefix
# followed by the first nine digits.
NPI_PREFIX = "80840"

SEQUENCE_AGREEMENT = 0.95
CHECK_DIGIT_AGREEMENT = 0.98
MIN_VALUES = 8


def luhn_check_digit(payload: str) -> int:
    total = 0
    for position, char in enumerate(reversed(payload)):
        digit = int(char)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10


def passes_luhn(value: str) -> bool:
    """True when the last digit is the Luhn check digit of the rest."""
    text = (value or "").strip()
    if len(text) < 2 or not text.isdigit():
        return False
    return int(text[-1]) == luhn_check_digit(text[:-1])


def passes_npi(value: str) -> bool:
    text = (value or "").strip()
    if len(text) != 10 or not text.isdigit():
        return False
    return int(text[-1]) == luhn_check_digit(NPI_PREFIX + text[:9])


@dataclass
class CheckDigitResult:
    scheme: str
    share: float
    tested: int

    def as_dict(self) -> dict:
        return {"scheme": self.scheme, "share": round(self.share, 4), "tested": self.tested}


def check_digits(values: Iterable[str]) -> CheckDigitResult | None:
    """Which check-digit scheme the values satisfy, if any.

    NPI is reported in preference to plain Luhn, because it is the narrower
    claim: every NPI passes Luhn over its own prefix, but few Luhn numbers are
    valid NPIs.
    """
    # Each distinct value is tested once and counted as often as it occurs.
    # Values already counted may be passed as a Counter of value to count.
    if isinstance(values, Counter):
        candidates: Counter = Counter()
        for v, n in values.items():
            text = str(v).strip() if v is not None else ""
            if text and n > 0:
                candidates[text] += n
    else:
        candidates = Counter(str(v).strip() for v in values if v is not None and str(v).strip())
    total = sum(candidates.values())
    if total < MIN_VALUES or len(candidates) < 2:
        return None

    npi_share = sum(n for v, n in candidates.items() if passes_npi(v)) / total
    if npi_share >= CHECK_DIGIT_AGREEMENT:
        return CheckDigitResult("npi", npi_share, total)

    luhn_share = sum(n for v, n in candidates.items() if passes_luhn(v)) / total
    if luhn_share >= CHECK_DIGIT_AGREEMENT:
        return CheckDigitResult("luhn", luhn_share, total)
    return None


@dataclass
class SequenceResult:
    is_sequence: bool
    step: int | None
    agreement: float
    gaps: int
    minimum: int | None
    maximum: int | None
    values_tested: int

    def as_dict(self) -> dict:
        return {
            "is_sequence": self.is_sequence,
            "step": self.step,
            "agreement": round(self.agreement, 4),
            "gaps": self.gaps,
            "min": self.minimum,
            "max": self.maximum,
            "values_tested": self.values_tested,
        }


def sequence_test(values: Sequence[int]) -> SequenceResult | None:
    """Do the integers advance by a constant step?

    Reports the step and how many places the run skips, so the proposer can
    start a generated sequence after the current maximum.
    """
    numbers = sorted({int(v) for v in values if v is not None})
    if len(numbers) < MIN_VALUES:
        return None

    differences = [b - a for a, b in zip(numbers, numbers[1:])]
    if not differences:
        return None

    step, occurrences = Counter(differences).most_common(1)[0]
    agreement = occurrences / len(differences)
    if step <= 0:
        return SequenceResult(False, None, agreement, 0, numbers[0], numbers[-1], len(numbers))

    expected = (numbers[-1] - numbers[0]) // step + 1
    gaps = max(0, expected - len(numbers))
    return SequenceResult(
        is_sequence=agreement >= SEQUENCE_AGREEMENT,
        step=step,
        agreement=agreement,
        gaps=gaps,
        minimum=numbers[0],
        maximum=numbers[-1],
        values_tested=len(numbers),
    )

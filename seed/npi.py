"""NPI generation for the seed script.

An NPI is ten digits. The tenth is a Luhn check digit computed over the string
"80840" + the first nine digits, which is how the profiler's check-digit test
recognizes the column.
"""

from __future__ import annotations

NPI_PREFIX = "80840"


def luhn_check_digit(payload: str) -> int:
    """Luhn check digit for a numeric string, computed right to left."""
    total = 0
    for position, char in enumerate(reversed(payload)):
        digit = int(char)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10


def make_npi(base_nine: int) -> str:
    """Build a valid ten-digit NPI from a nine-digit base number."""
    nine = f"{base_nine % 1_000_000_000:09d}"
    return nine + str(luhn_check_digit(NPI_PREFIX + nine))


def is_valid_npi(value: str) -> bool:
    value = (value or "").strip()
    if len(value) != 10 or not value.isdigit():
        return False
    return value[9] == str(luhn_check_digit(NPI_PREFIX + value[:9]))

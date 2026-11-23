"""Every place in a pack the code reads, found by reading the code.

A pack value is read by its place: held("features:loaders"), or
held_or(place, default) where a pack may leave it out (OPTIONAL_PLACES). A
pack is usable when it holds every place read here that is not optional; the
built-in packs are tested against it, and a generated pack is to be checked
with it before it is kept.
"""

from __future__ import annotations

import re
from pathlib import Path

# The code that reads packs: everything under sdg but the packs themselves.
CODE = Path(__file__).resolve().parents[1]

# A read by a written place, its call wrapped over lines or not.
_READ = re.compile(r"""\bheld(?:_or)?\(\s*["']((?:pack|features|story):[\w.]+)["']\s*[,)]""")


def read_by_code() -> set[str]:
    """Every pack value the code asks for by its place ("features:loaders")."""
    found = set()
    for path in CODE.rglob("*.py"):
        if "packs" in path.relative_to(CODE).parts:
            continue
        found |= set(_READ.findall(path.read_text(encoding="utf-8")))
    return found

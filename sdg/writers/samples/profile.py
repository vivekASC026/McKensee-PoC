"""Format profiles: what a kind of file repeats and what follows from what.

A profile is a JSON file in `profiles/`, read as data: the engine and the
readers hold no list of transactions, loops or derived positions. A profile
may extend another (`"extends": "x12"`); its own keys replace the base's.
Edits are live: a changed file is read again on its next use.
Adding a kind of file (an 835, a FHIR bundle) is adding a profile, and a
reader only when its syntax is new.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sdg.writers.samples.base import SampleError

DIR = Path(__file__).resolve().parent / "profiles"


# path -> ((modified time, size), contents): a profile edited is read again on its next
# use, with nothing to restart.
_read: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


def _own(name: str) -> dict[str, Any]:
    path = DIR / f"{name}.json"
    if not path.is_file():
        raise SampleError(f"No profile {name!r} (looked for {path.name} in {DIR}).")
    info = path.stat()
    stamp = (info.st_mtime_ns, info.st_size)
    held = _read.get(path)
    if held is None or held[0] != stamp:
        try:
            held = (stamp, json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise SampleError(f"Profile {path.name} is not JSON: {exc}") from None
        _read[path] = held
    return held[1]


def load(name: str) -> dict[str, Any]:
    own = _own(name)
    base = load(own["extends"]) if own.get("extends") else {}
    return {**base, **own, "name": name}


def _all() -> list[dict[str, Any]]:
    return [load(p.stem) for p in sorted(DIR.glob("*.json"))]


def for_kind(syntax: str, kind: str) -> dict[str, Any] | None:
    """The profile for this kind of file in this syntax, if one is written."""
    return next((p for p in _all() if p.get("syntax") == syntax and p.get("kind") == kind), None)


def kinds(syntax: str) -> list[str]:
    return sorted(p["kind"] for p in _all() if p.get("syntax") == syntax and p.get("kind"))


def name_hints() -> list[tuple[str, str | None]]:
    """Pieces of masking algorithm names and the element each says."""
    return [(piece, element) for piece, element in load("masking-names")["hints"]]

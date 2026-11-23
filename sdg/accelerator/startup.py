"""Does SDM start on a pack? Asked in a process of its own.

Modules read the pack when they are first imported, so the question is put
to a fresh Python with SDG_PACKS_DIR and SDG_PACK pointing at the pack: every
module under sdg imports, and every place the code reads by name
(places.read_by_code) that is not optional (packs.OPTIONAL_PLACES) is held.
A build checks its pack this way before it is put in place.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAID = "STARTUP "
# A problem said at most this long: a module's own error, never a page of it.
PROBLEM_MAX = 400

UNDER_THE_PACK = r"""
import importlib, json, pkgutil, sys
from pathlib import Path

import sdg
from sdg import packs
from sdg.packs.places import read_by_code

problems = []
for found in pkgutil.walk_packages(sdg.__path__, "sdg."):
    try:
        importlib.import_module(found.name)
    except Exception as exc:
        problems.append(f"{found.name} does not import: {type(exc).__name__}: {exc}")
try:
    pack = packs.active()
    if pack.folder.resolve() != Path(sys.argv[1]).resolve():
        problems.append(f"SDM started on {pack.folder}, not on the pack checked")
    for place in sorted(read_by_code() - packs.OPTIONAL_PLACES):
        try:
            pack.value(place)
        except packs.PackError as exc:
            problems.append(str(exc))
except Exception as exc:
    problems.append(f"the pack does not load: {type(exc).__name__}: {exc}")
print("STARTUP " + json.dumps(problems))
"""


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= PROBLEM_MAX else text[:PROBLEM_MAX - 3] + "..."


def check(folder: Path, timeout: int = 300) -> list[str]:
    """Why SDM would not start on the pack in `folder`; empty when it would.
    The folder's name is the pack's id, its parent the packs folder."""
    folder = Path(folder).resolve()
    env = dict(os.environ, SDG_PACKS_DIR=str(folder.parent), SDG_PACK=folder.name)
    try:
        done = subprocess.run([sys.executable, "-c", UNDER_THE_PACK, str(folder)], cwd=ROOT,
                              capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [f"SDM did not finish starting on the pack in {timeout} seconds."]
    line = next((ln for ln in reversed(done.stdout.splitlines()) if ln.startswith(SAID)), None)
    if line is None:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:]
        return [_short(f"SDM could not be started on the pack (exit {done.returncode}): "
                       f"{tail[0] if tail else 'nothing was said'}")]
    return [_short(p) for p in json.loads(line[len(SAID):])]

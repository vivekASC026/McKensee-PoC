"""A pack may leave out what only a pack about people fills (OPTIONAL_PLACES):
a household, a sex, a marital status, who holds the policy, when a record
began. A generated pack leaves them all out.

Each built-in pack is copied with every optional place deleted, and the copy
is loaded in a process of its own (modules read the pack when they are first
imported): every module imports, every place the code reads that is not
optional is still held, and the readers of the optional places take their
defaults. No database is read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sdg import packs
from sdg.packs import places
from sdg.packs.places import read_by_code

ROOT = Path(__file__).parents[1]
FILES = {"pack": "pack.json", "features": "features.json", "story": "story.json"}

UNDER_THE_COPY = r"""
import importlib, json, pkgutil, sys
import sdg
from sdg import packs
from sdg.packs.places import read_by_code

problems = []
for m in pkgutil.walk_packages(sdg.__path__, 'sdg.'):
    try:
        importlib.import_module(m.name)
    except Exception as e:
        problems.append(f'{m.name}: {type(e).__name__}: {e}')

pack = packs.active()
for place in sorted(read_by_code() - packs.OPTIONAL_PLACES):
    try:
        pack.value(place)
    except packs.PackError as e:
        problems.append(str(e))

from sdg.analyzer.services import story_reading
from sdg.engine import rules
from sdg.plan import patterns
from sdg.writers import elements

defaults = {
    "pack": pack.id,
    "sex_fields": list(rules._SEX_FIELDS),
    "tenure_element": patterns.DERIVATIONS["tenure"]["element"],
    "tenure_read_as": patterns.match_derivation("tenure"),
    "family": list(elements._FAMILY),
    "partner": sorted(elements._PARTNER),
    "child_years": elements._CHILD_YEARS,
    "gender": story_reading.GENDER,
    "marital": story_reading.MARITAL,
    "subscriber": sorted(elements.SUBSCRIBER),
}

# Places named by the pack's own configuration, which the scan cannot see:
# a loader's columns, a naming level's words, the story's word tables.
dynamic = []
for cfg in (pack.features.get("loaders") or {}).values():
    if isinstance(cfg, dict) and cfg.get("columns"):
        dynamic.append("features:" + cfg["columns"])
naming = (pack.features.get("primary_analysis") or {}).get("naming") or {}
for spec in (naming.get("levels") or {}).values():
    if spec.get("words_from"):
        dynamic.append(spec["words_from"])
for _, table_name, _ in story_reading.READING.get("words") or []:
    if table_name not in story_reading._TABLES:
        dynamic.append("story:" + table_name)
for place in dynamic:
    try:
        pack.value(place)
    except packs.PackError as e:
        problems.append(str(e))

# The readers of the person-specific places, run on a few rows.
import pandas as pd
from sdg.analyzer import profiler
from sdg.analyzer.services import defined_patterns, discovery_features

discovery_features.columns = lambda entity: ([], [])  # the store is not read
key = packs.held("features:store.member_key")
rows = {key: [1, 2, 3]}
for column in profiler.CONTINUOUS_FEATURES:
    rows[column] = [1.0, 2.0, 3.0]
frame = pd.DataFrame(rows)
profile = profiler.profile_cluster(frame, frame, pd.Series([True, True, False]), 0, 3)
members, labels, family, used = defined_patterns.narrow_members((frame, None, frame, {}), {})
exercised = {"profile_size": profile["size"], "profile_family": profile["family"],
             "narrowed": [len(members), len(family)], "dynamic": len(dynamic)}
print(json.dumps({"problems": problems, "defaults": defaults, "exercised": exercised}))
"""


def _strip(source: str, into: Path) -> Path:
    """A copy of a built-in pack, under another id, with every optional place gone."""
    pack_id = f"bare_{source}"
    folder = into / pack_id
    shutil.copytree(packs.BUILT_IN / source, folder)
    for part, name in FILES.items():
        data = json.loads((folder / name).read_text(encoding="utf-8"))
        for place in packs.OPTIONAL_PLACES:
            where, _, dotted = place.partition(":")
            if where != part:
                continue
            *parents, leaf = dotted.split(".")
            node = data
            for step in parents:
                node = node.get(step) if isinstance(node, dict) else None
            if isinstance(node, dict):
                node.pop(leaf, None)
        if part == "pack":
            data["id"] = pack_id
        (folder / name).write_text(json.dumps(data, indent=1), encoding="utf-8")
    return folder


def _raw(folder: Path, place: str):
    """A place as the copy's file holds it, before load fills any default."""
    where, _, dotted = place.partition(":")
    node = json.loads((folder / FILES[where]).read_text(encoding="utf-8"))
    for step in dotted.split("."):
        if not isinstance(node, dict) or step not in node:
            raise KeyError(place)
        node = node[step]
    return node


def test_every_optional_place_is_read_by_the_code():
    assert packs.OPTIONAL_PLACES <= read_by_code()


def test_a_read_is_found_however_its_call_is_wrapped():
    text = ('a = held(\n    "features:one.place")\n'
            'b = held_or( "story:two" , [])\n'
            'c = is_held("pack:not.a.read")\n'
            'd = packs.held("pack:three")\n')
    assert sorted(places._READ.findall(text)) == ["features:one.place", "pack:three", "story:two"]


# Calls named held that read no written place: a place built from the pack's own
# configuration, and two functions of the same name that are not pack reads.
NOT_WRITTEN = sorted([
    ("analyzer/entity_loader.py", 'held(f"features:{cfg'),
    ("analyzer/naming.py", 'held(spec["words_fro'),
    ("analyzer/services/story_reading.py", 'held(f"story:{table_'),
    ("api/routers/accelerator.py", "held() -> dict[str, "),
    ("writers/validate.py", "held(self, elements:"),
    ("writers/validate.py", "held(elements)\n     "),
])


def test_every_call_of_held_is_a_written_place_or_a_known_other():
    """A read by a place the scan cannot see is a place a generated pack is
    never checked for: each one here is known, and a new one fails."""
    call = re.compile(r"\bheld(?:_or)?\(")
    unseen = []
    for path in sorted(places.CODE.rglob("*.py")):
        if "packs" in path.relative_to(places.CODE).parts:
            continue
        text = path.read_text(encoding="utf-8")
        unseen += [(path.relative_to(places.CODE).as_posix(), text[m.start():m.start() + 20])
                   for m in call.finditer(text) if not places._READ.match(text, m.start())]
    assert sorted(unseen) == NOT_WRITTEN


def test_the_scan_finds_every_place_it_found_before_and_the_wrapped_one():
    # 79 before the scan read a call wrapped over lines; scenarios.py wraps one.
    found = read_by_code()
    assert len(found) == 80
    assert "features:patterns.scenario_dimensions" in found


def test_the_built_in_packs_still_hold_the_optional_places_the_code_reads():
    # held_or falls back only where a pack holds nothing; health's values stand.
    health = packs.load("health")
    assert health.value("pack:plan.sex_fields")
    assert health.value("pack:files.household.child_years") == 15
    assert health.value("story:gender")


def test_held_or_falls_back_only_on_a_missing_place(monkeypatch):
    monkeypatch.setenv("SDG_PACK", "banking")
    packs.load.cache_clear()
    try:
        assert packs.held_or("pack:plan.tenure_element", None) == "CUSTOMER_SINCE"
        assert packs.held_or("story:marital", ["x"]) == [], "an empty value is held, not missing"
        assert packs.held_or("features:primary_analysis.family", "none") is None
        for place in ("pack:tables.modes", "pack:plan.no_such_place"):
            with pytest.raises(packs.PackError, match="not a place a pack may leave out"):
                packs.held_or(place, ())
        monkeypatch.setenv("SDG_PACK", "no_such_pack")
        packs.load.cache_clear()
        with pytest.raises(packs.PackError, match="No business pack"):
            packs.held_or("pack:plan.tenure_element", None)
    finally:
        packs.load.cache_clear()


@pytest.mark.parametrize("source", ["banking", "health"])
def test_a_pack_without_the_optional_places_starts(source, tmp_path):
    folder = _strip(source, tmp_path)
    for place in packs.OPTIONAL_PLACES:
        with pytest.raises(KeyError):
            _raw(folder, place)

    done = subprocess.run(
        [sys.executable, "-c", UNDER_THE_COPY], cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, SDG_PACKS_DIR=str(tmp_path), SDG_PACK=folder.name), timeout=300)
    lines = done.stdout.strip().splitlines()
    assert done.returncode == 0 and lines, done.stdout + done.stderr
    said = json.loads(lines[-1])
    assert said["problems"] == [], said["problems"]
    assert said["defaults"] == {
        "pack": folder.name,
        "sex_fields": [],
        "tenure_element": None,
        "tenure_read_as": None,
        "family": [],
        "partner": [],
        "child_years": 15,
        "gender": [],
        "marital": [],
        "subscriber": [],
    }
    # The household readers take their defaults on real rows: no family, all kept.
    assert said["exercised"]["profile_size"] == 2
    assert said["exercised"]["profile_family"] == {}
    assert said["exercised"]["narrowed"] == [3, 3]
    # Every loader names its columns' place, and each was found held.
    assert said["exercised"]["dynamic"] >= 2


UNDER_HEALTH = r"""
import json
from sdg import packs
from sdg.analyzer.services import story_reading
from sdg.engine import rules
from sdg.writers import elements

print(json.dumps({
    "pack": packs.active().id,
    "sex_fields": list(rules._SEX_FIELDS),
    "family": list(elements._FAMILY),
    "child_years": elements._CHILD_YEARS,
    "gender": story_reading.GENDER,
    "subscriber": sorted(elements.SUBSCRIBER),
}))
"""


def test_under_the_health_pack_the_optional_readers_take_its_values():
    """The mirror of the bare copy: health holds every optional place, and its
    readers take the values its files hold, not the defaults."""
    folder = packs.BUILT_IN / "health"
    env = {k: v for k, v in os.environ.items() if k != "SDG_PACKS_DIR"}
    done = subprocess.run([sys.executable, "-c", UNDER_HEALTH], cwd=ROOT, capture_output=True,
                          text=True, env=dict(env, SDG_PACK="health"), timeout=300)
    lines = done.stdout.strip().splitlines()
    assert done.returncode == 0 and lines, done.stdout + done.stderr
    said = json.loads(lines[-1])
    assert said["pack"] == "health"
    assert said["sex_fields"], "health names the fields that hold a sex"
    assert said["family"] == _raw(folder, "pack:files.household.family")
    assert said["gender"] == _raw(folder, "story:gender")
    assert said["child_years"] == 15
    assert said["subscriber"] == sorted(_raw(folder, "pack:files.subscriber_codes"))

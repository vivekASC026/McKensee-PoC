"""Every health-insurance value in the code today, kept before it moves.

Making SDM domain neutral moves these constants into the health business
pack. Whatever reads them then -- the constant, or the pack -- must hold what
is kept in tests/fixtures/pack-constants.json, value for value. As each one
moves, its entry in WHERE changes to read it from the pack; the kept values
never change.

To record (only when a value is meant to change):
    set SDM_RECORD_BASELINES=1 && pytest tests/test_pack_constants.py
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
KEPT = Path(__file__).parent / "fixtures" / "pack-constants.json"
RECORDING = os.getenv("SDM_RECORD_BASELINES") == "1"

# Each health value, and where it is read from now: a module's name, or
# `pack:<place>` where the code asks the pack for it.
WHERE: dict[str, str] = {
    # Plan building and the engine
    "never generated": "sdg.plan.build:NEVER_GENERATED",
    "table modes": "sdg.plan.build:DEFAULT_TABLE_MODES",
    "store elements": "sdg.plan.combinations:STORE_ELEMENTS",
    "empty is a value": "sdg.plan.combinations:EMPTY_IS_A_VALUE",
    "combination key": "sdg.analyzer.db:COMBO_KEY",
    "always together": "sdg.plan.combinations:ALWAYS",
    "kind anchors": "sdg.plan.patterns:KIND_ANCHORS",
    "sex fields": "sdg.engine.rules:_SEX_FIELDS",
    # The analyzer
    "continuous features": "sdg.analyzer.config:CONTINUOUS_FEATURES",
    "geography columns": "sdg.analyzer.config:GEOGRAPHY_COLUMNS",
    "categorical features": "sdg.analyzer.config:CATEGORICAL_FEATURES",
    "description columns": "sdg.analyzer.config:DESCRIPTION_COLUMNS",
    "filter columns": "sdg.analyzer.config:FILTER_COLUMNS",
    "label cluster columns": "sdg.analyzer.config:LABEL_CLUSTER_COLUMNS",
    "member columns": "pack:features:store.member_columns",
    "claim columns": "pack:features:store.claim_columns",
    "defined features": "sdg.analyzer.services.defined_patterns:FEATURES",
    "discovery defaults": "sdg.analyzer.services.discovery_features:DEFAULTS",
    "scope levels": "sdg.analyzer.services.pattern_service:FEATURES",
    "scenario dimensions": "sdg.analyzer.services.scenarios:DIMENSIONS",
    "scenario split": "sdg.analyzer.services.scenarios:DEFAULT_SPLIT",
    "amount bands": "sdg.analyzer.services.pattern_report:AMOUNT_BANDS",
    "report fields": "sdg.analyzer.services.pattern_report:FIELDS",
    "report keys": "sdg.analyzer.services.report_fields:KEYS",
    "personal columns": "sdg.analyzer.services.report_fields:PERSONAL",
    "claim type names": "sdg.analyzer.services.claims_service:CLAIM_TYPE_NAMES",
    "clinical columns": "sdg.analyzer.services.claims_service:CLINICAL_COLUMNS",
    "enrollment columns": "sdg.analyzer.profiler:ENROLLMENT_COLUMNS",
    "provider member columns": "sdg.analyzer.provider_features:MEMBER_COLUMNS",
    "provider claim columns": "sdg.analyzer.provider_features:CLAIM_COLUMNS",
    "provider entity words": "sdg.analyzer.provider_features:ENTITY_WORDS",
    "feature labels": "sdg.analyzer.bridge:FEATURE_LABELS",
    "seed element labels": "sdg.analyzer.services.seed_elements:LABELS",
    "story stop words": "sdg.analyzer.services.story_reading:STOP",
    "story gender": "sdg.analyzer.services.story_reading:GENDER",
    "story marital": "sdg.analyzer.services.story_reading:MARITAL",
    "story claim types": "sdg.analyzer.services.story_reading:CLAIM_TYPES",
    "story network": "sdg.analyzer.services.story_reading:NETWORK",
    "story age words": "sdg.analyzer.services.story_reading:AGE_WORDS",
    "story states": "sdg.analyzer.services.story_reading:STATES",
    "story spoken for": "sdg.analyzer.services.story_reading:SPOKEN_FOR",
    "keyword aliases": "sdg.analyzer.keyword_search:_ALIASES",
    "naming states": "sdg.analyzer.naming:_STATE_ABBREVS",
    "numeric columns": "sdg.analyzer.coerce:NUMERIC_COLUMNS",
    "key suffixes": "sdg.analyzer.coerce:KEY_SUFFIXES",
    "personal columns (rows)": "sdg.analyzer.services.pattern_data:_PHI_COLUMNS",
    "personal suffixes (rows)": "sdg.analyzer.services.pattern_data:_PHI_SUFFIXES",
    "claims filters": "sdg.analyzer.services.pattern_data:_CLAIMS_FILTERS",
    "member key": "sdg.analyzer.services.saved_patterns:MEMBER_KEY",
    # Writers and the API
    "household": "sdg.writers.elements:_FAMILY",
    "partner codes": "sdg.writers.elements:_PARTNER",
    "child years": "sdg.writers.elements:_CHILD_YEARS",
    "record keys": "sdg.writers.elements:_KEYS",
    "subscriber codes": "sdg.writers.elements:SUBSCRIBER",
    "record tables": "sdg.writers.elements:RECORD_TABLES",
    "file elements": "sdg.api.app:DEFAULT_ELEMENTS",
    "scope elements": "sdg.api.app:SCOPE_ELEMENTS",
}

# Files the pack took over, kept by digest.
FILES = {
    "catalog": "sdg/packs/health/catalog.yaml",
    "column names": "sdg/packs/health/column_names.json",
    # Every word the screens show: changed only on purpose (record again).
    "glossary": "sdg/packs/health/glossary.json",
}


def _plain(value: Any) -> Any:
    """A value as JSON holds it: sets sorted, tuples as lists, patterns as text."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v) for v in value), key=json.dumps)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, re.Pattern):
        return value.pattern
    return value


def _read(where: str) -> Any:
    if where.startswith("pack:"):
        # Read where the code reads it: the active pack (sdg.analyzer.entity_loader).
        from sdg import packs

        return _plain(packs.held(where.removeprefix("pack:")))
    module, _, name = where.partition(":")
    return _plain(getattr(importlib.import_module(module), name))


def _digest(path: str) -> str:
    text = (ROOT / path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(text).hexdigest()


def _current() -> dict[str, Any]:
    return {**{key: _read(where) for key, where in WHERE.items()},
            **{f"file: {key}": _digest(path) for key, path in FILES.items()}}


def test_every_health_value_is_as_kept():
    now = _current()
    if RECORDING:
        KEPT.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    kept = json.loads(KEPT.read_text(encoding="utf-8"))
    assert sorted(now) == sorted(kept), "a kept value has no reader, or a reader no value"
    changed = [key for key in kept if now[key] != kept[key]]
    assert not changed, f"changed from what was kept: {changed}"


def test_the_screens_show_the_column_names_the_server_does():
    """The screens' column names are the pack's, read from the server: no copy
    of them is kept with the screens to drift from it."""
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    served = TestClient(app).get("/api/pack/column-names").json()
    assert served == json.loads((ROOT / FILES["column names"]).read_text(encoding="utf-8"))
    # The screens read them from the server...
    pack_ts = (ROOT / "ui" / "src" / "lib" / "pack.ts").read_text(encoding="utf-8")
    assert '"/api/pack/column-names"' in pack_ts
    # ...and keep no copy of them, whole or in part, in any form: the longest
    # business names are distinctive enough that only a copy would hold them.
    sample = sorted((v for k, v in served.items() if not k.startswith("_")),
                    key=lambda v: (-len(v), v))[:8]
    copies = sorted({path.relative_to(ROOT).as_posix()
                     for path in (ROOT / "ui" / "src").rglob("*")
                     if path.suffix in (".json", ".ts", ".tsx")
                     for name in sample
                     if f'"{name}"' in path.read_text(encoding="utf-8")})
    assert not copies, f"column names are copied into the screens: {copies}"

"""The banking pack: a second business, configured and nothing else.

Every built-in pack holds every value the code reads (packs.held), so the code
starts under any of them; under banking the server says a bank's words,
offers no health format and no health module, and offers pattern analysis
(phase 6b) and writing files and plans from a pattern (phase 6c).

The analysis modules read the pack when they are first imported, so what is
asked of the server under banking is asked in a process of its own: asked
here, it would leave those modules holding the bank's values for every test
after it.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from sdg import packs
from sdg.api.app import app
from sdg.packs.places import read_by_code as _read_by_code
from tests.test_pack_constants import ROOT

client = TestClient(app)
BUILT_IN = sorted(p.parent.name for p in (ROOT / "sdg" / "packs").glob("*/pack.json"))


@pytest.fixture
def banking(monkeypatch):
    monkeypatch.setenv("SDG_PACK", "banking")
    packs.load.cache_clear()
    yield packs.active()
    packs.load.cache_clear()


def test_the_built_in_packs_are_health_and_banking():
    assert BUILT_IN == ["banking", "health"]


@pytest.mark.parametrize("pack_id", BUILT_IN)
def test_every_pack_holds_every_value_the_code_reads(pack_id):
    places = _read_by_code()
    assert len(places) >= 50, f"the scan found too few: {sorted(places)}"
    pack = packs.load(pack_id)
    missing = []
    for place in sorted(places):
        try:
            pack.value(place)
        except packs.PackError:
            missing.append(place)
    assert not missing, f"the {pack_id} pack lacks: {missing}"


def test_every_module_imports_under_banking():
    """Values are read when modules load: a process of its own, under banking."""
    code = (
        "import importlib, pkgutil, sdg\n"
        "bad = []\n"
        "for m in pkgutil.walk_packages(sdg.__path__, 'sdg.'):\n"
        "    try:\n"
        "        importlib.import_module(m.name)\n"
        "    except Exception as e:\n"
        "        bad.append(f'{m.name}: {type(e).__name__}: {e}')\n"
        "print('\\n'.join(bad) or 'ok')\n"
    )
    import os

    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, env=dict(os.environ, SDG_PACK="banking"), timeout=300)
    assert done.stdout.strip().splitlines()[-1:] == ["ok"], done.stdout + done.stderr


def test_the_banking_pack_says_what_each_record_plays():
    bank = packs.load("banking")
    assert bank.primary == "customers" and bank.child == "accounts"
    assert bank.with_role("parent_of_primary") is None
    assert bank.term("accounts", "Lines") == "Transactions"
    assert bank.formats == {"customers": ["csv"], "accounts": ["csv"]}
    assert bank.data["modules"] == ["pattern_analysis", "pattern_generation"]
    assert bank.value("pack:plan_name") == "bank-slice"
    for entity in bank.entities:
        terms = bank.glossary[entity]
        assert terms["One"] == terms["one"].capitalize() and terms["Many"] == terms["many"].capitalize()


def test_the_screens_are_told_the_bank(banking):
    body = client.get("/api/pack").json()
    assert body["id"] == "banking" and body["name"] == "Retail banking"
    assert {e["key"]: e["role"] for e in body["entities"]} == {
        "customers": "primary", "accounts": "child"}
    assert body["modules"] == ["pattern_analysis", "pattern_generation"]
    assert client.get("/api/pack/column-names").json()["TXN_TYPE"] == "Transaction type"
    assert client.get("/api/samples/defaults").json() == {"defaults": []}
    got = client.get("/api/edi/formats")
    assert got.status_code == 404
    assert got.json()["detail"] == ("X12 EDI is not offered by the active business pack "
                                    "(Retail banking).")


HEALTH_WORDS = re.compile(r"\b(members?|claims?|diagnos\w*|providers?|834|837\w*|FACETS)\b", re.I)


@pytest.mark.parametrize("name", ["pack.json", "glossary.json", "features.json",
                                  "story.json", "column_names.json", "catalog.yaml"])
def test_no_health_word_in_the_banking_pack(name):
    """Except where a key is the code's own name for a structure (6b renames those)."""
    text = (ROOT / "sdg" / "packs" / "banking" / name).read_text(encoding="utf-8")
    if name == "features.json":
        body = json.loads(text)
        allowed = {"member_columns", "claim_columns", "member_key", "claims_filters",
                   "claim_type_names", "member_claims", "provider_member_columns",
                   "provider_claim_columns", "provider_entity_words"}
        text = json.dumps(body)
        for key in allowed:
            text = text.replace(f'"{key}"', '""')
        text = re.sub(r'"_about": "[^"]*"', "", text)
    if name == "story.json":
        text = text.replace('"claim_types"', '""')
    found = sorted({m.group(0) for m in HEALTH_WORDS.finditer(text)})
    assert not found, found


UNDER_BANKING = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from tests import conftest  # noqa: F401  the suite's client: this machine's name, the token
from fastapi.testclient import TestClient
from sdg.api.app import app

c = TestClient(app, raise_server_exceptions=False)
out = {}
for method, path, body in json.loads(sys.argv[2]):
    r = c.post(path, json=body) if method == "post" else c.get(path)
    out[path] = [r.status_code, r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") and isinstance(r.json(), dict) else None]
print("RESULT " + json.dumps(out))
'''


def _under_banking(calls: list) -> dict:
    import os

    done = subprocess.run([sys.executable, "-c", UNDER_BANKING, str(ROOT), json.dumps(calls)],
                          cwd=ROOT, capture_output=True, text=True, timeout=600,
                          env=dict(os.environ, SDG_PACK="banking"))
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, done.stdout[-2000:] + done.stderr[-2000:]
    return json.loads(line[len("RESULT "):])


GENERATING = "Generating from patterns is not offered by the active business pack (Retail banking)."
ANALYSIS_REFUSED = "Pattern analysis is not offered by the active business pack (Retail banking)."


def test_under_banking_patterns_are_analyzed_and_generated_from():
    """Phase 6c: a bank's patterns are written to files and plans too. No
    route of either answers as a refusal (a pack without the modules is
    refused: tests/test_pack_modules.py)."""
    got = _under_banking([
        ["post", "/api/patterns/to-plan", {}],
        ["post", "/api/story/to-plan", {}],
        ["get", "/api/patterns/1/claim-shape", None],
        ["post", "/api/generation-requests/1/claims-check", {}],
        ["post", "/api/export", {"format": "csv", "subject": "customers", "pattern_id": 1}],
        # A file not from a pattern (these may fail on this machine's store,
        # which is not a bank's, but never as a refusal).
        ["post", "/api/export?from=plan", {"format": "csv", "subject": "customers", "rows": 1}],
        # Analysis is offered: these answer as the analysis does, not as a refusal.
        ["get", "/api/scope?entity=customers", None],
        ["get", "/api/patterns/scenario-dimensions?entity=customers", None],
        ["get", "/api/patterns/features?entity=accounts", None],
    ])
    assert got["/api/scope?entity=customers"][0] == 200
    for path, (status, detail) in got.items():
        assert detail not in (ANALYSIS_REFUSED, GENERATING), (path, status, detail)
        # Not refused by the pack in any words (it may fail on this machine's
        # store, which is not a bank's).
        assert not (status == 404 and "is not offered" in str(detail)), (path, status, detail)


def test_both_packs_offer_analysis_and_generating_from_patterns():
    health, bank = packs.load("health"), packs.load("banking")
    assert health.has_module("pattern_analysis") and health.has_module("pattern_generation")
    assert bank.has_module("pattern_analysis") and bank.has_module("pattern_generation")


@pytest.mark.parametrize("pack_id", BUILT_IN)
def test_no_pack_file_holds_a_control_character(pack_id):
    """A pattern written "\b" in JSON is a backspace, not a word boundary: the
    banking story's age words once matched nothing for it."""
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for v in value:
                yield from strings(v)
        elif isinstance(value, dict):
            for k, v in value.items():
                yield k
                yield from strings(v)

    for path in sorted((ROOT / "sdg" / "packs" / pack_id).rglob("*.json")):
        bad = [s for s in strings(json.loads(path.read_text(encoding="utf-8")))
               if any(ord(ch) < 32 for ch in s)]
        assert not bad, (path.name, bad[:3])


@pytest.mark.parametrize("name, allow, why", [
    ("master", True, "system"), ("tempdb", True, "system"), ("PRODDB", True, "protected"),
    ("SDMT_X; DROP DATABASE Y", True, "plain"), ("SDMT_X]", True, "plain"), ("", True, "plain"),
    ("BANK_DEMO", False, "SDMT_"),
])
def test_the_sample_bank_refuses_a_database_before_dropping_it(monkeypatch, name, allow, why):
    from sdg.config import ProtectedDatabase
    from seed import bank as sample

    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODDB")

    def reached(*args, **kwargs):
        raise AssertionError("a database was reached before the name was checked")

    monkeypatch.setattr(sample, "_connect", reached)
    for step in (lambda: sample.build(name, allow=allow), lambda: sample.recreate(name, allow),
                 lambda: sample.drop(name, allow)):
        with pytest.raises((sample.Refused, ProtectedDatabase)) as refused:
            step()
        assert why.lower() in str(refused.value).lower()


def test_the_sample_bank_refuses_a_database_this_installation_uses(monkeypatch):
    from sdg.config import get_settings
    from seed import bank as sample

    monkeypatch.setattr(sample, "_connect", lambda *a, **k: pytest.fail("reached a database"))
    settings = get_settings()
    for name in {settings.source_db, settings.ref_db, settings.denorm_db, settings.target_db}:
        with pytest.raises(sample.Refused, match="configured"):
            sample.build(name, allow=True)
        with pytest.raises(sample.Refused, match="configured"):
            sample.build(name.lower(), allow=True)


def test_the_sample_bank_takes_an_sdmt_name_or_another_on_purpose():
    from seed import bank as sample

    sample.check("SDMT_BANK_SRC")
    sample.check("BANK_DEMO", allow=True)
    with pytest.raises(SystemExit):
        sample.main(["BANK_DEMO"])

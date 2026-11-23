"""Analysis waits for a restart once the running pack is rebuilt.

docs/day0-accelerator-phase4-plan.md, step 1. A pack written by a build (its
folder holds .sdm-build.json) is read with the build's hash. When the folder
later holds another build, every analysis route answers 409 with the restart
sentence, so nothing is measured under one build and read under another. A
pack no build wrote (health, banking) is never refused. No database is
reached: the refusal comes before any route runs.
"""

from __future__ import annotations

import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from sdg import packs
from sdg.api.app import app

client = TestClient(app)


@pytest.fixture
def generated(tmp_path, monkeypatch):
    """A copy of the banking pack as a build left it, running."""
    folder = tmp_path / "packs" / "gen_bank"
    shutil.copytree(packs.BUILT_IN / "banking", folder)
    data = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    data["id"] = "gen_bank"
    (folder / "pack.json").write_text(json.dumps(data), encoding="utf-8")
    marker = folder / packs.BUILD_MARKER
    marker.write_text(json.dumps({"source": "BANK", "built_hash": "a" * 64}), encoding="utf-8")
    monkeypatch.setenv("SDG_PACKS_DIR", str(tmp_path / "packs"))
    monkeypatch.setenv("SDG_PACK", "gen_bank")
    packs.load.cache_clear()
    yield marker
    packs.load.cache_clear()


def _rebuilt(response) -> None:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["refusal"] == "rebuilt"
    assert body["detail"] == ("The gen_bank pack was rebuilt after SDM started. Restart SDM to "
                              "analyze under the new build.")


def test_the_running_pack_knows_its_build(generated):
    assert packs.active().built_hash == "a" * 64
    assert packs.current_build_problem() is None


@pytest.mark.parametrize("change", ["another build", "no marker", "unreadable marker"])
def test_every_analysis_route_waits_for_a_restart_once_rebuilt(generated, change):
    assert packs.active().built_hash == "a" * 64  # read at start
    if change == "another build":
        generated.write_text(json.dumps({"built_hash": "b" * 64}), encoding="utf-8")
    elif change == "no marker":
        generated.unlink()
    else:
        generated.write_text("{not json", encoding="utf-8")
    _rebuilt(client.get("/api/discovery?entity=customers"))
    _rebuilt(client.post("/api/patterns/analyze", json={}))
    _rebuilt(client.post("/api/baseline/compute", json={}))
    _rebuilt(client.get("/api/discovery-features"))


def test_a_pattern_written_now_is_stamped_with_the_running_build(generated):
    from sdg.analyzer import db

    assert db.build_stamp() == {"pack_id": "gen_bank", "plan_version": "a" * 64}


def test_screens_other_than_analysis_still_answer_once_rebuilt(generated):
    assert packs.active().built_hash == "a" * 64  # read at start
    generated.write_text(json.dumps({"built_hash": "b" * 64}), encoding="utf-8")
    assert packs.current_build_problem()
    answer = client.get("/api/accelerator/sources")
    assert answer.status_code == 200, answer.text


def test_another_builds_tables_loaded_in_the_store_stop_analysis(generated, monkeypatch):
    """Rebuilt and loaded from another machine: this folder still holds the
    old build, but the store says another build's tables are loaded."""
    assert packs.active().built_hash == "a" * 64
    monkeypatch.setattr(packs, "loaded_build", lambda pack_id: ("c" * 64, "c" * 64))
    answer = client.get("/api/discovery?entity=customers")
    assert answer.status_code == 409 and "loaded from another build" in answer.json()["detail"]
    # This build is the store's latest, and its tables are not loaded yet.
    monkeypatch.setattr(packs, "loaded_build", lambda pack_id: ("c" * 64, "a" * 64))
    assert "Load this build" in packs.current_build_problem()
    monkeypatch.setattr(packs, "loaded_build", lambda pack_id: ("a" * 64, "a" * 64))
    assert packs.current_build_problem() is None


def _routes() -> list[tuple[str, set[str], bool]]:
    """Every API route as (path, methods, whether it carries the
    pattern_analysis gate), walking into included routers and their own
    dependencies, as the app serves them."""
    from fastapi.routing import APIRoute

    from sdg.api.app import app

    def named(deps) -> set[str]:
        return {getattr(getattr(d, "dependency", None) or getattr(d, "call", None),
                        "__qualname__", "") for d in deps}

    def below(dependant) -> list:
        out = []
        for d in dependant.dependencies:
            out += [d] + below(d)
        return out

    found: list[tuple[str, set[str], bool]] = []

    def visit(routes, prefix: str, gates: set[str]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                names = named(below(route.dependant)) | gates
                found.append((prefix + route.path, set(route.methods),
                              any("offered" in n for n in names)))
            elif hasattr(route, "original_router"):
                context = route.include_context
                router = route.original_router
                visit(router.routes, prefix + (context.prefix or ""),
                      gates | named(list(router.dependencies) + list(context.dependencies or [])))

    visit(app.routes, "", set())
    return found


def test_every_analysis_route_carries_the_rebuild_gate():
    """Each route that analyzes carries the pattern_analysis gate (and so the
    rebuild check): found by walking the app, not listed by hand."""
    gated = {path for path, _, gate in _routes() if gate}
    for prefix in ("/api/discovery", "/api/patterns", "/api/baseline", "/api/claims",
                   "/api/hierarchy", "/api/story"):
        assert any(p.startswith(prefix) for p in gated), (prefix, sorted(gated))
    assert not any(p.startswith(("/api/accelerator", "/api/layouts")) for p in gated)


def test_every_gated_route_answers_409_once_rebuilt(generated):
    assert packs.active().built_hash == "a" * 64
    generated.write_text(json.dumps({"built_hash": "b" * 64}), encoding="utf-8")
    refused = []
    for path, methods, gate in _routes():
        if not gate:
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        method = sorted(methods - {"HEAD"})[0]
        answer = client.request(method, concrete, json={})
        if answer.status_code == 404 and "not offered" in answer.text:
            continue  # a module the banking copy does not offer: refused before the gate
        assert answer.status_code == 409 and answer.json().get("refusal") == "rebuilt",             (method, path, answer.status_code, answer.text[:200])
        refused.append(path)
    assert len(refused) >= 20, refused


def test_a_restart_reads_the_new_build(generated):
    assert packs.active().built_hash == "a" * 64  # read at start
    generated.write_text(json.dumps({"built_hash": "b" * 64}), encoding="utf-8")
    assert packs.current_build_problem()
    packs.load.cache_clear()  # what a restart does
    assert packs.active().built_hash == "b" * 64 and packs.current_build_problem() is None


@pytest.mark.parametrize("pack_id", ["health", "banking"])
def test_a_pack_no_build_wrote_is_never_refused(pack_id, monkeypatch):
    monkeypatch.setenv("SDG_PACK", pack_id)
    packs.load.cache_clear()
    try:
        assert packs.active().built_hash is None
        assert packs.current_build_problem() is None
        packs.offered("pattern_analysis", "Pattern analysis")()  # the route's gate passes
        from sdg.analyzer import db

        assert db.build_stamp() == {}
    finally:
        packs.load.cache_clear()


def test_the_extract_command_loads_a_built_pack_as_the_screen_does(generated, monkeypatch):
    """extract.cmd (and a scheduled refresh) on a built pack goes through the
    Load tab's path: the build checked, statements checked, staged, archived."""
    from typer.testing import CliRunner

    from sdg.accelerator import loading
    from sdg.cli.main import app as cli

    asked: list = []

    def load(source, told, stop, *, allow_write_login=False, checker=None):
        asked.append((source, allow_write_login, stop()))
        told(1, 1, "SRCTABLE: 3 rows")
        return {"load": {"tables": [{"table": "t", "rows": 3, "seconds": 0.1, "error": None}]},
                "notes": [], "checks": []}

    monkeypatch.setattr(loading, "load", load)
    done = CliRunner().invoke(cli, ["extract", "BANK", "--allow-write-login"])
    assert done.exit_code == 0, done.output
    assert asked == [("BANK", True, False)]
    refused = CliRunner().invoke(cli, ["extract", "BANK", "--table", "t"])
    assert refused.exit_code == 1 and "load together" in refused.output

    def refuse(source, told, stop, **kw):
        raise loading.LoadRefused("The source changed since the plan was built.", "drift")

    monkeypatch.setattr(loading, "load", refuse)
    said = CliRunner().invoke(cli, ["extract", "BANK"])
    assert said.exit_code == 1 and "The source changed" in said.output


def test_a_built_pack_without_its_marker_still_loads_only_through_the_checks(generated,
                                                                              monkeypatch):
    """Removing the marker does not open the unchecked extract: the pack's
    kept columns mark it built, and the checked path refuses it."""
    from typer.testing import CliRunner

    from sdg.accelerator import loading
    from sdg.cli.main import app as cli

    data = json.loads((generated.parent / "pack.json").read_text(encoding="utf-8"))
    data["extract"] = {**(data.get("extract") or {}), "kept": {"BANK": ["CUSTOMER_ID"]}}
    (generated.parent / "pack.json").write_text(json.dumps(data), encoding="utf-8")
    generated.unlink()
    packs.load.cache_clear()
    assert packs.active().built_hash is None
    asked: list = []

    def load(source, told, stop, **kw):
        asked.append(source)
        raise loading.LoadRefused("BANK's pack has not been built: build it under Build first.",
                                  "not_built")

    monkeypatch.setattr(loading, "load", load)
    done = CliRunner().invoke(cli, ["extract", "BANK"])
    assert asked == ["BANK"] and done.exit_code == 1 and "has not been built" in done.output


"""The neutral routes answer as the routes they stand beside.

/api/entities/{key}/{overview,analyze,rows,save} are registered from the
business pack, each the route its entity's role already had; /api/scope
reads the pack's scope levels. The old paths (/api/patterns/analyze,
/api/claims/*, /api/hierarchy/*) are permanent aliases and keep working.
The export request takes neutral field names beside the old ones.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from sdg import packs
from sdg.analyzer import entity_loader
from sdg.analyzer.services import claims_service, service
from sdg.api.app import ExportRequest, app
from sdg.api.routers import claims, entities, hierarchy, patterns

client = TestClient(app)

PAIRS = [  # (neutral path, the path it stands beside)
    ("/api/entities/members/overview", "/api/hierarchy/overview"),
    ("/api/entities/members/analyze", "/api/patterns/analyze"),
    ("/api/entities/members/rows", "/api/patterns/rows"),
    ("/api/entities/members/save", "/api/patterns/save"),
    ("/api/entities/claims/overview", "/api/claims/overview"),
    ("/api/entities/claims/analyze", "/api/claims/analyze"),
    ("/api/entities/claims/rows", "/api/claims/rows"),
    ("/api/entities/claims/save", "/api/claims/save"),
    ("/api/entities/claims/available", "/api/claims/available"),
]


def _routes() -> dict[str, APIRoute]:
    """Every route of the routers mounted under /api, by its path."""
    return {f"/api{r.path}": r for router in (entities.router, hierarchy.router,
                                              patterns.router, claims.router)
            for r in router.routes if isinstance(r, APIRoute)}


def _route(path: str) -> APIRoute:
    return _routes()[path]


@pytest.mark.parametrize("neutral, old", PAIRS, ids=[p for p, _ in PAIRS])
def test_each_entity_route_is_the_route_its_role_had(neutral, old):
    new, before = _route(neutral), _route(old)
    assert new.endpoint is before.endpoint
    assert new.methods == before.methods
    assert new.response_model is before.response_model
    assert new.body_field is None and before.body_field is None or (
        new.body_field.field_info.annotation is before.body_field.field_info.annotation)


def test_only_the_packs_entities_have_routes():
    paths = {p for p in _routes() if p.startswith("/api/entities/") and "{" not in p}
    assert paths == {p for p, _ in PAIRS}


def test_both_paths_answer_the_same(monkeypatch):
    result = {"total_members": 9, "subset_members": 3, "pct_of_total": 1 / 3,
              "n_patterns": 0, "method": "kmeans", "filters_used": {"grgr_ck": [1]},
              "patterns": [], "cache_key": "x"}
    # `asked` is not in the response model: both paths leave it out alike.
    monkeypatch.setattr(service, "analyze", lambda **kw: dict(result, asked=kw))
    body = {"filters": {"grgr_ck": [1]}, "k": 1}
    old = client.post("/api/patterns/analyze", json=body)
    new = client.post("/api/entities/members/analyze", json=body)
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()
    assert new.json()["subset_members"] == 3 and "asked" not in new.json()

    monkeypatch.setattr(claims_service, "analyze", lambda kw, k: {"patterns": [], "asked": kw, "k": k})
    body = {"grgr_ck": [1], "claim_type": ["P"], "k": 2}
    old = client.post("/api/claims/analyze", json=body)
    new = client.post("/api/entities/claims/analyze", json=body)
    assert old.status_code == new.status_code == 200
    assert old.json() == new.json()
    assert new.json()["asked"] == {"grgr_ck": [1], "claim_type": ["P"]}

    # A body the role's request refuses is refused the same way on both paths.
    old = client.post("/api/claims/analyze", json={"k": 99})
    new = client.post("/api/entities/claims/analyze", json={"k": 99})
    assert old.status_code == new.status_code == 422
    assert old.json() == new.json()


@pytest.mark.parametrize("method, path, status, said", [
    ("get", "/api/entities/nothing/analyze", 404, "pack has no 'nothing'"),
    ("post", "/api/entities/subscribers/analyze", 404, "have no analyze of their own"),
    ("get", "/api/entities/members/analyze", 405, "Method Not Allowed"),
    ("get", "/api/entities/members/bogus", 404, "an entity has overview, analyze, rows, save"),
])
def test_what_no_route_answers_is_said_plainly(method, path, status, said):
    got = getattr(client, method)(path)
    assert got.status_code == status
    assert said in got.json()["detail"]


def test_the_scope_levels_are_the_packs():
    assert client.get("/api/scope").json() == entity_loader.scope_levels(packs.active().primary)
    assert client.get("/api/scope?entity=claims").json() == entity_loader.scope_levels("claims")
    got = client.get("/api/scope?entity=nothing")
    assert got.status_code == 404 and "pack has no 'nothing'" in got.json()["detail"]
    got = client.get("/api/scope/plan-categories?entity=claims")
    assert got.status_code == 404
    assert got.json()["detail"] == "Claims have no scope level 'plan-categories'."


def test_a_scope_level_is_narrowed_by_the_levels_above_it(monkeypatch):
    asked = []

    def options(entity, level, chosen=()):
        asked.append((entity, level, chosen))
        return [{"key": "1", "label": "One"}]

    monkeypatch.setattr(entity_loader, "options", options)
    got = client.get("/api/scope/plans?grgr_ck=1&grgr_ck=2&cspd_cat=MED&cspi_id=ignored")
    assert got.status_code == 200 and got.json() == [{"key": "1", "label": "One"}]
    got = client.get("/api/scope/types?entity=claims&pdpd_id=PRD1")
    assert got.status_code == 200
    assert asked == [("members", "plans", (("grgr_ck", ("1", "2")), ("cspd_cat", ("MED",)))),
                     ("claims", "types", (("pdpd_id", ("PRD1",)),))]


def test_a_failed_scope_lookup_is_not_an_empty_list(monkeypatch):
    def options(*a, **k):
        raise RuntimeError("the store is not reachable")

    monkeypatch.setattr(entity_loader, "options", options)
    got = client.get("/api/scope/groups")
    assert got.status_code == 500
    assert "Scope lookup failed" in got.json()["detail"]


def test_the_old_scope_paths_read_the_same_choices(monkeypatch):
    asked = []

    def options(entity, level, chosen=()):
        asked.append((entity, level, chosen))
        return []

    monkeypatch.setattr(entity_loader, "options", options)
    assert client.get("/api/hierarchy/plans?grgr_ck=1&cspd_cat=MED").status_code == 200
    assert client.get("/api/claims/types?grgr_ck=1&cspi_id=P1&pdpd_id=D1").status_code == 200
    assert asked == [
        ("members", "plans", (("grgr_ck", (1,)), ("cspd_cat", ("MED",)))),
        ("claims", "types", (("grgr_ck", (1,)), ("cspi_id", ("P1",)), ("pdpd_id", ("D1",))))]


NEUTRAL = {"count": "members", "children_per_record": "claims_per_member",
           "lines_per_child": "lines_per_claim", "with_children_share": "claimed_share",
           "children_min": "claims_min", "children_max": "claims_max",
           "children_mix": "claims_mix"}


def test_the_export_request_takes_neutral_names_beside_the_old():
    values = {"count": 7, "children_per_record": 2, "lines_per_child": 3,
              "with_children_share": 40.0, "children_min": 1, "children_max": 4,
              "children_mix": [[1, 60], [2, 40]]}
    neutral = ExportRequest.model_validate(values)
    old = ExportRequest.model_validate({NEUTRAL[k]: v for k, v in values.items()})
    assert neutral == old
    for name, field in NEUTRAL.items():
        assert getattr(neutral, field) == values[name]
    # What is kept with a request keeps the old names, so a kept request reads the same.
    kept = neutral.model_dump()
    assert not set(NEUTRAL) & set(kept)
    assert ExportRequest.model_validate(kept) == neutral
    assert ExportRequest().members == 100 and ExportRequest().claimed_share == 100


def _exported(monkeypatch, body: dict) -> ExportRequest:
    """The request /api/export hands on, as FastAPI read its body."""
    import sdg.api.app as api

    seen = []

    def write_file(request):
        seen.append(request)
        return iter([b"x"]), "text/plain", "f.txt", None

    monkeypatch.setattr(api, "write_file", write_file)
    got = client.post("/api/export", json=body)
    assert got.status_code == 200, got.text
    return seen[0]


def test_the_export_route_reads_neutral_names(monkeypatch):
    request = _exported(monkeypatch, {"count": 7, "children_per_record": 2,
                                      "with_children_share": 40, "children_mix": [[1, 100]]})
    assert (request.members, request.claims_per_member, request.claimed_share,
            request.claims_mix) == (7, 2, 40, [[1, 100]])


def test_the_old_name_wins_where_both_are_sent(monkeypatch):
    # The field's own name is the first alias: a body that says both is read
    # as it always was.
    request = _exported(monkeypatch, {"members": 5, "count": 9, "claims_min": 1,
                                      "children_min": 3})
    assert (request.members, request.claims_min) == (5, 1)


@pytest.mark.parametrize("query, said", [
    ("grgr_ck=abc", "grgr_ck takes numbers, not 'abc'."),
    ("grgr_ck=1&grgr_ck=", "grgr_ck takes numbers, not ''."),
    ("grgr_ck=inf", "grgr_ck takes numbers, not 'inf'."),
    ("grgr_ck=7.5", "grgr_ck takes numbers, not '7.5'."),
    ("grgr_ck=1e3", "grgr_ck takes numbers, not '1e3'."),
])
def test_an_id_that_is_not_a_number_is_refused_not_dropped(monkeypatch, query, said):
    monkeypatch.setattr(entity_loader, "options", lambda *a, **k: pytest.fail("read the store"))
    for entity in ("members", "claims"):
        got = client.get(f"/api/scope/plans?entity={entity}&{query}")
        assert got.status_code == 422 and got.json()["detail"] == said
        # As the old routes refused it.
        old = "/api/hierarchy/plans" if entity == "members" else "/api/claims/plans"
        assert client.get(f"{old}?{query}").status_code == 422


def test_a_scope_level_asked_as_an_entity_verb_is_pointed_to_its_route():
    got = client.get("/api/entities/claims/types")
    assert got.status_code == 404
    assert got.json()["detail"] == "types is a scope level: /api/scope/types?entity=claims"


def test_an_entity_lists_the_highlights_its_pack_names(monkeypatch):
    asked = []

    def highlight(entity, key, n=30):
        asked.append((entity, key, n))
        return [{"code": f"{key[0].upper()}1", "description": "One", "n": 9}]

    monkeypatch.setattr(entity_loader, "highlight", highlight)
    got = client.get("/api/entities/claims/highlights?n=6")
    assert got.status_code == 200, got.text
    assert got.json() == [
        {"key": "diagnoses", "label": "Most common diagnoses",
         "codes": [{"code": "D1", "description": "One", "n": 9}]},
        {"key": "procedures", "label": "Most common procedures",
         "codes": [{"code": "P1", "description": "One", "n": 9}]},
    ]
    assert asked == [("claims", "diagnoses", 6), ("claims", "procedures", 6)]
    # The old paths are the same highlight, one each.
    for path, key in (("/api/claims/diagnoses?n=6", "diagnoses"),
                      ("/api/claims/procedures?n=6", "procedures")):
        assert client.get(path).json() == got.json()[0 if key == "diagnoses" else 1]["codes"]
    # Records the pack names no highlight of have none, and no panel.
    assert client.get("/api/entities/members/highlights").json() == []
    assert client.get("/api/entities/subscribers/highlights").json() == []


@pytest.mark.parametrize("path, status, said", [
    ("/api/entities/nothing/highlights", 404, "pack has no 'nothing'"),
    ("/api/entities/claims/highlights?n=0", 422, None),
    ("/api/entities/claims/highlights?n=101", 422, None),
])
def test_highlights_refuse_what_they_cannot_answer(path, status, said):
    got = client.get(path)
    assert got.status_code == status
    if said:
        assert said in got.json()["detail"]


def test_a_failed_highlight_is_not_an_empty_list(monkeypatch):
    def broken(entity, key, n=30):
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(entity_loader, "highlight", broken)
    got = client.get("/api/entities/claims/highlights")
    assert got.status_code == 500
    assert got.json()["detail"].startswith("Most common diagnoses: ")


def test_the_screens_call_the_neutral_routes():
    """The old paths stay for whatever else calls them; the screens use the
    pack's entity keys (ui/src/lib/api.ts builds each path from keyOf/domain)."""
    import re
    from pathlib import Path

    old = re.compile(r"/api/(claims|hierarchy)/|/api/patterns/(analyze|rows|save)\b(?![/-])")
    found = []
    for path in (Path(__file__).parent.parent / "ui" / "src").rglob("*.ts*"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if old.search(line):
                found.append(f"{path.name}:{n}: {line.strip()}")
    assert not found, found
    api = (Path(__file__).parent.parent / "ui" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    for verb in ("overview", "highlights", "analyze", "rows", "save", "available"):
        assert f"/{verb}" in api and "/api/entities/${" in api, verb


def test_each_headline_count_is_one_the_overview_answers():
    """The counts above a pattern list (glossary <entity>.counts) are read by
    name from the entity's overview: each name is one its route returns."""
    pack = packs.load("health")
    for key, entity in pack.entities.items():
        counts = pack.glossary[key].get("counts")
        if entity["role"] not in entities.BY_ROLE:
            assert counts is None, key
            continue
        model = _route(f"/api/entities/{key}/overview").response_model
        assert counts, key
        assert {name for name, _ in counts} <= set(model.model_fields), key

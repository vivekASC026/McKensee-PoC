"""Onboard several databases one after another and switch between them.

Through the API the screens call, in the order a person would: choose a
database on Connect, read it, plan it, generate into it; choose another and do
the same; go back to the first and find its plan and its run exactly as left,
clean it up; go back to the second and clean that up; offboard both. Then home.

What must hold throughout: each database's rows go into that database and no
other; each database's working state comes back when it is chosen again; the
pattern store and the saved patterns never change; offboarding is refused
while a database still holds rows written into it.
"""

import pytest

from tests import facets_copies

A = "SDM_TEST_ONBOARD_A"
B = "SDM_TEST_ONBOARD_B"
# B differs from A where it shows: a name column too narrow for the store.
NARROW = "ALTER TABLE dbo.CMC_MEME_MEMBER ALTER COLUMN MEME_FIRST_NAME varchar(4) NULL"


def _store_signature():
    """The pattern store and the saved patterns, as counts that would move."""
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as conn:
        cur = conn.cursor()
        return {
            "patterns": cur.execute(f"SELECT COUNT(*) FROM {SQL_SCHEMA}.patterns").fetchone()[0],
            "saved": cur.execute(
                f"SELECT COUNT(*) FROM {SQL_SCHEMA}.patterns WHERE saved_by IS NOT NULL").fetchone()[0],
            "members": cur.execute(f"SELECT COUNT(*) FROM {SQL_SCHEMA}.member_denorm").fetchone()[0],
            "claim_lines": cur.execute(f"SELECT COUNT(*) FROM {SQL_SCHEMA}.claims_denorm").fetchone()[0],
        }


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _onboard(client, name):
    """Choose it, read it, plan it -- the Connect and Plan screens' calls."""
    _ok(client.post("/api/target", json={"database": name}))
    state = _ok(client.get("/api/state"))
    assert state["database"] == name, f"Connect says {state['database']}, not {name}"
    for step in ("connect", "discover", "profile", "map", "review"):
        _ok(client.post(f"/api/{step}"))
    plan = _ok(client.post("/api/plan"))
    blocking = [w for w in plan.get("warnings", []) if w.get("severity") == "block"]
    assert not blocking, blocking
    return plan


@pytest.mark.db
@pytest.mark.slow
def test_databases_are_onboarded_one_after_another_and_switched_between():
    from fastapi.testclient import TestClient

    from sdg.api.app import app, service

    client = TestClient(app)
    home = service.registry.database_for("target")
    source_before = facets_copies.counts(service.settings.source_db)
    store_before = _store_signature()

    with facets_copies.copies((A, ()), (B, (NARROW,))):
        try:
            # ---- A: onboard, generate --------------------------------------
            _onboard(client, A)
            plan_a = _ok(client.get("/api/plan"))
            run_a = _ok(client.post("/api/generate"))
            written_a = facets_copies.counts(A)
            print(f"[A] onboarded and generated: {written_a}")
            assert written_a["CMC_CLCL_CLAIM"] > 0, run_a
            assert facets_copies.counts(B) == dict.fromkeys(written_a, 0)

            # ---- B: onboard, generate -------------------------------------
            _onboard(client, B)
            onboarded = {d["database"]: d for d in _ok(client.get("/api/databases/onboarded"))["databases"]}
            assert {A, B} <= set(onboarded), onboarded
            assert onboarded[B]["current"] and onboarded[A]["rowsInIt"]
            _ok(client.post("/api/generate"))
            written_b = facets_copies.counts(B)
            print(f"[B] onboarded and generated: {written_b}; A unchanged: {facets_copies.counts(A) == written_a}")
            assert written_b["CMC_MEME_MEMBER"] > 0
            assert facets_copies.counts(A) == written_a, "B's run reached A"

            import pyodbc

            settings = service.settings
            conn = pyodbc.connect(settings.mssql.connection_string(B))
            longest = conn.execute("SELECT MAX(LEN(MEME_FIRST_NAME)) FROM dbo.CMC_MEME_MEMBER").fetchone()[0]
            conn.close()
            assert longest <= 4, "B's narrow column was given A's names"
            print(f"[B] longest first name {longest} (column holds 4)")

            # Offboarding refused while rows remain.
            refused = client.post("/api/databases/offboard", json={"key": onboarded[A]["key"]})
            assert refused.status_code != 200 and "still holds rows" in refused.text
            print(f"[A] offboard refused while rows remain: {refused.json()['detail'][:70]}")

            # ---- back to A: its plan and run come back; clean it ----------
            _ok(client.post("/api/target", json={"database": A}))
            assert _ok(client.get("/api/state"))["database"] == A
            again = _ok(client.get("/api/plan"))
            assert again["meta"] == plan_a["meta"], "A's plan did not come back as left"
            validated = _ok(client.post("/api/validate"))
            assert validated.get("passed"), validated
            _ok(client.post("/api/cleanup"))
            print(f"[A] switched back: plan as left, validated, cleaned -> {facets_copies.counts(A)}")
            assert facets_copies.counts(A) == dict.fromkeys(written_a, 0)
            assert facets_copies.counts(B) == written_b, "cleaning A touched B"

            # ---- back to B: clean it --------------------------------------
            _ok(client.post("/api/target", json={"database": B}))
            _ok(client.post("/api/cleanup"))
            assert facets_copies.counts(B) == dict.fromkeys(written_b, 0)
            print(f"[B] switched back, cleaned -> {facets_copies.counts(B)}")

            # ---- offboard both; the store is as it was ---------------------
            keys = {d["database"]: d["key"] for d in _ok(client.get("/api/databases/onboarded"))["databases"]}
            _ok(client.post("/api/databases/offboard", json={"key": keys[A]}))
            result = _ok(client.post("/api/databases/offboard", json={"key": keys[B]}))
            # Offboarding the one being worked on goes back to the configured database.
            assert result["workingOn"]["database"] == service.settings.target_db
            left = {d["database"] for d in _ok(client.get("/api/databases/onboarded"))["databases"]}
            assert not ({A, B} & left), left
            assert _store_signature() == store_before, "onboarding touched the pattern store"
            print(f"offboarded both; now on {result['workingOn']['database']}; pattern store unchanged: {store_before}")
        finally:
            # Whatever happened, the rest of the suite gets its home back.
            if service.registry.database_for("target") != home:
                service.set_target(home)

    assert facets_copies.counts(service.settings.source_db) == source_before
    plan = _ok(client.get("/api/plan"))
    assert plan, "the source's plan did not come back"

"""Saving patterns, generating from chosen records, and the session's working files.

A saved pattern is listed under File Generation and keeps the members it holds,
so a claim file made from it next week goes to the same people. Records chosen
under a pattern become a pattern of their own. And the working files that make
a second analysis fast must give back exactly what was put in.
"""

import numpy as np
import pandas as pd
import pytest

from sdg.analyzer.services import disk_cache
from tests.saved_cleanup import unrestore


def test_a_frame_comes_back_from_the_working_files_exactly_as_it_went_in():
    frame = pd.DataFrame({
        "code": ["A1", "B2", "A1", None] * 500,          # repetitive: packed
        "id": [f"C{i:06d}" for i in range(2000)],         # unique: left alone
        "amount": np.arange(2000, dtype=float),
    })
    frame["code"] = frame["code"].astype(object)
    packed = disk_cache._pack((frame, {"note": 1}))
    assert str(packed[0]["code"].dtype) == "category"
    back = disk_cache._unpack(packed)[0]
    pd.testing.assert_frame_equal(back, frame)
    assert not back.attrs


def test_a_value_held_in_memory_is_served_without_touching_the_disk(monkeypatch):
    calls = {"compute": 0, "disk": 0}

    @disk_cache.two_tier("test.memory_first", ttl=60)
    def compute(x):
        calls["compute"] += 1
        return {"x": x}

    real_get = disk_cache.get

    def counting_get(key):
        calls["disk"] += 1
        return real_get(key)

    monkeypatch.setattr(disk_cache, "get", counting_get)
    try:
        compute(41)
        compute(41)
        compute(41)
        assert calls["compute"] == 1
        assert calls["disk"] == 1, "the second and third calls read the disk"
    finally:
        compute.cache_clear()


def test_records_are_named_the_same_way_on_every_page():
    from sdg.analyzer.services.saved_patterns import row_keys

    claims = pd.DataFrame({"CLCL_ID": ["000000000012 "], "CDML_SEQ_NO": [3], "MEME_CK": [7]})
    members = pd.DataFrame({"MEME_CK": [7.0, 9.0]})
    assert row_keys(claims, "claims") == ["000000000012-3"]
    assert row_keys(members, "members") == ["7", "9"]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    return TestClient(app)


def _forget(kept):
    """Delete what a test saved -- only rows it created. A pattern that was
    already saved when the test saved it is somebody's, and stays."""
    unrestore(kept)
    pattern_ids = [k["pattern_id"] for k in kept if k.get("created")]
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as connection:
        cursor = connection.cursor()
        for pattern_id in pattern_ids:
            cursor.execute(f"DELETE FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,))
        connection.commit()


@pytest.mark.db
def test_a_saved_pattern_is_listed_for_files_and_saving_it_again_adds_nothing(client):
    analysis = client.post(
        "/api/patterns/analyze", json={"filters": {"grgr_ck": [1]}, "k": 3},
    ).json()
    body = {"domain": "members", "filters": {"grgr_ck": [1]}, "k": 3,
            "cluster_id": analysis["patterns"][0]["cluster_id"]}
    first = client.post("/api/patterns/keep", json=body).json()
    try:
        again = client.post("/api/patterns/keep", json=body).json()
        assert again["pattern_id"] == first["pattern_id"] and again["created"] is False
        # Saved again under another name: it is still listed by the first, and
        # says so, so the screen can tell where to find it.
        renamed = client.post("/api/patterns/keep", json={**body, "name": "another name"}).json()
        assert renamed["created"] is False and renamed["name"] == first["name"]
        listed = client.get("/api/patterns/for-files").json()["patterns"]
        assert first["pattern_id"] in {p["pattern_id"] for p in listed}
        assert all(p["saved"] for p in listed), "an unsaved pattern is offered for files"

        members = client.post("/api/export", json={
            "format": "edi:834", "subject": "members",
            "pattern_id": first["pattern_id"], "members": 4,
        })
        assert members.status_code == 200 and members.text.count("INS*") == 4
    finally:
        _forget([first])


@pytest.mark.db
def test_a_pattern_deleted_from_saved_is_saved_again_by_saving_it(client):
    from sdg.analyzer import db

    analysis = client.post(
        "/api/patterns/analyze", json={"filters": {"grgr_ck": [1]}, "k": 3},
    ).json()
    body = {"domain": "members", "filters": {"grgr_ck": [1]}, "k": 3,
            "cluster_id": analysis["patterns"][-1]["cluster_id"]}
    first = client.post("/api/patterns/keep", json=body).json()
    try:
        assert first["created"], "this test needs a pattern no one had saved"
        listed = lambda: {p["id"] for p in db.get_saved_patterns()}  # noqa: E731
        # Deleted from Saved: the row stays, unsaved, so Undo can restore it.
        db.mark_pattern_unsaved(first["pattern_id"])
        assert first["pattern_id"] not in listed()
        again = client.post("/api/patterns/keep", json=body).json()
        assert again["pattern_id"] == first["pattern_id"]
        assert again["restored"] is True and again["created"] is False
        assert first["pattern_id"] in listed(), "saved again, yet not on the Saved list"
        # Saved already: saving once more changes nothing.
        once_more = client.post("/api/patterns/keep", json=body).json()
        assert once_more["restored"] is False and once_more["created"] is False
    finally:
        _forget([first])


@pytest.mark.db
def test_chosen_claim_lines_become_a_pattern_whose_claims_go_to_their_members(client):
    analysis = client.post("/api/claims/analyze", json={"grgr_ck": [1]}).json()
    cluster = analysis["patterns"][0]["cluster_id"]
    page = client.post("/api/claims/rows", json={
        "grgr_ck": [1], "cluster_id": cluster, "limit": 6, "offset": 0,
    }).json()
    assert len(page["keys"]) == len(page["rows"])
    chosen = page["keys"][:3]
    expected = {int(r["MEME_CK"]) for r in page["rows"][:3]}

    kept = client.post("/api/patterns/keep", json={
        "domain": "claims", "filters": {"grgr_ck": [1]}, "k": None,
        "cluster_id": cluster, "record_keys": chosen,
    }).json()
    try:
        assert kept["records"] == 3
        assert kept["member_count"] == len(expected)

        shown = client.get(f"/api/patterns/{kept['pattern_id']}/rows?limit=10").json()
        assert sorted(shown["keys"]) == sorted(chosen)

        claims = client.post("/api/export", json={
            "format": "csv", "subject": "claims",
            "pattern_id": kept["pattern_id"], "rows": 30,
        })
        assert claims.status_code == 200, claims.text
        from sdg.api.app import service

        known = {
            str(r["MEME_ID"]).strip()
            for r in service.registry.query(
                "source",
                f"SELECT MEME_ID FROM dbo.CMC_MEME_MEMBER WHERE MEME_CK IN "
                f"({','.join(str(k) for k in expected)})",
                [],
            )
        }
        lines = [line.split(",") for line in claims.text.strip().splitlines()]
        column = lines[0].index("MEMBER_ID")
        assert {row[column].strip() for row in lines[1:]} <= known, (
            "a claim went to someone who was not among the chosen records' members"
        )
    finally:
        _forget([kept])


@pytest.mark.db
def test_a_file_from_a_pattern_needs_no_plan_at_all(client, monkeypatch):
    """Files from a pattern are built from the pattern store alone: its members,
    its codes, its claim structure. No target needs to be onboarded or planned."""
    from sdg.api.app import service

    analysis = client.post("/api/claims/analyze", json={"grgr_ck": [1]}).json()
    kept = client.post("/api/patterns/keep", json={
        "domain": "claims", "filters": {"grgr_ck": [1]}, "k": None,
        "cluster_id": analysis["patterns"][0]["cluster_id"]}).json()
    try:
        monkeypatch.setattr(type(service), "plan", property(lambda self: None))
        response = client.post("/api/export", json={
            "format": "edi:837P", "subject": "claims",
            "pattern_id": kept["pattern_id"], "rows": 40})
        assert response.status_code == 200, response.text
        patients = [s for s in response.text.split("~\n") if s.startswith("NM1*IL")]
        assert patients and all(s.split("*")[9].strip() for s in patients), (
            "a real member's ID should come from the store")
        from sdg.writers.snip import validate

        levels = {lv["level"]: lv for lv in validate(response.text)["levels"]}
        assert all(levels[n]["errors"] == 0 for n in (1, 2, 3, 4, 5)), levels
    finally:
        _forget([kept])


def test_the_saved_list_says_what_each_was_saved_from_and_survives_any_profile(monkeypatch):
    """The Find screen tells a saved found pattern by what it was saved from:
    the analysis, never the records chosen (which can be many). A profile
    that is not an object, or not JSON, lists the pattern with nothing."""
    import json

    from sdg.analyzer.services import discovery_service

    source = {"domain": "members", "filters": {"grgr_ck": [1]}, "k": 3, "cluster_id": 2,
              "claim_type": None, "records": 2, "record_keys": ["M1-1", "M2-1"]}
    stored = [
        {"id": 1, "contextual_name": "From an analysis",
         "profile_json": json.dumps({"saved_from": source})},
        {"id": 2, "contextual_name": "A list for a profile", "profile_json": "[1, 2]"},
        {"id": 3, "contextual_name": "Not JSON", "profile_json": "{not json"},
        {"id": 4, "contextual_name": "No profile", "profile_json": None},
        {"id": 5, "contextual_name": "Saved from a list",
         "profile_json": json.dumps({"saved_from": ["not", "a", "dict"]})},
    ]
    monkeypatch.setattr(discovery_service.db, "get_saved_patterns", lambda: stored)
    listed = {p["pattern_id"]: p for p in discovery_service.list_saved()}
    assert sorted(listed) == [1, 2, 3, 4, 5]
    assert listed[1]["saved_from"] == {key: source[key] for key in
                                       ("domain", "filters", "k", "cluster_id",
                                        "claim_type", "records")}
    assert "record_keys" not in listed[1]["saved_from"]
    for pattern_id in (2, 3, 4, 5):
        assert listed[pattern_id]["saved_from"] is None
        assert listed[pattern_id]["entity"] == "members"

"""Every kind of pattern, to every place a pattern goes.

A pattern is found by an analysis (live), kept (saved), or made from records a
person picked (chosen). It goes to a file -- CSV, or an 834, 837P or 835 -- or
into a database: the source, an empty one onboarded from the pattern store,
or one whose columns are too narrow for some of the store's values.

What must hold wherever it goes: the values in the output are the pattern's
values -- its sexes, its places (a ZIP always with its own city and state),
its diagnoses, its members -- and every EDI file passes every SNIP level. A
database run validates and cleans up to exactly what was there, and the
saved patterns come through all of it unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest

from tests import facets_copies
from tests.saved_cleanup import unrestore

pytestmark = pytest.mark.db

FILTERS = {"grgr_ck": [1]}
EMPTY = "SDM_TEST_ROUTES_EMPTY"
NARROW = "SDM_TEST_ROUTES_NARROW"
# Cities and states too narrow for most of the store's places.
NARROWING = (
    "ALTER TABLE dbo.CMC_MEME_MEMBER ALTER COLUMN MEME_CITY varchar(6) NULL",
)


def _ok(response):
    assert response.status_code == 200, response.text[:2000]
    return response


# ---------------------------------------------------------------------------
# The patterns
# ---------------------------------------------------------------------------

def _saved_profile(pattern_id: int) -> dict:
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as conn:
        row = conn.cursor().execute(
            f"SELECT profile_json FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,)
        ).fetchone()
    return json.loads(row[0])


def _saved_signature() -> dict[int, str]:
    """Every saved pattern, by content, so any change to one shows."""
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as conn:
        rows = conn.cursor().execute(
            f"SELECT id, profile_json FROM {SQL_SCHEMA}.patterns WHERE saved_by IS NOT NULL"
        ).fetchall()
    return {r[0]: hashlib.sha256((r[1] or "").encode()).hexdigest() for r in rows}


def _forget(pattern_ids) -> None:
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as conn:
        cur = conn.cursor()
        for pattern_id in pattern_ids:
            cur.execute(f"DELETE FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,))
        conn.commit()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    return TestClient(app)


@pytest.fixture(scope="module")
def patterns(client):
    """One of each kind, as the Patterns screen makes them. Kept ones are
    removed afterwards by the ids made here and no others."""
    member = _ok(client.post("/api/patterns/analyze", json={"filters": FILTERS, "k": 3})).json()
    member = max(member["patterns"], key=lambda p: p["size"])
    claim = _ok(client.post("/api/claims/analyze", json=FILTERS)).json()
    claim = max(claim["patterns"], key=lambda p: p["size"])

    made: list[int] = []
    saves: list[dict] = []
    try:
        def keep(body):
            kept = _ok(client.post("/api/patterns/keep", json=body)).json()
            # Only a row this test created is this test's to delete: saving a
            # pattern somebody already saved returns theirs.
            saves.append(kept)
            if kept.get("created"):
                made.append(kept["pattern_id"])
            return kept

        saved_member = keep({"domain": "members", "filters": FILTERS, "k": 3,
                             "cluster_id": member["cluster_id"]})
        saved_claim = keep({"domain": "claims", "filters": FILTERS, "k": None,
                            "cluster_id": claim["cluster_id"]})
        member_rows = _ok(client.post("/api/patterns/rows", json={
            "filters": FILTERS, "k": 3, "cluster_id": member["cluster_id"], "limit": 8})).json()
        chosen_members = keep({"domain": "members", "filters": FILTERS, "k": 3,
                               "cluster_id": member["cluster_id"],
                               "record_keys": member_rows["keys"][:8]})
        claim_rows = _ok(client.post("/api/claims/rows", json={
            **FILTERS, "cluster_id": claim["cluster_id"], "limit": 6, "offset": 0})).json()
        chosen_claims = keep({"domain": "claims", "filters": FILTERS, "k": None,
                              "cluster_id": claim["cluster_id"],
                              "record_keys": claim_rows["keys"][:6]})

        live = lambda p, domain, k: {"pattern": {  # noqa: E731
            "profile": p, "domain": domain, "filters": FILTERS, "k": k,
            "cluster_id": p["cluster_id"]}}
        yield {
            "live member": (live(member, "members", 3), member),
            "saved member": ({"pattern_id": saved_member["pattern_id"]},
                             _saved_profile(saved_member["pattern_id"])),
            "chosen members": ({"pattern_id": chosen_members["pattern_id"]},
                               _saved_profile(chosen_members["pattern_id"])),
            "live claim": (live(claim, "claims", None), claim),
            "saved claim": ({"pattern_id": saved_claim["pattern_id"]},
                            _saved_profile(saved_claim["pattern_id"])),
            "chosen claims": ({"pattern_id": chosen_claims["pattern_id"]},
                              _saved_profile(chosen_claims["pattern_id"])),
        }
    finally:
        _forget(made)
        unrestore(saves)


# ---------------------------------------------------------------------------
# What a pattern says
# ---------------------------------------------------------------------------

def _sexes(profile: dict) -> set[str]:
    counts = ((profile.get("categorical") or {}).get("MEME_SEX") or {}).get("counts") or {}
    return {str(k).strip() for k, v in counts.items() if v}


def _places(profile: dict) -> set[tuple[str, str, str]]:
    """The pattern's places; where the store holds no addresses, as a
    customer's denorm does not, the store's own address list."""
    from sdg.api.app import _addresses

    return {(r.get("ADDRESS_CITY", ""), r.get("ADDRESS_STATE", ""), r.get("ADDRESS_ZIP", ""))
            for r in profile.get("geography") or _addresses()}


def _diagnoses(profile: dict) -> set[str]:
    """The diagnoses a pattern holds: its real combinations, where it has them."""
    for group in profile.get("combinations") or []:
        if "DIAGNOSIS_CODE" in group["elements"]:
            return {str(r["DIAGNOSIS_CODE"]).strip() for r in group["records"]}
    clinical = profile.get("clinical") or []
    if clinical:
        return {str(r["DIAGNOSIS_CODE"]).strip() for r in clinical}
    return {str(k).strip() for k in (profile.get("top_diagnoses") or {})}


def _member_ids(profile: dict) -> set[str] | None:
    """The members a kept pattern holds, by member ID; None for a live one."""
    keys = profile.get("member_keys")
    if not keys:
        return None
    from sdg.api.app import service

    rows = service.registry.query(
        "landscape",
        f"SELECT MEME_ID FROM dbo.CMC_MEME_MEMBER WHERE MEME_CK IN ({','.join(str(int(k)) for k in keys)})",
        [],
    )
    return {str(r["MEME_ID"]).strip() for r in rows}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

MEMBER_KINDS = ("live member", "saved member", "chosen members")
CLAIM_KINDS = ("live claim", "saved claim", "chosen claims")
CASES = [(kind, fmt) for kind in MEMBER_KINDS for fmt in ("csv", "edi:834")] + \
        [(kind, fmt) for kind in CLAIM_KINDS for fmt in ("csv", "edi:837P", "edi:835")]


def _segments(text: str, tag: str) -> list[list[str]]:
    return [s.strip().split("*") for s in text.split("~") if s.strip().startswith(tag + "*")]


@pytest.mark.parametrize("kind,fmt", CASES, ids=[f"{k}->{f}" for k, f in CASES])
def test_a_pattern_becomes_a_file_carrying_its_own_values(client, patterns, kind, fmt):
    address, profile = patterns[kind]
    subject = "members" if "member" in kind else "claims"
    body = {"format": fmt, "subject": subject, "members": 25, "rows": 60, **address}
    text = _ok(client.post("/api/export", json=body)).text
    assert text.strip(), "an empty file"

    if fmt.startswith("edi"):
        from sdg.writers.snip import validate

        failing = [(lv["level"], lv["errors"]) for lv in validate(text)["levels"] if lv["errors"]]
        assert not failing, f"SNIP errors by level: {failing}"

    if subject == "members":
        if fmt == "csv":
            rows = list(csv.DictReader(io.StringIO(text)))
            sexes = {r["SEX"] for r in rows}
            places = {(r["ADDRESS_CITY"], r["ADDRESS_STATE"], r["ADDRESS_ZIP"]) for r in rows}
        else:
            sexes = {s[3] for s in _segments(text, "DMG")}
            places = {tuple(s[1:4]) for s in _segments(text, "N4")}
        assert rows if fmt == "csv" else places, "no members written"
        if _sexes(profile):
            assert sexes <= _sexes(profile), f"sexes {sexes} not the pattern's {_sexes(profile)}"
        assert _places(profile), "the pattern carries no places"
        assert places <= _places(profile), (
            f"places not the pattern's, or a ZIP apart from its city: {places - _places(profile)}")
        return

    if fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        assert rows, "no claim lines written"
        members = {r["MEMBER_ID"].strip() for r in rows}
        diagnoses = {r["DIAGNOSIS_CODE"].strip() for r in rows}
    elif fmt == "edi:837P":
        # The subscriber, where they are the patient. A dependent's claim names
        # its subscriber in NM1*IL and the patient in NM1*QC, which in 5010
        # carries no ID: the patient is a member of the subscriber's family.
        members, families = set(), set()
        segments = [line.rstrip("~").split("*") for line in text.splitlines()]
        for at, s in enumerate(segments):
            if s[:2] == ["NM1", "IL"]:
                ahead = next((n for n in segments[at + 1:] if n[0] in ("CLM", "PAT")), [""])
                (families if ahead[0] == "PAT" else members).add(s[9].strip())
        held = _member_ids(profile)
        if held is not None:
            assert {f[:-2] for f in families} <= {h[:-2] for h in held}, (
                f"claims for a family the pattern does not hold: {families}")
        diagnoses = {s[1].split(":")[1] for s in _segments(text, "HI")}
    else:
        members = {s[9].strip() for s in _segments(text, "NM1") if s[1] == "QC" and len(s) > 9}
        diagnoses = set()
    assert all(members), "a claim with no member ID"
    held = _member_ids(profile)
    if held is not None:
        assert members <= held, f"claims for people the pattern does not hold: {members - held}"
    if diagnoses and _diagnoses(profile):
        assert diagnoses <= _diagnoses(profile), (
            f"diagnoses not the pattern's: {diagnoses - _diagnoses(profile)}")


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

def _onboard(client, name):
    _ok(client.post("/api/target", json={"database": name}))
    assert _ok(client.get("/api/state")).json()["database"] == name
    for step in ("connect", "discover", "profile", "map", "review"):
        _ok(client.post(f"/api/{step}"))
    # A pattern goes into a database through its plan, so there has to be one;
    # the Patterns screen says so and offers the database route only after.
    _ok(client.post("/api/plan"))


def _apply(client, profile, kind):
    return _ok(client.post("/api/patterns/to-plan", json={
        "pattern": profile, "kind": kind, "apply": True})).json()


def _query(database, sql):
    import pyodbc

    from sdg.config import get_settings

    conn = pyodbc.connect(get_settings().mssql.connection_string(database))
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


@pytest.mark.slow
@pytest.mark.parametrize("where", ["empty", "narrow"])
def test_a_pattern_goes_into_an_onboarded_database_with_its_own_values(client, patterns, where):
    from sdg.api.app import service

    home = service.registry.database_for("target")
    saved_before = _saved_signature()
    name, alterations = (EMPTY, ()) if where == "empty" else (NARROW, NARROWING)
    member = patterns["live member"][1]
    claim = patterns["live claim"][1]

    with facets_copies.copies((name, alterations)):
        try:
            _onboard(client, name)
            applied = _apply(client, member, "member")
            assert applied["applied"], "the member pattern reached no column"
            # The address's columns: other combinations a pattern carries --
            # marital status with relationship -- are drawn as records too.
            address = [t for t in applied["resolution"]["targets"]
                       if (t.get("params") or {}).get("record_key") == "pattern_address"]
            if where == "narrow":
                assert not address, "an address went into a column too narrow for it"
                refused = " ".join(r["reason"] for r in applied["resolution"]["rejections"])
                assert "MEME_CITY" in refused and "ZIP is never written apart" in refused, refused
            else:
                assert {t["column"] for t in address} >= {"MEME_CITY", "MEME_ZIP", "MEME_STATE"}
            assert _apply(client, claim, "claim")["applied"], "the claim pattern reached no column"
            plan = _ok(client.post("/api/plan")).json()
            blocking = [w for w in plan.get("warnings", []) if w.get("severity") == "block"]
            assert not blocking, blocking
            run = _ok(client.post("/api/generate")).json()
            written = facets_copies.counts(name)
            assert written["CMC_MEME_MEMBER"] and written["CMC_CLCL_CLAIM"], written
            # The load is kept for audit, and regenerated is the rows it wrote,
            # without writing to any database.
            from sdg.analyzer.services import generation_requests as gr

            try:
                kept = run["request"]
                loaded = sum(t["rows_written"] for t in run["tables"])
                assert kept and gr.get_file(kept["file"])["params"]["rows"] == loaded
                again = client.post(f"/api/generation-requests/files/{kept['file']}/again")
                assert again.status_code == 200, again.text
                assert again.headers["x-sdm-matched"] == "1", again.content[:2000]
                assert facets_copies.counts(name) == written, "regenerating wrote rows"
            finally:
                if run.get("request"):
                    gr.delete_request(run["request"]["request"])

            sexes = {r[0].strip() for r in _query(name, "SELECT DISTINCT MEME_SEX FROM dbo.CMC_MEME_MEMBER")}
            assert sexes <= _sexes(member), f"sexes {sexes} not the pattern's"

            places = set(_query(name, "SELECT DISTINCT RTRIM(MEME_CITY), RTRIM(MEME_STATE), "
                                      "RTRIM(MEME_ZIP) FROM dbo.CMC_MEME_MEMBER"))
            if where == "empty":
                assert places <= _places(member), (
                    f"places not the pattern's, or a ZIP apart from its city: "
                    f"{sorted(places - _places(member))[:5]}")
            else:
                # The pattern's places were refused whole; the columns kept the
                # database's own rules and the load ran to the end.
                longest = _query(name, "SELECT MAX(LEN(MEME_CITY)) FROM dbo.CMC_MEME_MEMBER")[0][0]
                assert (longest or 0) <= 6

            diagnoses = {r[0].strip() for r in _query(
                name, "SELECT DISTINCT CLCL_IDCD_ID FROM dbo.CMC_CLCL_CLAIM")}
            assert diagnoses <= _diagnoses(claim), (
                f"diagnoses not the pattern's: {sorted(diagnoses - _diagnoses(claim))[:5]}")

            checked = _ok(client.post("/api/validate")).json()
            assert checked.get("passed")
            # Every diagnosis and procedure pair written is one real claims carry,
            # wherever the line's procedure column is matched to the procedure
            # element. An empty database's CDML_SVC_CD is not matched yet, so
            # there is no procedure to pair and nothing to check -- said here
            # rather than passed over.
            clinical = [c for c in checked["results"] if c["kind"] == "combination"
                        and "PROCEDURE_CODE" in c["name"]]
            procedure_matched = any(
                f.element == "PROCEDURE_CODE"
                for t in service.require_plan().tables for f in t.fields)
            if procedure_matched:
                assert clinical and all(c["passed"] for c in clinical), clinical
            else:
                assert not clinical
            _ok(client.post("/api/cleanup"))
            assert facets_copies.counts(name) == dict.fromkeys(written, 0)
            keys = {d["database"]: d["key"] for d in
                    _ok(client.get("/api/databases/onboarded")).json()["databases"]}
            _ok(client.post("/api/databases/offboard", json={"key": keys[name]}))
        finally:
            if service.registry.database_for("target") != home:
                service.set_target(home)
    assert _saved_signature() == saved_before, "a saved pattern changed"


@pytest.mark.slow
def test_a_pattern_goes_into_the_source_and_leaves_it_as_it_was(client, patterns):
    """The source: rows written beside its own, validated, removed. Its working
    files are put back afterwards, so the rest of the suite sees no pattern."""
    from sdg.api.app import service

    source = service.settings.source_db
    before = facets_copies.counts(source)
    saved_before = _saved_signature()
    work = service.work
    kept = Path(work) / "_routes_backup"
    kept.mkdir(exist_ok=True)
    for f in Path(work).iterdir():
        if f.is_file():
            shutil.copy2(f, kept / f.name)
    written = False
    try:
        assert _apply(client, patterns["live member"][1], "member")["applied"]
        _ok(client.post("/api/plan"))
        run = _ok(client.post("/api/generate")).json()
        written = True
        assert facets_copies.counts(source) != before, run
        assert _ok(client.post("/api/validate")).json().get("passed")
        _ok(client.post("/api/cleanup"))
        written = False
        assert facets_copies.counts(source) == before, "the source was not left as it was"
    finally:
        if written:
            # Whatever failed, the rows this run wrote come out again: left in
            # the source they would trip every test after this one.
            client.post("/api/cleanup")
        for f in kept.iterdir():
            shutil.copy2(f, Path(work) / f.name)
        shutil.rmtree(kept, ignore_errors=True)
        service._cache.clear()
    assert _saved_signature() == saved_before, "a saved pattern changed"

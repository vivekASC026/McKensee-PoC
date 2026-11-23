"""The whole thing, once, against a real database.

Every other suite tests a part. This one runs the sequence a person runs —
read the database, measure it, match it, decide, find a pattern, plan, preview,
write, check, remove — and asserts the handover between each step. The failures
it is here to catch are the ones that only appear in the seam: a step whose
output the next step cannot read, a run that writes rows nothing can remove, an
export that describes a different population from the one generated.

Two rules make it safe to run against the demo database:

    its own working directory  nothing it does touches `work/`, so a run never
                               disturbs a demo in progress
    its own seed               the rows it writes carry a seed no other run
                               uses, and the last step removes exactly those

It is deliberately small — a few hundred subscribers — because what is being
tested is that the steps fit together, not how fast they are. The volume runs
live in STATUS.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sdg.api.service import Service

pytestmark = [pytest.mark.db, pytest.mark.slow]

# Small enough to run in seconds, large enough that every table has rows and
# every ratio has something to draw from.
SUBSCRIBERS = 300
E2E_SEED = "e2e-suite"

MEMBERS = "dbo.CMC_MEME_MEMBER"
CLAIMS = "dbo.CMC_CLCL_CLAIM"
LINES = "dbo.CMC_CDML_CL_LINE"

REFERENCE_TABLES = {
    "dbo.CMC_PRPR_PROV": "reuse",
    "dbo.CMC_PDPD_PRODUCT": "reuse",
    "dbo.CMC_GRGR_GROUP": "reuse",
    "dbo.CMC_SGSG_SUBGROUP": "reuse",
    "dbo.CMC_CSPD_CAT_DESC": "reuse",
    "dbo.CMC_CSPI_CS_PLAN": "reuse",
    "dbo.CMC_PLDS_PLAN_DESC": "reuse",
    "dbo.CMC_PDDS_PROD_DESC": "reuse",
    "dbo.CMC_IDCD_DIAG_CODE": "reuse",
    "dbo.CMC_IPCD_PROC_CODE": "reuse",
    "dbo.CMC_NWNW_NETWORK": "reuse",
    "dbo.CMC_NWPR_RELATION": "reuse",
    "dbo.CMC_NWST_NET_SET": "reuse",
    # Who a member's PCP is comes from the store; assigning PCPs
    # to new members is not generated yet.
    "dbo.CMC_MEPR_PRIM_PROV": "exclude",
    "dbo.CMC_CLCL_AUDIT": "exclude",
}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One service with a working directory of its own, shared by every step.

    Module-scoped and ordered: these tests are a sequence, and each one depends
    on what the last wrote. That is unusual for a test file and is the point of
    this one.
    """
    service = Service(work_dir=tmp_path_factory.mktemp("sdg-e2e"))
    service.write_settings({
        "rootCounts": {"dbo.CMC_SBSB_SUBSC": SUBSCRIBERS},
        "modes": REFERENCE_TABLES,
        "seed": E2E_SEED,
        "planName": "e2e",
    })
    yield service

    # Whatever the tests did, the rows must not outlive them.
    try:
        if service.exists("manifest"):
            service.cleanup()
    except Exception as exc:  # reported, never swallowed
        pytest.fail(f"the end-to-end run left rows behind: {exc}")


# ---------------------------------------------------------------------------
# Reading the database
# ---------------------------------------------------------------------------

def test_01_connect(run):
    result = run.connect()
    assert all(check["ok"] for check in result["checks"]), result


def test_02_discover(run):
    result = run.discover()
    assert result["tables"] >= 15
    # The analyzer's own schema is the tool's working state, not data to model.
    assert not any(name.startswith("sdm.") for name in run.model.table_names())


def test_03_profile(run):
    result = run.profile()
    assert result["columns"] >= 90
    report = run.report
    assert report.ratio_for(CLAIMS, MEMBERS) is not None, (
        "the claims-per-member shape is what the cascade is built from"
    )


def test_04_map(run):
    result = run.map()
    assert result["auto_map_rate"] >= 0.80, result
    mappings = run.require_mappings()
    assert mappings.for_column(MEMBERS, "MEME_SEX").element == "SEX"


def test_05_review(run):
    result = run.review()
    assert result["proposals"] > 0, result
    assert run.exists("proposals")


# ---------------------------------------------------------------------------
# A pattern, found and applied
# ---------------------------------------------------------------------------

def test_06_a_member_pattern_resolves_onto_columns(run):
    from sdg.analyzer.bridge import to_pattern
    from sdg.api.app import app
    from fastapi.testclient import TestClient
    from sdg.plan.patterns import resolve_pattern

    # The plan has to exist before a pattern can be resolved against it, so the
    # first build happens here and is rebuilt with the pattern in the next step.
    run.build()

    analysis = TestClient(app).post(
        "/api/patterns/analyze", json={"filters": {"grgr_ck": [1]}, "k": 3},
    )
    assert analysis.status_code == 200, analysis.text

    pattern = to_pattern(
        analysis.json()["patterns"][0], kind="member", as_of="2026-06-30",
    )
    resolution = resolve_pattern(
        pattern,
        catalog=run.catalog,
        mappings=run.require_mappings(),
        plan=run.require_plan(),
        model=run.model,
    )
    assert resolution.targets, resolution.rejections
    run.e2e_resolution = resolution  # carried to the next step


def test_07_applying_it_as_a_cohort_writes_decisions(run):
    from sdg.plan.patterns import apply_to_decisions

    document, written = apply_to_decisions(
        [run.e2e_resolution], run.read_decisions(), as_cohort=True,
    )
    run.write_decisions(document)
    assert written, "the pattern reached no column"
    assert document["cohorts"], "a share was asked for but no cohort recorded"
    assert all(entry.get("_cohort") for entry in document["columns"] if entry.get("_pattern"))


# ---------------------------------------------------------------------------
# Reaching an attribute of a table nothing generates
# ---------------------------------------------------------------------------

def test_07d2_a_pattern_can_narrow_a_reference_set(run):
    """"Claims on circulatory diagnoses" used to be refused.

    The diagnosis chapter is a column of the diagnosis table, and that table is
    reused -- nothing generates it, so there is no column for a pattern to
    state. The refusal was correct about the column and wrong about the
    capability: a claim holds a diagnosis key, and which diagnosis that key
    picks is something the run decides.

    So the pattern narrows the key instead. The column still points at real
    rows of the real table and referential integrity is untouched; there are
    simply fewer rows it may choose from. What this asserts is the whole chain
    -- the resolver finding the route, the attribute being loaded with the key
    set, and the generated rows actually honoring it, which is the only part
    that proves the other two.

    Every decision it writes is put back before it returns.
    """
    import pandas as pd

    from sdg.analyzer.sql import read_sql

    from sdg.analyzer.bridge import AnalyzerPattern, PatternParameter
    from sdg.plan.patterns import apply_to_decisions, resolve_pattern

    # Through the source connection, not the analyzer's: the analyzer reads the
    # extract store, and the diagnosis table is a source table. They were the
    # same database once, which is the only reason reading it either way ever
    # worked.
    chapter = "Circulatory"
    with run.registry.connect("source") as conn:
        wanted = set(
            read_sql(
                "SELECT IDCD_ID FROM dbo.CMC_IDCD_DIAG_CODE WHERE IDCD_CHAPTER = ?",
                conn, params=[chapter],
            )["IDCD_ID"]
        )
    assert wanted, f"the fixture holds no {chapter} diagnoses to narrow to"

    def diagnoses_in(preview):
        table = next(t for t in preview["tables"] if "CLCL_CLAIM" in t["name"])
        index = table["columns"].index("CLCL_IDCD_ID")
        return {
            row[index] if isinstance(row, (list, tuple)) else row["CLCL_IDCD_ID"]
            for row in table["rows"]
        }

    before = diagnoses_in(run.dry_run(rows=40))
    assert not before <= wanted, (
        "every previewed claim was already in this chapter, so narrowing to it "
        "would prove nothing"
    )

    resolution = resolve_pattern(
        AnalyzerPattern(
            id="e2e-refset",
            kind="claim",
            label=f"Claims on {chapter.lower()} diagnoses",
            parameters=[
                PatternParameter(label="Diagnosis Chapter", values=[chapter]),
            ],
        ),
        catalog=run.catalog,
        mappings=run.require_mappings(),
        plan=run.require_plan(),
        model=run.model,
        as_of="2026-06-30",
    )

    assert resolution.targets, (
        f"an attribute of a reused table was refused: {resolution.rejections}"
    )
    target = resolution.targets[0]
    # Narrowed, not replaced: still drawn into the diagnosis table -- as a
    # reference, or as a combination kept within it.
    from sdg.plan.model import points_at

    assert target.generator in ("reference", "lookup"), (
        "the pattern replaced the key rather than narrowing it, which would "
        "break the reference"
    )
    assert target.params["filter_attribute"] == "IDCD_CHAPTER"
    assert target.params["filter_values"] == [chapter]
    assert points_at(target.params, target.generator) == "dbo.CMC_IDCD_DIAG_CODE", (
        "narrowing pointed at a different table than the one the element is on"
    )

    original = run.read_decisions()
    try:
        document, written = apply_to_decisions(
            [resolution], run.read_decisions(), as_cohort=False,
        )
        run.write_decisions(document)
        assert written == ["dbo.CMC_CLCL_CLAIM.CLCL_IDCD_ID"], written
        run.build()

        after = diagnoses_in(run.dry_run(rows=40))
        assert after, "the preview produced no claims to check"
        assert after <= wanted, (
            f"the run produced diagnoses outside {chapter}: "
            f"{sorted(after - wanted)}"
        )
    finally:
        run.write_decisions(original)
        run.build()


# ---------------------------------------------------------------------------
# The rows a pattern was read from
# ---------------------------------------------------------------------------

def test_07e_a_pattern_can_show_what_it_was_derived_from(run):
    """Every other fact about a pattern is a summary of these rows.

    A mean age, a share of the book, a band -- all of it is a claim about a
    subset, and the claim is only checkable if the subset can be looked at.
    Both domains answer in the same shape, which is what lets one screen serve
    both.

    The part that matters beyond "it returns rows" is `phi_columns`. These are
    real source rows: on this fixture they are synthetic, but the tool is
    pointed at production-shaped databases and the server sends names, dates of
    birth and SSNs exactly as stored. The screen covers those columns by
    default, and it can only do that if the server keeps saying which ones they
    are.
    """
    from fastapi.testclient import TestClient
    from sdg.api.app import app

    client = TestClient(app)

    cases = (
        ("members", "/api/patterns/rows",
         {"filters": {}, "k": 3, "cluster_id": 0, "limit": 5}),
        ("claims", "/api/claims/rows",
         {"k": 3, "cluster_id": 0, "limit": 5}),
    )

    shapes = {}
    for domain, path, body in cases:
        response = client.post(path, json=body)
        assert response.status_code == 200, f"{domain}: {response.text}"
        page = response.json()
        shapes[domain] = set(page)

        assert page["columns"], f"{domain} returned no columns"
        assert page["rows"], f"{domain} returned no rows"
        assert page["returned"] == len(page["rows"]) <= 5
        assert page["total"] >= page["returned"]

        # Rows are addressed by column name, so a column the header names has
        # to be a key the rows carry.
        assert set(page["rows"][0]) <= set(page["columns"]), (
            f"{domain}: a row carried a key the header does not name"
        )

        assert set(page["phi_columns"]) <= set(page["columns"]), (
            f"{domain}: a column was marked personal that is not in the table"
        )

        # Paging has to move, or every page is page one.
        second = client.post(path, json={**body, "offset": 5})
        assert second.status_code == 200, second.text
        if page["has_more"]:
            assert second.json()["rows"] != page["rows"], (
                f"{domain}: the second page repeated the first"
            )

    assert shapes["members"] == shapes["claims"], (
        "the two domains answer in different shapes, so one screen cannot "
        "serve both"
    )

    # The member subset is the one with people in it, and the screen's default
    # of covering them depends on this not being empty.
    members = client.post(
        "/api/patterns/rows",
        json={"filters": {}, "k": 3, "cluster_id": 0, "limit": 5},
    ).json()
    assert members["phi_columns"], (
        "no column was marked personal in a table holding names and SSNs, so "
        "the screen would show them uncovered"
    )


# ---------------------------------------------------------------------------
# A requirement, turned into the same thing
# ---------------------------------------------------------------------------

def test_07b_a_requirement_becomes_cohorts_with_shares(run):
    """Story to Data has no generator of its own, and does not need one.

    A requirement that names cohorts and how much of each it wants is a
    description of a population, and the plan engine makes populations. What is
    asserted here is the arithmetic that makes that true: the volume asked for
    splits across the patterns, and the splits are the shares written.
    """
    from fastapi.testclient import TestClient

    from sdg.analyzer import db
    from sdg.api.app import app

    client = TestClient(app)

    # Keep two patterns so there is something to split between.
    kept: list[int] = []
    for group in (1, 8):
        analysis = client.post(
            "/api/patterns/analyze", json={"filters": {"grgr_ck": [group]}, "k": 2},
        ).json()
        saved = client.post("/api/patterns/save", json={
            "filters": {"grgr_ck": [group]}, "k": 2,
            "cluster_id": analysis["patterns"][0]["cluster_id"],
        })
        assert saved.status_code == 200, saved.text
        kept.append(saved.json()["pattern_id"])

    try:
        response = client.post("/api/story/to-plan", json={
            "pattern_ids": kept, "total_volume": 1000, "as_of": "2026-06-30",
        })
        assert response.status_code == 200, response.text
        body = response.json()

        assert sum(body["allocation"].values()) == 1000, body["allocation"]
        assert not body["unreadable"], body["unreadable"]
        assert len(body["patterns"]) == len(kept)

        shares = [p["share"] for p in body["patterns"]]
        assert all(s is not None for s in shares)
        assert abs(sum(shares) - 1.0) < 0.01, shares
        for entry in body["patterns"]:
            assert entry["targets"], entry["rejections"]
            # Each row's share is its own slice of the volume, not a guess.
            assert abs(entry["share"] - entry["rows"] / 1000) < 0.01, entry
        # Resolving is a read; nothing was written without being asked.
        assert body["applied"] == []
    finally:
        # Unsaving only clears a flag -- the row stays, because a discovered
        # pattern is a catalog entry. These are this test's rows, so they go.
        _forget_patterns(kept)


def test_07c_rebuilding_the_schema_does_not_destroy_saved_work(run, tmp_path):
    """Rebuilding the fixture runs the analyzer's schema script.

    It used to drop every table before creating it, which is right for the
    lookups it owns and wrong for the ones a person fills. A rebuild emptied the
    pattern catalog, and that is the kind of loss nobody reports because it
    looks like nothing happened.
    """
    import pyodbc
    from fastapi.testclient import TestClient

    from sdg.analyzer import db
    from sdg.api.app import app
    from sdg.config import get_settings
    from seed.build import run_sql_script

    client = TestClient(app)
    analysis = client.post(
        "/api/patterns/analyze", json={"filters": {"grgr_ck": [1]}, "k": 2},
    ).json()
    saved = client.post("/api/patterns/save", json={
        "filters": {"grgr_ck": [1]}, "k": 2,
        "cluster_id": analysis["patterns"][0]["cluster_id"],
    })
    pattern_id = saved.json()["pattern_id"]

    settings = get_settings()
    # Against the store, which is where patterns and rules live, and only the
    # store's half of the script, as `seed.bootstrap.ensure_store` applies it.
    # Run against the source it proved nothing and put the analyzer's tables
    # back into the customer database.
    text = Path("seed/ddl_sdm.sql").read_text(encoding="utf-8")
    store_only = tmp_path / "store_only.sql"
    store_only.write_text(
        text[: text.index("IF OBJECT_ID('sdm.member_denorm', 'V')")], encoding="utf-8",
    )

    # A rule a person edited, which a rebuild used to recreate away.
    with db.get_connection() as conn:
        rule_id, label = conn.cursor().execute(
            "SELECT TOP 1 id, field_label FROM sdm.generation_rules ORDER BY id"
        ).fetchone()
        conn.cursor().execute(
            "UPDATE sdm.generation_rules SET field_label = ? WHERE id = ?",
            "edited by hand", rule_id,
        )
        conn.commit()

    try:
        # Twice: the first run proves it is preserved, the second that the
        # guards are idempotent rather than merely absent.
        for _ in range(2):
            connection = pyodbc.connect(
                settings.mssql.connection_string(settings.denorm_db), autocommit=False,
            )
            try:
                run_sql_script(connection, store_only)
            finally:
                connection.close()
            assert db.get_pattern(pattern_id) is not None, (
                "rebuilding the schema destroyed a saved pattern"
            )
            with db.get_connection() as conn:
                row = conn.cursor().execute(
                    "SELECT field_label FROM sdm.generation_rules WHERE id = ?", rule_id,
                ).fetchone()
            assert row is not None and row[0] == "edited by hand", (
                "rebuilding the schema lost an edited rule"
            )
    finally:
        _forget_patterns([pattern_id])
        with db.get_connection() as conn:
            conn.cursor().execute(
                "UPDATE sdm.generation_rules SET field_label = ? WHERE id = ?",
                label, rule_id,
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Planning and previewing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reading a source into the analysis store
# ---------------------------------------------------------------------------

def test_07c2_an_extract_fills_the_store_without_touching_the_source(run):
    """Pattern analysis reads an extract, not a source.

    The store's member_denorm and claims_denorm are shaped as the customer's
    platform defines them, which carry no column naming a source: the store
    holds one source's rows. An extract is a complete refresh: the tables are
    emptied and the source read again.

    What is asserted here is the contract the analyzer stands on: the store
    holds what the source holds, the source is untouched by the reading, and a
    second extract is not a second copy.
    """
    import pandas as pd

    from sdg.analyzer.sql import read_sql

    from sdg.analyzer.config import describe_connection
    from sdg.extract import extract, sources

    # The analyzer must be pointed at the store, not at a customer database.
    where = describe_connection()
    assert where["database"] == run.settings.denorm_db, where
    assert where["database"] != run.settings.source_db, (
        "the analysis store is the source database, so the tool is writing its "
        "own tables into data it does not own"
    )

    assert "FACETS_QA" in sources(), sources()

    def counts(conn, table):
        return int(read_sql(f"SELECT COUNT(*) n FROM sdm.{table}", conn)["n"].iloc[0])

    with run.registry.connect("source") as src:
        at_source = {
            t: counts(src, t) for t in ("member_denorm", "claims_denorm")
        }
    assert all(n > 0 for n in at_source.values()), at_source

    result = extract("FACETS_QA", registry=run.registry)
    assert result.rows > 0
    # The two denorms, beside which the extract also fills the provider tables.
    by_table = {t.table: t.rows for t in result.tables if t.table in at_source}
    assert by_table == at_source, (
        f"the extract did not read what the source holds: {by_table} vs {at_source}"
    )

    with run.registry.connect("denorm") as store:
        for table, expected in at_source.items():
            assert counts(store, table) == expected, (
                f"{table} holds rows from more than the source just extracted"
            )

    # A complete refresh replaces. Twice is the same as once.
    again = extract("FACETS_QA", registry=run.registry)
    assert {t.table: t.rows for t in again.tables if t.table in at_source} == at_source
    with run.registry.connect("denorm") as store:
        for table, expected in at_source.items():
            assert counts(store, table) == expected, (
                f"{table} doubled on a second extract rather than being replaced"
            )

    # And the source is only ever read.
    with run.registry.connect("source") as src:
        assert {
            t: counts(src, t) for t in ("member_denorm", "claims_denorm")
        } == at_source, "the extract changed the source it read"


# ---------------------------------------------------------------------------
# What the store says, in a form another database can use
# ---------------------------------------------------------------------------

def test_07c3_the_store_describes_itself_per_element(run):
    """A profile describes one database. A landscape describes a shape.

    The store was just filled by an extract. What it holds is keyed by table
    and column, which says nothing usable about any other database. Keyed by
    element it becomes portable: not "MEME_BIRTH_DT holds dates between these
    two" but "DOB does", which a target carrying a date of birth under any name
    can be told.

    The relationship matters as much as the values. Extracts are flat and
    declare no keys between them, so children-per-parent has to be measured
    rather than inferred, and without it a target's cascade has nothing to size
    itself from.
    """
    from sdg.landscape import derive, load, save
    from sdg.landscape.derive import unmeasurable

    shape = derive(
        source="FACETS_QA", catalog=run.catalog, registry=run.registry,
    )

    assert shape.elements, "the store described nothing"
    assert shape.rows_seen > 0

    # Values travel; data does not. A code column is a vocabulary and carries
    # its values. A name column is somebody's name and must not.
    coded = [e for e in shape.elements if e.is_coded]
    assert coded, "no element came back as a value set"
    for element in coded:
        assert sum(v["weight"] for v in element.values) == pytest.approx(1.0, abs=0.01)
        assert element.observed_in, f"{element.element} has no provenance"

    dob = shape.element("DOB")
    # Personal: its earliest and latest are withheld, the rows it was seen in kept.
    assert dob is not None and dob.minimum is None and dob.maximum is None
    assert dob.observed_in

    assert shape.relationships, (
        f"nothing measured a relationship in the store. "
        f"Unmeasurable: {unmeasurable()}"
    )
    relationship = shape.relationships[0]
    assert relationship.parents > 0 and relationship.children > 0
    assert relationship.minimum <= relationship.median <= relationship.maximum
    assert relationship.child_table and relationship.parent_table, (
        "a relationship that does not say which tables it was measured between "
        "cannot be matched onto a target by a stated subject mapping"
    )
    # The spread, not just its average: a mean of twenty describes a book where
    # everyone has twenty and one where half have two equally badly.
    assert relationship.histogram, "the relationship carried no distribution"
    assert sum(b["weight"] for b in relationship.histogram) == pytest.approx(1.0, abs=0.01)

    # It survives a round trip, because the target build reads it from disk.
    path = save(shape, run.work / "landscape-FACETS_QA.json")
    again = load(path)
    assert again.summary() == shape.summary()
    assert again.relationships[0].mean == relationship.mean


# ---------------------------------------------------------------------------
# The other half of the book
# ---------------------------------------------------------------------------

def test_07d_the_claims_domain_answers_the_same_questions(run):
    """One set of screens, two domains.

    The interface keeps no second set of claims screens: the domain is chosen
    once, in the rail, and every screen takes it. That only works while the
    claims half answers the same questions the member half does, in the same
    shapes, so what is asserted here is the contract the switch stands on
    rather than the markup that reads it.
    """
    from fastapi.testclient import TestClient
    from sdg.analyzer.bridge import to_pattern
    from sdg.api.app import app
    from sdg.plan.patterns import resolve_pattern

    client = TestClient(app)

    # This one answer is what enables the switch; false disables Claims and
    # the rest of this step would be unreachable from the screen.
    available = client.get("/api/claims/available")
    assert available.status_code == 200, available.text
    assert available.json()["available"] is True

    overview = client.get("/api/claims/overview").json()
    assert overview["n_claims"] > 0
    assert overview["n_claim_lines"] >= overview["n_claims"], (
        "the scope header would report fewer lines than claims"
    )

    # The evidence panel reads these two and shows the description, not the
    # code. A code with no description renders as a bare identifier.
    for path in ("/api/claims/diagnoses?n=6", "/api/claims/procedures?n=6"):
        codes = client.get(path).json()
        assert len(codes) == 6, path
        assert all(row["description"] for row in codes), path
        counts = [row["n"] for row in codes]
        assert counts == sorted(counts, reverse=True), f"{path} is not ranked"

    # The claim-type filter stands where the plan-category filter stands for
    # members. Every key needs a label of its own: a label equal to its key is
    # the unmapped case, and that reached the screen once already.
    types = client.get("/api/claims/types").json()
    assert types, "no claim types, so the filter would render empty"
    unlabeled = [row["key"] for row in types if row["label"] == row["key"]]
    assert not unlabeled, f"claim types with no label: {unlabeled}"
    assert len({row["label"] for row in types}) == len(types), "labels collide"

    # Analyzing is the same gesture in both domains, and the filter beside it
    # has to narrow what comes back or the control does nothing.
    whole = client.post("/api/claims/analyze", json={"k": 3})
    assert whole.status_code == 200, whole.text
    assert whole.json()["patterns"], "no claims patterns in the whole book"

    narrowed = client.post(
        "/api/claims/analyze", json={"claim_type": [types[0]["key"]], "k": 3},
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["subset_claim_lines"] < whole.json()["subset_claim_lines"], (
        "the claim-type filter did not narrow the subset"
    )

    # And a claims pattern has to reach columns the way a member one does --
    # the same resolver, told which subject it is reading.
    resolution = resolve_pattern(
        to_pattern(whole.json()["patterns"][0], kind="claim", as_of="2026-06-30"),
        catalog=run.catalog,
        mappings=run.require_mappings(),
        plan=run.require_plan(),
        model=run.model,
    )
    assert resolution.targets, (
        f"a claims pattern reached no column: {resolution.rejections}"
    )


def test_08_plan_carries_the_cohort(run):
    plan, _graph, _summary = run.build()
    assert plan.cohorts, "the decision file named a cohort; the plan did not take it"

    overridden = [
        field for table in plan.tables for field in table.fields if field.overrides
    ]
    assert overridden, "a cohort with no column overrides generates nothing different"
    for field in overridden:
        assert field.generator, "the base rule must survive; a cohort is an addition"

    generated = [t for t in plan.tables if t.mode == "generate"]
    assert {t.name for t in generated} == {
        "dbo.CMC_SBSB_SUBSC", MEMBERS, CLAIMS, LINES,
    }
    assert not plan.blocking_warnings(), plan.blocking_warnings()


def test_08b_the_driving_parent_can_be_changed_and_changes_the_shape(run):
    """Which parent a table is counted against, chosen rather than inferred.

    The builder picks the deepest parent by default, because that is the most
    specific: a claim belongs to a member far more than it belongs to a
    provider. Where that reading is wrong it has to be correctable, and the
    correction has to actually re-shape the cascade rather than be recorded
    and ignored.

    The candidate list matters as much as the setting. Only a parent that is
    itself generated can drive -- the cascade is over generated tables and a
    reference set sits outside it -- so a screen offering anything else offers
    a choice the builder will refuse.

    Every setting this touches is put back before it returns.
    """
    from sdg.plan.hierarchy import HierarchyError

    claims = "dbo.CMC_CLCL_CLAIM"
    provider = "dbo.CMC_PRPR_PROV"
    member = "dbo.CMC_MEME_MEMBER"

    plan = run.require_plan()
    generated = {t.name for t in plan.tables if t.mode == "generate"}
    before = plan.table(claims)
    assert before.driving_parent == member

    # Whatever a screen offers has to be something the builder accepts, which
    # is every generated parent and nothing else.
    candidates = {
        r.parent_table for r in (before.relationships or [])
        if r.parent_table in generated and r.parent_table != claims
    }
    assert candidates == {member}, candidates

    settings = run.read_settings()
    modes = dict(settings.get("modes") or {})
    driving = dict(settings.get("drivingParents") or {})
    ratios = dict(settings.get("ratios") or {})
    roots = dict(settings.get("rootCounts") or {})

    try:
        # Generating the provider gives the claim two parents, which is the
        # only state in which there is a choice to make.
        run.write_settings({
            "modes": {**modes, provider: "generate"},
            "rootCounts": {**roots, provider: 200},
        })
        run.build()
        both = run.require_plan().table(claims)
        assert {
            r.parent_table for r in (both.relationships or [])
            if r.parent_table in {
                t.name for t in run.require_plan().tables if t.mode == "generate"
            } and r.parent_table != claims
        } == {member, provider}

        per_member = both.effective_count

        run.write_settings({
            "drivingParents": {**driving, claims: provider}, "ratios": {},
        })
        run.build()
        switched = run.require_plan().table(claims)
        assert switched.driving_parent == provider
        assert switched.effective_count != per_member, (
            "the driving parent changed but the cascade produced the same "
            "number of rows, so the setting was recorded and ignored"
        )

        # A parent that is not a parent is refused, naming what is on offer.
        run.write_settings({"drivingParents": {claims: "dbo.NOPE"}})
        try:
            run.build()
        except Exception as exc:
            assert "driving parent" in str(exc), exc
        else:
            raise AssertionError("an invented driving parent was accepted")
    finally:
        run.write_settings({
            "modes": modes, "drivingParents": driving,
            "ratios": ratios, "rootCounts": roots,
        })
        run.build()
        restored = run.require_plan().table(claims)
        assert restored.driving_parent == before.driving_parent
        assert restored.effective_count == before.effective_count


def test_08c_a_mode_that_breaks_a_foreign_key_is_refused_at_plan_time(run):
    """Choosing a mode for one table is a choice about every table that
    references it.

    Excluding a table does not delete the foreign keys pointing at it. The plan
    used to build clean in that state -- zero blocking warnings, a plan
    declared ready -- and then stop on the first generated row with "no keys
    loaded for ...". Everything up to that point said it would work.

    The condition is visible the moment the plan exists: a reference field
    names its parent table, and that table has to be generated too, or reused
    with a source, for the key to resolve. Anything else is a reference into
    nothing, and it blocks.

    The modes are put back before this returns.
    """
    parent = "dbo.CMC_GRGR_GROUP"       # dbo.CMC_SBSB_SUBSC.GRGR_CK points here
    child = "dbo.CMC_SBSB_SUBSC"

    healthy = [w for w in run.require_plan().warnings if w.severity == "block"]
    assert not healthy, f"the plan was already blocked before this step: {healthy}"

    modes = dict(run.read_settings().get("modes") or {})
    assert modes.get(parent) == "reuse", modes.get(parent)

    try:
        run.write_settings({"modes": {**modes, parent: "exclude"}})
        run.build()
        blocking = [w for w in run.require_plan().warnings if w.severity == "block"]
        codes = {w.code for w in blocking}
        assert "reference_to_excluded_table" in codes, (
            f"excluding {parent} left the plan unblocked, so it would have "
            f"failed on the first generated row instead. Blocking: {codes}"
        )
        offending = next(
            w for w in blocking if w.code == "reference_to_excluded_table"
        )
        assert offending.table == child
        assert offending.field == "GRGR_CK"
        assert parent in offending.message, (
            "the message does not name the table that has to change"
        )
    finally:
        run.write_settings({"modes": modes})
        run.build()
        assert not [w for w in run.require_plan().warnings if w.severity == "block"]


def test_09_dry_run_produces_rows_without_writing(run):
    before = _row_count(run, MEMBERS)
    preview = run.dry_run(rows=3)
    members = next(t for t in preview["tables"] if t["name"] == MEMBERS)
    assert members["rows"], "the preview produced no members"
    assert _row_count(run, MEMBERS) == before, "a dry run wrote to the database"


# ---------------------------------------------------------------------------
# Writing, checking, removing
# ---------------------------------------------------------------------------

def test_10_generate_writes_every_generated_table(run):
    run.e2e_before = {
        name: _row_count(run, name) for name in (MEMBERS, CLAIMS, LINES)
    }
    result = run.generate()
    assert result["rowsWritten"] > 0, result
    for name, before in run.e2e_before.items():
        assert _row_count(run, name) > before, f"{name} gained no rows"


def test_11_validation_passes_on_what_was_written(run):
    result = run.validate()
    failed = [c for c in result["results"] if not c["passed"]]
    assert not failed, failed
    # `passed` is the whole run's verdict, not a count.
    assert result["passed"] is True, result
    assert result["failed"] == 0 and result["checks"] > 0, result


def test_12_the_cohort_landed_at_the_share_it_asked_for(run):
    """The claim the pattern made, checked against what came out."""
    from sdg.engine.execute import run_plan
    from sdg.plan.patterns import measure_cohorts

    plan = run.require_plan()
    result = run_plan(plan, limit=120, reference_sets=run.reference_sets_for_run())
    measured = measure_cohorts(plan, result)
    assert measured, "the plan carried no cohort to measure"
    for row in measured:
        assert row["actual"] is not None, row
        assert abs(row["actual"] - row["asked"]) < 0.12, row


# ---------------------------------------------------------------------------
# What a weight band is measured against
# ---------------------------------------------------------------------------

def test_12a_the_baseline_is_what_the_bands_are_read_against(run):
    """A band is a comparison, and without a baseline there is nothing to
    compare to.

    `weight_band` falls back to fixed shares when no baseline is stored --
    numbers chosen before anyone saw this data. With one, a band is a position
    among the patterns that actually exist. What is asserted here is that the
    walk runs, that what it stores reaches the profilers, and that the band
    thresholds survive the response model, which once silently dropped them.

    The walk is at depth 1 on purpose. Deeper is 340 combinations and ten
    minutes, and what is under test is the handover, not the hierarchy.
    """
    import time

    from fastapi.testclient import TestClient
    from sdg.api.app import app

    client = TestClient(app)

    # What a walk would cost, before committing to one. The screen shows this
    # so nobody starts a three-quarter-hour job by accident.
    estimate = client.get(
        "/api/baseline/estimate", params={"entity": "members", "max_depth": 1},
    )
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["combinations"] > 0
    assert estimate.json()["estimated_seconds"] > 0

    # The walk outlives the request that starts it.
    started = client.post(
        "/api/baseline/jobs", json={"entity": "members", "max_depth": 1},
    )
    assert started.status_code == 200, started.text
    job_id = started.json()["id"]
    assert started.json()["state"] == "running"

    # One at a time: two walks would compete for the same connections.
    assert client.post(
        "/api/baseline/jobs", json={"entity": "members", "max_depth": 1},
    ).status_code == 409

    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        job = client.get(f"/api/baseline/jobs/{job_id}").json()
        if job["state"] != "running":
            break
        time.sleep(1)
    else:
        raise AssertionError("the baseline walk did not finish within 10 minutes")

    assert job["state"] == "done", job.get("error")
    assert job["done"] == job["total"], "the walk reported fewer steps than combinations"
    result = job["result"]
    assert result["combinations_processed"] > 0
    assert result["total_patterns"] > 0
    assert result["thresholds"]["p95"] > 0

    # A job id this process never issued is a restart, and says so.
    unknown = client.get("/api/baseline/jobs/nosuchjob")
    assert unknown.status_code == 404
    assert "restart_lost" in unknown.json()["detail"]

    # Stored, and reaching the profiler that reads it.
    stored = client.get("/api/baseline", params={"entity": "members"}).json()
    assert stored["computed"], stored["note"]

    analysis = client.post(
        "/api/patterns/analyze", json={"filters": {}, "k": 3},
    )
    assert analysis.status_code == 200, analysis.text
    body = analysis.json()
    assert body["band_thresholds"], (
        "the member profiler banded against a baseline but the response model "
        "dropped the thresholds, so the screen cannot say where a band came from"
    )
    assert body["band_thresholds"]["p95"] > 0
    assert "percentiles" in body["weight_explained"], body["weight_explained"]
    assert body["scope_from_baseline"] is True


# ---------------------------------------------------------------------------
# What fills a layout field
# ---------------------------------------------------------------------------

def test_12b_the_rule_repository_describes_the_run(run):
    """A layout field is bound to a rule -- by element, or by a column name
    that is the element's alias -- or it is written blank.

    That binding is the whole point of the repository, and an empty repository
    is not a neutral starting state: every field of every layout falls to
    `default`, and `default` emits nothing. So what is asserted here is the
    difference seeding makes -- a sample whose columns this database generates
    has to come back bound, and a column it does not generate has to come back
    honest about being blank.

    The rules are left behind on purpose. They are configuration, like a saved
    pattern, not rows this run wrote, and the repository describing the plan is
    the state a working installation should be in.
    """
    from fastapi.testclient import TestClient
    from sdg.api.app import app

    client = TestClient(app)

    # Seeding reads the plan, so every column it offers has to be one the plan
    # actually writes -- a rule for a column no run produces is dead weight.
    preview = client.get("/api/rules/seed")
    assert preview.status_code == 200, preview.text
    offered = preview.json()
    planned = {
        field.name.upper()
        for table in run.require_plan().tables
        if table.mode == "generate"
        for field in table.fields
        if field.generator != "skip"
    }
    assert offered["n_columns"] > 0, "the plan offered no columns to seed from"
    assert {c["column"].upper() for c in offered["columns"]} <= planned, (
        "seeding offered a column the plan does not write"
    )

    applied = client.post("/api/rules/seed", json={"created_by": "e2e"})
    assert applied.status_code == 200, applied.text
    assert applied.json()["ok"], applied.json()["failed"]

    # Twice is the same as once. Seeding again after the plan grows a table
    # must not disturb a rule somebody has edited since.
    again = client.post("/api/rules/seed", json={"created_by": "e2e"})
    assert again.status_code == 200, again.text
    assert again.json()["added"] == [], "seeding is not idempotent"
    assert client.get("/api/rules/seed").json()["n_missing"] == 0

    # And now the binding itself, which is what any of this was for.
    sample = (
        b"MEME_ID,MEME_FIRST_NAME,MEME_SEX,COLUMN_THIS_DB_HAS_NO_IDEA_ABOUT\n"
        b"AAA00000001,Dana,F,x\n"
    )
    parsed = client.post(
        "/api/layouts/parse", files={"file": ("e2e-rules.csv", sample, "text/csv")},
    )
    assert parsed.status_code == 200, parsed.text
    fields = {f["name"]: f for f in parsed.json()["fields"]}

    for known in ("MEME_ID", "MEME_FIRST_NAME", "MEME_SEX"):
        assert not fields[known]["is_default"], (
            f"{known} fell to the default with a seeded repository, so the "
            f"layout would write it blank"
        )

    unknown = fields["COLUMN_THIS_DB_HAS_NO_IDEA_ABOUT"]
    assert unknown["is_default"], (
        "a column this database does not generate was bound to a rule anyway"
    )
    assert parsed.json()["n_default"] == 1

    # One element, many names: the alias is how a customer's spelling reaches
    # the same rule.
    rules = client.get("/api/rules").json()["rules"]
    # A rule is named by its element; the columns that mean it are aliases.
    first_name = next(r for r in rules if r["field_name"].upper() == "FIRST_NAME")
    assert "MEME_FIRST_NAME" in (first_name.get("aliases") or []), first_name
    alias = "E2E_GIVEN_NM"
    client.delete(f"/api/rules/aliases/{alias}")  # from an earlier run, if any
    added = client.post(f"/api/rules/{first_name['id']}/aliases",
                        json={"column_name": alias})
    assert added.status_code == 200, added.text
    try:
        aliased = client.post(
            "/api/layouts/parse",
            files={"file": ("e2e-alias.csv",
                            f"{alias}\nDana\n".encode(), "text/csv")},
        )
        assert aliased.status_code == 200, aliased.text
        assert not aliased.json()["fields"][0]["is_default"], (
            "an aliased column name did not reach the rule it names"
        )
    finally:
        client.delete(f"/api/rules/aliases/{alias}")


def test_13b_an_extract_leaves_behind_what_this_tool_generated(run):
    """The landscape is measured from the source, not from our own output.

    Generation writes to the target; the target is usually the source, because
    synthetic rows beside the real ones they were measured from is the normal
    case. The extract then reads the source -- and without this would carry
    those rows into the store, where the next landscape is measured partly
    from data this tool invented and the next pattern describes it as though
    it were found.

    It is not hypothetical: one run put 11,378 generated members into the store
    before this existed.

    The manifest records the exact keys a run wrote, which is what cleanup uses
    to remove precisely those. The same list, read the other way, is what an
    extract skips.
    """
    import pandas as pd

    from sdg.analyzer.sql import read_sql

    from sdg.extract import extract, keys_from_manifest

    def members_in_store():
        with run.registry.connect("denorm") as conn:
            return int(read_sql(
                "SELECT COUNT(*) n FROM sdm.member_denorm", conn,
            )["n"].iloc[0])

    before = members_in_store()
    assert before > 0, "the store is empty, so this proves nothing"

    manifest = run.manifest
    assert manifest is not None, (
        "this step runs after generation; without a manifest there is nothing "
        "to exclude and the check would pass for the wrong reason"
    )

    excluded = keys_from_manifest(manifest)
    assert excluded, (
        "the manifest recorded no keys by column, so an extract cannot tell "
        "which rows a run wrote"
    )

    result = extract("FACETS_QA", registry=run.registry, exclude_keys=excluded)
    assert result.skipped > 0, (
        "nothing was skipped, so either the run wrote nothing or the exclusion "
        "did not match a column the extract carries"
    )
    assert members_in_store() == before, (
        "the store changed size after a run, so generated rows reached the "
        "landscape"
    )


def test_14_cleanup_removes_exactly_what_the_run_wrote(run):
    run.cleanup()
    for name, before in run.e2e_before.items():
        assert _row_count(run, name) == before, (
            f"{name} did not return to the count it started at"
        )


def test_15_nothing_was_left_for_the_fixture_to_trip_over(run):
    """The seed checks still pass, so the next run starts from the same place.

    This also catches a suite that cleaned up its rows but not its patterns:
    the pattern table is small and a test that grows it every run would be
    quietly filling the catalog a person reads.
    """
    from sdg.analyzer import db
    from seed.verify import run_checks

    with run.registry.connect("source") as source, run.registry.connect("ref") as ref:
        checks = run_checks(source.cursor(), ref.cursor())
    failed = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    assert not failed, failed

    leftover = [
        p["contextual_name"] for p in db.get_all_patterns()
        if str(p.get("contextual_name") or "").startswith(("Riverside", "Pine", "Summit"))
        and p.get("saved_at") is None
    ]
    assert not leftover, f"the suite left patterns behind: {leftover}"


# ---------------------------------------------------------------------------

def _forget_patterns(pattern_ids: list[int]) -> None:
    """Delete pattern rows a test created. Nothing else may use this."""
    if not pattern_ids:
        return
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as connection:
        cursor = connection.cursor()
        for pattern_id in pattern_ids:
            cursor.execute(
                f"DELETE FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,)
            )
        connection.commit()


def _row_count(service: Service, table: str) -> int:
    rows = service.registry.query("source", f"SELECT COUNT(*) AS n FROM {table}")
    return int(rows[0]["n"])

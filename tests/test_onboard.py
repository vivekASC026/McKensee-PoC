"""Onboarding a database that was never part of this project.

Every other suite runs against the demo fixture, which this project seeded and
therefore knows. That proves the steps fit together; it does not prove the
claim the tool actually makes, which is that you can point it at a database it
has never seen, let it work out the model, and generate rows into that same
database.

So this one builds a database from scratch -- a different schema, different
names, nothing the catalog was written around -- and runs the whole pipeline
against it:

    create    a small schema with a parent, a child and a reference table
    onboard   connect, discover, profile, map, review
    plan      a cascade the tool derived rather than one it was told
    generate  rows written into that database, not the demo one
    verify    every promise the plan made, checked against what landed
    clean     the database dropped, whatever happened

It is deliberately small. What is under test is that nothing in the pipeline
depends on knowing FACETS, and a hundred rows prove that as well as a million.

The database is created and dropped by this module. Nothing else touches it.
"""

from __future__ import annotations

from dataclasses import replace

import pyodbc
import pytest

from sdg.api.service import Service
from sdg.config import get_settings

pytestmark = [pytest.mark.db, pytest.mark.slow]

ONBOARD_DB = "SDM_ONBOARD_TEST"
PARENT_ROWS = 40

# Nothing here is a FACETS name, and nothing here is in the element catalog by
# accident. A warehouse, the orders placed at it, and a courier they ship by.
SCHEMA = """
CREATE TABLE dbo.wh_courier (
    courier_code   VARCHAR(8)   NOT NULL PRIMARY KEY,
    courier_name   VARCHAR(60)  NOT NULL,
    service_level  VARCHAR(20)  NOT NULL
);

CREATE TABLE dbo.wh_customer (
    customer_id    INT          NOT NULL PRIMARY KEY,
    customer_ref   VARCHAR(16)  NOT NULL,
    given_name     VARCHAR(40)  NOT NULL,
    family_name    VARCHAR(40)  NOT NULL,
    date_of_birth  DATE         NULL,
    postcode       VARCHAR(10)  NULL,
    joined_on      DATE         NOT NULL
);

CREATE TABLE dbo.wh_order (
    order_id       INT          NOT NULL PRIMARY KEY,
    customer_id    INT          NOT NULL,
    courier_code   VARCHAR(8)   NOT NULL,
    placed_on      DATE         NOT NULL,
    order_total    DECIMAL(10,2) NOT NULL,
    order_status   VARCHAR(12)  NOT NULL,
    CONSTRAINT fk_order_customer FOREIGN KEY (customer_id)
        REFERENCES dbo.wh_customer (customer_id),
    CONSTRAINT fk_order_courier FOREIGN KEY (courier_code)
        REFERENCES dbo.wh_courier (courier_code)
);
"""

COURIERS = [
    ("DHL", "Deutsche Post DHL", "express"),
    ("RM", "Royal Mail", "standard"),
    ("UPS", "United Parcel Service", "express"),
    ("EVRI", "Evri", "economy"),
]
STATUSES = ["placed", "picked", "shipped", "delivered", "returned"]


def _connect(database: str, autocommit: bool = True) -> pyodbc.Connection:
    settings = get_settings()
    return pyodbc.connect(
        settings.mssql.connection_string(database), autocommit=autocommit
    )


@pytest.fixture(scope="module")
def onboarded(tmp_path_factory):
    """A database the tool has never seen, and a service pointed at it."""
    import random

    master = _connect("master")
    master.execute(
        f"IF DB_ID('{ONBOARD_DB}') IS NOT NULL "
        f"BEGIN ALTER DATABASE [{ONBOARD_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{ONBOARD_DB}]; END"
    )
    master.execute(f"CREATE DATABASE [{ONBOARD_DB}]")
    master.close()

    rng = random.Random(20261002)
    conn = _connect(ONBOARD_DB, autocommit=False)
    try:
        cur = conn.cursor()
        for statement in SCHEMA.split(";"):
            if statement.strip():
                cur.execute(statement)

        cur.executemany(
            "INSERT INTO dbo.wh_courier VALUES (?, ?, ?)", COURIERS
        )
        customers = [
            (
                i,
                f"CUST{i:06d}",
                rng.choice(["Ada", "Grace", "Alan", "Edsger", "Barbara", "Tony"]),
                rng.choice(["Lovelace", "Hopper", "Turing", "Dijkstra", "Liskov", "Hoare"]),
                f"{rng.randint(1950, 2006)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                f"{rng.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}{rng.randint(1, 99)} {rng.randint(1, 9)}XX",
                f"{rng.randint(2015, 2025)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            )
            for i in range(1, PARENT_ROWS + 1)
        ]
        cur.executemany(
            "INSERT INTO dbo.wh_customer VALUES (?, ?, ?, ?, ?, ?, ?)", customers
        )

        orders, order_id = [], 1
        for customer in customers:
            for _ in range(rng.randint(1, 6)):
                orders.append((
                    order_id,
                    customer[0],
                    rng.choice(COURIERS)[0],
                    f"{rng.randint(2023, 2026)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    round(rng.uniform(5, 900), 2),
                    rng.choice(STATUSES),
                ))
                order_id += 1
        cur.executemany(
            "INSERT INTO dbo.wh_order VALUES (?, ?, ?, ?, ?, ?)", orders
        )
        conn.commit()
    finally:
        conn.close()

    settings = replace(
        get_settings(), source_db=ONBOARD_DB, target_db=ONBOARD_DB
    )
    service = Service(
        work_dir=tmp_path_factory.mktemp("sdg-onboard"), settings=settings
    )

    yield service

    try:
        if service.exists("manifest"):
            service.cleanup()
    finally:
        master = _connect("master")
        master.execute(
            f"IF DB_ID('{ONBOARD_DB}') IS NOT NULL "
            f"BEGIN ALTER DATABASE [{ONBOARD_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
            f"DROP DATABASE [{ONBOARD_DB}]; END"
        )
        master.close()


def test_01_the_tool_reaches_a_database_it_has_never_seen(onboarded):
    result = onboarded.connect()
    assert result, result
    assert onboarded.registry.database_for("source") == ONBOARD_DB
    assert onboarded.registry.database_for("target") == ONBOARD_DB


def test_02_the_model_is_read_rather_than_configured(onboarded):
    """Discovery has to find the tables and the keys between them.

    Nothing told it these three tables exist or how they relate. If the FK
    graph came back empty the cascade below would be three unrelated tables.
    """
    onboarded.discover()
    model = onboarded.require_model()
    tables = {t.name for t in model.tables}
    assert tables == {
        "dbo.wh_courier", "dbo.wh_customer", "dbo.wh_order",
    }, tables

    order = model.table("dbo.wh_order")
    parents = {fk.parent_table for fk in order.foreign_keys}
    assert parents == {"dbo.wh_customer", "dbo.wh_courier"}, parents


def test_03_every_column_is_measured(onboarded):
    report = onboarded.profile()
    assert report["columns"] >= 16, report
    # A format fitted on at least one column means the profiler did more than
    # count rows against a schema it had never seen.
    assert report["formatsFitted"] > 0, report
    assert report["ratios"] > 0, (
        "no children-per-parent ratio was measured, so the cascade below would "
        "have nothing to size itself from"
    )


def test_04_columns_are_matched_without_being_told(onboarded):
    """The catalog was written for healthcare. Some of it still fits.

    A date of birth is a date of birth whatever the table is called, and this
    is where that claim is tested rather than assumed. What matters is not how
    many match -- a warehouse is not a health plan -- but that matching runs
    against a schema nobody wrote it for and produces something usable.
    """
    onboarded.map()
    mappings = onboarded.require_mappings()
    matched = {m.qualified: m.element for m in mappings.mappings if m.element}
    assert matched, "no column in an unseen schema matched any element"


def test_05_a_cascade_is_derived_from_the_keys(onboarded):
    onboarded.review()

    # Two decisions a person makes, not the tool: the courier list is reference
    # data to be reused rather than invented, and the customer book needs a
    # size. Everything else below is derived.
    onboarded.write_settings({
        "modes": {"dbo.wh_courier": "reuse"},
        "rootCounts": {"dbo.wh_customer": 120},
        "seed": "onboard-suite",
        "planName": "onboard",
    })

    plan, _graph, _extra = onboarded.build()

    # The tool read order_total as a derived column and refused to generate it
    # independently of whatever it should follow. It is right to refuse and
    # right to ask: there are no order lines here for it to be the sum of. A
    # person settles that in Review, which is what this is.
    blocking = [w for w in plan.warnings if w.severity == "block"]
    assert [w.code for w in blocking] == ["derivable_without_expression"], [
        w.message for w in blocking
    ]
    onboarded.record_decision({
        "table": "dbo.wh_order",
        "column": "order_total",
        "approved": True,
        "generator": "number",
        "params": {"distribution": "uniform", "min": 5.0, "max": 900.0,
                   "decimals": 2},
    })
    plan, _graph, _extra = onboarded.build()

    assert not [w for w in plan.warnings if w.severity == "block"], [
        w.message for w in plan.warnings if w.severity == "block"
    ]

    generated = {t.name: t for t in plan.tables if t.mode == "generate"}
    assert "dbo.wh_customer" in generated, sorted(generated)
    assert "dbo.wh_order" in generated, sorted(generated)

    # The order belongs to the customer, and the tool worked that out from the
    # foreign keys rather than from anything written here.
    assert generated["dbo.wh_order"].driving_parent == "dbo.wh_customer"
    assert generated["dbo.wh_customer"].effective_count == 120
    assert generated["dbo.wh_order"].effective_count > 0


def test_05b_sampling_gives_every_field_a_value_rule(onboarded):
    """Onboarding a target reads its data, and what it reads becomes the rules.

    This is the half a pattern does not do. A pattern names a few elements; the
    other columns -- most of them, in most databases -- get their values from
    what sampling found in this target: the shape a string takes, the range a
    number covers, the set a code draws from, the order dates fall in.

    So every column of every generated table must come out with a rule. One
    that does not is a column that will be written blank or not at all, and the
    row it lands in is wrong in a way the row count will not show.
    """
    plan = onboarded.require_plan()

    unruled: list[str] = []
    for table in plan.tables:
        if table.mode != "generate":
            continue
        for field in table.fields:
            if field.generator == "skip" and field.bucket != "system":
                unruled.append(f"{table.name}.{field.name}")
    assert not unruled, f"columns that would write nothing: {unruled}"

    customer = plan.table("dbo.wh_customer")
    order = plan.table("dbo.wh_order")

    # A string whose shape is consistent becomes that shape, learned here
    # rather than assumed from the type.
    reference = customer.field("customer_ref")
    assert reference.generator == "pattern", reference.generator
    assert reference.params.get("regex"), reference.params
    assert reference.provenance == "learned", (
        "the reference format came from somewhere other than this target's data"
    )

    # A code column becomes the values it actually holds.
    status = order.field("order_status")
    assert status.generator == "lookup", status.generator
    assert set(status.params["values"]) <= {
        "placed", "picked", "shipped", "delivered", "returned",
    }, status.params["values"]

    # A number becomes the range it was measured over, not the range its type
    # allows.
    total = order.field("order_total")
    assert total.generator in ("number", "expression"), total.generator
    if total.generator == "number":
        assert total.params["max"] <= 1000, total.params

    # A key continues the keys that are there, rather than colliding with them.
    key = customer.field("customer_id")
    assert key.generator == "sequence"
    assert key.params["start"] > PARENT_ROWS, (
        f"a new key would collide with the {PARENT_ROWS} rows already here"
    )


def test_06_rows_are_written_into_that_database(onboarded):
    """The point of the whole file: generated rows land where it was pointed."""
    before = _row_counts()
    result = onboarded.generate()
    assert result, result

    after = _row_counts()
    for table in ("dbo.wh_customer", "dbo.wh_order"):
        assert after[table] > before[table], (
            f"{table} did not grow, so nothing was written to {ONBOARD_DB}"
        )

    # And nothing went to the demo database by mistake.
    settings = get_settings()
    assert onboarded.registry.database_for("target") == ONBOARD_DB
    assert ONBOARD_DB != settings.source_db


def test_06b_a_pattern_from_the_store_reaches_this_target(onboarded):
    """The whole loop, in one step.

    A pattern is found in the central store, which was scooped out of some
    source database. It describes the landscape: people of a certain age, in a
    certain proportion. Nothing about it mentions this target -- the store
    holds FACETS columns and this target is a warehouse, sharing not one column
    name with it.

    It reaches the target anyway, because the resolver binds patterns to
    *elements* rather than to columns. The store's member birth date and this
    target's `date_of_birth` are both DOB, so a statement about age lands on
    the column that holds one here. That indirection is the only reason a
    pattern found in one landscape can generate into another.

    This is the step that makes the target half worth onboarding. Without it,
    onboarding gets you a plan the tool derived; with it, you get a plan the
    landscape dictated.
    """
    from sdg.analyzer.bridge import AnalyzerPattern, PatternParameter
    from sdg.plan.patterns import apply_to_decisions, resolve_pattern

    mappings = onboarded.require_mappings()
    by_element = {m.element: m for m in mappings.mappings if m.element}
    assert "DOB" in by_element, sorted(by_element)
    assert by_element["DOB"].qualified == "dbo.wh_customer.date_of_birth"

    # A pattern of the shape the analyzer produces from the store.
    pattern = AnalyzerPattern(
        id="landscape-cohort",
        kind="member",
        label="Working age",
        subject="dbo.wh_customer",
        parameters=[PatternParameter(label="Age", min=30, max=45, unit="years")],
    )

    resolution = resolve_pattern(
        pattern,
        catalog=onboarded.catalog,
        mappings=mappings,
        plan=onboarded.require_plan(),
        model=onboarded.model,
        as_of="2026-06-30",
    )
    assert resolution.targets, (
        f"a pattern from the store reached no column of this target: "
        f"{[(r.parameter, r.reason) for r in resolution.rejections]}"
    )
    target = resolution.targets[0]
    assert target.qualified == "dbo.wh_customer.date_of_birth", target.qualified
    assert target.generator == "date_relative", target.generator

    # The previous step's rows go first: this generates again, and the keys it
    # writes are the same ones. Leaving a run in place to write over it is how
    # a primary key violation looks from the outside.
    onboarded.cleanup()

    document, written = apply_to_decisions(
        [resolution], onboarded.read_decisions(), as_cohort=False,
    )
    onboarded.write_decisions(document)
    assert written == ["dbo.wh_customer.date_of_birth"], written
    onboarded.build()
    onboarded.generate()

    if True:
        # And the rows this target now holds answer the pattern.
        import datetime

        as_of = datetime.date(2026, 6, 30)
        conn = _connect(ONBOARD_DB)
        try:
            born = [
                row[0] for row in conn.cursor().execute(
                    "SELECT date_of_birth FROM dbo.wh_customer "
                    "WHERE customer_id > ?", PARENT_ROWS
                ).fetchall()
                if row[0] is not None
            ]
        finally:
            conn.close()

        assert born, "no generated customers to check"
        ages = [(as_of - d).days / 365.25 for d in born]
        assert min(ages) >= 29.5 and max(ages) <= 46.5, (
            f"the pattern asked for 30 to 45 and the target holds "
            f"{min(ages):.1f} to {max(ages):.1f}"
        )

    # The run is left in place: the steps after this validate it and remove it,
    # so what they check is a run a pattern shaped rather than a plain one.


def test_06c_a_second_run_does_not_collide_with_the_first(onboarded):
    """Where a key starts is read from the target, not remembered.

    A sequence proposal records the highest value a column held when it was
    profiled and starts after it. That is right once and wrong every time
    after: the rows the last run wrote are in the table, the plan does not know,
    and the next run begins at the same number and violates the primary key.

    The gap is not drift over weeks. It is the run that just finished.

    What this asserts is three runs into one target: each starting past what is
    there, none colliding, and no orphan left behind -- a child pointing at a
    parent key that was never written would be the way this fix could go wrong.
    """
    import pyodbc

    settings = get_settings()

    def state():
        conn = pyodbc.connect(settings.mssql.connection_string(ONBOARD_DB))
        try:
            cursor = conn.cursor()
            customers = cursor.execute(
                "SELECT COUNT(*), MAX(customer_id) FROM dbo.wh_customer"
            ).fetchone()
            orphans = cursor.execute(
                "SELECT COUNT(*) FROM dbo.wh_order o "
                "LEFT JOIN dbo.wh_customer c ON c.customer_id = o.customer_id "
                "WHERE c.customer_id IS NULL"
            ).fetchone()[0]
            return int(customers[0]), int(customers[1]), int(orphans)
        finally:
            conn.close()

    rows_before, highest_before, orphans = state()
    assert orphans == 0, "the target already held orphans"

    # The run before this one is still in place; generating again is the case
    # that used to fail.
    result = onboarded.generate()

    advanced = {a["table"] + "." + a["column"]: a for a in result["keysAdvanced"]}
    assert "dbo.wh_customer.customer_id" in advanced, (
        f"the key did not advance past the rows already written: "
        f"{result['keysAdvanced']}"
    )
    moved = advanced["dbo.wh_customer.customer_id"]
    assert moved["started"] > highest_before, (
        f"started at {moved['started']} with {highest_before} already there"
    )
    assert moved["started"] > moved["planned"], (
        "the plan's own start was used, which is the collision this prevents"
    )

    rows_after, highest_after, orphans_after = state()
    assert rows_after > rows_before
    assert highest_after > highest_before
    assert orphans_after == 0, (
        f"{orphans_after} orders point at a customer that was never written"
    )


def test_07_what_was_written_holds_what_the_plan_promised(onboarded):
    report = onboarded.validate()
    failures = [c for c in report["results"] if not c.get("passed", True)]
    assert not failures, failures
    # Referential integrity in particular: the orders written have to point at
    # customers and couriers that exist in this database.
    kinds = {c["kind"] for c in report["results"]}
    assert "referential_integrity" in kinds, sorted(kinds)


def test_08_cleanup_removes_exactly_what_the_run_wrote(onboarded):
    before = _row_counts()
    onboarded.cleanup()
    after = _row_counts()
    for table in ("dbo.wh_customer", "dbo.wh_order"):
        assert after[table] < before[table], f"{table} kept its generated rows"
    # The rows that were there before the run are still there.
    assert after["dbo.wh_customer"] == PARENT_ROWS, after
    assert after["dbo.wh_courier"] == len(COURIERS), after


def _row_counts() -> dict[str, int]:
    conn = _connect(ONBOARD_DB)
    try:
        cur = conn.cursor()
        return {
            table: cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("dbo.wh_courier", "dbo.wh_customer", "dbo.wh_order")
        }
    finally:
        conn.close()


def test_a_target_is_chosen_through_the_api_and_checked_first(tmp_path):
    """Pointing the tool at another database, without editing a file.

    Everything underneath already worked; what was missing was any way to say
    which database without `.env` and a restart. So this is the seam: the
    databases are listed rather than typed, the chosen one is reached before it
    is saved, and the choice outlives the process that made it.
    """
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from sdg.api.app import app
    from sdg.api.service import Service

    client = TestClient(app)

    listed = client.get("/api/databases")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    names = {row["name"] for row in body["databases"]}

    # The source has to be in the list and marked, or the screen cannot show a
    # person which database they are about to write into.
    assert body["source"] in names, names
    assert any(row["isSource"] for row in body["databases"])
    assert all("online" in row for row in body["databases"])

    # The analysis store is not offered at all. It holds the extracts, the
    # saved patterns and the baselines this tool reads; generating into it
    # would put synthetic rows beside the measurements and the next analysis
    # would describe them as though they were measured. Leaving it out beats
    # showing it with a warning.
    assert body["store"] not in names, (
        f"the analysis store {body['store']} was offered as somewhere to "
        f"generate into"
    )

    original = body["target"]
    try:
        # A database nobody can reach is a typo, and finding out at generate
        # time means finding out after a plan was built against it.
        refused = client.post(
            "/api/target", json={"database": "NO_SUCH_DATABASE_ANYWHERE"},
        )
        assert refused.status_code >= 400, refused.text
        assert client.get("/api/databases").json()["target"] == original, (
            "a target that could not be reached was kept anyway"
        )

        # A real one is accepted and reported back. Any offered database that
        # is not the one already in use.
        elsewhere = next(
            row["name"] for row in body["databases"]
            if row["name"] != original and row["online"]
        )
        chosen = client.post("/api/target", json={"database": elsewhere})
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["target"] == elsewhere
        assert client.get("/api/databases").json()["target"] == elsewhere

        # And it outlives the process: a service built fresh reads it back.
        from sdg.config import get_settings

        fresh = Service(settings=replace(get_settings()))
        assert fresh.registry.database_for("target") == elsewhere, (
            "the chosen target did not survive; it would revert on restart"
        )
    finally:
        client.post("/api/target", json={"database": original})
        assert client.get("/api/databases").json()["target"] == original

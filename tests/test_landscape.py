"""The landscape: measurements a target can use, from a store a target never sees.

A profile is keyed by table and column, which makes it a fact about one
database and useless about any other. A landscape is the same measurements
keyed by element, which makes them sayable about any target carrying the same
elements -- including one that holds no rows at all, which is the ordinary case
for a database you are about to populate.

What these assert is that the two halves of the claim hold: the store can be
described per element, and that description reaches a target's columns without
anything reading the target's data.
"""

from __future__ import annotations

from dataclasses import replace

import pyodbc
import pytest

from sdg.api.service import Service
from sdg.config import get_settings
from sdg.landscape import derive
from sdg.landscape.project import PROJECTED, project
from sdg.mapping.scorer import map_columns

pytestmark = [pytest.mark.db, pytest.mark.slow]

EMPTY_DB = "SDM_LANDSCAPE_TARGET_TEST"

# No column name here appears in the store. That is the point: if any
# measurement reaches these columns it got there through an element.
SCHEMA = """
CREATE TABLE dbo.party (
    party_id      INT          NOT NULL PRIMARY KEY,
    forename      VARCHAR(40)  NULL,
    surname       VARCHAR(40)  NULL,
    born_on       DATE         NULL,
    post_code     VARCHAR(10)  NULL
);

CREATE TABLE dbo.engagement (
    engagement_id INT          NOT NULL PRIMARY KEY,
    party_id      INT          NOT NULL,
    state_code    VARCHAR(4)   NULL,
    CONSTRAINT fk_engagement_party FOREIGN KEY (party_id)
        REFERENCES dbo.party (party_id)
);
"""


@pytest.fixture(scope="module")
def landscape():
    """What the store says, derived once."""
    service = Service()
    return derive(
        source="FACETS_QA", catalog=service.catalog, registry=service.registry
    )


@pytest.fixture(scope="module")
def empty_target(tmp_path_factory):
    """A database with a schema and no rows at all."""
    settings = get_settings()

    def master():
        return pyodbc.connect(
            settings.mssql.connection_string("master"), autocommit=True
        )

    drop = (
        f"IF DB_ID('{EMPTY_DB}') IS NOT NULL BEGIN "
        f"ALTER DATABASE [{EMPTY_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{EMPTY_DB}]; END"
    )
    conn = master()
    conn.execute(drop)
    conn.execute(f"CREATE DATABASE [{EMPTY_DB}]")
    conn.close()

    conn = pyodbc.connect(settings.mssql.connection_string(EMPTY_DB), autocommit=True)
    try:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.cursor().execute(statement)
    finally:
        conn.close()

    service = Service(
        work_dir=tmp_path_factory.mktemp("sdg-landscape"),
        settings=replace(settings, source_db=EMPTY_DB, target_db=EMPTY_DB),
    )
    yield service

    conn = master()
    conn.execute(drop)
    conn.close()


def test_the_store_is_described_per_element(landscape):
    assert landscape.elements, "the store described nothing"
    assert landscape.source == "FACETS_QA"

    dob = landscape.element("DOB")
    assert dob is not None, [e.element for e in landscape.elements][:20]
    # A birth date is personal: the earliest and latest are real people's, so
    # the profile withholds them and generation follows the catalog's rule.
    assert dob.minimum is None and dob.maximum is None
    assert dob.observed_in, "a shape with no provenance is a guess"

    # A low-cardinality element is a vocabulary and carries its values; a name
    # is data and must not.
    coded = [e for e in landscape.elements if e.is_coded]
    assert coded, "no element came back as a value set"
    for element in coded:
        assert sum(v["weight"] for v in element.values) == pytest.approx(1.0, abs=0.01)

    first_name = landscape.element("FIRST_NAME")
    if first_name is not None:
        assert not first_name.is_coded, (
            "the landscape carried a list of real first names, which is data "
            "rather than vocabulary"
        )


def test_a_relationship_is_measured_between_elements(landscape):
    """Extracts are flat and declare no keys, so this has to be measured."""
    assert landscape.relationships, (
        "no relationship was measured, so a target's cascade has nothing to "
        "size itself from"
    )
    relationship = landscape.relationships[0]
    assert relationship.parents > 0 and relationship.children > 0
    assert relationship.mean > 0
    assert relationship.minimum <= relationship.median <= relationship.maximum


def test_the_landscape_reaches_an_empty_target(landscape, empty_target):
    """Nothing reads the target. Everything it is told comes from the store."""
    empty_target.connect()
    empty_target.discover()
    model = empty_target.require_model()
    assert {t.name for t in model.tables} == {"dbo.party", "dbo.engagement"}

    # Mapped with no profile at all, because there is no data to profile.
    mappings = map_columns(model, empty_target.catalog, None)
    matched = {
        f"{m.table}.{m.column}": m.element for m in mappings.mappings if m.element
    }
    assert matched, "no column of an empty database matched any element"

    report = project(landscape, model, mappings)
    assert report.profiled == PROJECTED, (
        "a projected measurement must say so: a measurement of this column and "
        "a measurement of something else carrying the same element are not the "
        "same claim"
    )
    assert report.columns, "the landscape said nothing about this target"

    # Every projected column has to correspond to a column that matched, and
    # carry what the landscape knows rather than anything read from here.
    for column in report.columns:
        qualified = f"{column.table}.{column.column}"
        assert qualified in matched, qualified
        known = landscape.element(matched[qualified])
        assert known is not None
        assert column.minimum == known.minimum
        assert column.sample_size == 0, "a projection sampled the target"

    born = next((c for c in report.columns if c.column == "born_on"), None)
    if born is not None:
        assert born.minimum is not None and born.maximum is not None, (
            "the target's date of birth was told nothing about what one looks "
            "like, so the plan would invent a range"
        )


def test_a_stated_subject_mapping_gives_the_target_its_ratio(landscape, empty_target):
    """Matching columns gets you values. It does not get you a cascade.

    The relationship between two kinds of thing is carried by their keys, and a
    key column only matches an element if the catalog has heard of it. A
    healthcare catalog has nothing to say about a column called `party_id`, so
    the shape of "how many engagements per party" cannot be recovered from the
    column names however hard anything looks.

    A person knows. Saying "my engagements are like their claim lines and my
    parties like their members" is one decision per table, stated the way a
    root count is, and it is the difference between a target whose values come
    from the landscape and a target whose whole shape does.
    """
    model = empty_target.require_model()
    mappings = map_columns(model, empty_target.catalog, None)

    # Without it, nothing: the keys carry no element either side.
    unstated = project(landscape, model, mappings)
    assert not unstated.ratios, (
        "a ratio was found without a stated mapping, so this test is no longer "
        "testing what it says"
    )

    measured = landscape.relationships[0]
    stated = project(landscape, model, mappings, subjects={
        "dbo.engagement": measured.child_table,
        "dbo.party": measured.parent_table,
    })

    assert len(stated.ratios) == 1, stated.ratios
    ratio = stated.ratios[0]
    assert ratio.child_table == "dbo.engagement"
    assert ratio.parent_table == "dbo.party"
    assert ratio.mean == measured.mean, (
        "the target was given a different ratio from the one the landscape "
        "measured"
    )
    assert ratio.maximum == measured.maximum


def test_an_empty_target_is_generated_from_its_model_and_the_landscape(
    landscape, empty_target
):
    """The division of labor, end to end.

    A pattern or a catalog supplies a few elements. The landscape supplies the
    cascade. Everything else -- most columns, in most databases -- is generated
    from what the model says the column is: a key is a counter, a date is a
    date in a stated window, text is text of the declared width. Nothing here
    reads a row of the target, because there are none.

    Before this, a column with nothing to measure was proposed as `skip`, which
    is correct about the evidence and useless about the job: an empty target
    generated nothing at all.
    """
    import pyodbc

    settings = get_settings()
    measured = landscape.relationships[0]

    from sdg.landscape import save

    # The earlier steps mapped in memory; this run needs them on disk.
    empty_target.connect()
    empty_target.discover()
    empty_target.map()

    save(landscape, empty_target.work / "landscape-FACETS_QA.json")
    empty_target.write_settings({
        "rootCounts": {"dbo.party": 100},
        "subjects": {
            "dbo.engagement": measured.child_table,
            "dbo.party": measured.parent_table,
        },
        "seed": "landscape-suite",
        "planName": "from-landscape",
    })
    empty_target.review()
    plan, _graph, _extra = empty_target.build()

    assert not [w for w in plan.warnings if w.severity == "block"], [
        w.message for w in plan.warnings if w.severity == "block"
    ]

    # Every column of a generated table has a rule. None may be left to chance
    # because nobody measured it.
    for table in plan.tables:
        if table.mode != "generate":
            continue
        unruled = [
            f.name for f in table.fields
            if f.generator == "skip" and f.bucket != "system"
        ]
        assert not unruled, f"{table.name}: {unruled} would write nothing"

    party = plan.table("dbo.party")
    assert party.field("party_id").generator == "sequence"
    assert party.field("born_on").generator == "date_relative"
    # Never the clock: a plan built twice has to be the same plan.
    assert party.field("born_on").params["anchor"] != "today"

    # The cascade is the landscape's, not a guess.
    engagement = plan.table("dbo.engagement")
    assert engagement.ratio is not None
    # Within a thousandth: counts too many for one bin each are kept as ranges,
    # whose midpoints round the mean a little.
    assert engagement.ratio.mean_children() == pytest.approx(measured.mean, rel=1e-3)

    empty_target.generate()
    try:
        conn = pyodbc.connect(settings.mssql.connection_string(EMPTY_DB))
        try:
            counts = {
                name: conn.cursor().execute(
                    f"SELECT COUNT(*) FROM {name}"
                ).fetchone()[0]
                for name in ("dbo.party", "dbo.engagement")
            }
        finally:
            conn.close()

        assert counts["dbo.party"] == 100
        assert counts["dbo.engagement"] > 0
        # A wide band on purpose. The engine draws per parent from the
        # landscape's histogram, which runs out to 148, so a hundred parents
        # give a realized mean that swings either side of the measured one.
        # Tightening this would be asserting a particular seed rather than that
        # the cascade took its shape from the landscape at all -- which is what
        # a mean of 1, or of 148, would disprove.
        per_parent = counts["dbo.engagement"] / counts["dbo.party"]
        assert per_parent == pytest.approx(measured.mean, rel=0.5), (
            f"the landscape measured {measured.mean:.2f} per parent and the "
            f"target got {per_parent:.2f}"
        )
        assert measured.minimum <= per_parent <= measured.maximum

        report = empty_target.validate()
        failures = [c for c in report["results"] if not c.get("passed", True)]
        assert not failures, failures
    finally:
        empty_target.cleanup()

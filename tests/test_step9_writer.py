"""Step 9 acceptance: write to SQL Server, validate what was written, clean up.

One run is written, checked, and removed. Everything the tests assert on is
captured during that single cycle, so the seeded fixture is the same before and
after, and no two writes are ever in flight at once.
"""

from __future__ import annotations

import pytest

from sdg.catalog.loader import load_catalog
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.run import discover
from sdg.engine.execute import ReferenceSets, run_plan
from sdg.mapping.scorer import map_columns
from sdg.plan.build import BuildInputs, build_plan, default_reference_sets
from sdg.plan.detect import detect_sum_expressions
from sdg.plan.proposer import propose
from sdg.plan.refsets import load_for_plan
from sdg.profiling.run import profile
from sdg.writers.csvfile import write_run
from sdg.writers.manifest import build_manifest, load_manifest, save_manifest, summarize
from sdg.writers.sqlserver import (
    cleanup,
    key_predicate,
    manifest_key_columns,
    needs_identity_insert,
    trigger_side_effects,
    write,
)
from sdg.writers.validate import validate_run

ROOT_COUNT = 300
TEST_SEED = "step9-test-seed"

TABLES = [
    "dbo.CMC_SBSB_SUBSC", "dbo.CMC_MEME_MEMBER",
    "dbo.CMC_CLCL_CLAIM", "dbo.CMC_CDML_CL_LINE",
]


def _row_counts(registry: ConnectionRegistry) -> dict[str, int]:
    return {
        name: int(
            registry.scalar("source", f"SELECT COUNT_BIG(*) FROM {name}")  # noqa: S608
        )
        for name in TABLES
    }


@pytest.fixture(scope="module")
def prepared():
    """A plan for a small run, plus its reference sets."""
    registry = ConnectionRegistry()
    model = discover(registry, "source").model
    report = profile(registry, model)
    catalog = load_catalog()
    mappings = map_columns(model, catalog, report)
    sums = detect_sum_expressions(registry, model)
    proposals = propose(model, report, sums, mappings, catalog)

    inputs = BuildInputs(
        model=model, catalog=catalog, mappings=mappings, proposals=proposals,
        report=report,
        modes={
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
        },
        root_counts={"dbo.CMC_SBSB_SUBSC": ROOT_COUNT},
        seed=TEST_SEED,
        name="step9-test",
    )
    inputs.reference_sets = default_reference_sets(registry, inputs)
    plan, _graph = build_plan(inputs)

    sets = ReferenceSets()
    bindings = {
        t.name: {
            "connection": t.source.connection, "table": t.source.table,
            "schema": t.source.schema_name, "key_column": t.source.key_columns[0],
            "filter": t.source.filter, "load": t.source.load or "full",
        }
        for t in plan.tables if t.mode == "reuse" and t.source
    }
    for name, reference in load_for_plan(registry, bindings, seed=TEST_SEED).items():
        sets.add(name, reference.keys, reference.attributes)

    return {"plan": plan, "model": model, "sets": sets, "registry": registry}


@pytest.fixture(scope="module")
def written(prepared):
    """One write, validate, and cleanup cycle; every test reads what it captured.

    Exactly one run is written. Two runs of the same plan generate the same
    identity keys, because the starting point comes from the profile rather
    than from whatever is in the table at that moment, so a second overlapping
    write collides on the primary key by design.
    """
    registry = prepared["registry"]
    plan, model, sets = prepared["plan"], prepared["model"], prepared["sets"]

    before = _row_counts(registry)
    result = run_plan(plan, reference_sets=sets)
    report = write(registry, plan, result, model, connection="target")
    manifest = build_manifest(
        plan, report, connection="target", database=registry.database_for("target")
    )
    during = _row_counts(registry)

    try:
        validation = validate_run(
            registry, plan, model, manifest,
            connection="target", reference_sets=sets.keys,
        )
        # The same run with the keys removed, to prove a check that cannot be
        # scoped reports that rather than passing on someone else's rows.
        unscoped = validate_run(
            registry, plan, model, {**manifest, "keys": {}}, connection="target"
        )
        keys_during = {
            int(r["SBSB_CK"])
            for r in registry.query("source", "SELECT SBSB_CK FROM dbo.CMC_SBSB_SUBSC")
        }
    finally:
        deleted = cleanup(registry, manifest, connection="target")

    return {
        "result": result, "report": report, "manifest": manifest,
        "before": before, "during": during, "after": _row_counts(registry),
        "deleted": deleted, "validation": validation, "unscoped": unscoped,
        "keys_during": keys_during,
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_key_predicate_handles_a_single_column():
    where, values = key_predicate(["ID"], [(1,), (2,), (3,)])
    assert where == "[ID] IN (?, ?, ?)"
    assert values == [1, 2, 3]


def test_key_predicate_handles_a_composite_key():
    where, values = key_predicate(["A", "B"], [("x", 1), ("y", 2)])
    assert where == "(([A] = ? AND [B] = ?) OR ([A] = ? AND [B] = ?))"
    assert values == ["x", 1, "y", 2]


def test_manifest_key_columns_accepts_either_shape():
    assert manifest_key_columns({"key_columns": ["A", "B"]}) == ["A", "B"]
    assert manifest_key_columns({"key_column": "A"}) == ["A"]
    assert manifest_key_columns({}) == []


@pytest.mark.db
def test_identity_insert_is_needed_exactly_where_a_key_is_generated(prepared):
    plan, model = prepared["plan"], prepared["model"]
    assert needs_identity_insert(plan.table("dbo.CMC_SBSB_SUBSC"), model)
    assert needs_identity_insert(plan.table("dbo.CMC_MEME_MEMBER"), model)
    # No identity column, so nothing to turn on.
    assert not needs_identity_insert(plan.table("dbo.CMC_CLCL_CLAIM"), model)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_every_generated_table_is_written(written):
    by_table = written["report"].by_table()
    assert set(by_table) == set(TABLES)
    for name in TABLES:
        assert by_table[name].rows_written > 0, name
    assert written["report"].total_rows() == sum(written["result"].counts().values())


@pytest.mark.db
def test_the_rows_land_in_the_database(written):
    for name, record in written["report"].by_table().items():
        assert written["during"][name] == written["before"][name] + record.rows_written


@pytest.mark.db
def test_identity_keys_continue_after_the_rows_already_there(written):
    """Generated keys must not collide with what the table already holds."""
    subscribers = written["result"].rows("dbo.CMC_SBSB_SUBSC")
    assert min(row["SBSB_CK"] for row in subscribers) > written["before"][
        "dbo.CMC_SBSB_SUBSC"
    ]


@pytest.mark.db
def test_the_manifest_records_the_whole_primary_key(written):
    manifest = written["manifest"]
    keys = {t["table"]: manifest_key_columns(t) for t in manifest["tables"]}
    assert keys["dbo.CMC_CLCL_CLAIM"] == ["CLCL_ID"]
    assert keys["dbo.CMC_CDML_CL_LINE"] == ["CLCL_ID", "CDML_SEQ_NO"]
    for name in TABLES:
        assert len(manifest["keys"][name]) == next(
            t["rows_written"] for t in manifest["tables"] if t["table"] == name
        )


@pytest.mark.db
def test_the_manifest_names_the_seed_and_reports_the_run(written):
    manifest = written["manifest"]
    assert manifest["plan"]["seed"] == TEST_SEED
    assert manifest["run"]["rows_written"] == written["report"].total_rows()
    assert "total" in summarize(manifest)


@pytest.mark.db
def test_the_manifest_reports_triggers_that_fired(written):
    """Cleanup will not undo what a trigger wrote, so the run has to say so."""
    fired = trigger_side_effects(written["manifest"])
    assert any("TR_CMC_CLCL_CLAIM_AUDIT" in entry for entry in fired)


@pytest.mark.db
def test_the_manifest_round_trips(written, tmp_path):
    path = save_manifest(written["manifest"], tmp_path / "run-manifest.json")
    reloaded = load_manifest(path)
    assert reloaded["plan"]["seed"] == TEST_SEED
    assert reloaded["keys"].keys() == written["manifest"]["keys"].keys()


# ---------------------------------------------------------------------------
# Post-run validation
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_validation_passes_on_the_written_run(written):
    report = written["validation"]
    assert report.passed, [f"{c.name}: {c.detail}" for c in report.failures()]
    assert report.summary()["checks"] > 40


@pytest.mark.db
def test_validation_covers_every_kind_of_check(written):
    kinds = set(written["validation"].summary()["by_kind"])
    assert {
        "row_count", "referential_integrity", "uniqueness", "domain",
        "nullability", "null_rate", "reference_set", "expression",
    } <= kinds


@pytest.mark.db
def test_validation_checks_the_planted_expression(written):
    expression = next(c for c in written["validation"].checks if c.kind == "expression")
    assert "sum(dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT)" in expression.name
    assert expression.passed


@pytest.mark.db
def test_validation_says_so_when_it_cannot_scope_a_table(written):
    """A check that silently covered other people's rows would be worthless."""
    report = written["unscoped"]
    assert not report.passed
    assert all(c.kind == "scope" for c in report.failures())
    assert "cannot be told apart" in report.failures()[0].detail


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_the_same_seed_writes_byte_identical_csv(prepared, tmp_path):
    first = write_run(
        run_plan(prepared["plan"], reference_sets=prepared["sets"]),
        prepared["plan"], tmp_path / "a",
    )
    second = write_run(
        run_plan(prepared["plan"], reference_sets=prepared["sets"]),
        prepared["plan"], tmp_path / "b",
    )
    assert set(first) == set(second)
    for name in first:
        assert first[name].read_bytes() == second[name].read_bytes(), name


@pytest.mark.db
def test_the_same_seed_produces_the_same_row_keys(prepared):
    first = run_plan(prepared["plan"], reference_sets=prepared["sets"])
    second = run_plan(prepared["plan"], reference_sets=prepared["sets"])

    def keys(result, name, column):
        return [row[column] for row in result.rows(name)]

    for name, column in (
        ("dbo.CMC_SBSB_SUBSC", "SBSB_CK"),
        ("dbo.CMC_MEME_MEMBER", "MEME_CK"),
        ("dbo.CMC_CLCL_CLAIM", "CLCL_ID"),
    ):
        assert keys(first, name, column) == keys(second, name, column), name


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_cleanup_removes_exactly_what_the_run_wrote(written):
    assert all(written["during"][name] > written["before"][name] for name in TABLES)
    assert written["after"] == written["before"], "cleanup did not restore the tables"
    for name in TABLES:
        assert written["deleted"][name] == written["report"].by_table()[name].rows_written


@pytest.mark.db
def test_cleanup_leaves_the_seeded_rows_alone(written, prepared):
    """The rows that were there before a run are never the run's to delete."""
    keys_after = {
        int(r["SBSB_CK"])
        for r in prepared["registry"].query(
            "source", "SELECT SBSB_CK FROM dbo.CMC_SBSB_SUBSC"
        )
    }
    generated = {row["SBSB_CK"] for row in written["result"].rows("dbo.CMC_SBSB_SUBSC")}
    assert keys_after == written["keys_during"] - generated
    assert len(keys_after) == written["before"]["dbo.CMC_SBSB_SUBSC"]


# ---------------------------------------------------------------------------
# A load that stops partway
# ---------------------------------------------------------------------------

def test_a_failed_load_reports_the_tables_it_already_committed():
    """Each table commits on its own, so a failure leaves the earlier ones.

    Without the report traveling with the error there is no manifest, and
    nothing can find those rows again to remove them.
    """
    from sdg.writers.sqlserver import PartialWrite, TableWrite, WriteReport

    report = WriteReport(tables=[
        TableWrite(table="dbo.CMC_SBSB_SUBSC", rows_written=20_000),
    ])
    error = PartialWrite(ValueError("String data, right truncation"), report)
    assert "truncation" in str(error)
    assert error.report.total_rows() == 20_000
    assert error.report.tables[0].table == "dbo.CMC_SBSB_SUBSC"


def test_a_failure_before_anything_was_committed_is_raised_as_it_is():
    """There is nothing to clean up, so there is nothing extra to say."""
    from sdg.writers.sqlserver import PartialWrite, write

    class Unreachable(ConnectionRegistry):
        def connect(self, *args, **kwargs):
            raise RuntimeError("cannot reach the server")

    class _NoTables:
        tables: dict = {}
        def tables_in_order(self):
            return []

    with pytest.raises(RuntimeError) as caught:
        write(Unreachable(), _NoTables(), _NoTables(), None, connection="target")
    assert not isinstance(caught.value, PartialWrite)
    assert "cannot reach the server" in str(caught.value)

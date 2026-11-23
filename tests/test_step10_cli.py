"""Step 10 acceptance: the command line runs the whole sequence.

The pipeline commands are exercised in order against a shared working
directory, because each one reads the artifact the previous one wrote. The demo
command is run end to end and its success measures are checked.
"""

from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from sdg.cli.main import app
from sdg.config import REPO_ROOT

runner = CliRunner()


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    """A working directory of this module's own, so it cannot disturb a demo."""
    directory = tmp_path_factory.mktemp("sdg-cli")
    previous = os.environ.get("SDG_WORK_DIR")
    os.environ["SDG_WORK_DIR"] = str(directory)
    # get_settings reads the environment each call, so nothing needs reloading.
    yield directory
    if previous is None:
        os.environ.pop("SDG_WORK_DIR", None)
    else:
        os.environ["SDG_WORK_DIR"] = previous


def invoke(*args: str):
    result = runner.invoke(app, list(args))
    return result


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------

def test_every_step_has_a_command():
    result = invoke("--help")
    assert result.exit_code == 0
    for command in (
        "connect", "discover", "profile", "map", "review",
        "plan", "dryrun", "generate", "validate", "cleanup", "demo",
    ):
        assert command in result.output, command


def test_a_step_run_out_of_order_says_what_to_run_first(work):
    result = invoke("dryrun")
    assert result.exit_code != 0
    assert "sdg plan" in result.output


def test_the_demo_script_exists_and_covers_the_closing_move():
    script = (REPO_ROOT / "docs" / "demo-script.md").read_text(encoding="utf-8")
    assert "sdg discover" in script and "sdg validate" in script
    # The brief's closing step: change the format in the catalog and regenerate.
    assert "MEMBER_ID" in script
    assert "catalog" in script.lower() and "regenerate" in script.lower()


# ---------------------------------------------------------------------------
# The pipeline, in order
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_connect(work):
    result = invoke("connect")
    assert result.exit_code == 0, result.output
    assert (work / "connections.json").exists()
    assert "FACETS_QA" in result.output


@pytest.mark.db
def test_discover(work):
    result = invoke("discover")
    assert result.exit_code == 0, result.output
    model = json.loads((work / "physical-model.json").read_text(encoding="utf-8"))
    assert len(model["tables"]) == 19  # the network tables and member PCPs among them
    assert "Inferred foreign keys" in result.output


@pytest.mark.db
def test_profile(work):
    result = invoke("profile")
    assert result.exit_code == 0, result.output
    report = json.loads((work / "profile.json").read_text(encoding="utf-8"))
    assert report["columns"] and report["ratios"]
    assert "Children per parent" in result.output


@pytest.mark.db
def test_map_prints_the_auto_map_rate(work):
    result = invoke("map")
    assert result.exit_code == 0, result.output
    assert "Auto-map rate" in result.output
    store = json.loads((work / "mappings.json").read_text(encoding="utf-8"))
    assert store["mappings"]


@pytest.mark.db
def test_review_builds_the_page_and_the_decisions_file(work):
    result = invoke("review")
    assert result.exit_code == 0, result.output
    assert "Queue size" in result.output

    page = (work / "review.html").read_text(encoding="utf-8")
    assert "Review queue" in page and "{{" not in page

    decisions = json.loads((work / "review-decisions.json").read_text(encoding="utf-8"))
    assert "columns" in decisions and "tables" in decisions


@pytest.mark.db
def test_plan_prints_effective_counts(work):
    result = invoke(
        "plan",
        "--count", "dbo.CMC_SBSB_SUBSC=250",
        "--reuse", "dbo.CMC_PRPR_PROV",
        "--reuse", "dbo.CMC_PDPD_PRODUCT",
        # The enrollment hierarchy and the code sets are reference data; a run
        # points at them rather than generating employers of its own.
        "--reuse", "dbo.CMC_GRGR_GROUP",
        "--reuse", "dbo.CMC_SGSG_SUBGROUP",
        "--reuse", "dbo.CMC_CSPD_CAT_DESC",
        "--reuse", "dbo.CMC_CSPI_CS_PLAN",
        "--reuse", "dbo.CMC_PLDS_PLAN_DESC",
        "--reuse", "dbo.CMC_PDDS_PROD_DESC",
        "--reuse", "dbo.CMC_IDCD_DIAG_CODE",
        "--reuse", "dbo.CMC_IPCD_PROC_CODE",
        # Networks and who is in them are reference data too.
        "--reuse", "dbo.CMC_NWNW_NETWORK",
        "--reuse", "dbo.CMC_NWPR_RELATION",
        "--reuse", "dbo.CMC_NWST_NET_SET",
        "--exclude", "dbo.CMC_MEPR_PRIM_PROV",
        "--exclude", "dbo.CMC_CLCL_AUDIT",
        "--seed", "step10-seed",
    )
    assert result.exit_code == 0, result.output
    assert "Effective counts" in result.output
    assert "Total rows to generate" in result.output
    # The reference set filter is part of what the plan reports.
    assert "leaving out 3" in result.output

    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    assert plan["meta"]["seed"] == "step10-seed"
    modes = {t["name"]: t["mode"] for t in plan["tables"]}
    assert modes["dbo.CMC_PRPR_PROV"] == "reuse"
    assert modes["dbo.CMC_CLCL_AUDIT"] == "exclude"


@pytest.mark.db
def test_dryrun_writes_nothing_and_shows_rows(work):
    before = _claim_count()
    result = invoke("dryrun", "--rows", "3")
    assert result.exit_code == 0, result.output
    assert "dbo.CMC_CLCL_CLAIM" in result.output
    assert _claim_count() == before


@pytest.mark.db
def test_generate_validate_and_cleanup(work):
    before = _claim_count()

    generated = invoke("generate")
    assert generated.exit_code == 0, generated.output
    assert "total" in generated.output
    assert (work / "run-manifest.json").exists()
    assert _claim_count() > before

    validated = invoke("validate")
    assert validated.exit_code == 0, validated.output
    assert "Validation passed" in validated.output

    cleaned = invoke("cleanup", "--yes")
    assert cleaned.exit_code == 0, cleaned.output
    # The run has to say that a trigger fired and that its rows stay.
    assert "TR_CMC_CLCL_CLAIM_AUDIT" in cleaned.output
    assert _claim_count() == before
    assert (work / "run-manifest.cleaned.json").exists()


@pytest.mark.db
def test_generate_with_dry_writes_only_csv(work, tmp_path):
    before = _claim_count()
    out = tmp_path / "csv"
    result = invoke("generate", "--dry", "--csv", str(out))
    assert result.exit_code == 0, result.output
    assert _claim_count() == before
    assert sorted(p.name for p in out.glob("*.csv"))
    assert "nothing was written to the database" in result.output


def _claim_count() -> int:
    from sdg.discovery.connections import ConnectionRegistry

    return int(
        ConnectionRegistry().scalar("source", "SELECT COUNT_BIG(*) FROM dbo.CMC_CLCL_CLAIM")
    )


# ---------------------------------------------------------------------------
# The demo
# ---------------------------------------------------------------------------

@pytest.mark.db
@pytest.mark.slow
def test_demo_runs_end_to_end_and_reports_the_measures(work):
    before = _claim_count()
    # An earlier run not yet cleaned up: the demo keeps a record of its own and
    # leaves this one exactly as it was.
    earlier = work / "run-manifest.json"
    earlier.write_text('{"run": {"database": "SDMT_EARLIER"}}', encoding="utf-8")
    try:
        result = invoke("demo", "--small")
        assert earlier.read_text(encoding="utf-8") == '{"run": {"database": "SDMT_EARLIER"}}'
    finally:
        earlier.unlink()
    assert result.exit_code == 0, result.output
    assert (work / "demo-run-manifest.cleaned.json").exists()

    for heading in ("connect", "discover", "profile", "map", "review",
                    "plan", "dry run", "generate", "validate", "cleanup"):
        assert heading in result.output, heading

    assert "Proof-of-concept success measures" in result.output
    assert "auto-map rate at high confidence" in result.output
    assert "review queue size" in result.output
    assert "passed" in result.output

    # It cleaned up after itself.
    assert _claim_count() == before


@pytest.mark.db
@pytest.mark.slow
def test_demo_can_keep_what_it_generated(work):
    before = _claim_count()
    result = invoke("demo", "--small", "--subscribers", "50", "--keep", "--seed", "keep-test")
    assert result.exit_code == 0, result.output
    assert _claim_count() > before

    cleaned = invoke("cleanup", "--yes")
    assert cleaned.exit_code == 0, cleaned.output
    assert _claim_count() == before

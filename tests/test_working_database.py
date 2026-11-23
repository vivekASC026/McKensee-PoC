"""Choosing a database on Connect points the whole pipeline at it.

Reading, profiling, the reference rows a plan points at, generating and
cleaning up all follow the chosen database, and each database keeps its own
working files: what was done on one is set aside when another is chosen and
comes back when it is chosen again. A run's manifest above all must never be
carried to a database it was not written to.
"""

import json

import pytest


@pytest.mark.db
def test_choosing_a_database_moves_every_step_and_keeps_each_ones_files(tmp_path):
    from sdg.api.service import Service

    service = Service(work_dir=tmp_path)
    home = service.registry.database_for("target")
    assert service.registry.database_for("source") == home

    # Work done on the first database, including a manifest of rows written.
    (tmp_path / "plan.json").write_text('{"plan": "home"}', encoding="utf-8")
    (tmp_path / "run-manifest.json").write_text('{"run": "home"}', encoding="utf-8")
    (tmp_path / "landscape-FACETS_QA.json").write_text("{}", encoding="utf-8")

    other = "MDM_REF"
    service.set_target(other)
    assert service.registry.database_for("source") == other
    assert service.registry.database_for("landscape") == service.settings.source_db
    assert service.working_database()["database"] == other
    assert not (tmp_path / "plan.json").exists()
    assert not (tmp_path / "run-manifest.json").exists(), (
        "a manifest followed to a database it was not written to"
    )
    # The landscape describes the store, not either database, and stays.
    assert (tmp_path / "landscape-FACETS_QA.json").exists()

    service.set_target(home)
    assert service.registry.database_for("source") == home
    assert json.loads((tmp_path / "plan.json").read_text())["plan"] == "home"
    assert json.loads((tmp_path / "run-manifest.json").read_text())["run"] == "home"


@pytest.mark.db
def test_files_made_before_the_choice_was_recorded_belong_to_the_source(tmp_path):
    """An installation upgraded with another database already chosen: its
    files were all made from the source, because every read step read it."""
    from sdg.api.service import Service

    (tmp_path / "plan.json").write_text('{"plan": "source"}', encoding="utf-8")
    (tmp_path / "ui-settings.json").write_text(json.dumps({"targetDb": "MDM_REF"}),
                                               encoding="utf-8")
    service = Service(work_dir=tmp_path)
    assert service.registry.database_for("source") == "MDM_REF"
    assert not (tmp_path / "plan.json").exists()
    shelved = list((tmp_path / "databases").glob(f"*__{service.settings.source_db}/plan.json"))
    assert shelved, "the source's files were left in place for another database"

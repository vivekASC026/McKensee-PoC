"""The filters an analysis takes are the business pack's, not written in code.

Under health the request models have the fields the hand-written ones had
(and the child's loader's other filters beside them); a pack whose loader
lists other filters gets those fields instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError

from sdg.analyzer.services import filter_models
from sdg.analyzer.services.schemas import Filters
from sdg.api.routers.claims import ChildAnalyzeRequest, ClaimsAnalyzeRequest
from tests.test_pack_constants import ROOT


def test_health_primary_filters_are_the_four_levels():
    assert set(Filters.model_fields) == {"grgr_ck", "cspd_cat", "cspi_id", "pdpd_id", "conditions"}
    assert Filters.entity_key == "members"


def test_health_child_filters_are_its_loaders():
    assert ClaimsAnalyzeRequest is ChildAnalyzeRequest
    assert set(ChildAnalyzeRequest.model_fields) == {
        "grgr_ck", "cspi_id", "pdpd_id", "claim_type", "gender", "age_min", "age_max",
        "diagnosis", "procedure", "conditions", "k"}
    assert ChildAnalyzeRequest(k=3).k == 3
    with pytest.raises(ValidationError):
        ChildAnalyzeRequest(k=0)


def test_only_given_filters_are_passed_on():
    assert Filters(grgr_ck=["12", 7], cspd_cat=[], cspi_id="", pdpd_id=None).as_kwargs() == {
        "grgr_ck": [12, 7]}
    # One bare value, as the loader takes it.
    assert ChildAnalyzeRequest(gender="F", cspi_id="P").as_kwargs() == {"gender": "F", "cspi_id": "P"}
    with pytest.raises(ValidationError):
        Filters(grgr_ck=["not a number"])


def test_stored_filters_are_read_as_the_model_reads_them():
    stored = {"grgr_ck": [1], "cspd_cat": ["M"], "claim_type": ["P"], "stray": 1}
    # The child takes no plan category; the record type chosen replaces the stored one.
    assert filter_models.of(ChildAnalyzeRequest, stored, "I") == {
        "grgr_ck": [1], "claim_type": ["I"]}
    assert filter_models.of(Filters, stored) == {"grgr_ck": [1], "cspd_cat": ["M"]}


ANOTHER = r'''
import json
from sdg.analyzer.services.schemas import Filters
from sdg.api.routers.claims import ChildAnalyzeRequest
print(json.dumps({"primary": sorted(Filters.model_fields), "entity": Filters.entity_key,
                  "kwargs": Filters(branch_id=["3"], segment="MASS").as_kwargs(),
                  "child": sorted(ChildAnalyzeRequest.model_fields)}))
'''


def test_a_pack_with_other_filters_gets_those_fields(tmp_path):
    folder = tmp_path / "other"
    shutil.copytree(ROOT / "sdg" / "packs" / "health", folder)
    features = json.loads((folder / "features.json").read_text(encoding="utf-8"))
    features["loaders"]["members"]["filters"] = [
        {"key": "branch_id", "column": "BRANCH_ID", "kind": "ids"},
        {"key": "segment", "column": "SEGMENT", "kind": "values"}]
    features["loaders"]["claims"]["filters"] = []
    (folder / "features.json").write_text(json.dumps(features), encoding="utf-8")
    body = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    body.update(id="other", name="Other")
    (folder / "pack.json").write_text(json.dumps(body), encoding="utf-8")
    done = subprocess.run([sys.executable, "-c", ANOTHER], cwd=ROOT, capture_output=True,
                          text=True, timeout=300,
                          env=dict(os.environ, SDG_PACKS_DIR=str(tmp_path), SDG_PACK="other"))
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["primary"] == ["branch_id", "conditions", "segment"]
    assert out["kwargs"] == {"branch_id": [3], "segment": "MASS"}
    assert out["child"] == ["conditions", "k"]

"""A saved pattern still has its records after the analysis is run again.

A pattern kept from an analysis -- a scenario of one, say -- names the records
it holds and the cluster they were in. Run again later, the analysis can group
the same records differently: cluster 2 is no longer those claims. Looking for
the named records only in the old cluster found none, so the pattern had no
scenarios and nothing to generate. The records are found wherever they are.
"""

from __future__ import annotations

import pandas as pd

from sdg.analyzer.services import saved_patterns


def _claims(ids: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"CLCL_ID": ids, "CDML_SEQ_NO": [1] * len(ids)})


def test_records_of_a_regrouped_cluster_are_found_in_the_whole_scope(monkeypatch):
    # Clustered again, cluster 2 now holds other claims; the scope holds all.
    monkeypatch.setattr(saved_patterns, "rows", lambda *a, **k: _claims(["C9", "C8"]))
    monkeypatch.setattr(saved_patterns, "_scope_rows",
                        lambda *a, **k: _claims(["C1", "C2", "C8", "C9"]))
    profile = {"saved_from": {"domain": "claims", "filters": {}, "k": None, "cluster_id": 2,
                              "record_keys": ["C1-1", "C2-1"]}}
    found = saved_patterns.saved_rows(profile)
    assert sorted(found["CLCL_ID"]) == ["C1", "C2"]


def test_records_still_in_their_cluster_are_read_from_it(monkeypatch):
    monkeypatch.setattr(saved_patterns, "rows", lambda *a, **k: _claims(["C1", "C2", "C3"]))

    def no_scope(*_a, **_k):
        raise AssertionError("the whole scope was read when the cluster had every record")

    monkeypatch.setattr(saved_patterns, "_scope_rows", no_scope)
    profile = {"saved_from": {"domain": "claims", "filters": {}, "k": None, "cluster_id": 2,
                              "record_keys": ["C1-1", "C2-1"]}}
    assert sorted(saved_patterns.saved_rows(profile)["CLCL_ID"]) == ["C1", "C2"]

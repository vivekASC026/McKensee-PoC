"""A pattern carries where its members live, as whole addresses.

A ZIP and its city belong together. Carried as separate distributions they
would be drawn apart and put members in places that do not exist, so a
pattern carries address records, and every column fed from them -- in a file
or in a database load -- takes its field from the same record.
"""

import pytest

from sdg.analyzer.bridge import to_pattern
from sdg.writers import rule_values

PLACES = [
    {"ADDRESS_ZIP": "62701", "ADDRESS_CITY": "Springfield", "ADDRESS_STATE": "IL", "share": 0.6},
    {"ADDRESS_ZIP": "96813", "ADDRESS_CITY": "Honolulu", "ADDRESS_STATE": "HI", "share": 0.3},
    {"ADDRESS_ZIP": "19102", "ADDRESS_CITY": "Philadelphia", "ADDRESS_STATE": "PA", "share": 0.1},
]


def test_a_pattern_carries_its_places_as_records():
    pattern = to_pattern({"name": "p", "cluster_id": 0, "geography": PLACES}, kind="member")
    address = {p.label: p for p in pattern.parameters}
    assert set(address) == {"Address City", "Address State", "Address Zip"}
    for parameter in address.values():
        assert parameter.record_field and len(parameter.values) == 3
        assert parameter.weights == pytest.approx([0.6, 0.3, 0.1])


def test_a_file_draws_zip_city_and_state_from_one_address():
    rules = {
        element: {"gen_method": "lookup", "values": [{k: v for k, v in p.items() if k != "share"} for p in PLACES],
                  "weights": [0.6, 0.3, 0.1], "record_key": "pattern_address", "field": element}
        for element in ("ADDRESS_ZIP", "ADDRESS_CITY", "ADDRESS_STATE")
    }
    real = {(p["ADDRESS_ZIP"], p["ADDRESS_CITY"], p["ADDRESS_STATE"]) for p in PLACES}
    for row in range(200):
        drawn = tuple(rule_values.produce({**rules[e], "element": e}, seed="t", row_index=row)
                      for e in ("ADDRESS_ZIP", "ADDRESS_CITY", "ADDRESS_STATE"))
        assert drawn in real, drawn


@pytest.mark.db
def test_a_member_pattern_reaches_address_columns_together():
    from fastapi.testclient import TestClient

    from sdg.api.app import app

    client = TestClient(app)
    pattern = client.post("/api/patterns/analyze",
                          json={"filters": {"grgr_ck": [1]}, "k": 2}).json()["patterns"][0]
    # The store holds no addresses, as a customer's denorm does not; the
    # places come from its address list, still whole.
    assert not pattern["geography"]
    resolved = client.post("/api/patterns/to-plan", json={
        "pattern": pattern, "kind": "member", "as_of": "2026-06-30", "apply": False}).json()
    address = [t for t in resolved["resolution"]["targets"]
               if t["parameter"].startswith("Address")]
    assert {t["column"] for t in address} >= {"MEME_ZIP", "MEME_CITY"}, resolved["resolution"]
    keys = {t["params"].get("record_key") for t in address}
    assert keys == {"pattern_address"}, "address columns would be drawn apart"

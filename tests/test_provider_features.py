"""Provider and network features, and the choice of what patterns are found on.

A claim is in network when its provider is in one of the member's plan's
networks on the service date; then it names that network. The store's
member_denorm and claims_denorm are not altered: provider features are joined
from the tables beside them. Patterns are found on the features the Config
screen chooses, and generation draws provider, plan, network and in/out of
network together.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app, service

pytestmark = pytest.mark.db
client = TestClient(app)


@pytest.fixture
def keep_choice():
    """Whatever features were chosen before the test are chosen after it."""
    from sdg.analyzer.services import discovery_features

    before = {e: discovery_features.chosen(e) for e in ("members", "claims")}
    yield
    for entity, keys in before.items():
        discovery_features.choose(entity, keys)


def test_a_claim_is_in_network_exactly_when_its_provider_is_in_the_plans_network():
    rows = service.registry.query("denorm", """
        WITH c AS (SELECT DISTINCT CLCL_ID, CLCL_NTWK_IND, NWNW_ID, PRPR_ID, CSPI_ID,
                          CLCL_LOW_SVC_DT AS d FROM sdm.claims_denorm),
        f AS (SELECT c.CLCL_NTWK_IND, c.NWNW_ID,
                     CASE WHEN EXISTS (
                        SELECT 1 FROM sdm.plan_denorm p
                        JOIN sdm.plan_network n ON n.NWST_PFX = p.NWST_PFX
                        JOIN sdm.provider_network r ON r.NWNW_ID = n.NWNW_ID AND r.PRPR_ID = c.PRPR_ID
                         AND r.NWPR_EFF_DT <= c.d AND (r.NWPR_TERM_DT IS NULL OR r.NWPR_TERM_DT >= c.d)
                        WHERE p.CSPI_ID = c.CSPI_ID) THEN 1 ELSE 0 END AS inside
              FROM c)
        SELECT CLCL_NTWK_IND AS ind, SUM(inside) AS inside, COUNT(*) AS n,
               COUNT(NWNW_ID) AS named FROM f GROUP BY CLCL_NTWK_IND""")
    by = {r["ind"]: r for r in rows}
    assert by["I"]["inside"] == by["I"]["n"] == by["I"]["named"]
    assert by["O"]["inside"] == 0 and by["O"]["named"] == 0
    assert 0.05 < by["O"]["n"] / (by["O"]["n"] + by["I"]["n"]) < 0.25


def test_provider_features_are_offered_with_their_values():
    claims = {f["key"]: f for f in client.get("/api/patterns/features?entity=claims")
              .json()["features"]}
    assert {"provider", "provider_specialty", "provider_entity", "network", "network_id",
            "network_type"} <= set(claims)
    assert "Out of network" in [o["value"] for o in claims["network_id"]["options"]]
    assert {"Practitioner", "Facility"} <= {o["value"] for o in claims["provider_entity"]["options"]}
    members = {f["key"]: f for f in client.get("/api/patterns/features?entity=members")
               .json()["features"]}
    assert {"has_pcp", "pcp_specialty", "pcp_entity"} <= set(members)
    assert members["pcp_specialty"]["options"]


def test_the_choice_of_features_is_kept_and_a_bad_one_refused(keep_choice):
    shown = client.get("/api/discovery-features").json()
    assert {f["key"] for f in shown["members"] if f["default"]} == \
        {"age", "tenure", "gender", "marital_status"}
    assert client.put("/api/discovery-features",
                      json={"entity": "claims", "features": []}).status_code == 422
    assert client.put("/api/discovery-features",
                      json={"entity": "claims", "features": ["nonsense"]}).status_code == 422
    saved = client.put("/api/discovery-features", json={
        "entity": "claims", "features": ["network_type", "provider_specialty", "charge"]}).json()
    assert {f["key"] for f in saved["claims"] if f["chosen"]} == \
        {"network_type", "provider_specialty", "charge"}


def test_patterns_are_found_on_the_chosen_features_and_describe_them(keep_choice):
    client.put("/api/discovery-features", json={
        "entity": "members", "features": ["age", "has_pcp", "pcp_specialty"]})
    members = client.post("/api/patterns/analyze",
                          json={"filters": {"grgr_ck": [1]}, "k": 3}).json()["patterns"]
    assert members and all("_pcp_specialty" in p["categorical"] for p in members)

    client.put("/api/discovery-features", json={
        "entity": "claims", "features": ["network", "network_type", "provider_entity"]})
    claims = client.post("/api/claims/analyze", json={"grgr_ck": [1], "k": 3}).json()["patterns"]
    assert claims
    for pattern in claims:
        assert {"network", "network_type", "provider_type"} <= set(pattern["categorical"])
    # Clustered on network alone, the patterns split by it.
    shares = sorted(p["categorical"]["network"].get("O", 0.0) for p in claims)
    assert shares[-1] - shares[0] > 0.5, shares


def test_generation_draws_provider_plan_and_network_together():
    from sdg.plan.combinations import for_patterns

    group = next(g for g in for_patterns() if "NETWORK_ID" in g.elements)
    assert {"PROVIDER_ID", "COVERAGE_PLAN_ID", "NETWORK_INDICATOR"} <= set(group.elements)
    ind = group.elements.index("NETWORK_INDICATOR")
    net = group.elements.index("NETWORK_ID")
    assert all((v[ind] == "O") == (v[net] is None) for v, _ in group.combinations)
    total = sum(n for _, n in group.combinations)
    drawn = sum(n for v, n in group.combinations if v[ind] == "O") / total
    real = service.registry.query("denorm", "SELECT AVG(CASE WHEN CLCL_NTWK_IND = 'O' "
                                            "THEN 1.0 ELSE 0.0 END) AS s FROM sdm.claims_denorm")[0]["s"]
    assert abs(drawn - float(real)) < 0.03, (drawn, real)


@pytest.mark.slow
def test_a_generated_claim_keeps_its_members_plan_product_and_network(tmp_path, monkeypatch):
    """Rows a run writes: the product is the member's plan's, an in-network
    provider is in one of that plan's networks and the claim names it, an
    out-of-network claim names none and its provider is outside them."""
    import pyodbc
    from typer.testing import CliRunner

    from sdg.cli.main import app as cli
    from sdg.config import get_settings

    monkeypatch.setenv("SDG_WORK_DIR", str(tmp_path))
    runner = CliRunner()
    made = runner.invoke(cli, ["demo", "--small", "--keep"])
    conn = pyodbc.connect(get_settings().mssql.connection_string(get_settings().source_db))
    try:
        assert made.exit_code == 0, made.output[-2000:]
        # The claims this run wrote, as its manifest records them.
        manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
        written = [str(k[0] if isinstance(k, list) else k).strip()
                   for k in manifest["keys"]["dbo.CMC_CLCL_CLAIM"]]
        row = conn.execute("""
            WITH g AS (
                SELECT cl.PDPD_ID, cl.PRPR_ID, cl.CLCL_NTWK_IND AS ind, cl.NWNW_ID,
                       p.PDPD_ID AS plan_product, p.NWST_PFX
                FROM dbo.CMC_CLCL_CLAIM cl
                JOIN dbo.CMC_MEME_MEMBER m ON m.MEME_CK = cl.MEME_CK
                JOIN dbo.CMC_SBSB_SUBSC s ON s.SBSB_CK = m.SBSB_CK
                JOIN dbo.CMC_CSPI_CS_PLAN p ON p.CSPI_ID = s.CSPI_ID
                WHERE cl.CLCL_ID IN (SELECT value FROM OPENJSON(?))),
            f AS (SELECT g.*, CASE WHEN EXISTS (
                     SELECT 1 FROM dbo.CMC_NWST_NET_SET ns
                     JOIN dbo.CMC_NWPR_RELATION r ON r.NWNW_ID = ns.NWNW_ID
                     WHERE ns.NWST_PFX = g.NWST_PFX AND r.PRPR_ID = g.PRPR_ID
                       AND (g.NWNW_ID IS NULL OR r.NWNW_ID = g.NWNW_ID)) THEN 1 ELSE 0 END AS inside
                  FROM g)
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN PDPD_ID = plan_product THEN 1 ELSE 0 END) AS same_product,
                   SUM(CASE WHEN ind = 'I' THEN 1 ELSE 0 END) AS inn,
                   SUM(CASE WHEN ind = 'I' AND inside = 1 AND NWNW_ID IS NOT NULL THEN 1 ELSE 0 END) AS inn_ok,
                   SUM(CASE WHEN ind = 'O' THEN 1 ELSE 0 END) AS oon,
                   SUM(CASE WHEN ind = 'O' AND inside = 0 AND NWNW_ID IS NULL THEN 1 ELSE 0 END) AS oon_ok
            FROM f""", json.dumps(written)).fetchone()
        n, same, inn, inn_ok, oon, oon_ok = row
        assert n > 0 and same == n, row
        assert inn_ok == inn and oon_ok == oon and oon > 0, row
    finally:
        runner.invoke(cli, ["cleanup", "--yes"])
        conn.close()

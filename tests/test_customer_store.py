"""The tool, run against a store shaped exactly like the customer's.

The customer's SDM platform database is theirs: sdm.member_denorm and
sdm.claims_denorm as their DDL defines them (tests/fixtures), and the
platform's own tables with plain int ids and no defaults. This builds a
throwaway database from that DDL, runs the customer upgrade script on it, and
then drives the tool against it as its store, in a process of its own with the
store's settings pointed there: extract into their tables, find patterns, keep
one, add a rule, save a layout, write an 834 and an 837 from the kept pattern.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pyodbc
import pytest

pytestmark = [pytest.mark.db, pytest.mark.slow]

ROOT = Path(__file__).resolve().parent.parent
NAME = "SDMT_CUSTOMER_STORE"
DDL = ROOT / "tests" / "fixtures" / "customer_sdm_platform.sql"


def _master():
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _drop() -> None:
    with _master() as master:
        if master.execute("SELECT DB_ID(?)", NAME).fetchone()[0]:
            master.execute(f"ALTER DATABASE [{NAME}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            master.execute(f"DROP DATABASE [{NAME}]")


@pytest.fixture(scope="module")
def customer():
    """The customer's schema in a throwaway database, upgraded."""
    from sdg.config import get_settings
    from seed.build import run_sql_script

    _drop()
    with _master() as master:
        master.execute(f"CREATE DATABASE [{NAME}]")
    conn = pyodbc.connect(get_settings().mssql.connection_string(NAME), autocommit=True)
    try:
        conn.execute("CREATE SCHEMA sdm")
        for statement in DDL.read_text(encoding="utf-8").split(");"):
            if statement.strip():
                conn.execute(statement + ");")
        # A few addresses, as the platform's lookup holds them.
        for i, (zip_, city, state) in enumerate(
                [("40202", "Louisville", "KY"), ("45202", "Cincinnati", "OH")], start=1):
            conn.execute("INSERT INTO sdm.lkp_zip_city_state (id, zip, city, state, county, created_at) "
                         "VALUES (?, ?, ?, ?, ?, SYSDATETIME())", i, zip_, city, state, "Any")
        run_sql_script(conn, ROOT / "scripts" / "customer" / "01_sdm_upgrade.sql")
        # The provider, plan and claim-detail tables beside the denorms.
        run_sql_script(conn, ROOT / "sdg" / "packs" / "health" / "sql" / "03_denorm_837_tables.sql")
    finally:
        conn.close()
    work = Path(tempfile.mkdtemp(prefix="sdm-customer-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True)
    yield work
    shutil.rmtree(work, ignore_errors=True)
    _drop()


def _run(work: Path, code: str) -> dict:
    env = dict(os.environ, SDG_DB_DENORM=NAME, SDG_WORK_DIR=str(work),
               SDG_DENORM_SCHEMA="sdm")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=1800)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


SESSION = r'''
import json, time, warnings
warnings.filterwarnings("ignore")
from sdg.extract.run import extract
from sdg.api.app import app, service
from fastapi.testclient import TestClient

out = {}
started = time.time()
result = extract("FACETS_QA")
out["extracted"] = {t.table: t.rows for t in result.tables}
out["extract_seconds"] = round(time.time() - started, 1)

from sdg.landscape.derive import derive
import json as _j
shape = derive(source="FACETS_QA", catalog=service.catalog, registry=service.registry)
(service.work / "landscape-FACETS_QA.json").write_text(shape.model_dump_json(), encoding="utf-8")

import os
c = TestClient(app, headers={"X-SDM-Token": os.environ["SDG_API_TOKEN"]})
started = time.time()
members = c.post("/api/patterns/analyze", json={"filters": {"grgr_ck": [1]}, "k": 3}).json()
out["analyze_seconds"] = round(time.time() - started, 1)
pattern = members["patterns"][0]
out["geography"] = len(pattern.get("geography") or [])
claims = c.post("/api/claims/analyze", json={"grgr_ck": [1], "k": 3}).json()
out["claims_patterns"] = len(claims["patterns"])

kept = c.post("/api/patterns/keep", json={"domain": "members", "filters": {"grgr_ck": [1]},
              "k": 3, "cluster_id": pattern["cluster_id"], "name": "SDMT customer"}).json()
out["kept"] = kept
claim_kept = c.post("/api/patterns/keep", json={"domain": "claims", "filters": {"grgr_ck": [1]},
                    "k": 3, "cluster_id": claims["patterns"][0]["cluster_id"]}).json()
out["claim_kept"] = claim_kept["pattern_id"]
row = service.registry.query("denorm", "SELECT id, created_at, updated_at, origin FROM sdm.patterns WHERE id = ?", [kept["pattern_id"]])[0]
out["pattern_row"] = {k: str(v) for k, v in row.items()}
out["kinds"] = {str(r["id"]): [r["entity"], r["scope_json"]] for r in service.registry.query(
    "denorm", "SELECT id, entity, scope_json FROM sdm.patterns WHERE id IN (?, ?)",
    [kept["pattern_id"], claim_kept["pattern_id"]])}
out["kept_ids"] = [kept["pattern_id"], claim_kept["pattern_id"]]

rule = c.post("/api/rules", json={"field_name": "SDMT_CUSTOMER_RULE", "gen_method": "constant", "domain": "X"})
out["rule"] = rule.status_code
layout = c.post("/api/layouts/save", json={"name": "SDMT customer layout", "kind": "csv", "delimiter": ",",
                "fields": [{"name": "MEMBER_ID"}], "purpose": "generation"})
out["layout"] = layout.status_code

# The scope kept beside a pattern, on a real store: a row from before
# scope_json gets one; one kept with no names gets them once known; a named
# one is left as it is.
import json as _json
from sdg.analyzer import db as _db
from sdg.analyzer.services import pattern_service as _ps
from sdg.analyzer.services.discovery_service import canonical_combo

def scope_of(pid):
    row = service.registry.query("denorm", "SELECT scope_json, entity, cspi_id FROM sdm.patterns WHERE id = ?", [pid])[0]
    return {"scope": _json.loads(row["scope_json"]) if row["scope_json"] else None,
            "entity": row["entity"], "cspi_id": row["cspi_id"]}

unnamed = canonical_combo({"grgr_ck": [1], "cspi_id": ["SDMT-PLAN"]}, entity="claims")
pid = _db.upsert_pattern(unnamed, 991, "SDMT backfill", 5, 0.1, 0.0, {"k": 1})
out["backfill_first"] = scope_of(pid)
with service.registry.connect("denorm", autocommit=True) as _conn:
    _conn.execute("UPDATE sdm.patterns SET scope_json = NULL WHERE id = ?", pid)
_db.upsert_pattern(unnamed, 991, "SDMT backfill", 5, 0.1, 0.0, {"k": 1})
out["backfill_null"] = scope_of(pid)
named = canonical_combo({"grgr_ck": [1], "cspi_id": ["SDMT-PLAN"],
                         "_labels": {"cspi_id": "SDMT plan"}}, entity="claims")
_db.upsert_pattern(named, 991, "SDMT backfill", 5, 0.1, 0.0, {"k": 1})
out["backfill_named"] = scope_of(pid)
renamed = canonical_combo({"grgr_ck": [1], "cspi_id": ["SDMT-PLAN"],
                           "_labels": {"cspi_id": "Another name"}}, entity="claims")
_db.upsert_pattern(renamed, 991, "SDMT backfill", 5, 0.1, 0.0, {"k": 1})
out["backfill_kept"] = scope_of(pid)

made = _ps.create_user_pattern("SDMT defined", {"cspi_id": ["SDMT-A"]}, entity="claims",
                               labels={"plan": "Plan A"})
out["defined"] = scope_of(made["pattern_id"])
_ps.update_user_pattern(made["pattern_id"], features={"cspi_id": ["SDMT-B"]})
out["updated"] = scope_of(made["pattern_id"])
out["made_ids"] = [pid, made["pattern_id"]]

f834 = c.post("/api/export", json={"format": "edi:834", "subject": "members", "pattern_id": kept["pattern_id"], "members": 3, "rows": 3})
f837 = c.post("/api/export", json={"format": "edi:837P", "subject": "claims", "pattern_id": claim_kept["pattern_id"], "rows": 3})
out["834"] = [s for s in f834.text.replace("\n", "").split("~") if s.startswith(("INS", "N4"))][:4]
out["837"] = [s for s in f837.text.replace("\n", "").split("~") if s.startswith("NM1*IL")][:2]
print(json.dumps(out, default=str))
'''


def test_the_tool_runs_against_the_customers_store(customer):
    out = _run(customer, SESSION)
    assert out["extracted"]["member_denorm"] > 0 and out["extracted"]["claims_denorm"] > 0
    assert out["geography"] == 0, "the customer's store holds no addresses"
    assert out["claims_patterns"] >= 2

    # Kept on a table whose id is a plain int and whose times have no default.
    assert out["kept"]["created"] and out["kept"]["pattern_id"] >= 1
    assert out["pattern_row"]["created_at"] != "None" and out["pattern_row"]["updated_at"] != "None"
    assert out["pattern_row"]["origin"] == "discovered"
    assert out["rule"] == 200 and out["layout"] == 200

    # The scope kept beside a pattern: written, filled where empty, named
    # once its names are known, never overwritten once named.
    first = out["backfill_first"]
    assert first["entity"] == "claims" and first["scope"]["labels"] == {}
    assert first["scope"]["filters"] == {"grgr_ck": ["1"], "cspi_id": ["SDMT-PLAN"]}
    assert out["backfill_null"]["scope"] == first["scope"]
    assert out["backfill_named"]["scope"]["labels"] == {"cspi_id": "SDMT plan"}
    assert out["backfill_kept"]["scope"]["labels"] == {"cspi_id": "SDMT plan"}
    # A user-defined pattern: its scope and health's own column, changed together.
    assert out["defined"]["scope"] == {"entity": "claims", "filters": {"cspi_id": ["SDMT-A"]},
                                       "labels": {"cspi_id": "Plan A"}}
    assert out["updated"]["scope"]["filters"] == {"cspi_id": ["SDMT-B"]}
    assert out["updated"]["scope"]["labels"] == {"cspi_id": "Plan A"}
    assert out["updated"]["cspi_id"] == '["SDMT-B"]' and out["updated"]["entity"] == "claims"

    # Each kept pattern says which records it describes, and the scope it was
    # found in, in the pack's words (the entity column was never written before).
    member_id, claim_id = (str(i) for i in out["kept_ids"])
    assert out["kinds"][member_id][0] == "members" and out["kinds"][claim_id][0] == "claims"
    for pid, kind in ((member_id, "members"), (claim_id, "claims")):
        held = json.loads(out["kinds"][pid][1])
        assert held["entity"] == kind and held["filters"] == {"grgr_ck": ["1"]}, held

    # An 834 with X12 relationship codes and whole addresses from the lookup;
    # an 837 with member IDs composed from subscriber ID and suffix.
    ins = [s for s in out["834"] if s.startswith("INS")]
    assert ins and all(s.split("*")[2] in {"18", "01", "19", "53"} for s in ins), out["834"]
    n4 = [s for s in out["834"] if s.startswith("N4")]
    assert n4 and all(s in {"N4*Louisville*KY*40202", "N4*Cincinnati*OH*45202"} for s in n4), n4
    il = out["837"]
    assert il and all(s.split("*")[-1].strip() for s in il), il

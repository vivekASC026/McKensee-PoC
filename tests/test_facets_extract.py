"""The customer's deployment, end to end: FACETS on one connection, a new and
empty SDM platform database on another.

FACETS here is built from the customer's data dictionary (tests/facets_dd.py).
The tool runs in a process of its own with the two connections set the way
the customer's .env sets them, and does what the runbook says: create the
tables, extract FACETS -- twice, since a refresh replaces and does not add --
and find patterns on what was loaded. Creating the tables again afterwards
changes nothing.
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
TABLES_03 = 21


@pytest.fixture(scope="module")
def customer():
    from sdg.config import get_settings

    from tests import facets_dd

    facets_dd.build_facets()
    facets_dd.data()
    facets_dd.build_empty_store()
    work = Path(tempfile.mkdtemp(prefix="sdm-facets-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True)
    try:
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)
        facets_dd.drop(facets_dd.FDB)
        facets_dd.drop(facets_dd.SDB)


def _env(work: Path) -> dict:
    from tests import facets_dd

    return dict(os.environ, SDG_DB_SOURCE=facets_dd.FDB, SDG_DB_TARGET=facets_dd.FDB,
                SDG_DB_DENORM=facets_dd.SDB, SDG_DENORM_SCHEMA="sdm", SDG_WORK_DIR=str(work),
                SDG_EXTRACT_KEY="test-extract-key-not-a-secret")


def _cli(work: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "sdg.cli.main", *args], cwd=ROOT,
                          env=_env(work), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=1800)


def _extract(work: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "sdg.cli.main", "extract", "FACETS", "--keep-going"],
        cwd=ROOT, env=_env(work), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800)


def _counts() -> dict[str, int]:
    from sdg.config import get_settings

    from tests import facets_dd

    with pyodbc.connect(get_settings().mssql.connection_string(facets_dd.SDB)) as conn:
        rows = conn.execute(
            "SELECT t.name, SUM(p.rows) FROM sys.tables t JOIN sys.partitions p "
            "ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
            "WHERE SCHEMA_NAME(t.schema_id) = 'sdm' GROUP BY t.name").fetchall()
    return {name: int(n) for name, n in rows}


def test_the_tables_are_created_in_an_empty_store(customer):
    made = _cli(customer, "customer-tables")
    assert made.returncode == 0, made.stdout[-3000:] + made.stderr[-3000:]
    counts = _counts()
    for table in ("member_denorm", "claims_denorm", "provider_denorm", "member_pcp",
                  "patterns", "generation_rules", "layouts", "discovery_features",
                  "lkp_zip_city_state", "extract_log"):
        assert table in counts, (table, sorted(counts))
    assert counts["member_denorm"] == counts["claims_denorm"] == 0
    assert counts["lkp_zip_city_state"] > 0, "the reference lists are seeded"


def test_the_extract_fills_every_denorm_table_from_facets_and_a_refresh_replaces(customer):
    first = _extract(customer)
    assert first.returncode == 0, first.stdout[-3000:] + first.stderr[-3000:]
    assert "failed" not in first.stdout
    counts = _counts()
    assert counts["member_denorm"] > 0 and counts["claims_denorm"] > 0
    for table in ("provider_denorm", "provider_network", "member_pcp", "plan_network",
                  "member_elig", "claims_detail_denorm"):
        assert counts[table] > 0, (table, counts)

    again = _extract(customer)
    assert again.returncode == 0, again.stdout[-3000:]
    later = _counts()
    for table in ("member_denorm", "claims_denorm", "provider_denorm", "member_pcp"):
        assert later[table] == counts[table], f"{table} grew on a refresh"
    # Every table filled is stamped, twice.
    assert later["extract_log"] == 2 * (2 + TABLES_03)

    # Creating the tables again empties nothing.
    again = _cli(customer, "customer-tables")
    assert again.returncode == 0, again.stdout[-3000:]
    assert _counts()["member_denorm"] == counts["member_denorm"]


def test_only_current_and_active_records_are_extracted(customer):
    """Members eligible today, their claims of the last 24 months, plans,
    PCPs, providers and network rows in force today. Nothing expired."""
    from sdg.config import get_settings

    from tests import facets_dd

    with pyodbc.connect(get_settings().mssql.connection_string(facets_dd.SDB)) as conn:
        def ids(sql):
            return sorted(str(r[0]).strip() for r in conn.execute(sql).fetchall())

        # 3 (span ended), 5 (not eligible) and 6 (termed 2020) are left out.
        assert ids("SELECT DISTINCT MEME_CK FROM sdm.member_denorm") == ["1", "2", "4"]
        assert ids("SELECT DISTINCT MEME_CK FROM sdm.member_elig") == ["1", "2", "4"]
        assert ids("SELECT MEPE_EFF_DT FROM sdm.member_elig WHERE MEME_CK = 1 AND CSPD_CAT = 'M'") \
            == ["2024-01-01 00:00:00"]
        # CL3 no lines, CL4 member not eligible, CL5 older than 24 months.
        for table in ("claims_denorm", "claims_header_ext_denorm", "claims_detail_denorm"):
            assert ids(f"SELECT DISTINCT CLCL_ID FROM sdm.{table}") == \
                ["CL0000000001", "CL0000000002"], table
        assert ids("SELECT DISTINCT CLCL_ID FROM sdm.claims_diag") == ["CL0000000001", "CL0000000002"]
        # Only the plan rows in force today, of plans an eligible span names.
        assert ids("SELECT CSPI_ID FROM sdm.plan_denorm") == ["PLAND001", "PLANM001", "PLANM002"]
        assert ids("SELECT CONVERT(char(10), CSPI_EFF_DT, 23) FROM sdm.plan_denorm "
                   "WHERE CSPI_ID = 'PLANM001'") == ["2024-01-01"]
        assert ids("SELECT DISTINCT NWST_PFX FROM sdm.plan_network") == ["NS01", "NS02"]
        assert ids("SELECT DISTINCT SEDF_UM_REQ_IND FROM sdm.plan_benefit_denorm "
                   "WHERE PDPD_ID = 'MED00001' AND SESE_ID = 'OV01'") == ["Y"]
        # Only Service IDs used on the claims in scope (OV01); IP01 and DN01
        # are covered but never billed.
        assert ids("SELECT DISTINCT SESE_ID FROM sdm.plan_benefit_denorm") == ["OV01"]
        # ...and only for the products they were billed under: OV01 was billed
        # under MED00001 (CL1) and MED00002 (CL2); DEN00001 never.
        assert ids("SELECT DISTINCT PDPD_ID FROM sdm.plan_benefit_denorm") == ["MED00001", "MED00002"]
        # ...and only under the payment prefix the lines were priced with (SP01).
        assert ids("SELECT DISTINCT SEPY_PFX FROM sdm.plan_benefit_denorm") == ["SP01"]
        # The PCP in force today only (member 4's ended 2023).
        assert ids("SELECT MEME_CK FROM sdm.member_pcp") == ["1"]
        # PR8 is terminated, PR9 only on a network no current plan uses.
        provs = ids("SELECT PRPR_ID FROM sdm.provider_denorm")
        assert "PR0000000008" not in provs and "PR0000000009" not in provs, provs
        assert "PR0000000001" in provs and "GR0000000001" in provs and "FA0000000002" in provs
        assert ids("SELECT DISTINCT PRPR_ID FROM sdm.provider_network WHERE PRPR_ID = 'PR0000000008'") == []
        # Only the address and agreement in force today.
        assert ids("SELECT CONVERT(char(10), PRAD_EFF_DT, 23) FROM sdm.provider_address "
                   "WHERE PRPR_ID = 'PR0000000001' AND PRAD_TYPE = 'PRA'") == ["2023-01-01"]
        assert ids("SELECT DISTINCT AGAG_DESC FROM sdm.provider_network") == ["Current agreement"]


def test_patterns_are_found_on_what_was_extracted(customer):
    code = r'''
import json, warnings
warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from sdg.api.app import app
import os
c = TestClient(app, headers={"X-SDM-Token": os.environ["SDG_API_TOKEN"]})
members = c.post("/api/patterns/analyze", json={"filters": {}, "k": 2})
claims = c.post("/api/claims/analyze", json={"k": 2})
features = c.get("/api/discovery-features")
print(json.dumps({"members": members.status_code, "member_patterns": len(members.json().get("patterns", [])),
                  "claims": claims.status_code, "claim_patterns": len(claims.json().get("patterns", [])),
                  "features": features.status_code}))
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(customer),
                            capture_output=True, text=True, timeout=1800)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["members"] == 200 and out["member_patterns"] >= 1, out
    assert out["claims"] == 200 and out["claim_patterns"] >= 1, out
    assert out["features"] == 200


RW_LOGIN = "SDMT_rw_only"
RW_PASSWORD = "Rw-only-2026!x"

USE_THE_TOOL = r'''
import json, warnings
warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from sdg.api.app import app
import pyodbc
from sdg.config import get_settings
s = get_settings()
with pyodbc.connect(s.store.connection_string(s.denorm_db)) as conn:
    who = conn.execute("SELECT SUSER_SNAME(), IS_MEMBER('db_owner'), "
                       "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')").fetchone()
import os
c = TestClient(app, headers={"X-SDM-Token": os.environ["SDG_API_TOKEN"]})
out = {"login": [0, str(who[0])], "owner": [0, str(who[1])], "create": [0, str(who[2])]}
for route in app.routes:
    path = getattr(route, "path", "")
    if ("GET" in getattr(route, "methods", ()) and path.startswith("/api")
            and "{" not in path and path != "/api/docs"):
        r = c.get(path)
        out["GET " + path] = [r.status_code, r.text[:300]]
def post(name, path, body, method="post"):
    r = getattr(c, method)(path, json=body)
    out[name] = [r.status_code, r.text[:300]]
    return r.json() if r.status_code == 200 else {}
members = post("analyze members", "/api/patterns/analyze", {"filters": {}, "k": 2})
claims = post("analyze claims", "/api/claims/analyze", {"k": 2})
chosen = [f["key"] for f in c.get("/api/discovery-features").json()["members"] if f["chosen"]]
post("save features", "/api/discovery-features", {"entity": "members", "features": chosen},
     method="put")
counted = [f["key"] for f in c.get("/api/report-fields").json()["members"] if f["builtin"]]
post("save counted fields", "/api/report-fields", {"entity": "members", "fields": counted},
     method="put")
if members.get("patterns"):
    post("count a pattern", "/api/patterns/report", {"domain": "members", "filters": {}, "k": 2,
         "cluster_id": members["patterns"][0]["cluster_id"]})
r = c.post("/api/patterns/report.xlsx", json={"domain": "members", "filters": {}, "k": 2})
out["counts sheet"] = [r.status_code, "" if r.status_code == 200 else r.text[:300]]
if members.get("patterns"):
    post("keep member pattern", "/api/patterns/keep", {"domain": "members", "filters": {}, "k": 2,
         "cluster_id": members["patterns"][0]["cluster_id"], "name": "SDMT rw member"})
if claims.get("patterns"):
    post("keep claim pattern", "/api/patterns/keep", {"domain": "claims", "filters": {}, "k": 2,
         "cluster_id": claims["patterns"][0]["cluster_id"], "name": "SDMT rw claim"})
post("save rule", "/api/rules", {"field_name": "SDMT_RW_RULE", "gen_method": "constant", "domain": "X"})
post("try rule", "/api/rules/sample", {"gen_method": "constant", "domain": "X"})
post("save layout", "/api/layouts/save", {"name": "SDMT rw layout", "kind": "csv", "delimiter": ",",
     "fields": [{"name": "MEMBER_ID"}], "purpose": "generation"})
print(json.dumps(out))
'''


def test_a_login_that_only_reads_and_writes_uses_the_whole_tool(customer):
    """At the customer the tables are created by a scheduled job under an
    account that may create them; the people using the screens have only
    read and write. Nothing the screens do may need more."""
    from sdg.config import get_settings

    from tests import facets_dd

    admin = get_settings().mssql
    with pyodbc.connect(admin.connection_string("master"), autocommit=True) as conn:
        conn.execute(f"IF SUSER_ID(N'{RW_LOGIN}') IS NOT NULL DROP LOGIN [{RW_LOGIN}]")
        conn.execute(f"CREATE LOGIN [{RW_LOGIN}] WITH PASSWORD = N'{RW_PASSWORD}', "
                     "CHECK_POLICY = OFF")
    with pyodbc.connect(admin.connection_string(facets_dd.SDB), autocommit=True) as conn:
        conn.execute(f"CREATE USER [{RW_LOGIN}] FOR LOGIN [{RW_LOGIN}]")
        conn.execute(f"ALTER ROLE db_datareader ADD MEMBER [{RW_LOGIN}]")
        conn.execute(f"ALTER ROLE db_datawriter ADD MEMBER [{RW_LOGIN}]")
    try:
        # Both connections as that login: it may not even read FACETS.
        env = dict(_env(customer), SDG_MSSQL_USER=RW_LOGIN, SDG_MSSQL_PASSWORD=RW_PASSWORD,
                   SDG_DENORM_USER=RW_LOGIN, SDG_DENORM_PASSWORD=RW_PASSWORD,
                   SDG_MSSQL_TRUSTED="", SDG_DENORM_TRUSTED="")
        result = subprocess.run([sys.executable, "-c", USE_THE_TOOL], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=1800)
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
        out = json.loads(result.stdout.strip().splitlines()[-1])
        assert out.pop("login")[1] == RW_LOGIN
        assert out.pop("owner")[1] == "0" and out.pop("create")[1] == "0", "may not create"
        refused = {k: v for k, v in out.items()
                   if any(word in v[1].lower() for word in
                          ("permission", "denied", "login failed", "cannot open database"))}
        assert not refused, refused
        broken = {k: v for k, v in out.items() if v[0] >= 500}
        assert not broken, broken
        for step in ("analyze members", "analyze claims", "save features",
                     "save counted fields", "count a pattern", "counts sheet",
                     "keep member pattern", "keep claim pattern", "save rule",
                     "try rule", "save layout"):
            assert out[step][0] == 200, (step, out[step])

        # The extract too, once the login may read FACETS: it cannot truncate,
        # so it deletes, and the log setup created takes its rows.
        with pyodbc.connect(admin.connection_string(facets_dd.FDB), autocommit=True) as conn:
            conn.execute(f"CREATE USER [{RW_LOGIN}] FOR LOGIN [{RW_LOGIN}]")
            conn.execute(f"ALTER ROLE db_datareader ADD MEMBER [{RW_LOGIN}]")
        before = _counts()
        extract = subprocess.run(
            [sys.executable, "-m", "sdg.cli.main", "extract", "FACETS", "--keep-going"],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800)
        assert extract.returncode == 0, extract.stdout[-3000:] + extract.stderr[-3000:]
        after = _counts()
        assert after["member_denorm"] == before["member_denorm"] > 0
        assert after["extract_log"] == before["extract_log"] + 2 + TABLES_03
    finally:
        with pyodbc.connect(admin.connection_string(facets_dd.FDB), autocommit=True) as conn:
            conn.execute(f"IF USER_ID(N'{RW_LOGIN}') IS NOT NULL DROP USER [{RW_LOGIN}]")
        with pyodbc.connect(admin.connection_string(facets_dd.SDB), autocommit=True) as conn:
            conn.execute(f"IF USER_ID(N'{RW_LOGIN}') IS NOT NULL DROP USER [{RW_LOGIN}]")
        with pyodbc.connect(admin.connection_string("master"), autocommit=True) as conn:
            conn.execute(f"IF SUSER_ID(N'{RW_LOGIN}') IS NOT NULL DROP LOGIN [{RW_LOGIN}]")


def test_a_table_that_fails_says_so_and_never_0_rows(customer):
    """With --keep-going a failed table must be reported as FAILED with its
    reason the moment it fails, not as '0 rows -- done'."""
    from sdg.config import get_settings

    from tests import facets_dd

    admin = get_settings().mssql
    with pyodbc.connect(admin.connection_string(facets_dd.FDB), autocommit=True) as conn:
        conn.execute("EXEC sp_rename 'dbo.CMC_PYPY_PAYOR.PYPY_NAIC_CODE', 'PYPY_NAIC_GONE', 'COLUMN'")
    try:
        run = _cli(customer, "extract", "FACETS", "--keep-going", "--table", "plan_denorm")
        assert run.returncode == 1, run.stdout[-2000:]
        line = next(x for x in run.stdout.splitlines() if "plan_denorm:" in x)
        assert "FAILED" in line and "0 rows" not in line, line
        assert "PYPY_NAIC_CODE" in run.stdout, run.stdout[-2000:]
        assert "source database" in line and "upgrade" not in line.lower(), line
    finally:
        with pyodbc.connect(admin.connection_string(facets_dd.FDB), autocommit=True) as conn:
            conn.execute("EXEC sp_rename 'dbo.CMC_PYPY_PAYOR.PYPY_NAIC_GONE', 'PYPY_NAIC_CODE', 'COLUMN'")

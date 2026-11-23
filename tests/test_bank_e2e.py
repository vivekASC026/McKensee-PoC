"""A bank, end to end: SDM with SDG_PACK=banking and no other change.

The sample bank (seed/bank.py, about 2,000 customers) is the source, in a
database of its own; a second, empty one is the SDM platform database,
set up as a customer's is (customer-tables). The tool runs in a process of
its own under the banking pack and does what the screens do: connect,
discover, profile, map, plan -- with the pack's table modes -- then writes
a customers file and a transactions file (CSV), a fixed-width file through
a saved layout, writes each again from its run's record alone (the same
bytes), loads the plan's run into the target, checks it and removes it.
Then pattern analysis (phase 6b): the bank is read into its store through the
pack's extract, its customers and accounts are analyzed, one of each is kept
and listed, its rows are looked at, a walk and a story are read -- and no
answer says a health word. Writing a file from a pattern is refused by name.
Only databases named SDMT_BANK_* are touched; both are dropped afterwards.
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
SOURCE, STORE = "SDMT_BANK_SRC", "SDMT_BANK_STORE"

SCRIPT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from tests import conftest  # noqa: F401  the suite's client: this machine's name, the token
from fastapi.testclient import TestClient
from sdg.api.app import app

c = TestClient(app)
out = {}

def call(method, path, **kw):
    r = getattr(c, method)(path, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"{method.upper()} {path}: {r.status_code} {r.text[:400]}")
    return r

out["pack"] = call("get", "/api/pack").json()["id"]
for step in ("connect", "discover", "profile", "map"):
    call("post", f"/api/{step}")
plan = call("post", "/api/plan").json()
out["modes"] = {t["name"]: t["mode"] for t in plan["tables"]}
started = "2026-11-09T10:00:00"
files = {}
for subject in ("customers", "accounts"):
    files[subject] = call("post", "/api/export", json={
        "format": "csv", "subject": subject, "record": True, "request_started": started,
        "rows": 200}).content
layout = call("post", "/api/layouts/save", json={
    "name": "Bank customers, fixed", "kind": "fixed", "delimiter": None, "purpose": "generation",
    "fields": [{"name": "CUSTOMER_ID", "type": "int", "length": 8},
               {"name": "LAST_NAME", "type": "text", "length": 20},
               {"name": "DOB", "type": "date", "length": 8, "date_format": "YYYYMMDD"},
               {"name": "ADDRESS_STATE", "type": "text", "length": 2}]}).json()
layout_id = next(row["id"] for row in call("get", "/api/layouts").json()["layouts"]
                 if row["name"] == layout["name"])
files["fixed"] = call("post", "/api/export", json={
    "format": "fixed", "subject": "customers", "layout_id": layout_id, "record": True,
    "request_started": started, "rows": 200}).content
out["texts"] = {k: v.decode("utf-8") for k, v in files.items()}
out["heads"] = {k: v.decode("utf-8").splitlines()[:3] for k, v in files.items()}
out["lines"] = {k: len(v.decode("utf-8").splitlines()) for k, v in files.items()}
kept = [f for q in call("get", "/api/generation-requests").json()["requests"] for f in q["files"]]
out["again"] = {}
for f in kept:
    name = "fixed" if f["format"] == "fixed" else f["subject"]
    out["again"][name] = call("post", f"/api/generation-requests/files/{f['id']}/again").content == files[name]
# Another request, started at another time: a file of the plan's run keeps
# the plan's seed (the people a database load writes), so the same bytes.
out["other_start_same"] = call("post", "/api/export", json={
    "format": "csv", "subject": "customers", "record": False,
    "request_started": "2026-11-09T11:00:00", "rows": 200}).content == files["customers"]
out["generated"] = {t["table"]: t["rows_written"] for t in call("post", "/api/generate").json()["tables"]}
check = call("post", "/api/validate").json()
out["validation"] = {"checks": check["checks"], "failed": check["failed"]}
out["removed"] = call("post", "/api/cleanup").json()["deleted"]

# Pattern analysis: the bank read into its store (the pack's extract), its
# customers and accounts analyzed, one of each kept, listed, looked inside.
from sdg.extract.run import extract
out["extracted"] = {t.table: t.rows for t in extract("BANK").tables}
said = []  # every word the analysis answers in
def heard(body):
    if isinstance(body, str):
        said.append(body)
    elif isinstance(body, dict):
        for v in body.values():
            heard(v)
    elif isinstance(body, list):
        for v in body:
            heard(v)
    return body
heard(call("get", "/api/entities/customers/overview").json())
heard(call("get", "/api/scope/branches?entity=accounts").json())
customers = heard(call("post", "/api/entities/customers/analyze", json={"filters": {}, "k": None}).json())
accounts = heard(call("post", "/api/entities/accounts/analyze", json={"branch_id": [1]}).json())
out["customer_patterns"] = [[p["name"], p["size"]] for p in customers["patterns"]]
out["account_patterns"] = [[p["name"], p["n_claims"]] for p in accounts["patterns"]]
out["account_top"] = [sorted(p.get("top_merchant_categories") or {}) for p in accounts["patterns"]]
out["scope_labels"] = [customers["scope_label"], accounts["scope_label"]]
kept_customer = call("post", "/api/patterns/keep", json={
    "domain": "customers", "filters": {}, "k": None,
    "cluster_id": customers["patterns"][0]["cluster_id"], "name": "Bank customers"}).json()
kept_account = call("post", "/api/patterns/keep", json={
    "domain": "accounts", "filters": {"branch_id": [1]}, "k": None,
    "cluster_id": accounts["patterns"][0]["cluster_id"]}).json()
saved = heard(call("get", "/api/discovery/saved").json())["patterns"]
out["saved"] = {str(p["pattern_id"]): p["entity"] for p in saved}
out["kept"] = [kept_customer["pattern_id"], kept_account["pattern_id"]]
rows = heard(call("post", "/api/entities/accounts/rows", json={
    "branch_id": [1], "cluster_id": accounts["patterns"][0]["cluster_id"], "limit": 5}).json())
out["rows"] = [len(rows["rows"]), rows["name"]]
found = heard(call("get", "/api/discovery?entity=customers&max_depth=1").json())
out["discovered"] = found["n_discovered"]
story = heard(call("post", "/api/story/read", json={
    "story": "Affluent customers in Ohio aged 40 to 60"}).json())
out["story"] = {"entity": story["entity"], "rules": story["rules"]}
report = heard(call("post", "/api/patterns/report", json={
    "domain": "accounts", "filters": {"branch_id": [1]},
    "cluster_id": accounts["patterns"][0]["cluster_id"]}).json())
out["report"] = {"fields": sorted({r["field"] for r in report["rows"]}),
                 "bases": sorted({r["basis"] for r in report["rows"]}),
                 "size_basis": report["size_basis"]}
# The baseline the weights are read against, walked over the bank's scope.
out["baseline"] = {}
for entity in ("customers", "accounts"):
    estimate = heard(call("get", f"/api/baseline/estimate?entity={entity}&max_depth=1").json())
    computed = heard(call("post", "/api/baseline/compute",
                          json={"entity": entity, "max_depth": 1}).json())
    stored = heard(call("get", f"/api/baseline?entity={entity}").json())["baselines"]
    out["baseline"][entity] = {
        "estimated": estimate["combinations"], "walked": computed["combinations_processed"],
        "failed": computed["combinations_failed"], "patterns": computed["total_patterns"],
        "keys": sorted({str(b["scope_key"]).split(":")[0] for b in stored})}
out["said"] = said

# Files and plans from the bank's patterns (phase 6c): the rules seeded from
# the plan, the store described, then a customer pattern found in one branch
# written to a file through a layout, an account pattern's transactions for
# its own customers, each written again from its request alone, and a
# pattern loaded into the plan.
call("post", "/api/rules/seed")
from sdg.api.app import service
from sdg.landscape.derive import derive
shape = derive(source="BANK", catalog=service.catalog, registry=service.registry)
(service.work / "landscape-BANK.json").write_text(shape.model_dump_json(), encoding="utf-8")
out["line_elements"] = sorted(shape.line_elements)
branch = call("post", "/api/entities/customers/analyze",
              json={"filters": {"branch_id": [3]}, "k": None}).json()
kept_branch = call("post", "/api/patterns/keep", json={
    "domain": "customers", "filters": {"branch_id": [3]}, "k": None,
    "cluster_id": branch["patterns"][0]["cluster_id"], "name": "Louisville customers"}).json()
out["branch_age"] = branch["patterns"][0]["continuous"]["_age"]
pattern_layout = call("post", "/api/layouts/save", json={
    "name": "Bank pattern customers", "kind": "csv", "delimiter": ",", "purpose": "generation",
    "fields": [{"name": n} for n in ("CUSTOMER_ID", "FIRST_NAME", "LAST_NAME", "DOB", "SEGMENT",
                                     "BRANCH_ID", "ADDRESS_STATE")]}).json()
pattern_layout_id = next(row["id"] for row in call("get", "/api/layouts").json()["layouts"]
                         if row["name"] == pattern_layout["name"])
later = "2026-11-09T12:00:00"
customers_file = call("post", "/api/export", json={
    "format": "csv", "subject": "customers", "pattern_id": kept_branch["pattern_id"],
    "members": 40, "layout_id": pattern_layout_id, "record": True,
    "request_started": later})
customers_request = int(customers_file.headers["X-SDM-Request"])
from_patterns = {
    "customers": customers_file.content,
    "accounts": call("post", "/api/export", json={
        "format": "csv", "subject": "accounts", "pattern_id": kept_account["pattern_id"],
        "rows": 80, "record": True, "request_started": later}).content,
}
out["pattern_files"] = {k: v.decode("utf-8") for k, v in from_patterns.items()}
out["pattern_again"] = {}
for q in call("get", "/api/generation-requests").json()["requests"]:
    if q.get("pattern_id") not in (kept_branch["pattern_id"], kept_account["pattern_id"]):
        continue
    for f in q["files"]:
        again = call("post", f"/api/generation-requests/files/{f['id']}/again").content
        out["pattern_again"][f["subject"]] = again == from_patterns.get(f["subject"])
# Accounts for the customers of that file (a request's own people), and the
# check of the pattern against them.
for_them = call("post", "/api/export", json={
    "format": "csv", "subject": "accounts", "pattern_id": kept_account["pattern_id"],
    "of_request": customers_request, "rows": 60, "request_started": later}).content.decode("utf-8")
out["accounts_for_request"] = for_them
out["checked"] = heard(call("post", f"/api/generation-requests/{customers_request}/claims-check",
                            json={"pattern_id": kept_account["pattern_id"]}).json())
heard(for_them)
shape_of = heard(call("get", f"/api/patterns/{kept_account['pattern_id']}/claim-shape").json())
out["claim_shape"] = {"customers": shape_of["members"], "accounts": shape_of["claims"]}
out["planned"] = heard(call("post", "/api/story/to-plan", json={
    "pattern_ids": [kept_branch["pattern_id"]], "total_volume": 100, "apply": False}).json())
# Loaded into the plan as the screen's Load to database does: the pattern,
# the bank's word for its records, applied, and the plan read back.
loaded = heard(call("post", "/api/patterns/to-plan", json={
    "pattern": branch["patterns"][0], "kind": "customer", "as_of": "2026-06-30",
    "apply": True}).json())
out["loaded"] = {
    "subject": loaded["resolution"]["subject"],
    "elements": sorted({t["element"] for t in loaded["resolution"]["targets"] if t["element"]}),
    "applied": len(loaded["applied"]),
}
heard(out["pattern_files"]["customers"])
heard(out["pattern_files"]["accounts"])
out["said"] = said
print("RESULT " + json.dumps(out))
'''


def _drop(database: str) -> None:
    from sdg.config import get_settings

    assert database.startswith("SDMT_BANK_")
    with pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True) as m:
        m.execute(f"IF DB_ID(N'{database}') IS NOT NULL BEGIN ALTER DATABASE [{database}] "
                  f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{database}]; END")


@pytest.fixture(scope="module")
def bank():
    from sdg.config import get_settings

    from seed import bank as sample

    counts = sample.build(SOURCE)
    _drop(STORE)
    with pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True) as m:
        m.execute(f"CREATE DATABASE [{STORE}]")
    work = Path(tempfile.mkdtemp(prefix="sdm-bank-"))
    env = dict(os.environ, SDG_PACK="banking", SDG_DB_SOURCE=SOURCE, SDG_DB_REF=SOURCE,
               SDG_DB_TARGET=SOURCE, SDG_DB_DENORM=STORE, SDG_DENORM_SCHEMA="sdm",
               SDG_WORK_DIR=str(work))
    try:
        tables = subprocess.run([sys.executable, "-m", "sdg.cli.main", "customer-tables"],
                                cwd=ROOT, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=900)
        assert tables.returncode == 0, tables.stdout + tables.stderr
        done = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], cwd=ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800)
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")), None)
        assert line, done.stdout[-3000:] + done.stderr[-3000:]
        yield counts, json.loads(line[len("RESULT "):])
    finally:
        shutil.rmtree(work, ignore_errors=True)
        _drop(SOURCE)
        _drop(STORE)


def test_the_sample_bank_is_built(bank):
    counts, _ = bank
    assert counts["CUSTOMER"] == 2000
    assert counts["ACCOUNT"] > 4000 and counts["TXN"] > 40000
    assert {"BRANCH", "PRODUCT", "MCC", "GL_ENTRY", "AUDIT_LOG"} <= set(counts)


def test_the_plan_takes_the_banks_table_modes(bank):
    _, out = bank
    assert out["pack"] == "banking"
    assert out["modes"] == {
        "dbo.BRANCH": "reuse", "dbo.PRODUCT": "reuse", "dbo.MCC": "reuse",
        "dbo.GL_ENTRY": "reuse", "dbo.AUDIT_LOG": "exclude",
        "dbo.CUSTOMER": "generate", "dbo.ACCOUNT": "generate", "dbo.TXN": "generate"}


def test_files_are_written_in_the_banks_words(bank):
    _, out = bank
    assert out["heads"]["customers"][0] == (
        "CUSTOMER_ID,FIRST_NAME,LAST_NAME,DOB,SEGMENT,ADDRESS_CITY,ADDRESS_STATE,ADDRESS_ZIP")
    assert out["heads"]["accounts"][0] == (
        "ACCOUNT_ID,TXN_SEQ,CUSTOMER_ID,PRODUCT_CODE,TXN_DATE,TXN_TYPE,CHANNEL,AMOUNT")
    # Every transaction names a product the bank has.
    codes = {row.split(",")[3] for row in out["heads"]["accounts"][1:]}
    assert codes and codes <= {"CHK01", "CHK02", "SAV01", "SAV02", "CRD01", "CRD02", "LON01", "LON02"}
    assert out["lines"]["customers"] == 201 and out["lines"]["accounts"] == 201
    fixed = out["heads"]["fixed"]
    assert out["lines"]["fixed"] == 200 and all(len(row) == 38 for row in fixed), fixed


def test_the_fixed_width_fields_sit_where_the_layout_puts_them(bank):
    _, out = bank
    rows = out["texts"]["fixed"].splitlines()
    assert len(rows) == 200
    from datetime import datetime

    for row in rows:
        customer, last, born, state = row[0:8], row[8:28], row[28:36], row[36:38]
        assert customer.strip().isdigit(), row
        assert last.strip() and not any(ch.isdigit() for ch in last), row
        assert 1900 < datetime.strptime(born, "%Y%m%d").year < 2010, row
        assert len(state) == 2 and state.isalpha() and state.isupper(), row


def test_no_file_says_a_health_word(bank):
    """The whole of every file, not only its header."""
    import re

    _, out = bank
    health = re.compile(r"\b(members?|claims?|subscribers?|diagnos\w*|providers?|834|837[PID]?|"
                        r"MEME_\w+|SBSB_\w+|CLCL_\w+|FACETS)\b", re.I)
    for name, text in out["texts"].items():
        found = sorted({m.group(0) for m in health.finditer(text)})
        assert not found, (name, found)


def test_every_file_is_written_again_the_same(bank):
    _, out = bank
    assert out["again"] == {"customers": True, "accounts": True, "fixed": True}


def test_a_file_of_the_plans_run_is_the_same_whenever_it_is_asked_for(bank):
    """The plan's seed, not the request's time: the file describes the people
    a database load of the plan writes (app._request_seed)."""
    _, out = bank
    assert out["other_start_same"] is True


def test_the_run_loads_checks_and_is_removed(bank):
    _, out = bank
    assert out["generated"]["dbo.CUSTOMER"] == 2000
    assert out["generated"]["dbo.ACCOUNT"] > 4000 and out["generated"]["dbo.TXN"] > 40000
    assert not {"dbo.BRANCH", "dbo.PRODUCT", "dbo.MCC", "dbo.GL_ENTRY", "dbo.AUDIT_LOG"} & set(
        out["generated"])
    assert out["validation"]["failed"] == 0 and out["validation"]["checks"] > 30
    assert out["removed"] == out["generated"]


def test_the_bank_is_read_into_its_store(bank):
    counts, out = bank
    assert out["extracted"] == {"customer_denorm": counts["CUSTOMER"],
                                "account_denorm": counts["TXN"]}


def test_customers_and_accounts_are_analyzed_into_patterns(bank):
    _, out = bank
    assert len(out["customer_patterns"]) >= 2 and len(out["account_patterns"]) >= 2
    assert sum(size for _, size in out["customer_patterns"]) == 2000
    assert out["scope_labels"] == ["all customers", "all accounts"]
    # Named in the bank's words: an age band, a product type.
    assert all(name.split("-")[0] in {"Young", "EarlyCareer", "Established", "Retired"}
               for name, _ in out["customer_patterns"]), out["customer_patterns"]
    assert all(name.split(" ")[0] in {"Checking", "Savings", "Card", "Loan", "Established",
                                        "Young", "Early", "Retired"}
               for name, _ in out["account_patterns"]), out["account_patterns"]


def test_a_kept_pattern_is_listed_as_the_records_it_describes(bank):
    _, out = bank
    customer, account = (str(i) for i in out["kept"])
    assert out["saved"][customer] == "customers" and out["saved"][account] == "accounts"
    assert out["rows"][0] == 5 and not out["rows"][1].startswith("Claim")
    assert out["discovered"] >= 6


def test_a_pattern_is_reported_on_the_banks_fields(bank):
    _, out = bank
    assert out["report"] == {
        "fields": ["age_band", "amount", "branch", "channel", "product", "product_type",
                   "segment", "transaction_type"],
        "bases": ["accounts", "transactions"], "size_basis": "accounts"}


def test_the_baseline_is_walked_over_the_banks_branches(bank):
    _, out = bank
    for entity in ("customers", "accounts"):
        walked = out["baseline"][entity]
        # The unfiltered book and each of the six branches.
        assert walked["estimated"] == 7 and walked["failed"] == 0, (entity, walked)
        assert walked["walked"] == 7 and walked["patterns"] >= 7, (entity, walked)
        assert walked["keys"] == ["ALL", "BRANCH"], (entity, walked)


def test_a_story_is_read_in_the_banks_words(bank):
    _, out = bank
    assert out["story"] == {"entity": "customers", "rules": {
        "segment": {"in": ["AFFLUENT"]}, "state": {"in": ["OH"]},
        "age": {"min": 40.0, "max": 60.0}}}


def test_no_answer_of_the_analysis_says_a_health_word(bank):
    import re

    _, out = bank
    health = re.compile(r"\b(members?|claims?|subscribers?|diagnos\w*|providers?|"
                        r"MEME_\w+|CLCL_\w+|FACETS)\b", re.I)
    found = sorted({m.group(0) for text in out["said"] for m in health.finditer(text)})
    assert not found, found


def test_the_store_says_which_elements_vary_by_transaction(bank):
    """Grouped by the account (the pack's anchor), the elements a transaction
    carries apart from its account."""
    _, out = bank
    assert {"TXN_TYPE", "CHANNEL", "AMOUNT", "TXN_DATE"} <= set(out["line_elements"]), \
        out["line_elements"]
    # What an account (or its customer) carries once is not a line's.
    assert not {"ACCOUNT_ID", "CUSTOMER_ID", "PRODUCT_CODE", "OPEN_DATE"} & set(
        out["line_elements"]), out["line_elements"]


def _rows(text: str) -> list[dict[str, str]]:
    import csv
    import io

    return list(csv.DictReader(io.StringIO(text)))


def test_a_customer_file_from_a_pattern_keeps_its_branch_and_ages(bank):
    _, out = bank
    rows = _rows(out["pattern_files"]["customers"])
    assert len(rows) == 40
    assert list(rows[0]) == ["CUSTOMER_ID", "FIRST_NAME", "LAST_NAME", "DOB", "SEGMENT",
                             "BRANCH_ID", "ADDRESS_STATE"]
    assert all(r["CUSTOMER_ID"] and r["FIRST_NAME"] and r["LAST_NAME"] for r in rows), rows[:3]
    assert len({r["CUSTOMER_ID"] for r in rows}) == 40
    # Found in Louisville Main: every customer written there.
    assert {r["BRANCH_ID"] for r in rows} == {"3"}, {r["BRANCH_ID"] for r in rows}
    # Born within the pattern's ages, as of the file's date.
    age = out["branch_age"]
    years = [2026 - int(r["DOB"][:4]) for r in rows]
    assert min(years) >= int(age["min"]) - 1 and max(years) <= int(age["max"]) + 2, (years, age)


def test_an_account_file_from_a_pattern_is_for_its_own_customers(bank):
    _, out = bank
    rows = _rows(out["pattern_files"]["accounts"])
    assert len(rows) == 80
    # The bank's columns, not a claim's.
    assert not {"CLAIM_ID", "LINE_NUMBER"} & set(rows[0]), list(rows[0])
    by_account: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_account.setdefault(r["ACCOUNT_ID"], []).append(r)
    # Each account's transactions numbered from 1, its own customer on each.
    for account, lines in by_account.items():
        assert [int(r["TXN_SEQ"]) for r in lines] == list(range(1, len(lines) + 1)), account
        assert len({r["CUSTOMER_ID"] for r in lines}) == 1, account
        # What an account carries once is the same on each of its transactions.
        assert len({r["PRODUCT_CODE"] for r in lines}) == 1, (account, lines)
    assert len(by_account) < len(rows), "every transaction made an account of its own"
    # The customers are the pattern's own, read from the store.
    assert all(r["CUSTOMER_ID"].isdigit() and 100000 < int(r["CUSTOMER_ID"]) < 102001
               for r in rows)
    # A transaction's own elements vary within an account.
    assert any(len({(r["TXN_TYPE"], r["AMOUNT"]) for r in lines}) > 1
               for lines in by_account.values() if len(lines) > 1)


def test_a_file_from_a_pattern_is_written_again_the_same(bank):
    _, out = bank
    # Each first file held rows, not only a header: the same bytes mean something.
    assert all(len(text.splitlines()) > 10 for text in out["pattern_files"].values())
    assert out["pattern_again"] == {"customers": True, "accounts": True}, out["pattern_again"]


def test_an_account_patterns_shape_is_measured_by_the_banks_keys(bank):
    _, out = bank
    assert out["claim_shape"]["customers"] > 0 and out["claim_shape"]["accounts"] > 0


def test_a_requirement_s_patterns_are_previewed_in_the_plan(bank):
    _, out = bank
    planned = out["planned"]
    assert planned.get("patterns"), planned
    assert any(p.get("targets") for p in planned["patterns"]), planned


def test_a_pattern_is_loaded_into_the_plan_as_the_screen_loads_it(bank):
    """The bank's word for its records is a kind the plan knows; the age and
    the tenure land on the bank's own dates; the rules are written."""
    _, out = bank
    loaded = out["loaded"]
    assert loaded["subject"] == "dbo.CUSTOMER", loaded
    assert {"DOB", "CUSTOMER_SINCE"} <= set(loaded["elements"]), loaded
    assert loaded["applied"] > 0, loaded


def test_accounts_are_written_for_an_earlier_files_customers(bank):
    """The child records for a request's own people: every account is one of
    the customers that file wrote, and the check of the pattern against them
    answers in the bank's words."""
    _, out = bank
    customers = {r["CUSTOMER_ID"] for r in _rows(out["pattern_files"]["customers"])}
    rows = _rows(out["accounts_for_request"])
    assert rows and {r["CUSTOMER_ID"] for r in rows} <= customers, (
        sorted({r["CUSTOMER_ID"] for r in rows} - customers)[:5])
    assert isinstance(out["checked"], dict) and "detail" not in out["checked"], out["checked"]


def test_an_account_pattern_keeps_its_most_frequent_merchant_codes(bank):
    """The account profile's top codes (child_analysis.shown_top), kept for
    the screens' chart and for the rules a file is drawn with."""
    _, out = bank
    codes = {c for top in out["account_top"] for c in top}
    assert codes and all(c.isdigit() and len(c) == 4 for c in codes), out["account_top"]

"""The screens under the banking pack say nothing of health insurance.

The sample bank (seed/bank.py) is read and planned as Database setup does,
in a working folder of its own, and the app is served under SDG_PACK=banking.
Every screen and tab is opened, patterns are found, kept and looked inside
(phase 6b: a requirement in the bank's words, a pattern defined by its rules,
a scan of the accounts, the saved list), a kept pattern is written to a file
(phase 6c), and the plan's run is written to a file from the Generate
screen: no screen or file says member, claim, subscriber, diagnosis,
provider, 834, 837, X12, FHIR, HL7 or FACETS, and none shows a console error
or a failed request. Only databases named SDMT_BANK_UI_* are touched; both
are dropped afterwards.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pyodbc
import pytest

from tests.test_browser import _free_port

pytestmark = [pytest.mark.db, pytest.mark.slow, pytest.mark.browser]

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
SOURCE, STORE = "SDMT_BANK_UI_SRC", "SDMT_BANK_UI_STORE"
HEALTH = re.compile(r"\b(members?|claims?|subscribers?|diagnos\w*|providers?|834|837\w*|X12|"
                    r"FHIR|HL7|FACETS|CMC_\w+)\b", re.I)

PREPARE = r'''
import sys
sys.path.insert(0, sys.argv[1])
from tests import conftest  # noqa: F401
from fastapi.testclient import TestClient
from sdg.api.app import app

c = TestClient(app)
for step in ("connect", "discover", "profile", "map", "plan"):
    r = c.post(f"/api/{step}")
    assert r.status_code < 400, (step, r.text[:300])
r = c.post("/api/layouts/save", json={
    "name": "Customers", "kind": "csv", "delimiter": ",", "purpose": "generation",
    "fields": [{"name": "CUSTOMER_ID"}, {"name": "LAST_NAME"}, {"name": "SEGMENT"}]})
assert r.status_code < 400, r.text[:300]
# The bank read into its store, for pattern analysis, and described, with
# the rules seeded from the plan, for files from its patterns.
from sdg.extract.run import extract
extract("BANK")
r = c.post("/api/rules/seed")
assert r.status_code < 400, r.text[:300]
from sdg.api.app import service
from sdg.landscape.derive import derive
shape = derive(source="BANK", catalog=service.catalog, registry=service.registry)
(service.work / "landscape-BANK.json").write_text(shape.model_dump_json(), encoding="utf-8")
print("PREPARED")
'''


def _drop(database: str) -> None:
    from sdg.config import get_settings

    assert database.startswith("SDMT_BANK_UI_")
    with pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True) as m:
        m.execute(f"IF DB_ID(N'{database}') IS NOT NULL BEGIN ALTER DATABASE [{database}] "
                  f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{database}]; END")


@pytest.fixture(scope="module")
def bank_server():
    from sdg.config import get_settings

    from seed import bank as sample

    sample.build(SOURCE, customers=400)
    _drop(STORE)
    with pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True) as m:
        m.execute(f"CREATE DATABASE [{STORE}]")
    work = Path(tempfile.mkdtemp(prefix="sdm-bank-ui-"))
    env = dict(os.environ, SDG_PACK="banking", SDG_DB_SOURCE=SOURCE, SDG_DB_REF=SOURCE,
               SDG_DB_TARGET=SOURCE, SDG_DB_DENORM=STORE, SDG_DENORM_SCHEMA="sdm",
               SDG_WORK_DIR=str(work))
    proc = None
    try:
        for args in (["-m", "sdg.cli.main", "customer-tables"], ["-c", PREPARE, str(ROOT)]):
            done = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=900)
            assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(port),
             "--log-level", "warning"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}"
        for _ in range(240):
            try:
                urllib.request.urlopen(url + "/", timeout=2)
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("the server stopped while starting")
                time.sleep(0.5)
        yield url
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        _drop(SOURCE)
        _drop(STORE)


def test_no_screen_says_a_health_word(bank_server):
    from playwright.sync_api import sync_playwright

    trouble: list[str] = []
    said: dict[str, list[str]] = {}
    with sync_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome"):
            try:
                browser = p.chromium.launch(channel=channel, headless=True)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("neither Edge nor Chrome is installed")
        context = browser.new_context(accept_downloads=True,
                                      viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(120_000)
        page.on("console", lambda m: m.type == "error" and trouble.append(m.text))
        page.on("pageerror", lambda e: trouble.append(f"page error: {e}"))
        page.on("response", lambda r: "/api/" in r.url and r.status >= 400
                and trouble.append(f"{r.status} {r.url}"))
        page.goto(bank_server)
        main = page.get_by_role("navigation", name="Main")

        def read(where: str) -> None:
            page.wait_for_timeout(1500)
            text = page.locator("body").inner_text()
            found = sorted({m.group(0) for m in HEALTH.finditer(text)})
            if found:
                said[where] = found

        def settled() -> None:
            page.wait_for_function("() => !document.querySelector('.btn--busy')",
                                   timeout=300_000)
            page.wait_for_load_state("networkidle")

        def chip(name: str) -> None:
            page.locator(".find__chips").get_by_role("button", name=name, exact=True).click()
            page.wait_for_load_state("networkidle")

        # Pattern analysis (phase 6b): the app opens on Patterns. A requirement
        # in the bank's words is read into rules and analyzed at once.
        page.locator(".app__heading", has_text="Patterns").wait_for()
        page.get_by_label("Search or requirement").fill(
            "Affluent customers in Ohio aged 40 to 60")
        page.get_by_role("button", name="Find", exact=True).click()
        analysis = page.locator(".story__analysis")
        analysis.wait_for()
        rules = analysis.locator(".story__rules").inner_text()
        assert "Segment" in rules and "State" in rules and "Age" in rules, rules
        analysis.locator(".pattern__head").first.wait_for(timeout=300_000)
        settled()
        read("Patterns > requirement")
        # Kept, looked inside, and written to a file (phase 6c: a bank's
        # patterns are generated from too).
        card = analysis.locator("li.pattern").first
        if not card.get_by_role("button", name="Save pattern").count():
            card.locator(".pattern__head").click()
        card.get_by_role("button", name="Save pattern").click()
        card.get_by_role("button", name="Saved").wait_for()
        assert card.get_by_role("button", name="Load to database").count() == 1
        with page.expect_download(timeout=300_000) as from_pattern:
            card.get_by_role("button", name="Generate file").click()
            menu = page.get_by_role("menuitem")
            if menu.count():
                menu.first.click()
        pattern_file = Path(from_pattern.value.path()).read_text(encoding="utf-8")
        settled()
        read("Patterns > a file from a pattern")
        card.get_by_role("button", name="Show source records").click()
        card.locator(".rows__table").wait_for(timeout=120_000)
        read("Patterns > a pattern's records")
        card.get_by_role("button", name="Scenarios", exact=True).click()
        scenarios = card.locator(".scenarios")
        scenarios.get_by_role("button", name="List scenarios").click()
        scenarios.locator("tbody tr").first.wait_for(timeout=120_000)
        read("Patterns > a pattern's scenarios")

        # A pattern defined by its rules, measured.
        chip("Criteria")
        page.get_by_label("Pattern name").fill("SDMT affluent")
        page.get_by_label("Add field").select_option(label="Segment")
        page.get_by_role("group", name="Segment").get_by_label("AFFLUENT", exact=True).check()
        page.get_by_role("button", name="Measure pattern").click()
        page.locator(".patterns__summary").wait_for(timeout=300_000)
        settled()
        read("Patterns > criteria")

        # The accounts: a scan of the book, as deep as a bank's scope goes.
        chip("Accounts")
        chip("Full book scan")
        depths = page.locator(".discover__controls select option").all_inner_texts()
        assert depths == ["Branches", "Branches and products"], depths
        # The requirement still in the box would search the scan's list.
        page.get_by_label("Search or requirement").fill("")
        page.get_by_role("button", name="Run scan").click()
        page.locator(".discover__table tbody tr").first.wait_for(timeout=300_000)
        settled()
        read("Patterns > scan")
        chip("Customers")
        # "Saved (1)": the chip says how many.
        page.locator(".find__chips").get_by_role("button", name=re.compile(r"^Saved")).click()
        page.locator(".kept__row").first.wait_for(timeout=120_000)
        read("Patterns > saved")

        for screen in ("Generate", "History", "Layouts and samples", "Database setup"):
            main.get_by_role("button", name=screen).click()
            page.locator(".app__heading").wait_for()
            read(screen)
            for tab in page.locator(".app__body [role=tab]").all_inner_texts():
                page.locator(".app__body [role=tab]", has_text=tab).first.click()
                read(f"{screen} > {tab}")
        settings = page.get_by_role("navigation", name="Settings")
        tools = settings.get_by_role("button").all_inner_texts()
        assert "Pattern settings" in tools
        for tool in tools:
            settings.get_by_role("button", name=tool).click()
            read(f"Settings > {tool}")

        # A file from the plan's run, through the saved layout.
        main.get_by_role("button", name="Generate").click()
        page.locator(".app__body [role=tab]", has_text="Files").first.click()
        page.get_by_role("heading", name="File generation").wait_for()
        row = page.locator("tr", has_text="Customers")
        with page.expect_download() as download:
            row.get_by_role("button", name="Generate file").click()
            menu = page.get_by_role("menuitem")
            if menu.count():
                menu.first.click()
        text = Path(download.value.path()).read_text(encoding="utf-8")
        read("Generate after writing a file")
        browser.close()

    assert text.splitlines()[0] == "CUSTOMER_ID,LAST_NAME,SEGMENT"
    # The file from the kept pattern: the bank's customers, in the bank's words.
    lines = pattern_file.splitlines()
    assert lines[0].startswith("CUSTOMER_ID,") and len(lines) > 10, lines[:3]
    assert not HEALTH.search(pattern_file), HEALTH.search(pattern_file).group(0)
    assert len(text.splitlines()) > 100
    assert not said, said
    assert not trouble, trouble

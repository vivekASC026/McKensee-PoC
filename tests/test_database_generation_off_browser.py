"""With database generation turned off, the screens offer files only.

The app runs on a port of its own with SDG_DATABASE_GENERATION=no. Generate's
Database tab says the load is turned off and offers a file; a pattern offers a
file, and its Load to database is shown off with the reason; everything else
opens as usual.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_browser import LONG, _free_port, _go, _on_tab, _settled

pytestmark = [pytest.mark.db, pytest.mark.slow, pytest.mark.browser]

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def files_only_page():
    from playwright.sync_api import sync_playwright

    from sdg.config import get_settings

    work = Path(tempfile.mkdtemp(prefix="sdm-files-only-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, SDG_WORK_DIR=str(work), SDG_DATABASE_GENERATION="no"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(240):
            try:
                urllib.request.urlopen(url + "/api/state", timeout=2)
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("the server stopped while starting")
                time.sleep(0.5)
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
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.set_default_timeout(60_000)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            page.wait_for_load_state("networkidle")
            yield page
            browser.close()
            assert not errors, errors
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)


def test_generation_says_the_database_load_is_off_and_offers_a_file(files_only_page):
    page = files_only_page
    _go(page, "Generate", "Database")
    page.get_by_text("Load to database is off").wait_for()
    assert not page.get_by_role("button", name="Build plan").count()
    page.locator(".app__body").get_by_role("button", name="Generate file").click()
    page.wait_for_load_state("networkidle")
    _on_tab(page, "Files")


def test_reading_the_database_is_still_offered(files_only_page):
    page = files_only_page
    for tab in ("Connection", "Profile", "Map"):
        _go(page, "Database setup", tab)
        page.wait_for_timeout(300)
        assert not page.locator(".banner--alarm").count(), tab


def test_a_pattern_offers_a_file_and_no_database_load(files_only_page):
    page = files_only_page
    _go(page, "Patterns")
    page.locator(".find__chips").get_by_role("button", name="Criteria", exact=True).click()
    page.get_by_label("Pattern name").fill("SDMT files only")
    page.get_by_label("Add field").select_option(label="Gender")
    page.get_by_role("group", name="Gender").get_by_label("F", exact=True).check()
    page.get_by_role("button", name="Measure pattern").click()
    page.locator(".patterns__summary").wait_for(timeout=LONG)
    _settled(page)

    card = page.locator("li.pattern").first
    if not card.get_by_role("button", name="Generate file").count():
        card.locator(".pattern__head").click()
    card.get_by_role("button", name="Generate file").wait_for()
    # Load to database is shown, unavailable, with the reason.
    load = card.locator(".pattern__actions").get_by_role("button", name="Load to database")
    assert load.is_disabled()
    assert load.get_attribute("title") == "Load to database is off for this installation"

    card.get_by_role("button", name="Scenarios", exact=True).click()
    scenarios = card.locator(".scenarios")
    scenarios.get_by_role("button", name="List scenarios").click()
    scenarios.locator(".scenarios__table").wait_for(timeout=LONG)
    assert scenarios.get_by_role("button", name="as one file").count()
    assert scenarios.get_by_role("button", name="to database").is_disabled()

    card.get_by_role("button", name="Show source records").click()
    rows = card.locator(".rows__table tbody tr")
    rows.first.wait_for(timeout=LONG)
    rows.first.get_by_role("checkbox").check()
    card.get_by_role("button", name="Generate file").last.wait_for()
    for load in card.get_by_role("button", name="Load to database").all():
        assert load.is_disabled()

"""The screens under a pack that offers no health format.

The health pack with its format modules taken away (pattern analysis kept),
served on a port of its own over a copy of the working files: the
Generate screen shows no X12, FHIR, HL7 or custom-format tab and no hint
naming them, and the Patterns screen still shows its records' counts and
highlights, with no console error or failed request. Nothing is saved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_browser import ROOT, _free_port

pytestmark = [pytest.mark.db, pytest.mark.slow, pytest.mark.browser]

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def plain_server():
    from sdg.config import get_settings

    work = Path(tempfile.mkdtemp(prefix="sdm-plain-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="sdm-packs-"))
    shutil.copytree(ROOT / "sdg" / "packs" / "health", folder / "plain")
    body = json.loads((folder / "plain" / "pack.json").read_text(encoding="utf-8"))
    body.update(id="plain", name="Plain", modules=["pattern_analysis"],
                formats={"members": ["csv"], "claims": ["csv"]})
    (folder / "plain" / "pack.json").write_text(json.dumps(body), encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, SDG_WORK_DIR=str(work), SDG_PACKS_DIR=str(folder),
                           SDG_PACK="plain"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
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
        proc.terminate()
        proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(folder, ignore_errors=True)


def test_no_screen_offers_a_format_the_pack_does_not(plain_server):
    from playwright.sync_api import sync_playwright

    trouble: list[str] = []
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
        page.set_default_timeout(120_000)
        page.on("console", lambda m: m.type == "error" and trouble.append(m.text))
        page.on("response", lambda r: "/api/" in r.url and r.status >= 400
                and trouble.append(f"{r.status} {r.url}"))
        page.goto(plain_server)
        main = page.get_by_role("navigation", name="Main")

        # Patterns: the claims' counts and highlights, as the pack names them.
        main.get_by_role("button", name="Patterns").click()
        page.get_by_role("button", name="Claims", exact=True).first.click()
        scope = page.locator(".patterns__scope")
        scope.wait_for()
        assert "LINES" in scope.inner_text().upper()
        page.locator(".clinical h3").first.wait_for()
        assert [h.upper() for h in page.locator(".clinical h3").all_inner_texts()] == [
            "MOST COMMON DIAGNOSES", "MOST COMMON PROCEDURES"]

        # Generate: the flat file alone, with no tab row and no format named.
        main.get_by_role("button", name="Generate").click()
        page.get_by_role("heading", name="File generation").wait_for()
        page.get_by_role("button", name="New layout").wait_for()
        tabs = page.get_by_role("tab").all_inner_texts()
        for health in ("Flat file", "X12 EDI", "FHIR", "HL7 v2", "HL7 v3", "Custom format"):
            assert health not in tabs, tabs
        hint = page.get_by_text("A flat file: delimited or fixed width.")
        assert hint.count() == 1
        for word in ("X12", "FHIR", "HL7"):
            assert page.get_by_text(word).count() == 0, word
        browser.close()
    assert not trouble, trouble

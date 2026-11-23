"""The screens, driven in a real browser the way a person drives them.

A server of its own, on a copy of the suite's working files, opened in the
Edge or Chrome already installed on the machine (no browser is downloaded).
In order, as somebody would go:

1. Database setup names the database being worked on, and the pattern store
   the patterns come from;
2. every screen and tab opens without an error; a folder of files is
   connected and profiled; under Layouts and samples a sample read and a
   layout defined by hand each get a rule picked for every field, are edited,
   approved and written through;
3. a database is onboarded from Database setup, read, planned, written,
   validated and cleaned up, all from the screens;
4. a pattern is loaded into that database in one step, validated and deleted;
5. a saved pattern is listed under Generate's Files tab and writes an 834 that
   passes every SNIP level; Generate file on a pattern downloads at once, as
   a run History shows;
6. back on the source, its work is as it was, and the onboarded database is
   offboarded from Database setup.

No console error, failed request or alarm banner is allowed anywhere. The
onboarded database is a throwaway copy; patterns saved here are removed by
the ids the screens returned for them, and no others.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from tests import facets_copies
from tests.saved_cleanup import unrestore

pytestmark = [pytest.mark.db, pytest.mark.slow, pytest.mark.browser]

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
A = "SDM_TEST_BROWSER_A"
LONG = 600_000  # ms: reading a database or writing a run

# The server's API answers only calls carrying its token (sdg/api/guard.py):
# the page gets it from the server, and the suite's own reads send it here.
_opener = urllib.request.build_opener()
_opener.addheaders = [("X-SDM-Token", os.environ.get("SDG_API_TOKEN", ""))]
urllib.request.install_opener(_opener)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """The app on a port of its own, on a copy of the suite's working files."""
    from sdg.config import get_settings

    work = Path(tempfile.mkdtemp(prefix="sdm-browser-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True)
    # The log is kept outside work/: switching databases moves work/'s files,
    # and Windows will not move one a process holds open.
    log = open(str(work) + "-server.log", "w")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, SDG_WORK_DIR=str(work)),
        stdout=subprocess.DEVNULL, stderr=log,
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
        facets_copies.build(A)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        log.close()
        facets_copies.drop(A)
        shutil.rmtree(work, ignore_errors=True)
        Path(str(work) + "-server.log").unlink(missing_ok=True)


class Trouble:
    """What went wrong in the browser: console errors, failed requests."""

    def __init__(self):
        self.seen: list[str] = []
        self.kept: list[int] = []  # pattern ids the screens created
        self.saves: list[dict] = []  # every save the screens made
        self.aliases: list[str] = []  # names a layout save taught the rules
        self.requests: set[int] = set()  # generation requests its downloads were kept under

    def take(self) -> list[str]:
        seen, self.seen = self.seen, []
        return seen


@pytest.fixture(scope="module")
def browser_page(server):
    from playwright.sync_api import sync_playwright

    trouble = Trouble()
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
        page.set_default_timeout(60_000)
        page.on("console", lambda m: m.type == "error" and trouble.seen.append(m.text))
        page.on("pageerror", lambda e: trouble.seen.append(f"page error: {e}"))

        def on_response(response):
            if "/api/" not in response.url:
                return
            if response.status >= 500:
                trouble.seen.append(f"{response.status} from {response.url}")
            kept_under = response.headers.get("x-sdm-request")
            if kept_under and kept_under.isdigit():
                trouble.requests.add(int(kept_under))
            if response.url.endswith("/api/patterns/scenarios/keep") and response.ok:
                try:
                    body = response.json()
                except Exception:
                    return
                for entry in body.get("kept") or []:
                    trouble.saves.append(entry)
                    if entry.get("created"):
                        trouble.kept.append(int(entry["pattern_id"]))
                return
            if response.url.endswith("/api/patterns/keep") and response.ok:
                try:
                    body = response.json()
                except Exception:
                    return
                # A pattern somebody had already saved comes back as theirs,
                # created False: it is never this test's to delete.
                trouble.saves.append(body)
                if body.get("created"):
                    trouble.kept.append(int(body["pattern_id"]))

            if "/api/layouts" in response.url and response.ok \
                    and response.request.method in ("POST", "PUT"):
                try:
                    body = response.json()
                except Exception:
                    return
                if isinstance(body, dict):
                    trouble.aliases.extend(body.get("aliased") or [])

        page.on("response", on_response)
        page.goto(server)
        page.wait_for_load_state("networkidle")
        before = _newest_request()
        try:
            yield page, trouble
        finally:
            _forget(trouble.kept)
            unrestore(trouble.saves)
            _unteach(trouble.aliases)
            # Only requests this run opened: one a download joined that was
            # there before it began is somebody's own.
            _drop_requests(i for i in trouble.requests if i > before)
            browser.close()


@pytest.fixture
def page(browser_page):
    """The page, and after each test: nothing went wrong on it."""
    page, trouble = browser_page
    trouble.take()
    yield page
    problems = trouble.take()
    alarms = page.locator(".banner--alarm, .toast--alarm").all_inner_texts()
    assert not problems, f"the browser saw: {problems}"
    assert not alarms, f"the screen shows: {alarms}"


@pytest.fixture
def kept(browser_page):
    """Every save the screens made, whether it created a row or found one."""
    return browser_page[1].saves


def _forget(pattern_ids) -> None:
    if not pattern_ids:
        return
    from sdg.analyzer import db
    from sdg.analyzer.config import SQL_SCHEMA

    with db.get_connection() as conn:
        cur = conn.cursor()
        for pattern_id in set(pattern_ids):
            cur.execute(f"DELETE FROM {SQL_SCHEMA}.patterns WHERE id = ?", (pattern_id,))
        conn.commit()


def _patients(claims: str) -> list[tuple[str, str]]:
    """Each claim's patient in an 837P: the subscriber (NM1*IL), or for a
    dependent's claim the patient after PAT (NM1*QC)."""
    out, held = [], None
    for segment in claims.splitlines():
        parts = segment.rstrip("~").split("*")
        if parts[:2] in (["NM1", "IL"], ["NM1", "QC"]):
            held = (parts[3], parts[4])
        elif parts[0] == "CLM" and held is not None:
            out.append(held)
            held = None
    return out


def _newest_request() -> int:
    from sdg.analyzer.services import generation_requests as gr

    gr.ensure_tables()
    conn = gr._conn()
    try:
        return int(conn.execute(f"SELECT ISNULL(MAX(id), 0) FROM {gr.REQUESTS}").fetchone()[0])
    finally:
        conn.close()


def _drop_requests(request_ids) -> None:
    from sdg.analyzer.services import generation_requests as gr

    for request_id in set(request_ids):
        gr.delete_request(request_id)


def _unteach(aliases) -> None:
    """Remove the aliases the screens' layout saves reported creating."""
    from sdg.analyzer.services import layout_service

    for name in set(aliases):
        layout_service.remove_alias(name)


def _drop_layouts(names) -> None:
    """Remove the layouts this run saved, by the names it gave them."""
    from sdg.analyzer.services import layout_service

    for row in layout_service.list_layouts(include_archived=True):
        if row["name"] in names:
            layout_service.delete_layout(row["id"])


# ---------------------------------------------------------------------------
# Moving about
# ---------------------------------------------------------------------------

def _go(page, screen: str, tab: str | None = None) -> None:
    """An item on the rail, then one of its screen's own tabs where it has them."""
    page.locator(".rail__step", has_text=screen).first.click()
    page.wait_for_load_state("networkidle")
    if tab is not None:
        _tab(page, tab)


def _tab(page, tab: str) -> None:
    """One of the open screen's own tabs: Generate's Files, Database setup's Map."""
    page.locator(".pagetabs").get_by_role("tab", name=tab, exact=True).click()
    page.wait_for_load_state("networkidle")


def _on_tab(page, tab: str, timeout: int = 60_000) -> None:
    """The open screen's own tab is this one."""
    page.wait_for_function(
        "t => document.querySelector('.pagetabs [role=tab][aria-selected=true]')"
        "?.textContent.trim() === t", arg=tab, timeout=timeout)


def _chip(page, name) -> None:
    """One of the Find screen's filter chips: Saved, Criteria, Full book scan..."""
    page.locator(".find__chips").get_by_role(
        "button", name=name, exact=isinstance(name, str)).click()
    page.wait_for_load_state("networkidle")


def _heading(page, text: str, timeout: int = 60_000) -> None:
    page.wait_for_function(
        "t => document.querySelector('.app__heading')?.textContent.trim() === t",
        arg=text, timeout=timeout)


def _state(page) -> dict:
    """What the screens are drawn from: the server's /api/state."""
    with urllib.request.urlopen(page.url.split("#")[0].rstrip("/") + "/api/state",
                                timeout=60) as r:
        return json.load(r)


def _step(page, step_id: str) -> str:
    return next(s["detail"] for s in _state(page)["steps"] if s["id"] == step_id)


def _settled(page, timeout: int = LONG) -> None:
    """Every button that was busy has finished."""
    page.wait_for_function("() => !document.querySelector('.btn--busy')", timeout=timeout)
    page.wait_for_load_state("networkidle")


def _analyzed(page) -> None:
    """A sample read on upload (there is no Analyze button) has been read."""
    page.wait_for_function(
        "() => ![...document.querySelectorAll('[role=status]')]"
        ".some(e => e.textContent.includes('Analyzing sample'))", timeout=LONG)
    page.wait_for_load_state("networkidle")
    page.locator(".edi-sample__table").first.wait_for(timeout=LONG)


def _rail(page) -> dict[str, str]:
    out = {}
    for step in page.locator(".rail__step").all():
        label = step.locator(".rail__label").inner_text().strip()
        detail = step.locator(".rail__detail")
        out[label] = detail.inner_text().strip() if detail.count() else ""
    out["footer"] = page.locator(".rail__target-db").inner_text().strip()
    return out


def _setup_facts(page) -> dict[str, str]:
    facts = {}
    for item in page.locator(".setup__target > div").all():
        facts[item.locator("dt").inner_text().strip()] = item.locator("dd").inner_text().strip()
    return facts


def _choose_database(page, name: str) -> None:
    _go(page, "Database setup", "Connection")
    page.get_by_label("Database", exact=True).select_option(name)
    page.get_by_role("button", name="Use database").click()
    _settled(page)
    page.wait_for_function(
        "name => document.querySelector('.rail__target-db')?.textContent.trim() === name",
        arg=name)


def _source():
    from sdg.config import get_settings

    return get_settings().source_db


# ---------------------------------------------------------------------------
# 1-2. Where things stand
# ---------------------------------------------------------------------------

def test_1_connect_names_the_database_worked_on_and_the_pattern_store(page):
    from sdg.config import get_settings

    settings = get_settings()
    # The screens open on Patterns, not on a database to connect, and the
    # whole book's patterns show with nothing to press.
    assert page.locator(".app__heading").inner_text().strip() == "Patterns"
    page.locator("li.pattern .pattern__head").first.wait_for(timeout=LONG)
    assert not page.get_by_role("tab", name="Targeted analysis").count()
    _go(page, "Database setup", "Connection")
    facts = _setup_facts(page)
    assert facts["Target"] == settings.source_db
    assert facts["Pattern data"] == settings.denorm_db
    rail = _rail(page)
    assert rail["Database setup"] == settings.source_db
    assert rail["footer"] == settings.source_db
    # The footer goes to the connection from anywhere.
    _go(page, "Patterns")
    page.locator(".rail__target").click()
    _heading(page, "Database setup")
    _on_tab(page, "Connection")


SCREENS = (
    ("Patterns", None),
    ("Generate", "Files"), ("Generate", "Database"),
    ("History", None),
    ("Layouts and samples", "File layouts"), ("Layouts and samples", "Folder"),
    ("Layouts and samples", "File profile"),
    ("Database setup", "Connection"), ("Database setup", "Profile"), ("Database setup", "Map"),
)


def test_2_every_screen_opens_without_an_error(page):
    for screen, tab in SCREENS:
        _go(page, screen, tab)
        page.wait_for_timeout(500)
        text = page.locator(".app__body").inner_text()
        assert "Traceback" not in text and "Internal Server Error" not in text, (screen, tab)
    _go(page, "History")
    assert page.locator(".app__heading").inner_text().strip() == "History"
    page.get_by_role("navigation", name="Settings").get_by_role(
        "button", name="Layout approval", exact=True).click()
    assert page.locator(".app__heading").inner_text().strip() == "Layout approval"
    assert page.get_by_label("Second approver", exact=True).count() == 1


def test_2_screens_say_so_when_the_business_pack_cannot_be_read(browser_page, server):
    """The screens wait for the pack's words before they draw; when the server
    can't give them, the page says so rather than staying blank. A page of its
    own, so the suite's page keeps its pack."""
    page = browser_page[0].context.new_page()
    try:
        page.route("**/api/pack", lambda route: route.fulfill(status=500, body="down"))
        page.goto(server)
        alert = page.get_by_role("alert")
        playwright.expect(alert).to_contain_text("Couldn't read the business pack")
        playwright.expect(alert).to_contain_text("reload the page")
        assert not page.locator(".rail").count(), "the screens drew without the pack"
        # Given the pack, the same page draws the screens in its words.
        page.unroute("**/api/pack")
        page.reload()
        page.locator(".rail").wait_for()
        # The app opens where it was last left; the record words are on Patterns.
        page.get_by_role("navigation", name="Main").get_by_role(
            "button", name="Patterns").click()
        playwright.expect(page.get_by_role("button", name="Members", exact=True)).to_be_visible()
        playwright.expect(page.get_by_role("button", name="Claims", exact=True)).to_be_visible()
    finally:
        page.close()


# ---------------------------------------------------------------------------
# 2a. A folder of files, connected and profiled for layouts
# ---------------------------------------------------------------------------

def test_2a_files_are_connected_profiled_and_mapped_like_a_database(page, tmp_path):
    (tmp_path / "sdmt-members.txt").write_text(
        "SDMT_NO|SDMT_GIVEN|SDMT_SEX\nX1|Dana|F\nX2||M\nX3|Lee|F\n")

    _go(page, "Layouts and samples", "Folder")
    # The field by its own label: the tab's panel is labelled "Folder" too.
    page.get_by_label("Folder path", exact=True).fill(str(tmp_path))
    page.get_by_role("button", name="Connect folder").click()
    page.locator(".app__body td", has_text="sdmt-members.txt").wait_for()

    _go(page, "Layouts and samples", "File profile")
    file = page.locator(".files__file", has_text="sdmt-members.txt")
    file.wait_for()
    assert "3 fields" in file.inner_text() and "3 rows" in file.inner_text()
    given = file.locator("tr", has_text="SDMT_GIVEN").inner_text()
    assert "33%" in given, given  # one of three is empty

    _go(page, "Layouts and samples", "File layouts")
    page.get_by_label("File from source folder").select_option("sdmt-members.txt")
    parsed = page.locator(".parsed")
    parsed.wait_for()
    assert parsed.get_by_label("Rule for SDMT_GIVEN").count() == 1


# ---------------------------------------------------------------------------
# 2b. Layouts: a rule for every field
# ---------------------------------------------------------------------------

PIPE_LAYOUT = "SDMT browser pipe layout"
FIXED_LAYOUT = "SDMT browser fixed layout"
PIPE_COPY = "SDMT browser pipe copy"


def _rule_ids(server) -> dict[str, int]:
    with urllib.request.urlopen(server + "/api/layouts/rules", timeout=30) as r:
        return {x["field_name"]: x["id"] for x in json.load(r)["rules"]}


def _layout_row(page, name: str):
    return page.locator(".panel__table tr", has_text=name)


def _flat_file(page) -> None:
    """Generate › Files › Flat file, where layouts are made and chosen."""
    _go(page, "Generate", "Files")
    page.get_by_role("tab", name="Flat file", exact=True).click()
    page.wait_for_load_state("networkidle")


def _write(page, name: str) -> str:
    """Generate file through a layout, from its row on the Flat file tab."""
    with page.expect_download(timeout=LONG) as download:
        _layout_row(page, name).get_by_role("button", name="Generate file").click()
    text = Path(download.value.path()).read_text(encoding="utf-8")
    _settled(page)
    return text


def _second_approver(page, name: str) -> None:
    """Settings › Layout approval: name a second approver, or clear it."""
    page.get_by_role("navigation", name="Settings").get_by_role(
        "button", name="Layout approval", exact=True).click()
    box = page.get_by_label("Second approver", exact=True)
    box.wait_for()
    page.wait_for_function(
        "() => !document.querySelector('[aria-label=\"Layout approval\"] input').disabled")
    box.fill(name)
    page.locator("[aria-label='Layout approval']").get_by_role(
        "button", name="Save", exact=True).click()
    _settled(page)
    page.locator(".banner--affirm", has_text="approval").wait_for()


def test_2b_every_layout_field_takes_a_rule_when_read_defined_or_edited(page, server):
    """On Generate › Files › Flat file, where layouts are chosen: a sample read
    and a layout defined with New layout each get a rule for every field, are
    edited and generate files at once -- no approval step, since no second
    approver is configured. With one configured, a layout must be approved
    before it generates, and deleting an approved one keeps its approval."""
    rules = _rule_ids(server)
    for needed in ("MEMBER_ID", "FIRST_NAME", "LAST_NAME"):
        if needed not in rules:
            pytest.skip(f"the rule repository has no {needed}")
    _drop_layouts({PIPE_LAYOUT, PIPE_COPY, FIXED_LAYOUT})
    with urllib.request.urlopen(server + "/api/layouts/approval", timeout=30) as r:
        approver_before = json.load(r)["secondApprover"]
    assert approver_before is None, "the suite's copy names a second approver"
    try:
        _flat_file(page)

        # Read: a pipe-delimited sample whose names nothing knows.
        page.locator("input[aria-label='Sample to upload']").set_input_files(files=[{
            "name": "sdmt-browser.txt", "mimeType": "text/plain",
            "buffer": (b"SDMT_BR_ID|SDMT_BR_GIVEN|SDMT_BR_PAD\n"
                       b"X1|Dana|zz\nX2|Lee|zz\n"),
        }])
        _settled(page)
        parsed = page.locator(".parsed")
        # Every field is listed before they are counted.
        parsed.get_by_label("Rule for SDMT_BR_PAD").wait_for()
        assert parsed.get_by_text("No match", exact=True).count() == 3
        parsed.get_by_label("Rule for SDMT_BR_ID").select_option(str(rules["MEMBER_ID"]))
        parsed.get_by_label("Rule for SDMT_BR_GIVEN").select_option(str(rules["FIRST_NAME"]))
        assert parsed.get_by_text("Manual", exact=True).count() == 2
        parsed.get_by_label("Name", exact=True).fill(PIPE_LAYOUT)
        parsed.get_by_role("button", name="Save layout").click()
        _settled(page)
        said = page.locator(".banner--affirm").first.inner_text()
        assert "bind by alias" in said and "pending" not in said.lower(), said

        # No second approver: no approval step anywhere, no status to read.
        row = _layout_row(page, PIPE_LAYOUT)
        assert row.get_by_role("button", name="Approve").count() == 0
        assert page.locator(".panel__table th", has_text="Status").count() == 0

        # Edit it: the pad field was blank by default; choose it blank on
        # purpose, and the given name becomes the last name instead.
        row.get_by_role("button", name="Edit").click()
        editor = page.locator(".editor")
        editor.get_by_label("Rule for SDMT_BR_PAD").select_option("blank")
        editor.get_by_label("Rule for SDMT_BR_GIVEN").select_option(str(rules["LAST_NAME"]))
        assert editor.get_by_label("Delimiter").input_value() == "|"
        editor.get_by_role("button", name="Save changes").click()
        _settled(page)

        text = _write(page, PIPE_LAYOUT)
        lines = [line for line in text.splitlines() if line]
        assert lines[0] == "SDMT_BR_ID|SDMT_BR_GIVEN|SDMT_BR_PAD", lines[0]
        assert len(lines) > 1, "nothing was written"
        for line in lines[1:]:
            ident, given, pad = line.split("|")
            assert ident and given and pad == "", line

        # Save a copy under a new name, then delete it: it was never approved.
        _layout_row(page, PIPE_LAYOUT).get_by_role("button", name="Edit").click()
        editor = page.locator(".editor")
        editor.get_by_label("Layout name").fill(PIPE_COPY)
        editor.get_by_role("button", name="Save as new layout").click()
        _layout_row(page, PIPE_COPY).wait_for()
        _settled(page)
        assert _layout_row(page, PIPE_LAYOUT).count() == 1, "the original was replaced"
        _layout_row(page, PIPE_COPY).get_by_role("button", name="Delete", exact=True).click()
        _layout_row(page, PIPE_COPY).get_by_role("button", name=f"Delete {PIPE_COPY}").click()
        _layout_row(page, PIPE_COPY).wait_for(state="detached")
        _settled(page)
        assert "deleted" in page.locator(".banner--affirm").first.inner_text()

        # Define: New layout, here on the tab layouts are chosen on -- a
        # fixed-width layout, a field renamed, a filler between.
        page.get_by_role("button", name="New layout", exact=True).click()
        designer = page.locator(".designer").first
        designer.get_by_role("button", name="Fixed width").click()
        designer.get_by_label("Layout name").fill(FIXED_LAYOUT)
        designer.get_by_label("Add a field").select_option(str(rules["MEMBER_ID"]))
        designer.get_by_label("Add a field").select_option("__blank__")
        designer.get_by_label("Add a field").select_option(str(rules["FIRST_NAME"]))
        designer.get_by_label("Name of field 1").fill("SDMT_BR_MEMBER")
        designer.get_by_label("Rule for FIRST_NAME").select_option(str(rules["LAST_NAME"]))
        designer.get_by_role("button", name="Save layout").click()
        _settled(page)
        designer.locator(".banner--affirm", has_text=f"{FIXED_LAYOUT} saved.").wait_for()

        detail_rows = _write(page, FIXED_LAYOUT)
        rows = [line for line in detail_rows.splitlines() if line]
        assert rows, "nothing was written"
        with urllib.request.urlopen(server + "/api/layouts", timeout=30) as r:
            layout = next(x for x in json.load(r)["layouts"] if x["name"] == FIXED_LAYOUT)
        with urllib.request.urlopen(server + f"/api/layouts/{layout['id']}", timeout=30) as r:
            fields = json.load(r)["fields"]
        assert [f["name"] for f in fields] == ["SDMT_BR_MEMBER", "FILLER_1", "FIRST_NAME"]
        assert fields[2]["rule_id"] == rules["LAST_NAME"]
        id_width, filler = fields[0]["length"], fields[1]["length"]
        for row in rows:
            assert row[:id_width].strip(), f"the renamed field is blank: {row!r}"
            assert row[id_width:id_width + filler] == " " * filler, row
            assert row[id_width + filler:].strip(), f"the re-ruled field is blank: {row!r}"

        # A second approver configured: the same layout waits for approval,
        # is approved, generates, and deleted keeps its approval record.
        _second_approver(page, "SDMT approver")
        _flat_file(page)
        row = _layout_row(page, PIPE_LAYOUT)
        row.get_by_role("button", name="Approve").wait_for()
        assert row.get_by_role("button", name="Generate file").count() == 0
        assert "Pending approval" in row.inner_text()
        # Approve asks for the approver's name, typed: Approve is enabled only
        # once it matches (any case); Cancel leaves the layout pending.
        row.get_by_role("button", name="Approve").click()
        confirm = row.get_by_role("group", name="Approve layout")
        # The field is named by its visible label, nothing else.
        typed = confirm.get_by_label("Type SDMT approver to approve", exact=True)
        assert typed.get_attribute("aria-label") is None
        assert typed.evaluate("e => e === document.activeElement"), "focus is not in the name box"
        confirming = confirm.get_by_role("button", name="Approve", exact=True)
        assert confirming.is_disabled()
        typed.fill("SDMT approve")
        assert confirming.is_disabled(), "Approve is enabled for a name that does not match"
        confirm.get_by_role("button", name="Cancel").click()
        assert row.get_by_role("group", name="Approve layout").count() == 0
        assert "Pending approval" in row.inner_text()
        row.get_by_role("button", name="Approve").click()
        typed = row.get_by_role("group", name="Approve layout").get_by_label(
            "Type SDMT approver to approve", exact=True)
        typed.fill("sdmt APPROVER")
        confirming = row.get_by_role("group", name="Approve layout").get_by_role(
            "button", name="Approve", exact=True)
        assert confirming.is_enabled()
        confirming.click()
        _settled(page)
        row.get_by_role("button", name="Generate file").wait_for()
        assert "Approved" in row.inner_text() and "SDMT approver" in row.inner_text()
        # Recorded in the approver's name, confirmed by this computer's user.
        import getpass

        with urllib.request.urlopen(server + "/api/layouts", timeout=30) as r:
            approved = next(l for l in json.load(r)["layouts"] if l["name"] == PIPE_LAYOUT)
        assert approved["approved_by"] == "SDMT approver", approved
        assert approved["confirmed_by"] == getpass.getuser(), approved
        assert _write(page, PIPE_LAYOUT).splitlines()[0] == "SDMT_BR_ID|SDMT_BR_GIVEN|SDMT_BR_PAD"
        row.get_by_role("button", name="Delete", exact=True).click()
        row.get_by_role("button", name=f"Delete {PIPE_LAYOUT}").click()
        _layout_row(page, PIPE_LAYOUT).wait_for(state="detached")
        _settled(page)
        assert "approval record is kept" in page.locator(".banner--affirm").first.inner_text()

        # Cleared again: no approval step.
        _second_approver(page, "")
        _flat_file(page)
        _layout_row(page, FIXED_LAYOUT).get_by_role("button", name="Generate file").wait_for()
        assert page.get_by_role("button", name="Approve").count() == 0
    finally:
        # The setting as the run found it, whatever happened above.
        request = urllib.request.Request(
            server + "/api/layouts/approval", method="PUT",
            data=json.dumps({"secondApprover": approver_before}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=30).close()
        _drop_layouts({PIPE_LAYOUT, PIPE_COPY, FIXED_LAYOUT})


# ---------------------------------------------------------------------------
# 2c. A pattern defined by its rules
# ---------------------------------------------------------------------------

def test_2c_a_pattern_defined_by_rules_is_measured_saved_and_looked_inside(page, server, kept):
    _go(page, "Patterns")
    _chip(page, "Criteria")
    page.get_by_label("Pattern name").fill("SDMT women 30 to 45")
    page.get_by_label("Add field").select_option(label="Gender")
    page.get_by_role("group", name="Gender").get_by_label("F", exact=True).check()
    page.get_by_label("Add field").select_option(label="Age")
    page.get_by_label("Age from").fill("30")
    page.get_by_label("Age to").fill("45")

    page.get_by_role("button", name="Measure pattern").click()
    summary = page.locator(".patterns__summary")
    summary.wait_for(timeout=LONG)
    _settled(page)
    assert "meet these rules" in summary.inner_text()
    assert page.locator("li.pattern").count() == 1

    before = len(kept)
    card = page.locator("li.pattern").first
    card.get_by_role("button", name="Save pattern").click()
    card.get_by_role("button", name="Saved").wait_for()
    assert len(kept) == before + 1
    # Health writes files and plans from a pattern (pattern_generation): the
    # card offers both, as it always has.
    assert card.get_by_role("button", name="Generate file").count() == 1
    assert card.get_by_role("button", name="Load to database").count() == 1
    saved = kept[-1]
    assert saved["name"] == "SDMT women 30 to 45"
    with urllib.request.urlopen(server + "/api/patterns/for-files", timeout=30) as r:
        listed = {p["pattern_id"]: p for p in json.load(r)["patterns"]}
    assert listed[saved["pattern_id"]]["origin"] == "user_defined"

    page.get_by_role("button", name="Find patterns within").click()
    page.wait_for_function(
        "() => document.querySelectorAll('li.pattern').length > 1", timeout=LONG)
    _settled(page)
    assert "split into" in page.locator(".patterns__summary").inner_text()

    # Counts and %: every field's values, the pattern's scenarios, and how
    # generation uses each; all patterns downloadable on one sheet.
    card = page.locator("li.pattern").first
    card.locator(".pattern__head").click()
    card.get_by_role("button", name="Counts and %").click()
    counts = card.locator(".counts")
    counts.locator(".counts__field").first.wait_for(timeout=LONG)
    said = counts.inner_text()
    for words in ("Age band", "Sex", "Plan", "Product", "Diagnosis", "Generated data:",
                  "Scenarios by"):
        assert words in said, words
    with page.expect_download(timeout=LONG) as download:
        page.locator(".patterns__export").get_by_role("button", name="Excel", exact=True).click()
    assert download.value.suggested_filename == "members-patterns.xlsx"
    import io as _io

    from openpyxl import load_workbook

    sheet = load_workbook(_io.BytesIO(Path(download.value.path()).read_bytes()))
    assert sheet.sheetnames == ["Patterns"]
    _settled(page)
    # The panel itself downloads as it shows: CSV and PDF here.
    downloads = counts.get_by_role("group", name="Download")
    with page.expect_download(timeout=LONG) as download:
        downloads.get_by_role("button", name="CSV (plain text)").click()
    assert download.value.suggested_filename.endswith(".csv")
    head = Path(download.value.path()).read_text(encoding="utf-8-sig").splitlines()[0]
    assert head.startswith("Domain,Pattern,Pattern size")
    _settled(page)
    with page.expect_download(timeout=LONG) as download:
        downloads.get_by_role("button", name="PDF", exact=True).click()
    assert download.value.suggested_filename.endswith(".pdf")
    assert Path(download.value.path()).read_bytes().startswith(b"%PDF-")
    _settled(page)
    card.locator(".pattern__head").click()

    # One scenario, many: the same rules across both plan categories, each
    # measured, both saved.
    variants = page.locator(".define__variants")
    variants.get_by_label("Vary by").select_option(label="Product category")
    group = variants.get_by_role("group", name="Vary Product category")
    for box in group.get_by_role("checkbox").all():
        box.check()
    before = len(kept)
    variants.get_by_role("button", name="Create 2 variants").click()
    variants.get_by_role("button", name="Save all 2").wait_for(timeout=LONG)
    counts = variants.locator("tbody tr td.num").all_inner_texts()
    assert len(counts) == 2 and all(c.replace(",", "").isdigit() for c in counts), counts
    variants.get_by_role("button", name="Save all 2").click()
    page.wait_for_function(
        "() => document.querySelectorAll('.define__variants tbody .chip').length === 2",
        timeout=LONG)
    # The screen can show both saved before the second response has reached
    # the listener that records it.
    for _ in range(100):
        if len(kept) >= before + 2:
            break
        page.wait_for_timeout(100)
    assert len(kept) == before + 2
    # A variant saved by an earlier run is found again and keeps the name it
    # was saved under; one made now is named for its variant.
    assert all(k["name"].startswith("SDMT women 30 to 45 · ") for k in kept[-2:])
    assert all("Product category" in k["name"] for k in kept[-2:] if k.get("created"))

    # Kept patterns export from the screen too: the one saved above, chosen.
    _chip(page, re.compile(r"^Saved"))
    row = page.locator("li.kept__row").filter(
        has=page.locator(".kept__name", has_text=re.compile(r"^SDMT women 30 to 45$"))).first
    row.get_by_role("checkbox").check()
    with page.expect_download(timeout=LONG) as download:
        page.get_by_role("button", name="Download 1 selected").click()
    assert download.value.suggested_filename == "saved-patterns.xlsx"
    rows = list(load_workbook(_io.BytesIO(Path(download.value.path()).read_bytes()))
                .active.iter_rows(values_only=True))
    assert {r[1] for r in rows[1:]} == {"SDMT women 30 to 45"}
    _settled(page)


# ---------------------------------------------------------------------------
# 2d. A requirement analyzed; a pattern's scenarios written together
# ---------------------------------------------------------------------------

def test_2d_a_requirement_is_analyzed_and_a_patterns_scenarios_written_together(page, kept):
    _go(page, "Patterns")
    # The one box at the top: searched as typed, read as a requirement on Find.
    page.get_by_label("Search or requirement").fill(
        "claims for type 2 diabetes from female members over 40")
    page.get_by_role("button", name="Find", exact=True).click()
    analysis = page.locator(".story__analysis")
    analysis.wait_for()
    rules = analysis.locator(".story__rules").inner_text()
    assert "Diagnosis" in rules and "Type 2 diabetes" in rules and "Gender" in rules, rules
    # Analyze is the pattern analysis: the patterns appear with nothing more to press.
    analysis.locator(".pattern__head").first.wait_for(timeout=LONG)
    _settled(page)
    assert not analysis.get_by_role("button", name="Measure pattern").count()

    # A found pattern, split into scenarios, all of them into one 834.
    card = analysis.locator("li.pattern").first
    card.locator(".pattern__head").click()
    card.get_by_role("button", name="Scenarios", exact=True).click()
    scenarios = card.locator(".scenarios")
    scenarios.get_by_role("button", name="List scenarios").click()
    scenarios.locator("tbody tr").first.wait_for(timeout=LONG)
    _settled(page)
    listed = scenarios.locator("tbody tr").count()
    assert listed >= 2
    # Only the ones chosen: nothing is ticked until somebody ticks it.
    scenarios.get_by_label("Selected scenarios only").check()
    assert not scenarios.get_by_role("button", name="Generate 0 as one file").is_enabled()
    for row in scenarios.locator("tbody tr").all():
        row.get_by_role("checkbox").check()
    before = len(kept)
    # Generate file's More options: Generate › Files with the scenarios chosen.
    scenarios.get_by_role("button", name=f"Generate {listed} as one file").click()
    scenarios.get_by_role("menuitem", name="More options…").click()
    page.wait_for_function(
        "() => document.querySelector('.app__heading')?.textContent.trim() === 'Generate'",
        timeout=LONG)
    # The screen can move on before the response reaches the listener.
    for _ in range(100):
        if len(kept) >= before + listed:
            break
        page.wait_for_timeout(100)
    assert len(kept) >= before + listed
    assert page.get_by_label("Pattern", exact=True).input_value() == "scenarios"
    page.get_by_role("tab", name="X12 EDI", exact=True).click()
    page.get_by_label("Transaction set").select_option("834")
    with page.expect_download(timeout=LONG) as download:
        page.get_by_role("button", name="Generate file").click()
    text = Path(download.value.path()).read_text(encoding="utf-8")
    _settled(page)
    assert text.startswith("ISA") and text.count("INS*") >= 2, text[:300]


def test_2d2_the_split_chosen_is_what_the_counts_say_and_every_scenario_is_one_choice(page):
    """Unticking one feature and ticking another changes the counts and the
    downloads, and every scenario of a pattern is generated in one go."""
    _go(page, "Patterns")
    _chip(page, "Criteria")
    page.get_by_label("Pattern name").fill("SDMT split follows")
    page.get_by_label("Add field").select_option(label="Gender")
    page.get_by_role("group", name="Gender").get_by_label("F", exact=True).check()
    page.get_by_role("button", name="Measure pattern").click()
    page.locator(".patterns__summary").wait_for(timeout=LONG)
    _settled(page)
    card = page.locator("li.pattern").first
    if not card.get_by_role("button", name="Scenarios", exact=True).count():
        card.locator(".pattern__head").click()

    card.get_by_role("button", name="Scenarios", exact=True).click()
    split = card.get_by_role("group", name="Split by")
    split.get_by_label("Gender", exact=True).uncheck()
    split.get_by_label("Marital status").check()
    card.get_by_role("button", name="Counts and %").click()
    heading = card.locator(".counts__scenarios h5")
    heading.wait_for(timeout=LONG)
    said = heading.inner_text().lower()
    assert "marital status" in said and "gender" not in said, said

    with page.expect_download(timeout=LONG) as download:
        card.get_by_role("group", name="Download").get_by_role(
            "button", name="CSV (plain text)").click()
    text = Path(download.value.path()).read_text(encoding="utf-8-sig")
    assert "Marital status" in text and "Gender F" not in text, text[-600:]

    scenarios = card.locator(".scenarios")
    scenarios.get_by_role("button", name="List scenarios").click()
    scenarios.locator("tbody tr").first.wait_for(timeout=LONG)
    _settled(page)
    assert scenarios.get_by_role("radio", name=re.compile(r"^All scenarios")).is_checked()
    # Every scenario, straight to a CSV of the total asked for, as a run.
    scenarios.get_by_label("Total members").fill("20")
    every = scenarios.get_by_role("button", name=re.compile(r"^Generate all [\d,]+ as one file$"))
    every.click()
    with page.expect_download(timeout=LONG) as download:
        scenarios.get_by_role("menuitem", name="CSV", exact=True).click()
    _settled(page)
    assert re.fullmatch(r"members-request-\d+\.csv", download.value.suggested_filename), \
        download.value.suggested_filename
    rows = Path(download.value.path()).read_text(encoding="utf-8").strip().splitlines()
    assert rows[0].startswith("MEMBER_ID,") and len(rows) == 21, rows[:3]
    page.locator(".toast", has_text=re.compile(r"Created CSV file · Run \d+")).wait_for()
    page.locator(".toast").get_by_role("button", name="Dismiss").click()


def test_2d3_one_find_screen_searches_filters_and_lists_saved_patterns(page, kept, tmp_path):
    """Patterns is one Find screen: the whole book's patterns on open, one box
    that searches them, chips for size and saved, the Saved list with its row
    actions (Delete asks first), and Full book scan and a requirements file
    behind chips of their own."""
    _go(page, "Patterns")
    _chip(page, "All")
    cards = page.locator("li.pattern")
    cards.locator(".pattern__head").first.wait_for(timeout=LONG)
    _settled(page)
    names = [n.strip() for n in cards.locator(".pattern__name").all_inner_texts()]
    assert names, "no patterns on open"
    search = page.get_by_label("Search or requirement")

    # Typed, the box narrows the patterns to those holding the words.
    search.fill(names[0])
    shown = [n.strip() for n in cards.locator(".pattern__name").all_inner_texts()]
    assert shown and all(names[0].lower() in n.lower() for n in shown), shown
    search.fill("zz no such pattern zz")
    empty = page.locator(".empty", has_text="No matching patterns")
    empty.wait_for()
    assert empty.get_by_role("button", name="Find as requirement").count() == 1
    assert not cards.count()
    search.fill("")
    cards.first.wait_for()
    assert cards.count() == len(names)

    # A size chip shows only the patterns of that size, and again shows all.
    band = cards.first.locator(".pattern__head .chip").inner_text().strip()
    _chip(page, band)
    bands = [b.strip() for b in cards.locator(".pattern__head .chip").all_inner_texts()]
    assert bands and set(bands) == {band}, bands
    _chip(page, band)
    assert cards.count() == len(names)

    # Saved: the Saved list alone, as many rows as the chip says.
    _chip(page, re.compile(r"^Saved"))
    label = page.locator(".find__chips").get_by_role(
        "button", name=re.compile(r"^Saved \(\d+\)$")).inner_text()
    saved_count = int(re.search(r"\d+", label).group())
    assert not cards.count()
    rows = page.locator("li.kept__row")
    assert rows.count() == saved_count
    saved_names = {n.strip() for n in rows.locator(".kept__name").all_inner_texts()}
    # Each row has the pattern's actions.
    first = rows.first
    for action in ("Generate file", "Load to database", "Scenarios", "Delete"):
        assert first.get_by_role("button", name=action).count() == 1, action
    # Scenarios opens under its own row, in view: a panel opened below the
    # whole list looked like a button that did nothing.
    first.get_by_role("button", name="Scenarios", exact=True).click()
    panel = page.locator("li.kept__scenarios .discover__scenarios")
    panel.wait_for()
    assert page.evaluate(
        "() => document.querySelector('li.kept__row').nextElementSibling"
        ".classList.contains('kept__scenarios')"), "the panel is not under its row"
    playwright.expect(panel.locator("h3")).to_be_in_viewport()
    playwright.expect(panel.locator("h3")).to_be_focused()
    hide = first.get_by_role("button", name="Hide scenarios")
    assert hide.get_attribute("aria-expanded") == "true"
    hide.click()
    playwright.expect(panel).to_have_count(0)
    # The box searches the saved rows too.
    search.fill("SDMT women 30 to 45")
    listed = [n.strip() for n in rows.locator(".kept__name").all_inner_texts()]
    assert listed and all(n.startswith("SDMT women 30 to 45") for n in listed), listed

    # Delete asks first; Cancel keeps the row, Delete removes it, Undo restores
    # it. Only a variant this run created is ever deleted.
    mine = next((k for k in reversed(kept) if k.get("created")
                 and k["name"].startswith("SDMT women 30 to 45 · ")), None)
    if mine is not None:
        row = rows.filter(has=page.locator(".kept__name", has_text=mine["name"])).first
        row.get_by_role("button", name="Delete", exact=True).click()
        assert "Delete this pattern?" in row.inner_text()
        row.get_by_role("button", name="Cancel").click()
        row.get_by_role("button", name="Delete", exact=True).click()
        row.get_by_role("button", name="Delete", exact=True).click()
        page.locator(".banner", has_text=f"{mine['name']} deleted from Saved").wait_for()
        assert not rows.filter(has=page.locator(".kept__name", has_text=mine["name"])).count()
        page.locator(".banner").get_by_role("button", name="Undo").click()
        rows.filter(has=page.locator(".kept__name", has_text=mine["name"])).first.wait_for()
    search.fill("")

    # Not saved: the found patterns no one has saved, and no Saved list.
    _chip(page, "Not saved")
    page.wait_for_function("() => !document.querySelector('.discover__kept')")
    unsaved = {n.strip() for n in cards.locator(".pattern__name").all_inner_texts()}
    assert not unsaved & saved_names, unsaved & saved_names

    # Saved from Not saved: the card stays for this visit (a card being
    # written to a file does not vanish part way), and once the screen is
    # opened again it is no longer under Not saved -- told by what it was
    # saved from, not by its name. Four patterns of the whole book: the
    # automatic ones may all be saved already in this store.
    def four_patterns() -> None:
        # The cards shown before the four arrive are the earlier analysis's:
        # wait for the four-pattern analysis itself, or the card saved is a
        # stale one that the re-render then replaces.
        number = page.get_by_label("Number of patterns")
        if number.input_value() == "4":
            cards.first.wait_for(timeout=LONG)
        else:
            with page.expect_response(
                    lambda r: r.url.endswith("/api/entities/members/analyze")
                    and r.request.method == "POST"
                    and (r.request.post_data_json or {}).get("k") == 4,
                    timeout=LONG):
                number.select_option("4")
        cards.first.wait_for(timeout=LONG)
        _settled(page)

    four_patterns()
    last = cards.last
    last_name = last.locator(".pattern__name").inner_text().strip()

    def unsaved_named() -> int:
        return sum(1 for n in cards.locator(".pattern__name").all_inner_texts()
                   if n.strip() == last_name)

    named_before = unsaved_named()
    last.locator(".pattern__head").click()
    saves_before = len(kept)
    last.get_by_role("button", name="Save pattern").click()
    last.get_by_role("button", name="Saved").wait_for()
    assert len(kept) == saves_before + 1
    mine = kept[-1]
    try:
        assert mine.get("created"), "a pattern listed under Not saved was saved already"
        assert unsaved_named() == named_before, "the card left Not saved during the visit"
        _go(page, "History")
        _go(page, "Patterns")
        _chip(page, "Not saved")
        page.get_by_label("Number of patterns").wait_for(timeout=LONG)
        four_patterns()
        assert unsaved_named() == named_before - 1, "the saved pattern is still under Not saved"
    finally:
        # This test's own save, removed by its id even when a check above
        # fails, so the tests after it never generate from it.
        _forget([mine["pattern_id"]])
        kept.remove(mine)
    # Back to the automatic number.
    _chip(page, "All")
    page.get_by_label("Number of patterns").select_option("")
    cards.first.wait_for(timeout=LONG)
    _settled(page)

    # Full book scan, behind its chip: the depth and a scan to run.
    _chip(page, "Full book scan")
    scan = page.locator("section[aria-label='Full book scan']")
    scan.get_by_label("Depth").wait_for()
    assert page.get_by_role("button", name=re.compile(r"^(Run|Rerun) scan$")).count() >= 1
    assert not cards.count()

    # A requirements file: read, matched to saved patterns, and any one of
    # them found as the box would.
    _chip(page, "Requirements file")
    stories = tmp_path / "requirements.txt"
    stories.write_text("female members aged 30 to 45\n\nclaims for type 2 diabetes\n",
                       encoding="utf-8")
    page.locator(".story input[type=file]").set_input_files(str(stories))
    page.get_by_text("requirements.txt:").wait_for(timeout=LONG)
    assert "2 requirements" in page.locator(".story__file").inner_text()
    page.get_by_role("button", name="Match to saved patterns").click()
    page.locator(".story__matches .match").first.wait_for(timeout=LONG)
    _settled(page)
    assert page.locator(".story__matches .match").count() == 2
    page.locator(".story__matches .match").first.get_by_role(
        "button", name="Find patterns").click()
    analysis = page.locator(".story__analysis")
    analysis.wait_for()
    assert search.input_value() == "female members aged 30 to 45"
    analysis.locator(".pattern__head").first.wait_for(timeout=LONG)
    _settled(page)
    # Closed, the box is empty again and the whole book's patterns are back.
    analysis.get_by_role("button", name="Close").click()
    cards.first.wait_for(timeout=LONG)
    assert search.input_value() == ""


def test_2d4_a_requirement_shows_its_subject_and_a_failed_reading_is_tried_again(
        page, browser_page):
    """Find reads a requirement: the Subject chip shows what it is about --
    Claims for a claims requirement, Members again for a members one after it.
    A reading that fails says why, and Try again reads it again."""
    _go(page, "Patterns")
    _chip(page, "All")
    trouble = browser_page[1]
    search = page.get_by_label("Search or requirement")
    chips = page.locator(".find__chips")
    analysis = page.locator(".story__analysis")

    def read(text: str) -> None:
        search.fill(text)
        page.get_by_role("button", name="Find", exact=True).click()
        analysis.wait_for()
        page.wait_for_function(
            "() => !document.querySelector('.story__analysis [role=status]')"
            " || !document.querySelector('.story__analysis [role=status]')"
            ".textContent.includes('Analyzing')", timeout=LONG)
        _settled(page)

    def subject() -> str:
        return chips.locator("[aria-pressed=true]", has_text=re.compile(
            r"^(Members|Claims)$")).inner_text().strip()

    failed: list[int] = []

    def fail_once(route):
        if not failed:
            failed.append(1)
            route.fulfill(status=422, content_type="application/json",
                          body=json.dumps({"detail": "SDMT the reading failed"}))
        else:
            route.continue_()

    page.route("**/api/story/read", fail_once)
    try:
        search.fill("claims for type 2 diabetes")
        page.get_by_role("button", name="Find", exact=True).click()
        alarm = analysis.locator(".banner--alarm")
        alarm.wait_for(timeout=LONG)
        assert "SDMT the reading failed" in alarm.inner_text()
        # The forced failure's own console line is expected; only it is removed.
        forced = [s for s in trouble.seen if "422" in s]
        assert len(forced) == 1, trouble.seen
        trouble.seen.remove(forced[0])
        alarm.get_by_role("button", name="Try again").click()
        page.wait_for_function(
            "() => !document.querySelector('.story__analysis .banner--alarm')"
            " && !(document.querySelector('.story__analysis [role=status]')?.textContent"
            " || '').includes('Analyzing')", timeout=LONG)
        _settled(page)
    finally:
        page.unroute("**/api/story/read", fail_once)
    assert subject() == "Claims", "a claims requirement does not show Claims"

    read("female members aged 30 to 45")
    assert subject() == "Members", "the Subject chip stayed on the earlier requirement's"
    analysis.get_by_role("button", name="Close").click()
    page.locator("li.pattern").first.wait_for(timeout=LONG)
    assert subject() == "Members"

    # Another subject chip with a requirement open closes it, and its
    # sentence leaves the box: the list shows that subject's patterns.
    read("female members aged 30 to 45")
    _chip(page, "Claims")
    assert subject() == "Claims"
    assert search.input_value() == "", "the requirement's sentence stayed in the box"
    assert analysis.count() == 0
    page.locator("li.pattern").first.wait_for(timeout=LONG)
    _chip(page, "Members")
    page.locator("li.pattern").first.wait_for(timeout=LONG)
    assert subject() == "Members"


# ---------------------------------------------------------------------------
# 2e. A rule tried on the Rules screen makes real values
# ---------------------------------------------------------------------------

def test_2e_a_rule_is_tried_on_the_rules_screen_and_makes_real_values(page, server):
    if "STREET_1" not in _rule_ids(server):
        pytest.skip("the rule repository has no STREET_1")
    _go(page, "Layouts and samples", "File layouts")
    page.get_by_role("button", name="Settings › Rules").click()
    assert page.locator(".app__heading").inner_text().strip() == "Rules"
    page.get_by_label("Find").fill("STREET_1")
    row = page.locator("tr.rules__row", has_text="STREET_1").first
    row.get_by_role("button", name="Edit").click()
    editor = page.locator("tr.rules__editor")
    editor.get_by_role("button", name="Preview", exact=True).click()
    shown = editor.locator(".rules__sample .mono")
    shown.wait_for(timeout=LONG)
    streets = [s.strip() for s in shown.inner_text().split("·")]
    assert len(streets) == 5 and all(re.fullmatch(r"\d{3,4} [A-Za-z' .]+", s) for s in streets), streets


def test_2f_a_column_takes_a_business_element_and_only_custom_offers_algorithms(page):
    _go(page, "Database setup", "Map")
    rows = page.locator("table tbody tr[tabindex='0']")
    if not rows.count():
        pytest.skip("nothing has been read on this database")
    rows.first.click()
    detail = page.locator("aside.detail")
    detail.get_by_role("button", name="Edit rule").click()
    editor = detail.locator(".editor")
    element = editor.get_by_label("Business element")
    rule = editor.get_by_label("Rule")

    # An element brings its rule: nothing to choose.
    names = [o for o in element.locator("option").all_inner_texts() if o != "Custom (no business element)"]
    element.select_option(names[0])
    assert not rule.count() or rule.evaluate("e => e.tagName") != "SELECT"
    assert editor.locator(".editor__rule").inner_text().strip()

    # Custom: every algorithm the engine has, to choose from.
    element.select_option(label="Custom (no business element)")
    offered = rule.locator("option").all_inner_texts()
    assert {"Street address", "Template"} <= set(offered) and len(offered) >= 10, offered
    editor.get_by_role("button", name="Cancel").click()


def test_2g_a_layout_field_with_no_element_gets_a_custom_rule_of_its_own(page, server):
    _drop_layouts({PIPE_LAYOUT})
    _go(page, "Layouts and samples", "File layouts")
    page.locator(".panel input[type=file]").set_input_files(files=[{
        "name": "sdmt-custom.txt", "mimeType": "text/plain",
        "buffer": b"SDMT_CUSTOM_CODE|SDMT_BR_OTHER\nA1|x\nA2|y\n",
    }])
    _settled(page)
    choice = page.get_by_label("Rule for SDMT_CUSTOM_CODE")
    choice.select_option("custom")
    form = page.locator("tr.fields__rule .rules__form")
    form.wait_for(timeout=LONG)
    form.get_by_role("button", name="Preview", exact=True).click()
    shown = form.locator(".rules__sample .mono")
    shown.wait_for(timeout=LONG)
    assert len([v for v in shown.inner_text().split("·") if v.strip()]) == 5
    assert "SDMT_CUSTOM_CODE" in choice.locator("option:checked").inner_text()
    # The rule made for this test goes, by its own exact name.
    from sdg.analyzer import db

    db.delete_generation_rule("SDMT_CUSTOM_CODE")


def test_2h_the_top_menu_configures_discovery_and_shows_each_elements_algorithm(page):
    from sdg.analyzer.services import discovery_features, report_fields

    before = discovery_features.chosen("members")
    counted = report_fields._stored("members")
    try:
        menu = page.get_by_role("navigation", name="Settings")
        menu.get_by_role("button", name="Pattern settings").click()
        assert page.locator(".app__heading").inner_text().strip() == "Pattern settings"
        config = page.locator("section[aria-label='Pattern discovery features']")
        members = config.locator(".discoveryconfig__entity").first
        members.get_by_label("PCP specialty").check()
        members.get_by_role("button", name="Save").click()
        config.locator(".banner", has_text="PCP specialty").wait_for(timeout=LONG)

        # The fields a pattern's counts are made on: a default switched off, a
        # denorm column added, saved; then back to the defaults.
        fields = page.locator("section[aria-label='Fields counted in patterns']")
        counted_members = fields.locator(".discoveryconfig__entity").first
        counted_members.get_by_label("ZIP", exact=True).uncheck()
        counted_members.get_by_label("Add member pattern data column").select_option("col:GRGR_STATE")
        counted_members.get_by_role("button", name="Add", exact=True).click()
        counted_members.get_by_text("Group State", exact=True).wait_for()
        counted_members.get_by_role("button", name="Save").click()
        fields.locator(".banner", has_text="now counted on").wait_for(timeout=LONG)
        keys = report_fields.chosen("members")
        assert "col:GRGR_STATE" in keys and "zip" not in keys
        counted_members.get_by_role("button", name="Defaults").click()
        fields.locator(".banner", has_text="default fields again").wait_for(timeout=LONG)
        assert "col:GRGR_STATE" not in report_fields.chosen("members")

        # Back on the workflow, the patterns screen says what it finds them on.
        page.locator(".rail__step", has_text="Patterns").first.click()
        assert page.locator(".app__heading").inner_text().strip() == "Patterns"
        lede = page.locator(".patterns__lede")
        lede.get_by_text("PCP specialty").wait_for(timeout=LONG)

        menu.get_by_role("button", name="Rules", exact=True).click()
        assert page.locator(".app__heading").inner_text().strip() == "Rules"
        page.locator("tr.rules__row").first.wait_for(timeout=LONG)
        assert page.locator("tr.rules__row").count() > 10
    finally:
        discovery_features.choose("members", before)
        if counted is None:
            report_fields.reset("members")
        else:
            report_fields.choose("members", counted)


# ---------------------------------------------------------------------------
# 3. Onboarding a database, all from the screens
# ---------------------------------------------------------------------------

def test_3_a_database_is_onboarded_read_planned_written_and_cleaned_from_the_screens(page):
    _choose_database(page, A)
    assert _setup_facts(page)["Target"] == A
    rail = _rail(page)
    assert rail["footer"] == A, rail
    assert _step(page, "discover") == "", "the new database was read already"

    # The read ends on the Profile tab, with what it found.
    page.get_by_role("button", name="Discover Schema").click()
    _on_tab(page, "Profile", timeout=LONG)
    _settled(page)
    assert "auto-mapped" in _step(page, "map")
    found = _step(page, "discover")
    assert found.endswith("columns") and "tables" in found, found
    assert _rail(page)["Database setup"] == A

    # From the read to the load by the Next button on each tab, as somebody
    # new to it would go; the plan builds itself on arrival.
    page.get_by_role("button", name="Next: Map →").click()
    _on_tab(page, "Map")
    page.get_by_role("button", name="Next: Generate →").click()
    _heading(page, "Generate")
    _on_tab(page, "Database")
    _planned(page)
    page.get_by_text("rows in this plan").wait_for(timeout=LONG)
    # What the load does to each kind of table is said before anything runs.
    kinds = page.locator(".cascade__kinds").inner_text().lower()
    assert "transactional — generated" in kinds and "reference — kept as is" in kinds
    assert page.locator(".cascade__kind--kept").count() > 0
    # One Rebuild plan and one Preview: no second pair in the header or below.
    assert page.get_by_role("button", name="Rebuild plan").count() == 1
    assert page.get_by_role("button", name=re.compile(r"^Next: Preview")).count() == 0

    page.get_by_role("button", name="Preview", exact=True).click()
    _settled(page)
    page.get_by_text("Sample rows", exact=True).first.wait_for()
    assert _step_done(page, "Preview")
    page.get_by_role("button", name="Load to database", exact=True).click()
    _settled(page)
    page.get_by_text("Loaded", exact=True).wait_for()
    assert _step_done(page, "Load")
    written = facets_copies.counts(A)
    assert written["CMC_MEME_MEMBER"] and written["CMC_CLCL_CLAIM"], written
    assert _step(page, "run").endswith("written")

    page.get_by_role("button", name="Validate", exact=True).click()
    _settled(page)
    checked = page.locator(".app__body").inner_text()
    assert "checks passed" in checked and "failed" not in checked.lower(), checked[-1500:]
    assert _step_done(page, "Validate")

    page.get_by_role("button", name="Delete loaded rows").click()
    _settled(page)
    assert facets_copies.counts(A) == dict.fromkeys(written, 0)


# ---------------------------------------------------------------------------
# 4. Chosen records into that database
# ---------------------------------------------------------------------------

def _step_done(page, label: str) -> bool:
    step = page.locator(".gensteps__step", has_text=label)
    return "gensteps__step--done" in (step.get_attribute("class") or "")


def _planned(page) -> None:
    """Generate › Database is open and its plan is built and current."""
    page.wait_for_function(
        "() => [...document.querySelectorAll('.gensteps__step--done')]"
        ".some(s => s.textContent.includes('Plan'))",
        timeout=LONG)
    _settled(page)


def _find_patterns(page) -> None:
    """Patterns, whose whole-book patterns show on open."""
    _go(page, "Patterns")
    _chip(page, "All")
    page.locator("li.pattern .pattern__head").first.wait_for(timeout=LONG)
    _settled(page)


def _open_first_pattern(page):
    card = page.locator("li.pattern").first
    card.locator(".pattern__head").click()
    card.get_by_role("button", name="Save pattern").wait_for()
    return card


def test_4_a_pattern_is_loaded_into_the_onboarded_database_in_one_step(page, kept, server):
    """Load to database on a pattern says where, how much and which columns;
    on chosen records, Load applies them, rebuilds the plan, loads and
    validates; the result names the run, deletes what it loaded and opens it
    in History. (The card's own share is confirmed, not loaded: a cohort load
    of the whole plan takes about ten minutes on the test copy.)"""
    assert page.locator(".rail__target-db").inner_text().strip() == A
    try:
        _load_in_one_step(page, kept, server)
    finally:
        # A dialog left open by a failure would block every later test.
        if page.locator(".dialog").count():
            page.keyboard.press("Escape")
        if page.locator(".dialog").count():
            # Still working: the page is opened again.
            page.reload()
            page.wait_for_load_state("networkidle")


def _plan_now(server) -> dict:
    with urllib.request.urlopen(server + "/api/plan", timeout=120) as r:
        return json.load(r)


def _plan_built(server) -> dict:
    """The plan built now from the decisions as they are, as the screen's
    Rebuild would: the dialog then has a current plan to say."""
    request = urllib.request.Request(server + "/api/plan", data=b"", method="POST")
    urllib.request.urlopen(request, timeout=900).close()
    plan = _plan_now(server)
    assert plan["built"] and plan["stale"] is False, plan
    return plan


def _plan_said(plan: dict) -> tuple[str, bool]:
    """What the dialog says for Rows with the plan in this state, and whether
    it lists columns (a plan is built, even an out-of-date one) or says they
    are calculated when loaded (none built)."""
    if plan.get("built") and not plan.get("stale"):
        return f"{plan['totalRows']:,} in the plan", True
    return "Calculated when loaded", bool(plan.get("built"))


def _stamp(plan: dict) -> str | None:
    return (plan.get("meta") or {}).get("created") if plan.get("built") else None


def _dialog_facts(dialog, plan: dict) -> None:
    """The dialog says, for the plan's state read before it opened, exactly
    the Rows text and the columns (or that they are calculated when loaded)."""
    rows_said, lists_columns = _plan_said(plan)
    columns = dialog.locator(".dialog__columns")
    if lists_columns:
        columns.locator("tbody tr").first.wait_for(timeout=LONG)
    else:
        columns.get_by_text("Calculated when loaded", exact=True).wait_for(timeout=LONG)
    # Rows is the facts' only number (Pattern and Target are names).
    dialog.locator(".dialog__facts dd.num").get_by_text(rows_said, exact=True).wait_for(
        timeout=LONG)


def _load_in_one_step(page, kept, server) -> None:
    _find_patterns(page)
    card = _open_first_pattern(page)

    # The card's Load to database says where, how much and which columns
    # before anything is written; Cancel leaves everything as it was --
    # the plan too: opening and cancelling build nothing. The plan is built
    # first, so the dialog has its rows and columns to say. (With none
    # built, it says they are calculated when loaded: test_load_preview.)
    plan = _plan_built(server)
    card.locator(".pattern__actions").get_by_role(
        "button", name="Load to database", exact=True).click()
    dialog = page.get_by_role("dialog", name="Load to database")
    _dialog_facts(dialog, plan)
    said = dialog.inner_text()
    assert A in said, said
    assert f"{plan['totalRows']:,} in the plan" in said, said
    assert "Member Gender" in said, said
    assert dialog.get_by_role("radio", name=re.compile(r"of rows$")).is_checked()
    dialog.get_by_role("button", name="Cancel").click()
    assert not page.locator(".dialog").count()
    _settled(page)
    assert _stamp(_plan_now(server)) == _stamp(plan), "opening the dialog built the plan"
    assert not facets_copies.counts(A)["CMC_MEME_MEMBER"]

    # Chosen records, loaded in one step: their values set the rules for
    # every row, the plan is rebuilt, the rows loaded and validated.
    card.get_by_role("button", name="Show source records").click()
    boxes = card.get_by_label("Select member", exact=False)
    boxes.first.wait_for(timeout=LONG)
    for i in range(3):
        boxes.nth(i).check()
    assert "3 members selected" in card.inner_text()
    before = len(kept)
    choose = card.locator(".rows__choose").get_by_role("button", name="Load to database")
    plan = _plan_now(server)
    assert plan["built"] and plan["stale"] is False, "Cancel left the plan out of date"
    choose.click()
    dialog = page.get_by_role("dialog", name="Load to database")
    _dialog_facts(dialog, plan)
    assert "3 selected members" in dialog.inner_text()
    # Opening the dialog only shows what Load would do: nothing is saved, no
    # plan is built, and Cancel leaves no saved pattern behind.
    assert len(kept) == before, "opening the dialog saved a pattern"
    dialog.get_by_role("button", name="Cancel").click()
    assert not page.locator(".dialog").count()
    _settled(page)
    assert len(kept) == before, "Cancel left a saved pattern"
    assert _stamp(_plan_now(server)) == _stamp(plan), "opening the dialog built the plan"
    choose.click()
    dialog = page.get_by_role("dialog", name="Load to database")
    _dialog_facts(dialog, plan)
    dialog.get_by_role("button", name="Load", exact=True).click()

    done = page.get_by_role("dialog", name="Loaded to database")
    done.wait_for(timeout=LONG)
    result = done.locator(".dialog__result").first.inner_text()
    assert re.search(rf"Loaded [\d,]+ rows into {A} · Run \d+", result), result
    run = int(re.search(r"Run (\d+)", result).group(1))
    assert done.locator(".dialog__passed").count(), done.inner_text()
    assert "checks passed" in done.locator(".dialog__passed").inner_text()
    written = facets_copies.counts(A)
    assert written["CMC_MEME_MEMBER"] and written["CMC_CLCL_CLAIM"], written
    assert len(kept) == before + 1, "the chosen records were not saved as a pattern on Load"
    # Load applied the chosen records and rebuilt the plan from them.
    after = _plan_now(server)
    assert after["built"] and after["stale"] is False, after
    assert _stamp(after) != _stamp(plan), "Load did not rebuild the plan"

    # Delete loaded rows asks first: Cancel deletes nothing, Delete does.
    done.get_by_role("button", name="Delete loaded rows").click()
    confirm = done.get_by_role("group", name="Delete loaded rows")
    confirm.wait_for()
    assert re.search(r"Delete [\d,]+ rows\?", confirm.inner_text()), confirm.inner_text()
    confirm.get_by_role("button", name="Cancel").click()
    assert confirm.count() == 0
    assert facets_copies.counts(A) == written, "Cancel deleted rows"
    done.get_by_role("button", name="Delete loaded rows").click()
    done.get_by_role("group", name="Delete loaded rows").get_by_role(
        "button", name="Delete", exact=True).click()
    done.get_by_text("loaded rows.", exact=False).wait_for(timeout=LONG)
    assert facets_copies.counts(A) == dict.fromkeys(written, 0)

    from sdg.analyzer.services import generation_requests as gr

    try:
        done.get_by_role("button", name="View in History").click()
        _heading(page, "History")
        focused = page.locator(".requests__card--focus")
        focused.wait_for(timeout=LONG)
        assert focused.locator("h3").inner_text().startswith(f"Run {run}")
        assert f"Loaded into {A}" in focused.inner_text()
    finally:
        gr.delete_request(run)


# ---------------------------------------------------------------------------
# 5. A saved pattern, to a file
# ---------------------------------------------------------------------------

def test_5_a_saved_pattern_is_listed_for_files_and_writes_an_834_that_passes_snip(page, kept):
    from sdg.writers.snip import validate

    _find_patterns(page)
    card = _open_first_pattern(page)
    before = len(kept)
    card.get_by_role("button", name="Save pattern").click()
    card.get_by_role("button", name="Saved").wait_for()
    assert len(kept) == before + 1
    pattern_id = kept[-1]["pattern_id"]

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    page.get_by_role("tab", name="X12 EDI", exact=True).click()
    page.get_by_label("Transaction set").select_option("834")
    with page.expect_download(timeout=LONG) as download:
        page.get_by_role("button", name="Generate file").click()
    text = Path(download.value.path()).read_text(encoding="utf-8")
    _settled(page)

    assert text.startswith("ISA") and "INS*" in text and "N4*" in text, text[:400]
    report = validate(text)
    failing = [(lv["level"], lv["errors"]) for lv in report["levels"] if lv["errors"]]
    assert not failing, f"SNIP errors by level: {failing}"

    page.get_by_role("button", name="Run SNIP validation").click()
    _settled(page)
    summary = page.locator(".snip__summary").inner_text()
    # The file Write the file wrote, all of it: "SNIP passed" is said of it.
    assert "entire generated file" in summary, summary


def test_5b_an_837_is_modeled_on_a_sample_and_written_for_a_saved_pattern(page, kept):
    """Read a sample against the masking config, change a proposal, save as a
    new config, and write the file: the sample everywhere but where chosen."""
    from sdg.analyzer.services import sample_gen_configs
    from sdg.writers.snip import validate

    fixtures = ROOT / "tests" / "fixtures" / "edi"
    sample = (fixtures / "claim.837").read_text(encoding="utf-8")
    master = (fixtures / "master-837-masking.json").read_text(encoding="utf-8")
    name = f"browser test 837 {int(time.time())}"
    pattern_id = kept[-1]["pattern_id"]

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    page.get_by_role("tab", name="Custom format").click()
    page.locator("input[aria-label='Sample file']").set_input_files(files=[{
        "name": "claim.837", "mimeType": "text/plain", "buffer": sample.encode()}])
    _analyzed(page)
    page.get_by_label("Base configuration").select_option("file")
    page.locator("input[aria-label='Configuration file']").set_input_files(files=[{
        "name": "master-837-masking.json", "mimeType": "application/json",
        "buffer": master.encode()}])
    page.locator(".chip", has_text="Based on master-837-masking.json").wait_for(timeout=LONG)
    _analyzed(page)

    # The master's proposals, read from its masking names.
    assert page.get_by_label("Rule for NM1*IL NM103").input_value() == "LAST_NAME"
    assert page.get_by_label("Rule for REF*EI REF02").input_value() == ""
    # The person's change: procedure codes from the pattern, not the sample.
    page.get_by_label("Rule for SV1 SV101-2").select_option("PROCEDURE_CODE")
    page.get_by_label("New configuration name").fill(name)
    page.get_by_role("button", name="Save configuration as").click()
    _settled(page)
    page.locator(".chip", has_text=f"saved as {name}").wait_for()
    saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
    try:
        body = sample_gen_configs.get_config(saved["id"])["config"]
        assert "genSegments" in body and body["based_on"] == "master-837-masking.json"
        assert page.get_by_label("Sample configuration").input_value() == str(saved["id"])

        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        text = Path(download.value.path()).read_text(encoding="utf-8")
        _settled(page)

        assert "NM1*85*2*RIVERSIDE FAMILY CLINIC" in text, "the sample's provider is kept"
        codes = {s.split("*")[1].split(":")[1] for s in text.split("~") if s.strip().startswith("SV1*")}
        assert codes and not codes <= {"99213", "85025"}, "procedure codes still the sample's"
        failing = [(lv["level"], lv["errors"]) for lv in validate(text)["levels"]
                   if lv["level"] <= 4 and lv["errors"]]
        assert not failing, f"SNIP errors by level: {failing}"

        page.get_by_role("button", name="Run SNIP validation").click()
        _settled(page)
        assert page.locator(".snip__summary").inner_text()
    finally:
        # Only the config this test saved, by the id the name it chose found.
        sample_gen_configs.delete_config(saved["id"])


def test_5b2_a_sample_uploaded_with_no_setup_is_accepted_and_generated_unsaved(
        page, kept, browser_page, server):
    """Layouts and samples › Upload sample with an 837: its format is detected,
    it opens under Generate › Files read against the standard X12 configuration,
    Accept all takes the suggestions and Generate file writes it with nothing
    saved. The run keeps the configuration and facts about the sample, never the
    sample (user decision 2026-11-06): Rerun says it can't be rerun, and why.
    A run whose sample came from a saved configuration reruns -- and once that
    sample changes, Rerun is refused with an alert that stays."""
    from datetime import datetime, timezone

    from sdg.api.app import UNSAVED_CHANGED, UNSAVED_RERUN
    from sdg.analyzer.services import generation_requests as gr
    from sdg.analyzer.services import sample_gen_configs
    from sdg.writers.snip import validate

    sample = (ROOT / "tests" / "fixtures" / "edi" / "claim.837").read_text(encoding="utf-8")
    pattern_id = kept[-1]["pattern_id"]
    configs_before = {c["id"] for c in sample_gen_configs.list_configs()}
    opened: list[int] = []
    made_configs: list[int] = []

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    try:
        # 1. Upload, where samples and layouts are kept: no format, no base, no Analyze.
        _go(page, "Layouts and samples", "File layouts")
        page.locator("input[aria-label='Sample to upload']").set_input_files(files=[{
            "name": "claim.837", "mimeType": "text/plain", "buffer": sample.encode()}])
        _heading(page, "Generate")
        page.locator(".tabs [role=tab][aria-selected=true]", has_text="Custom format").wait_for()
        _analyzed(page)
        assert page.get_by_label("Pattern", exact=True).input_value() == f"saved:{pattern_id}"
        base = page.get_by_label("Base configuration")
        assert base.input_value() == "auto"
        assert "Standard: X12 837" in base.locator("option:checked").inner_text()
        page.locator(".chip", has_text="Based on standard X12").wait_for()

        # 2. The suggestions, accepted in one click.
        page.locator(".chip", has_text="suggestions").wait_for()
        procedure = page.get_by_label("Rule for SV1 SV101-2")
        suggested = procedure.input_value()
        page.get_by_role("button", name="Accept all").click()
        assert page.get_by_role("button", name="Accept all").count() == 0
        if not suggested:
            assert procedure.input_value(), "the procedure code's suggestion was not taken"

        # 3. Generate file, with nothing saved.
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        _settled(page)
        text = Path(download.value.path()).read_text(encoding="utf-8")
        run = int(re.search(r"request-(\d+)", download.value.suggested_filename).group(1))
        opened.append(run)
        page.locator(".forwhom__recorded", has_text=f"run {run}").wait_for()
        assert text.startswith("ISA") and "CLM*" in text, text[:300]
        assert "NM1*85*2*RIVERSIDE FAMILY CLINIC" in text, "the sample's provider is kept"
        failing = [(lv["level"], lv["errors"]) for lv in validate(text)["levels"]
                   if lv["level"] <= 4 and lv["errors"]]
        assert not failing, f"SNIP errors by level: {failing}"
        assert {c["id"] for c in sample_gen_configs.list_configs()} == configs_before, \
            "a configuration was saved"

        first = gr.get_file(gr.first_file(run)["id"])
        kept_config = gr.snapshot(first["snapshots"][first["format"]])
        assert "sample_text" not in kept_config, "the run kept the uploaded sample"
        assert "RIVERSIDE FAMILY CLINIC" not in gr.dumps(kept_config), \
            "the sample's content is in the run"
        assert kept_config["sample"]["size"] == len(sample.encode("utf-8")), kept_config["sample"]

        # 4. Rerun from History: the run cannot write the file again without
        # the sample, and History says so in one sentence; no file is written.
        _go(page, "History")
        card = page.locator(".requests__card", has_text=f"Run {run} ")
        card.wait_for(timeout=LONG)
        assert "Custom format" in card.inner_text()
        def files_of_run() -> int:
            return len(next(r for r in gr.list_requests(pattern_id=pattern_id)
                            if r["id"] == run)["files"])

        files_before = files_of_run()
        trouble = browser_page[1]

        def conflicts() -> list[str]:
            return [s for s in trouble.seen if "409" in s]

        # Unavailable, said before it is pressed: focusable, the reason shown
        # in its row and describing the button; pressed, nothing is sent and
        # nothing is written.
        rerun = card.get_by_role("button", name="Rerun").first
        assert rerun.get_attribute("aria-disabled") == "true"
        reason = card.locator(".requests__refusal").first
        assert reason.is_visible() and reason.inner_text().strip() == UNSAVED_RERUN
        assert rerun.get_attribute("aria-describedby") == reason.get_attribute("id")
        assert not conflicts()
        rerun.click(force=True)
        _settled(page)
        assert not conflicts(), "an unavailable Rerun was sent"
        assert page.locator(".toast--alarm").count() == 0
        assert card.locator(".chip", has_text="Identical file").count() == 0
        assert files_of_run() == files_before, "a refused Rerun recorded a file"

        # 5. A run of a configuration read from a saved one, no sample
        # uploaded: Rerun is offered. Its sample changed after History was
        # listed, Rerun is refused with an alert kept until dismissed.
        def call(path, body, method="POST"):
            request = urllib.request.Request(
                server + path, data=json.dumps(body).encode(), method=method,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=LONG // 1000) as r:
                return r.headers, r.read()

        from sdg.writers.samples import standard

        read = json.loads(call("/api/samples/read", {"defaultId": "hl7-adt"})[1])
        config = json.loads(call("/api/samples/configs", {
            "name": f"SDMT browser source {time.time_ns()}", "rows": read["rows"],
            "basedOn": read["basedOn"], "fromDefaultId": "hl7-adt"})[1])
        made_configs.append(config["id"])
        read = json.loads(call("/api/samples/read", {"configId": config["id"]})[1])
        rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        headers, _ = call("/api/export", {
            "format": "unsaved", "subject": "members", "pattern_id": pattern_id,
            "members": 2, "record": True, "request_started": started, "file_name": "adt.hl7",
            "sample_config": {"sample": None, "sampleName": read["sampleName"],
                              "basedOn": read["basedOn"], "rows": rows, "vary": read["vary"],
                              "fromConfigId": config["id"], "fromDefaultId": None}})
        second = int(headers["X-SDM-Request"])
        opened.append(second)
        _go(page, "Generate", "Files")
        _go(page, "History")
        other = page.locator(".requests__card", has_text=f"Run {second} ")
        other.wait_for(timeout=LONG)
        offered = other.get_by_role("button", name="Rerun").first
        assert offered.get_attribute("aria-disabled") is None and offered.is_enabled()
        changed = standard.files("hl7-adt")["sample"].replace("DOE^JANE", "POE^JUNE")
        call(f"/api/samples/configs/{config['id']}", {
            "name": config["name"], "sample": changed, "rows": read["rows"]}, method="PUT")
        def files_of(run_id: int) -> int:
            return len(next(r for r in gr.list_requests(pattern_id=pattern_id)
                            if r["id"] == run_id)["files"])

        second_files = files_of(second)
        assert not conflicts()
        offered.click()
        alert = page.locator(".toast--alarm[role=alert]")
        alert.wait_for(timeout=LONG)
        assert alert.locator(".toast__message").inner_text().strip() == UNSAVED_CHANGED
        _settled(page)
        assert len(conflicts()) == 1, conflicts()
        # It stays: still there after a while, until dismissed.
        page.wait_for_timeout(1500)
        assert alert.count() == 1
        alert.locator(".toast__close").click()
        assert page.locator(".toast--alarm").count() == 0
        assert files_of(second) == second_files, "a refused Rerun recorded a file"
        # The refusal was the answer, not a fault: only its own 409 is removed.
        trouble.seen.remove(conflicts()[0])
        _go(page, "Generate", "Files")
    finally:
        for request_id in opened:
            gr.delete_request(request_id)
        for config_id in made_configs:
            sample_gen_configs.delete_config(config_id)


def test_5c_fhir_claims_are_modeled_on_a_bundle_the_same_way(page, kept):
    """The same screen, steps and engine as 5b, for a FHIR Bundle and the
    masking tool's FHIR config: the format is read off the sample."""
    import json as _json

    from sdg.analyzer.services import sample_gen_configs

    fixtures = ROOT / "tests" / "fixtures" / "fhir"
    sample = (fixtures / "claims-bundle.json").read_text(encoding="utf-8")
    master = (fixtures / "master-fhir-masking.json").read_text(encoding="utf-8")
    name = f"browser test fhir {int(time.time())}"
    pattern_id = kept[-1]["pattern_id"]

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    page.get_by_role("tab", name="Custom format").click()
    page.locator("input[aria-label='Sample file']").set_input_files(files=[{
        "name": "claims-bundle.json", "mimeType": "application/json",
        "buffer": sample.encode()}])
    _analyzed(page)
    page.get_by_label("Base configuration").select_option("file")
    page.locator("input[aria-label='Configuration file']").set_input_files(files=[{
        "name": "master-fhir-masking.json", "mimeType": "application/json",
        "buffer": master.encode()}])
    page.locator(".chip", has_text="Based on master-fhir-masking.json").wait_for(timeout=LONG)
    _analyzed(page)

    page.locator(".chip", has_text="patients").wait_for()
    assert page.get_by_label("Rule for Patient name[*].family").input_value() == "LAST_NAME"
    assert page.get_by_label("Rule for Organization name").input_value() == ""
    page.get_by_role("button", name="Accept").click()
    page.get_by_label("New configuration name").fill(name)
    page.get_by_role("button", name="Save configuration as").click()
    _settled(page)
    page.locator(".chip", has_text=f"saved as {name}").wait_for()
    saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
    try:
        assert (saved["format"], saved["kind"], saved["subject"]) == ("fhir", "claims", "claims")
        assert page.get_by_role("button", name="Run SNIP validation").count() == 0, "SNIP is X12's"
        page.get_by_role("button", name="Run FHIR validation").click()
        _settled(page)
        summary = page.locator(".snip__summary").inner_text()
        assert "No errors" in summary and "resources" in summary, summary

        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        doc = _json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
        _settled(page)

        resources = [e["resource"] for e in doc["entry"]]
        patients = {f"Patient/{r['id']}" for r in resources if r["resourceType"] == "Patient"}
        claims = [r for r in resources if r["resourceType"] == "Claim"]
        assert patients and claims, "members and their claims written"
        assert {c["patient"]["reference"] for c in claims} <= patients, "claims point at their member"
        assert {r["id"] for r in resources if r["resourceType"] == "Organization"} == {"hl7"}
    finally:
        sample_gen_configs.delete_config(saved["id"])


def test_5d_cda_documents_are_modeled_on_a_ccd_the_same_way(page, kept):
    """The same screen again, for a CCD and the masking tool's CDA config: a
    document per claim, downloaded as a zip of documents."""
    import io as _io
    import zipfile as _zipfile

    from sdg.analyzer.services import sample_gen_configs

    fixtures = ROOT / "tests" / "fixtures" / "cda"
    sample = (fixtures / "ccd.xml").read_text(encoding="utf-8")
    master = (fixtures / "master-cda-masking.json").read_text(encoding="utf-8")
    name = f"browser test cda {int(time.time())}"
    pattern_id = kept[-1]["pattern_id"]

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    page.get_by_role("tab", name="Custom format").click()
    page.locator("input[aria-label='Sample file']").set_input_files(files=[{
        "name": "ccd.xml", "mimeType": "application/xml", "buffer": sample.encode()}])
    _analyzed(page)
    page.get_by_label("Base configuration").select_option("file")
    page.locator("input[aria-label='Configuration file']").set_input_files(files=[{
        "name": "master-cda-masking.json", "mimeType": "application/json",
        "buffer": master.encode()}])
    page.locator(".chip", has_text="Based on master-cda-masking.json").wait_for(timeout=LONG)
    _analyzed(page)

    page.locator(".chip", has_text="sections").wait_for()
    family = "Rule for recordTarget/patientRole/patient/name/family"
    assert page.get_by_label(family).input_value() == "LAST_NAME"
    author = "Rule for author/assignedAuthor/assignedPerson/name/family"
    assert page.get_by_label(author).input_value() == ""
    page.get_by_role("button", name="Accept").click()
    page.get_by_label("New configuration name").fill(name)
    page.get_by_role("button", name="Save configuration as").click()
    _settled(page)
    page.locator(".chip", has_text=f"saved as {name}").wait_for()
    saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
    try:
        assert (saved["format"], saved["kind"], saved["subject"]) == ("cda", "CCD", "claims")
        page.get_by_role("button", name="Preview", exact=True).click()
        _settled(page)
        assert page.locator(".edi__sample").inner_text().startswith("<?xml")
        page.get_by_role("button", name="Run HL7 v3 validation").click()
        _settled(page)
        summary = page.locator(".snip__summary").inner_text()
        assert "No errors" in summary and "documents" in summary, summary

        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        assert download.value.suggested_filename.endswith(".zip")
        packed = Path(download.value.path()).read_bytes()
        _settled(page)
        with _zipfile.ZipFile(_io.BytesIO(packed)) as archive:
            docs = [archive.read(n).decode("utf-8") for n in archive.namelist()]
        assert docs and all("<ClinicalDocument" in d for d in docs)
        assert all("<family>Primary</family>" in d for d in docs), "the author is the sample's"
    finally:
        sample_gen_configs.delete_config(saved["id"])


def test_5e_hl7_messages_are_modeled_on_an_adt_and_checked_the_same_way(page, kept):
    """The same screen for an HL7 v2 ADT and the masking tool's HL7 config: a
    message per member, checked as HL7 (structure, control ids, dates, tables)."""
    from sdg.analyzer.services import sample_gen_configs

    fixtures = ROOT / "tests" / "fixtures" / "hl7"
    sample = (fixtures / "adt_a01.hl7").read_bytes()
    master = (fixtures / "master-hl7-masking.json").read_bytes()
    name = f"browser test hl7 {int(time.time())}"
    pattern_id = kept[-1]["pattern_id"]

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    page.get_by_role("tab", name="Custom format").click()
    page.locator("input[aria-label='Sample file']").set_input_files(files=[{
        "name": "adt_a01.hl7", "mimeType": "text/plain", "buffer": sample}])
    _analyzed(page)
    page.get_by_label("Base configuration").select_option("file")
    page.locator("input[aria-label='Configuration file']").set_input_files(files=[{
        "name": "master-hl7-masking.json", "mimeType": "application/json",
        "buffer": master}])
    page.locator(".chip", has_text="Based on master-hl7-masking.json").wait_for(timeout=LONG)
    _analyzed(page)

    page.locator(".chip", has_text="messages").wait_for()
    page.get_by_role("button", name="Accept").click()
    page.get_by_label("New configuration name").fill(name)
    page.get_by_role("button", name="Save configuration as").click()
    _settled(page)
    page.locator(".chip", has_text=f"saved as {name}").wait_for()
    saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
    try:
        assert (saved["format"], saved["kind"], saved["subject"]) == ("hl7v2", "ADT", "members")
        assert page.get_by_role("button", name="Run SNIP validation").count() == 0
        page.get_by_role("button", name="Run HL7 v2 validation").click()
        _settled(page)
        summary = page.locator(".snip__summary").inner_text()
        assert "No errors" in summary and "messages" in summary, summary
    finally:
        sample_gen_configs.delete_config(saved["id"])


def test_5f_fhir_hl7_and_cda_tabs_open_on_their_standard_layouts(page, kept):
    """FHIR, HL7 v2 and CDA have tabs of their own, as EDI has, each opened on
    the standard sample and master layout SDM ships: the places are listed at
    once, a changed mapping is saved as a new config of that format only, and
    the file is written with no sample of the customer's."""
    import io as _io
    import json as _json
    import zipfile as _zipfile

    from sdg.analyzer.services import sample_gen_configs

    stamp = int(time.time())
    pattern_id = kept[-1]["pattern_id"]
    made: list[int] = []

    def save(name: str, family: str, kind: str) -> dict:
        page.get_by_label("New configuration name").fill(name)
        page.get_by_role("button", name="Save configuration as").click()
        _settled(page)
        page.locator(".chip", has_text=f"saved as {name}").wait_for()
        saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
        made.append(saved["id"])
        assert (saved["format"], saved["kind"]) == (family, kind), saved
        assert saved["based_on"].startswith("standard"), saved
        return saved

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    try:
        # FHIR: opens on the members Bundle, mapped by the master layout.
        page.get_by_role("tab", name="FHIR", exact=True).click()
        page.locator(".edi-sample__table").wait_for(timeout=LONG)
        assert page.get_by_label("Master layout").input_value() == "std:fhir-members"
        assert "(standard)" in page.locator(".edi__pick").first.inner_text(), "the standard sample"
        family = page.get_by_label("Rule for Patient name[*].family")
        assert family.input_value() == "LAST_NAME"
        family.select_option("")  # a changed mapping: keep the sample's surname
        save(f"browser std fhir {stamp}", "fhir", "members")
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        doc = _json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
        _settled(page)
        assert any(e["resource"]["resourceType"] == "Patient" for e in doc["entry"])

        # HL7 v2: the ORU standard, chosen from the master layouts, and checked.
        page.get_by_role("tab", name="HL7 v2").click()
        page.locator(".edi-sample__table").wait_for(timeout=LONG)
        assert page.get_by_label("Master layout").input_value() == "std:hl7-adt"
        page.get_by_label("Master layout").select_option("std:hl7-oru")
        _settled(page)
        page.locator(".chip", has_text="ORU").first.wait_for()
        save(f"browser std hl7 {stamp}", "hl7v2", "ORU")
        page.get_by_role("button", name="Run HL7 v2 validation").click()
        _settled(page)
        summary = page.locator(".snip__summary").inner_text()
        assert "No errors" in summary, summary

        # CDA: the CCD, written as a zip of documents.
        page.get_by_role("tab", name="HL7 v3", exact=True).click()
        page.locator(".edi-sample__table").wait_for(timeout=LONG)
        assert page.get_by_label("Master layout").input_value() == "std:cda-ccd"
        names = page.get_by_label("Sample configuration").locator("option").all_inner_texts()
        assert not any("browser std hl7" in n or "browser std fhir" in n for n in names),             "a tab lists only its own format's configs"
        save(f"browser std cda {stamp}", "cda", "CCD")
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        packed = Path(download.value.path()).read_bytes()
        _settled(page)
        with _zipfile.ZipFile(_io.BytesIO(packed)) as archive:
            assert archive.namelist()
            assert all("<ClinicalDocument" in archive.read(n).decode("utf-8")
                       for n in archive.namelist())
    finally:
        for config_id in made:
            sample_gen_configs.delete_config(config_id)


def test_5f2_an_element_is_made_optional_and_repeated_and_the_file_varies(page, kept):
    """On the FHIR tab a Patient's telecom is made optional and repeated 1 to 3
    times: saved with the config, read back with it, and the file written has
    patients without one and patients with several."""
    import json as _json

    from sdg.analyzer.services import sample_gen_configs

    name = f"browser vary fhir {int(time.time())}"
    made: list[int] = []
    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{kept[-1]['pattern_id']}")
    try:
        page.get_by_role("tab", name="FHIR", exact=True).click()
        page.locator(".edi-sample__table").first.wait_for(timeout=LONG)
        at = "Patient telecom[*]"
        page.get_by_label("Element to make optional or repeat").fill(at)
        page.get_by_role("button", name="Add", exact=True).click()
        page.get_by_label(f"Share of documents with {at}").fill("70")
        page.get_by_label(f"Fewest times {at} occurs").fill("1")
        page.get_by_label(f"Most times {at} occurs").fill("3")
        page.get_by_label("New configuration name").fill(name)
        page.get_by_role("button", name="Save configuration as").click()
        _settled(page)
        page.locator(".chip", has_text=f"saved as {name}").wait_for()
        saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
        made.append(saved["id"])
        assert sample_gen_configs.get_config(saved["id"])["config"]["vary"] == [
            {"at": at, "present": 70, "min": 1, "max": 3}]
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        doc = _json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
        _settled(page)
        phones = {len(e["resource"].get("telecom", [])) for e in doc["entry"]
                  if e["resource"]["resourceType"] == "Patient"}
        assert 0 in phones and max(phones) > 1, phones
        # Opened again, the config shows what it varies.
        page.get_by_label("Master layout").select_option(str(saved["id"]))
        _settled(page)
        page.locator(".edi-sample__vary tbody tr", has_text=at).wait_for(timeout=LONG)
    finally:
        for config_id in made:
            sample_gen_configs.delete_config(config_id)


def test_5f4_hl7_v2_segments_and_fields_are_optional_or_repeat_in_a_mix(page, kept):
    """On the HL7 v2 tab an NK1 is in 80% of messages, once or twice in a mix,
    and PID-13 holds exactly two phone numbers: saved with the config, and the
    messages written vary so."""
    from collections import Counter

    from sdg.analyzer.services import sample_gen_configs

    name = f"browser vary hl7 {int(time.time())}"
    made: list[int] = []
    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{kept[-1]['pattern_id']}")
    try:
        page.get_by_role("tab", name="HL7 v2", exact=True).click()
        page.locator(".edi-sample__table").first.wait_for(timeout=LONG)
        for at in ("NK1", "PID-13"):
            page.get_by_label("Element to make optional or repeat").fill(at)
            page.get_by_role("button", name="Add", exact=True).click()
        page.get_by_label("Share of documents with NK1").fill("80")
        page.get_by_label("How many times NK1 occurs").select_option("mix")
        page.get_by_label("Mix of times NK1 occurs").fill("1:50, 2:50")
        page.get_by_label("How many times PID-13 occurs").select_option("exactly")
        page.get_by_label("Exact count of PID-13").fill("2")
        page.get_by_label("New configuration name").fill(name)
        page.get_by_role("button", name="Save configuration as").click()
        _settled(page)
        page.locator(".chip", has_text=f"saved as {name}").wait_for()
        saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
        made.append(saved["id"])
        assert sample_gen_configs.get_config(saved["id"])["config"]["vary"] == [
            {"at": "NK1", "present": 80, "mix": [[1, 50.0], [2, 50.0]]},
            {"at": "PID-13", "present": 100, "min": 2, "max": 2}]
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        text = Path(download.value.path()).read_text(encoding="utf-8").replace("\r", "\n")
        _settled(page)
        messages = text.split("MSH|")[1:]
        assert set(Counter(m.count("\nNK1|") for m in messages)) == {0, 1, 2}
        phones = [l.split("|")[13] for l in text.split("\n") if l.startswith("PID|")]
        assert phones and all(p.count("~") == 1 for p in phones)
        page.get_by_role("button", name="Run HL7 v2 validation").click()
        _settled(page)
        summary = page.locator(".snip__summary").inner_text()
        assert "No errors" in summary and "messages" in summary, summary
    finally:
        for config_id in made:
            sample_gen_configs.delete_config(config_id)


def test_5f3_hl7v3_messages_are_written_from_the_standard_patient_registry_add(page, kept):
    """The HL7 v3 tab offers a patient registry message (PRPA_IN201301UV02)
    beside the C-CDA: chosen, its telecoms made optional and repeating, saved,
    written as a zip of messages, one per member, and checked."""
    import io as _io
    import zipfile as _zipfile

    from sdg.analyzer.services import sample_gen_configs

    name = f"browser v3 add {int(time.time())}"
    telecom = "controlActProcess/subject/registrationEvent/subject1/patient/patientPerson/telecom"
    made: list[int] = []
    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{kept[-1]['pattern_id']}")
    try:
        page.get_by_role("tab", name="HL7 v3", exact=True).click()
        page.locator(".edi-sample__table").first.wait_for(timeout=LONG)
        page.get_by_label("Master layout").select_option("std:hl7v3-prpa-add")
        _settled(page)
        page.locator(".chip", has_text="PRPA_IN201301UV02").first.wait_for(timeout=LONG)
        page.get_by_label("Element to make optional or repeat").fill(telecom)
        page.get_by_role("button", name="Add", exact=True).click()
        page.get_by_label(f"Fewest times {telecom} occurs").fill("1")
        page.get_by_label(f"Most times {telecom} occurs").fill("4")
        page.get_by_label("New configuration name").fill(name)
        page.get_by_role("button", name="Save configuration as").click()
        _settled(page)
        page.locator(".chip", has_text=f"saved as {name}").wait_for()
        saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
        made.append(saved["id"])
        assert (saved["format"], saved["kind"]) == ("cda", "PRPA_IN201301UV02"), saved
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        packed = Path(download.value.path()).read_bytes()
        _settled(page)
        with _zipfile.ZipFile(_io.BytesIO(packed)) as archive:
            names = archive.namelist()
            texts = [archive.read(n).decode("utf-8") for n in names]
        assert names[0] == "PRPA_IN201301UV02-00001.xml"
        assert all("<PRPA_IN201301UV02" in t for t in texts)
        assert len({t.count("<telecom") for t in texts}) > 1, "telecoms vary"
        page.get_by_role("button", name="Run HL7 v3 validation").click()
        _settled(page)
        summary = page.locator(".snip__summary").inner_text()
        assert "no errors" in summary.lower() and "messages" in summary, summary
    finally:
        for config_id in made:
            sample_gen_configs.delete_config(config_id)


def test_5a_generate_file_on_a_pattern_downloads_an_834_recorded_as_a_run(page, kept):
    """Generate file ▾ › X12 834 on a found pattern: the pattern is saved, the
    file downloads at once with 100 new members, and the confirmation names
    the run, which History shows with the file."""
    from sdg.writers.snip import validate

    _find_patterns(page)
    card = _open_first_pattern(page)
    before = len(kept)
    card.get_by_role("button", name="Generate file").click()
    menu = card.get_by_role("menu", name="Generate file")
    labels = [t.strip() for t in menu.get_by_role("menuitem").all_inner_texts()]
    assert labels == ["X12 834", "CSV", "FHIR", "HL7 v2", "HL7 v3", "More options…"], labels
    with page.expect_download(timeout=LONG) as download:
        menu.get_by_role("menuitem", name="X12 834").click()
    _settled(page)
    name = download.value.suggested_filename
    assert re.fullmatch(r"834-request-\d+\.txt", name), name
    run = int(re.search(r"request-(\d+)", name).group(1))
    text = Path(download.value.path()).read_text(encoding="utf-8")
    assert text.startswith("ISA") and text.count("INS*") == 100, text[:300]
    failing = [(lv["level"], lv["errors"]) for lv in validate(text)["levels"] if lv["errors"]]
    assert not failing, f"SNIP errors by level: {failing}"

    # Saved first, in the same click, so the run is kept against it.
    card.get_by_role("button", name="Saved").wait_for()
    assert len(kept) == before + 1
    toast = page.locator(".toast")
    assert toast.locator(".toast__message").inner_text() == f"Created X12 834 file · Run {run}"

    toast.get_by_role("button", name="View in History").click()
    _heading(page, "History")
    focused = page.locator(".requests__card--focus")
    focused.wait_for(timeout=LONG)
    assert focused.locator("h3").inner_text().startswith(f"Run {run}")
    assert kept[-1]["name"] in focused.locator("h3").inner_text()
    # Kept under the name it was asked for; the download adds the run.
    focused.locator("td", has_text="834.txt").first.wait_for()


def test_5h_a_saved_pattern_generates_a_file_from_its_row_with_the_patterns_values(page, kept):
    """Saved Patterns has Generate a file, as a found pattern's card has: it
    opens Files written for that pattern, and the standard FHIR layout writes
    the pattern's sex (the master masks gender not, so SDM generates it)."""
    import json as _json

    from sdg.analyzer.services import sample_gen_configs

    women = next(k for k in kept if k["name"] == "SDMT women 30 to 45")
    made: list[int] = []
    _go(page, "Patterns")
    _chip(page, re.compile(r"^Saved"))
    row = page.locator("li.kept__row").filter(
        has=page.locator(".kept__name", has_text=re.compile(r"^SDMT women 30 to 45$"))).first
    # Straight from the row: FHIR through the standard layout, nothing saved
    # first, 100 new members carrying the pattern's sex.
    row.get_by_role("button", name="Generate file").click()
    with page.expect_download(timeout=LONG) as download:
        row.get_by_role("menuitem", name="FHIR", exact=True).click()
    _settled(page)
    assert re.fullmatch(r"fhir-members-request-\d+\.json", download.value.suggested_filename)
    doc = _json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
    genders = [e["resource"]["gender"] for e in doc["entry"]
               if e["resource"]["resourceType"] == "Patient"]
    assert len(genders) == 100 and set(genders) == {"female"}, genders
    page.locator(".toast", has_text=re.compile(r"^Created FHIR file · Run \d+")).wait_for()
    page.locator(".toast").get_by_role("button", name="Dismiss").click()

    # More options: Generate › Files with the pattern chosen.
    row.get_by_role("button", name="Generate file").click()
    row.get_by_role("menuitem", name="More options…").click()
    page.wait_for_function(
        "() => document.querySelector('.app__heading')?.textContent.trim() === 'Generate'",
        timeout=LONG)
    assert page.get_by_label("Pattern", exact=True).input_value() == f"saved:{women['pattern_id']}"
    try:
        page.get_by_label("New members").fill("12")
        page.get_by_role("tab", name="FHIR", exact=True).click()
        page.locator(".edi-sample__table").wait_for(timeout=LONG)
        _settled(page)
        assert page.get_by_label("Rule for Patient gender").input_value() == "SEX"
        name = f"browser saved row {int(time.time())}"
        page.get_by_label("New configuration name").fill(name)
        page.get_by_role("button", name="Save configuration as").click()
        _settled(page)
        page.locator(".chip", has_text=f"saved as {name}").wait_for()
        made.append(next(c for c in sample_gen_configs.list_configs() if c["name"] == name)["id"])
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        doc = _json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
        _settled(page)
        genders = [e["resource"]["gender"] for e in doc["entry"]
                   if e["resource"]["resourceType"] == "Patient"]
        assert len(genders) == 12 and set(genders) == {"female"}, genders
    finally:
        for config_id in made:
            sample_gen_configs.delete_config(config_id)


def test_5i_files_are_written_under_a_request_and_written_again_from_it(page, kept):
    """Every download is a run of new people; Options › Same members as run
    writes another file for an earlier run's people; and a file written again
    from the run's record alone is the same file."""
    from sdg.analyzer.services import generation_requests as gr

    women = next(k for k in kept if k["name"] == "SDMT women 30 to 45")
    opened: list[int] = []

    def subscribers(text: str) -> set[str]:
        return {s.split("*")[2].rstrip("~") for s in text.splitlines() if s.startswith("REF*0F")}

    def write() -> str:
        with page.expect_download(timeout=LONG) as download:
            page.get_by_role("button", name="Generate file").click()
        _settled(page)
        return Path(download.value.path()).read_text(encoding="utf-8")

    def recorded(where=None) -> int:
        """The run the last download was recorded as, as the screen says it."""
        said = (where or page).locator(".forwhom__recorded")
        said.wait_for()
        return int(re.search(r"run (\d+)", said.inner_text()).group(1))

    try:
        _go(page, "Generate", "Files")
        page.get_by_label("Pattern", exact=True).select_option(f"saved:{women['pattern_id']}")
        page.get_by_label("New members").fill("6")
        # No run to start by hand: each download starts one.
        assert page.get_by_role("button", name="New run").count() == 0
        page.get_by_role("tab", name="X12 EDI").click()
        page.get_by_label("Transaction set").select_option("834")

        first = write()
        request_id = recorded()
        opened.append(request_id)
        assert first.count("INS*") == 6

        second = write()
        page.locator(".forwhom__recorded", has_text=re.compile(r"run (?!%d\b)\d+" % request_id)) \
            .wait_for()
        opened.append(recorded())
        assert opened[1] != opened[0]
        assert not subscribers(first) & subscribers(second), "a new run repeated people"

        # The same people again: Options › Same members as run.
        page.locator(".forwhom__options summary").click()
        page.get_by_label("Same members as run").select_option(str(request_id))
        assert page.get_by_label("Pattern", exact=True).input_value() == f"request:{request_id}"
        page.get_by_label("Transaction set").select_option("834")
        assert subscribers(write()) == subscribers(first)
        assert recorded() == request_id

        _go(page, "History")
        card = page.locator(".requests__card", has_text=f"Run {request_id} ")
        card.wait_for(timeout=LONG)
        with page.expect_download(timeout=LONG) as download:
            card.get_by_role("button", name="Rerun").first.click()
        _settled(page)
        assert Path(download.value.path()).read_text(encoding="utf-8") == first
        card.locator(".chip", has_text="Identical file").first.wait_for()

        # More files for the same people, later: from the run's card, which
        # opens Generate › Files on X12 EDI for that run.
        card.get_by_role("button", name=re.compile("Generate other format")).click()
        _heading(page, "Generate")
        _on_tab(page, "Files")
        assert page.get_by_label("Pattern", exact=True).input_value() == f"request:{request_id}"
        assert page.get_by_label("Same members as run").input_value() == str(request_id)
        page.get_by_label("Transaction set").select_option("834")
        assert subscribers(write()) == subscribers(first)
        assert recorded() == request_id
        files = [r for r in gr.list_requests() if r["id"] == request_id][0]["files"]
        assert len([f for f in files if f["replay_of"] is None]) == 3

        # Claims of a claims pattern for these members: what the members
        # override is said first, and the claims are a run of their own.
        panel = page.locator(".claimsfor")
        claims_patterns = panel.get_by_label("Claims pattern").locator("option").all()
        assert len(claims_patterns) > 1, "no saved claims pattern to choose"
        panel.get_by_label("Claims pattern").select_option(
            claims_patterns[1].get_attribute("value"))
        panel.locator(".claimsfor__features li", has_text="Gender").wait_for(timeout=120_000)
        page.get_by_label("Transaction set").select_option("837P")
        billed = write()
        names = {tuple(s.split("*")[3:5]) for s in first.splitlines() if s.startswith("NM1*IL")}
        assert set(_patients(billed)) <= names
        said = panel.locator(".forwhom__recorded")
        said.wait_for()
        assert f"linked to run {request_id}" in said.inner_text()
        opened.append(recorded(panel))
        assert opened[-1] not in (request_id, opened[1])
    finally:
        for request_id in opened:
            gr.delete_request(request_id)


def test_5j_a_database_load_is_regenerated_as_evidence_from_its_request(page):
    """A load kept for audit is listed under History, and Rerun gives its
    rows again, as CSV with their evidence, without writing to a database."""
    import zipfile

    from sdg.analyzer.services import generation_requests as gr
    from tests.test_load_requests import _load, _plan

    plan = _plan()
    plan.tables = plan.tables[1:]
    plan.tables[0].fields = plan.tables[0].fields[:2]
    kept, _ = _load(plan, plan)
    try:
        _go(page, "History")
        card = page.locator(".requests__card", has_text=f"Run {kept['request']} ")
        card.wait_for(timeout=LONG)
        card.get_by_text("Loaded into SDM_TEST_TARGET").wait_for()
        card.get_by_text("25 rows").wait_for()
        assert card.get_by_role("button", name=re.compile("Generate other format")).count() == 0
        with page.expect_download(timeout=LONG) as download:
            card.get_by_role("button", name="Rerun").click()
        _settled(page)
        assert download.value.suggested_filename == (
            f"request-{kept['request']}-SDM_TEST_TARGET-load.zip")
        with zipfile.ZipFile(download.value.path()) as archive:
            evidence = json.loads(archive.read("evidence.json"))
            assert len(archive.read("dbo.MEMBER.csv").decode().splitlines()) == 26
        assert evidence["matched"], evidence
        card.locator(".chip", has_text="Identical rows").first.wait_for()
    finally:
        gr.delete_request(kept["request"])


def test_5g_an_835_config_is_saved_changed_in_place_previewed_and_removed(page, kept):
    """An 835 remittance read with no master, saved, then opened again with
    Edit config: one place set back to the sample's and the name changed are
    saved to the same config, the first 100 lines of the file shown, and the
    config removed."""
    from sdg.analyzer.services import sample_gen_configs

    sample = (ROOT / "tests" / "fixtures" / "edi" / "remit.835").read_bytes()
    name = f"browser test 835 {int(time.time())}"
    pattern_id = kept[-1]["pattern_id"]

    _go(page, "Generate", "Files")
    page.get_by_label("Pattern", exact=True).select_option(f"saved:{pattern_id}")
    page.get_by_role("tab", name="Custom format").click()
    page.locator("input[aria-label='Sample file']").set_input_files(files=[{
        "name": "remit.835", "mimeType": "text/plain", "buffer": sample}])
    _analyzed(page)
    page.get_by_label("Base configuration").select_option("none")
    page.wait_for_function(
        "() => ![...document.querySelectorAll('.chip')].some(c => c.textContent.startsWith('Based on'))",
        timeout=LONG)
    _analyzed(page)
    page.locator(".chip", has_text="service lines").wait_for()
    page.get_by_role("button", name="Accept").click()
    page.get_by_label("New configuration name").fill(name)
    page.get_by_role("button", name="Save configuration as").click()
    _settled(page)
    page.locator(".chip", has_text=f"saved as {name}").wait_for()
    saved = next(c for c in sample_gen_configs.list_configs() if c["name"] == name)
    try:
        assert (saved["format"], saved["kind"], saved["subject"]) == ("x12", "835", "claims")

        page.get_by_role("button", name="Edit config").click()
        _settled(page)
        page.get_by_label("Configuration name", exact=True).wait_for()
        assert page.get_by_label("Configuration name", exact=True).input_value() == name
        page.get_by_label("Rule for CLP CLP07").select_option("")
        page.get_by_label("Configuration name", exact=True).fill(name + " v2")
        page.get_by_role("button", name="Save changes").click()
        _settled(page)
        page.locator(".chip", has_text=f"saved as {name} v2").wait_for()
        after = next(c for c in sample_gen_configs.list_configs() if c["id"] == saved["id"])
        assert after["name"] == name + " v2", "the same config, renamed"
        assert after["positions"] == saved["positions"] - 1
        assert not any(c["name"] == name for c in sample_gen_configs.list_configs())

        page.get_by_role("button", name="Preview", exact=True).click()
        _settled(page)
        shown = page.locator(".edi__sample").inner_text().splitlines()
        assert shown[0].startswith("ISA") and len(shown) <= 100
        assert any(line.startswith("BPR") for line in shown)

        # Delete asks first: Cancel keeps it, confirming removes it.
        page.get_by_role("button", name="Delete configuration").click()
        page.get_by_role("button", name="Cancel", exact=True).click()
        assert any(c["id"] == saved["id"] for c in sample_gen_configs.list_configs())
        page.get_by_role("button", name="Delete configuration").click()
        page.get_by_role("button", name=f"Delete {name} v2").click()
        _settled(page)
        page.get_by_role("button", name="Delete configuration").wait_for(state="detached")
        assert not any(c["id"] == saved["id"] for c in sample_gen_configs.list_configs())
    finally:
        sample_gen_configs.delete_config(saved["id"])


# ---------------------------------------------------------------------------
# 6. Back to the source; offboard
# ---------------------------------------------------------------------------

def test_6_back_on_the_source_its_work_is_as_left_and_the_copy_is_offboarded(page):
    source = _source()
    _choose_database(page, source)
    rail = _rail(page)
    assert rail["Database setup"] == source and rail["footer"] == source, rail
    assert "tables" in _step(page, "discover")
    assert _setup_facts(page)["Target"] == source

    row = page.locator(".target__onboarded-row", has_text=A)
    row.get_by_role("button", name="Delete", exact=True).click()
    row.get_by_role("button", name=f"Delete {A} from SDM").click()
    _settled(page)
    page.wait_for_function(
        "name => ![...document.querySelectorAll('.target__onboarded-row')]"
        ".some(r => r.textContent.includes(name))", arg=A)
    listed = json.loads(urllib.request.urlopen(
        page.url.split("#")[0].rstrip("/") + "/api/databases/onboarded").read())
    assert A not in {d["database"] for d in listed["databases"]}


def test_7_files_only_opens_on_patterns_and_writes_for_a_saved_pattern(browser_page):
    """Where database loads are off -- every customer machine -- nothing on
    Database setup is needed: the screens open on Patterns, and Generate's
    files are written for the latest saved pattern, never a plan there is none of."""
    from sdg.analyzer import db
    from sdg.config import get_settings

    if not db.get_saved_patterns():
        pytest.skip("no saved pattern in the pattern store")
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
        # The module's browser, in a context of its own: a second Playwright
        # cannot start while the first one runs.
        context = browser_page[0].context.browser.new_context(
            viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(60_000)
        try:
            page.goto(url)
            page.wait_for_load_state("networkidle")
            assert page.locator(".app__heading").inner_text().strip() == "Patterns"

            page.locator(".rail__step", has_text="Generate").first.click()
            page.wait_for_load_state("networkidle")
            _on_tab(page, "Files")
            chooser = page.get_by_label("Pattern", exact=True)
            chooser.locator("option[value^='saved:']").first.wait_for(state="attached")
            # Files only: the plan is never offered as what a file is written for.
            options = [o.strip() for o in chooser.locator("option").all_inner_texts()]
            assert "Plan" not in options, options
            page.wait_for_function(
                "() => document.querySelector('.forwhom select').value.startsWith('saved:')")
            assert page.locator(".banner--alarm").count() == 0
        finally:
            context.close()
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)


def test_8_generation_rules_open_when_no_plan_is_built(browser_page):
    """A database changed and read has no plan until one is built: Generation
    Rules still lists the rules, with no error, and says a plan adds more."""
    from sdg.config import get_settings

    work = Path(tempfile.mkdtemp(prefix="sdm-no-plan-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("wheels-*", "server.log"))
    # As a database just read: no mappings yet, so no plan can be built.
    for name in ("plan.json", "mappings.json"):
        for found in work.rglob(name):
            found.unlink()
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, SDG_WORK_DIR=str(work)),
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
        context = browser_page[0].context.browser.new_context(
            viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(60_000)
        try:
            page.goto(url)
            page.wait_for_load_state("networkidle")
            page.get_by_role("navigation", name="Settings").get_by_role(
                "button", name="Rules", exact=True).click()
            assert page.locator(".app__heading").inner_text().strip() == "Rules"
            page.locator("tr.rules__row").first.wait_for(timeout=LONG)
            body = page.locator(".app__body").inner_text()
            assert "No plan has been built" in body, body[:600]
            assert page.locator(".banner--alarm").count() == 0
        finally:
            context.close()
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)


def test_9_with_no_target_database_load_is_off_and_said_once(browser_page):
    """Loads on, no target database set up (no connection made): every Load
    to database is unavailable with the reason as its title, and the screen
    says it once -- not beside every pattern."""
    from sdg.config import get_settings

    work = Path(tempfile.mkdtemp(prefix="sdm-no-target-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("wheels-*", "server.log"))
    for found in work.rglob("connections.json"):
        found.unlink()
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, SDG_WORK_DIR=str(work)),
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
        with urllib.request.urlopen(url + "/api/state", timeout=30) as r:
            steps = {s["id"]: s for s in json.load(r)["steps"]}
        assert not steps["connect"]["done"], "the copy still has a connection"
        context = browser_page[0].context.browser.new_context(
            viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(60_000)
        try:
            page.goto(url)
            page.wait_for_load_state("networkidle")
            assert page.locator(".app__heading").inner_text().strip() == "Patterns"
            cards = page.locator("li.pattern")
            cards.locator(".pattern__head").first.wait_for(timeout=LONG)
            cards.first.locator(".pattern__head").click()
            page.locator(".pattern__actions").get_by_role(
                "button", name="Load to database", exact=True).wait_for()
            from sdg.analyzer import db

            if db.get_saved_patterns():
                # The Saved list's rows have theirs too.
                page.locator("li.kept__row").first.wait_for(timeout=LONG)
            loads = page.get_by_role("button", name="Load to database", exact=True)
            assert loads.count() >= (2 if db.get_saved_patterns() else 1), loads.count()
            for load in loads.all():
                assert load.is_disabled()
                assert load.get_attribute("title") == "No target database set up"
            said = page.get_by_text("No target database set up", exact=True)
            assert said.count() == 1, f"said {said.count()} times"
        finally:
            context.close()
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)

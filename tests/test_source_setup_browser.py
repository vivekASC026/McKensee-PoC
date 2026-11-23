"""Source setup, driven in a real browser the way the team at a new customer would.

A sample bank (seed/bank.py) is made as SDMT_ACCEL_UI_SRC and named in the
server's environment as a source. On the screen:

1. the source is picked and read. The demo login (sa) can write, so the
   screen's own refusal is met and gone past with its button; the read's
   result is shown;
2. the hierarchy is proposed; a level is deselected and the Tables tab no
   longer lists the table it needed;
3. the primary record kind is named and the screen uses the person's words;
   a name that is not plain words is refused in the engine's words and
   changes nothing;
4. the plan is taken to a pack: under Criteria a value is typed, the
   criterion selected and the shares measured (the login's refusal met and
   gone past again); under Structure an identifying column is added, and it
   is locked, so making it Kept is refused on screen; under Approval the pack
   is named, the plan submitted and approved by the second approver, whose
   name is typed to confirm; under Build the pack is built, its files listed
   and one of them shown as text, with the install steps; under Load its
   tables are created and loaded (the login's refusal met and gone past),
   the checks shown, and the tables refreshed;
5. a source whose database is not there fails to read, said by its codes
   only;
6. the landscape is read again behind the proposal's back: the proposal is
   shown as made from an earlier read, and its controls are locked.

What is checked is what the screen shows. No page error is allowed, no alarm
banner where none is expected, no health word, and no command-line flag on
any tab. Only SDMT_ACCEL_UI_SRC and the store SDMT_ACCEL_UI_STORE (approvals
and the loaded tables) are made, and both are dropped afterwards by their
exact names; the pack is built in a temporary folder, removed afterwards.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_browser import LONG, _free_port, _go, _on_tab, _tab

pytestmark = [pytest.mark.db, pytest.mark.slow, pytest.mark.browser]

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "SDMT_ACCEL_UI_SRC"
# Where the screen's approvals are kept: a store of the test's own.
STORE = "SDMT_ACCEL_UI_STORE"
# The second approver named in the copied work folder's setting.
SECOND = "Second Reviewer"
# A source whose database is not on the server: reading it fails.
MISSING = "SDMT_ACCEL_UI_NONE"
HEALTH = re.compile(r"\b(members?|claims?|subscribers?|diagnos\w*|providers?|FACETS)\b", re.I)
CLI = re.compile(r"--[a-z]|sdg accelerate", re.I)
TABS = ("Sources", "Landscape", "Hierarchy", "Tables", "Criteria", "Structure", "Approval",
        "Build", "Load")
# A key for this test's surrogates only: never a customer's.
TEST_EXTRACT_KEY = "browser-test-key-not-a-secret"


def _master():
    import pyodbc

    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _drop_store() -> None:
    """The test's own store, by its exact name only."""
    assert STORE == "SDMT_ACCEL_UI_STORE"
    with _master() as master:
        master.execute(f"IF DB_ID(N'{STORE}') IS NOT NULL BEGIN ALTER DATABASE [{STORE}] "
                       f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{STORE}]; END")


def _make_store() -> None:
    """A fresh store for approvals; one left by an earlier run is never reused."""
    with _master() as master:
        if master.execute("SELECT DB_ID(?)", STORE).fetchone()[0] is not None:
            pytest.fail(f"{STORE} is there already: drop it by hand if it is an earlier run's, "
                        f"then run again.", pytrace=False)
        master.execute(f"CREATE DATABASE [{STORE}]")
    from sdg.config import get_settings

    import pyodbc

    with pyodbc.connect(get_settings().mssql.connection_string(STORE), autocommit=True) as conn:
        conn.execute("CREATE SCHEMA sdm")


@pytest.fixture(scope="module")
def opened():
    from playwright.sync_api import sync_playwright

    from sdg.config import get_settings
    from seed import bank as sample

    sample.build(SOURCE, customers=400)
    _make_store()
    work = Path(tempfile.mkdtemp(prefix="sdm-source-setup-"))
    packs = Path(tempfile.mkdtemp(prefix="sdm-source-setup-packs-"))
    shutil.copytree(get_settings().work_dir, work, dirs_exist_ok=True)
    shutil.rmtree(work / "accelerator", ignore_errors=True)
    # The copy's approval setting names a second approver: approving asks for the name.
    (work / "layout-approval.json").write_text(json.dumps({"secondApprover": SECOND}),
                                               encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sdg.api.app:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=dict(os.environ, SDG_WORK_DIR=str(work), SDG_PACKS_DIR=str(packs),
                           SDG_DB_DENORM=STORE, SDG_DENORM_SCHEMA="sdm",
                           SDG_EXTRACT_KEY=TEST_EXTRACT_KEY,
                           **{f"SDG_SOURCE_{SOURCE}_DATABASE": SOURCE}),
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
            yield page, work
            browser.close()
            assert not errors, errors
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(packs, ignore_errors=True)
        sample.drop(SOURCE)
        _drop_store()


def _tables(page) -> list[str]:
    _tab(page, "Tables")
    page.locator("table.srctables").wait_for()
    return [cell.inner_text().strip()
            for cell in page.locator("table.srctables tbody tr td:first-child").all()]


def _no_alarm(page) -> None:
    alarms = page.locator(".banner--alarm")
    assert not alarms.count(), alarms.first.inner_text()


def _plain_words_on_every_tab(page) -> None:
    for tab in TABS:
        _tab(page, tab)
        # Hidden text too (the proposal's notes are folded away).
        said = page.locator(".app__body").text_content() or ""
        assert not HEALTH.search(said), (tab, HEALTH.search(said))
        assert not CLI.search(said), (tab, CLI.search(said))


def _choose(page, name: str):
    row = page.locator("label.srcsource", has_text=name)
    row.wait_for()
    row.get_by_role("radio").check()
    page.wait_for_load_state("networkidle")
    return row


def test_pick_read_shape_and_name_a_source(opened):
    page, work = opened
    _go(page, "Source setup", "Sources")
    _on_tab(page, "Sources")

    # 1. Pick the source named in the environment, then read it: one click.
    row = page.locator("label.srcsource", has_text=SOURCE)
    row.wait_for()
    assert "Not read" in row.inner_text()
    _choose(page, SOURCE)
    page.get_by_role("button", name="Read source").click()

    # The demo login can write: the screen says so in its own words, with no
    # command-line flag, and goes on only when told to.
    go_on = page.get_by_role("button", name="Read with this login")
    go_on.wait_for()
    caution = page.locator(".banner--caution").first.inner_text()
    assert "This login can change data in the source. SDM only reads from it." in caution
    assert not CLI.search(caution), caution
    go_on.click()

    # Progress while it reads, naming the source; then what it found.
    status = page.locator(".srcprogress")
    status.wait_for(timeout=LONG)
    assert SOURCE in status.inner_text()
    result = page.locator(".srcsetup__result")
    result.wait_for(timeout=LONG)
    found = result.inner_text()
    assert f"What the read of {SOURCE} found" in found
    # The labels are drawn in capitals; what they say is checked as written.
    for label in ("Tables", "Keys declared", "Keys found", "Most referenced tables"):
        assert label.lower() in found.lower(), label
    tables_count = result.locator(".srccounts__item", has_text="Tables").first
    assert int(tables_count.locator("dd").inner_text().replace(",", "")) >= 8
    assert "Proposal saved" in row.inner_text()
    _no_alarm(page)

    # 2. The hierarchy: record kinds at low confidence, levels broadest first.
    result.get_by_role("button", name="Open hierarchy").click()
    _on_tab(page, "Hierarchy")
    kinds = page.locator(".srckind")
    kinds.first.wait_for()
    assert kinds.filter(has_text="Primary records").count() == 1
    assert kinds.filter(has_text="Child records").count() == 1
    assert "Low confidence" in kinds.first.inner_text()
    assert page.locator(".srclevels li.srclevel").count() > 0

    before = _tables(page)
    assert "dbo.PRODUCT" in before, before

    # Deselect the child records' product level: the table it needed leaves,
    # and focus stays on the box that was clicked, now in the other list.
    _tab(page, "Hierarchy")
    page.locator(".srcsetup__roles").get_by_role("tab", name="Child records").click()
    card = page.locator("li.srclevel[data-level='dbo.PRODUCT.PRODUCT_CODE']")
    card.wait_for()
    assert "Walk level" in card.inner_text() or "Narrowing level" in card.inner_text()
    assert "values" in card.inner_text().lower() and "records each" in card.inner_text().lower()
    # Checked as the server holds it: the box changes with the edit's answer.
    card.get_by_label("Selected").click()
    page.locator(".srclevels--off li.srclevel[data-level='dbo.PRODUCT.PRODUCT_CODE']").wait_for()
    page.wait_for_function(
        "() => document.activeElement?.closest('.srclevels--off li')"
        "?.dataset.level === 'dbo.PRODUCT.PRODUCT_CODE'")
    # Announced: the region is cleared, then given the words.
    page.wait_for_function(
        "() => document.querySelector('.srcsetup [role=status][aria-live]')"
        "?.textContent.includes('Tables updated')")
    _no_alarm(page)

    after = _tables(page)
    assert "dbo.PRODUCT" not in after, after
    assert set(after) < set(before)

    # 3. Name the primary record kind: the screen uses the person's words.
    _tab(page, "Hierarchy")
    primary = page.locator(".srckind").first
    primary.get_by_label("Name of one").fill("customer")
    primary.get_by_label("Name of many").fill("customers")
    primary.get_by_role("button", name="Save name").click()
    page.locator(".srcsetup__roles").get_by_role("tab", name="Customers").wait_for()
    assert page.locator(".srckind__title").first.inner_text() == "Customers"
    _no_alarm(page)

    _tables(page)
    serves = page.locator("table.srctables").inner_text()
    assert "Customers: Records" in serves
    assert "Primary records" not in serves

    # A name that is not plain words is refused in the engine's words, and
    # the name stays as it was.
    _tab(page, "Hierarchy")
    primary = page.locator(".srckind").first
    primary.get_by_label("Name of one").fill("<b>customer</b>")
    primary.get_by_role("button", name="Save name").click()
    alarm = page.locator(".banner--alarm")
    alarm.wait_for()
    assert "must be plain words" in alarm.inner_text()
    assert page.locator(".srckind__title").first.inner_text() == "Customers"

    _plain_words_on_every_tab(page)


def _said(page, words: str) -> None:
    """The live region announces these words."""
    page.wait_for_function(
        "w => document.querySelector('.srcsetup [role=status][aria-live]')"
        "?.textContent.includes(w)", arg=words)


def _read_and_named(page) -> None:
    """The source read and proposed, and both record kinds named: done here
    when an earlier test has not, so this walk stands on its own."""
    _go(page, "Source setup", "Sources")
    row = _choose(page, SOURCE)
    if "Proposal saved" not in row.inner_text():
        page.get_by_role("button", name="Read source").click()
        go_on = page.get_by_role("button", name="Read with this login")
        go_on.wait_for()
        go_on.click()
        page.locator(".srcsetup__result").wait_for(timeout=LONG)
    _tab(page, "Hierarchy")
    for index, (one, many) in enumerate((("customer", "customers"), ("account", "accounts"))):
        kind = page.locator(".srckind").nth(index)
        kind.wait_for()
        if kind.locator(".srckind__title").inner_text() == many.capitalize():
            continue
        kind.get_by_label("Name of one").fill(one)
        kind.get_by_label("Name of many").fill(many)
        kind.get_by_role("button", name="Save name").click()
        page.locator(".srcsetup__roles").get_by_role("tab", name=many.capitalize()).wait_for()
    _no_alarm(page)


def _fact(page, label: str) -> str:
    """The value beside one label of the plan's status."""
    return page.locator(".srcstatus > div").filter(
        has=page.locator("dt", has_text=re.compile(f"^{label}$"))).locator("dd").inner_text()


def _api(page, method: str, path: str, body: dict | None = None) -> dict:
    """A call to the server the page talks to, as another person's screen
    would make it (the suite's opener carries the token)."""
    base = "/".join(page.url.split("/")[:3])
    request = urllib.request.Request(
        f"{base}/api/accelerator/sources/{SOURCE}{path}", method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as answer:
        return json.loads(answer.read())


def _saved(work) -> dict:
    return json.loads((work / "accelerator" / SOURCE / "proposal.json").read_text("utf-8"))


def _column_saved(work, table: str, column: str) -> list[dict]:
    return [c for t in _saved(work)["structure"] for c in t["columns"]
            if c["source_table"] == table and c["source_column"] == column]


def _approve_on_screen(page) -> None:
    """Submit, then approve with the second approver's name typed."""
    page.get_by_role("button", name="Submit", exact=True).click()
    page.locator(".srcstatus", has_text="Waiting for review").wait_for()
    page.get_by_role("button", name="Approve", exact=True).click()
    group = page.get_by_role("group", name="Approve the plan")
    group.get_by_label(f"Type {SECOND} to approve").fill(SECOND)
    group.get_by_role("button", name="Approve", exact=True).click()
    page.locator(".srcstatus", has_text="Approved").wait_for()


def test_criteria_structure_approval_and_build(opened):
    page, work = opened
    _read_and_named(page)

    # Criteria: proposals, each with its share; a value typed, then selected.
    _tab(page, "Criteria")
    page.get_by_role("heading", name="Criteria", exact=True).wait_for()
    card = page.locator("li[data-criterion*=':values:']").first
    card.wait_for()
    assert "Share kept" in card.inner_text() and "Not measured" in card.inner_text()
    # Each control names the criterion it belongs to.
    card.get_by_label("Values of ACCT_STATUS to keep, one per line").fill("A\nD")
    card.get_by_role("button", name="Save values for ACCT_STATUS").click()
    _said(page, "Values for ACCT_STATUS saved")
    card = page.locator("li[data-criterion*=':values:']").first
    assert "is one of: A, D" in card.inner_text()
    card.get_by_label("Selected: ACCT_STATUS is one of: A, D").click()
    page.wait_for_function(
        "() => document.querySelector(\"li[data-criterion*=':values:'] input[type=checkbox]\")"
        "?.checked === true")
    _no_alarm(page)

    # Measuring counts in the source: the demo login can write, so the
    # screen's refusal is met and gone past with its button.
    page.get_by_role("button", name="Measure shares").click()
    go_on = page.get_by_role("button", name="Measure with this login")
    go_on.wait_for()
    go_on.click()
    page.wait_for_function(
        "() => !document.querySelector('.srcprogress') && document.querySelector("
        "\"li[data-criterion*=':values:']\")?.textContent.includes('%')", timeout=LONG)
    assert "Not measured" not in page.locator("li[data-criterion*=':values:']").first.inner_text()
    _said(page, "Shares measured for 1 criterion")
    _no_alarm(page)

    # Structure: one table per record kind, each column with its privacy.
    _tab(page, "Structure")
    tables = page.locator("section.srcdenorm")
    tables.first.wait_for()
    assert tables.count() == 2
    first = tables.first
    assert "customers_denorm" in first.inner_text()
    first.get_by_label("Table to add a column to customers_denorm from").select_option(
        "dbo.CUSTOMER")
    first.get_by_label("Column to add to customers_denorm").fill("LAST_NAME")
    first.get_by_role("button", name="Add column to customers_denorm").click()
    row = page.locator("tr[data-column='dbo.CUSTOMER.LAST_NAME']")
    row.wait_for()
    # It identifies a person: withheld, locked, and never made Kept.
    privacy = row.get_by_label("Privacy of LAST_NAME")
    assert privacy.input_value() == "withheld"
    assert privacy.get_attribute("aria-disabled") == "true"
    assert "Locked" in row.inner_text() and "never kept" in row.inner_text()
    # A pointer cannot choose on it (the browser driver refuses a disabled
    # control), and a change made anyway, as a keyboard could, is not sent.
    with pytest.raises(playwright.Error):
        privacy.select_option("kept", timeout=2_000)
    privacy.evaluate("el => { el.value = 'kept'; "
                     "el.dispatchEvent(new Event('change', { bubbles: true })); }")
    privacy.evaluate("el => el.blur()")
    page.wait_for_load_state("networkidle")
    assert row.get_by_label("Privacy of LAST_NAME").input_value() == "withheld"
    assert {c["privacy"] for c in _column_saved(work, "dbo.CUSTOMER", "LAST_NAME")} == {
        "withheld"}
    _no_alarm(page)

    # Stepping through a privacy list with the arrow keys sends nothing; Enter
    # saves the choice, and so does leaving the list.
    segment = page.locator("tr[data-column='dbo.CUSTOMER.SEGMENT']").get_by_label(
        "Privacy of SEGMENT")
    assert segment.input_value() == "kept"
    segment.focus()
    segment.press("ArrowDown")
    assert segment.input_value() == "withheld"
    assert "Not saved yet" in page.locator("tr[data-column='dbo.CUSTOMER.SEGMENT']").inner_text()
    page.wait_for_load_state("networkidle")
    assert {c["privacy"] for c in _column_saved(work, "dbo.CUSTOMER", "SEGMENT")} == {"kept"}
    segment.press("Enter")
    _said(page, "SEGMENT is now withheld")
    assert {c["privacy"] for c in _column_saved(work, "dbo.CUSTOMER", "SEGMENT")} == {"withheld"}
    # Picked from the list, a choice is saved at once.
    segment.select_option("kept")
    _said(page, "SEGMENT is now kept")
    assert {c["privacy"] for c in _column_saved(work, "dbo.CUSTOMER", "SEGMENT")} == {"kept"}
    # A step made with the keys is saved on leaving the list.
    segment.focus()
    segment.press("ArrowDown")
    segment.press("Tab")
    _said(page, "SEGMENT is now withheld")
    segment.select_option("kept")
    _said(page, "SEGMENT is now kept")
    assert {c["privacy"] for c in _column_saved(work, "dbo.CUSTOMER", "SEGMENT")} == {"kept"}

    # A key proposed as a surrogate is kept only with the person's word that
    # it holds nothing personal.
    key_row = page.locator("tr[data-column='dbo.ACCOUNT.ACCOUNT_ID']")
    key = key_row.get_by_label("Privacy of ACCOUNT_ID")
    assert key.input_value() == "surrogate"
    key.select_option("kept")
    keep = key_row.get_by_role("group", name="Keep ACCOUNT_ID")
    keep.wait_for()
    keep_button = keep.get_by_role("button", name="Keep ACCOUNT_ID")
    assert keep_button.get_attribute("aria-disabled") == "true"
    key.press("Tab")  # leaving the list does not keep it unconfirmed
    page.wait_for_load_state("networkidle")
    assert {c["privacy"] for c in _column_saved(work, "dbo.ACCOUNT", "ACCOUNT_ID")} == {
        "surrogate"}
    keep.get_by_label("This column holds nothing personal").check()
    keep_button.click()
    _said(page, "ACCOUNT_ID is now kept")
    assert [(c["privacy"], c["confirmed_not_personal"])
            for c in _column_saved(work, "dbo.ACCOUNT", "ACCOUNT_ID")] == [("kept", True)]
    _no_alarm(page)

    # Two joins from the child to the primary: the person names the one it
    # follows. A second one is stated as another person's screen would.
    _api(page, "POST", "/proposal/join", {
        "role": "child", "from_table": "dbo.ACCOUNT", "from_columns": ["ACCOUNT_ID"],
        "to_table": "dbo.CUSTOMER", "to_columns": ["CUSTOMER_ID"]})
    page.reload()
    page.wait_for_load_state("networkidle")
    _go(page, "Source setup", "Structure")
    joining = page.locator("section.srcjoin")
    joining.wait_for()
    choice = joining.get_by_label("Join from accounts to customers")
    assert "(named)" in choice.locator("option:checked").inner_text()
    choice.select_option(label="dbo.ACCOUNT.CUSTOMER_ID → dbo.CUSTOMER.CUSTOMER_ID")
    joining.get_by_role("button", name="Use this join").click()
    _said(page, "Accounts now join customers by CUSTOMER_ID")
    stated = [j["from_columns"] for j in _saved(work)["joins"]["stated"]
              if j["to_table"] == "dbo.CUSTOMER"]
    assert stated == [["CUSTOMER_ID"]]
    # One join is left, so nothing is left to choose.
    page.wait_for_function("() => !document.querySelector('section.srcjoin')")
    _no_alarm(page)

    # A link with no dates: the demo source has none, so the server's answer
    # is given one here (the engine's own path is proved in
    # test_accelerator_plan_api); the screen offers the explicit acceptance
    # and sends it as the route expects.
    link = {"table": "dbo.BRANCH_LINK", "levels": ["BRANCH_ID"], "accepted": False}
    sent: list[dict] = []
    latest: dict = {}

    def with_link(route) -> None:
        answer = route.fetch()
        body = answer.json()
        body["grain"]["primary"] = [link]
        latest.update(body)
        route.fulfill(response=answer, json=body)

    def accept(route) -> None:
        sent.append(route.request.post_data_json)
        latest["grain"]["primary"] = [{**link, "accepted": True}]
        route.fulfill(json=latest)

    page.route(f"**/api/accelerator/sources/{SOURCE}/proposal", with_link)
    page.route(f"**/api/accelerator/sources/{SOURCE}/structure/grain", accept)
    try:
        page.reload()
        page.wait_for_load_state("networkidle")
        _go(page, "Source setup", "Structure")
        refused = page.locator(".banner--caution", has_text="dbo.BRANCH_LINK")
        refused.wait_for()
        assert "The extract refuses it until you accept that" in refused.inner_text()
        refused.get_by_role(
            "button", name="Accept: each customer may appear once per BRANCH_ID").click()
        page.locator(".srcgrain", has_text="Accepted: each customer may appear once per").wait_for()
        assert sent == [{"role": "primary", "table": "dbo.BRANCH_LINK", "accept": True}]
    finally:
        page.unroute(f"**/api/accelerator/sources/{SOURCE}/proposal")
        page.unroute(f"**/api/accelerator/sources/{SOURCE}/structure/grain")
    page.reload()
    page.wait_for_load_state("networkidle")
    _go(page, "Source setup", "Sources")
    _choose(page, SOURCE)
    _no_alarm(page)

    # Approval: what is left to do, then the pack named, submitted, approved.
    _tab(page, "Approval")
    page.get_by_role("heading", name="Approval", exact=True).wait_for()
    assert "Draft" in page.locator(".srcstatus").inner_text()
    assert page.locator(".srcproblems li", has_text="Name the pack").count() == 1
    page.get_by_label("Pack id").fill("ui_bank")
    page.get_by_label("Pack name").fill("UI bank")
    page.get_by_role("button", name="Save pack name").click()
    page.get_by_text("Nothing is left to do").wait_for()
    page.get_by_role("button", name="Submit", exact=True).click()
    page.locator(".srcstatus", has_text="Waiting for review").wait_for()
    assert "Submitted by" in page.locator(".srcstatus").inner_text()
    assert "Approved" not in page.locator(".srcstatus").inner_text()

    page.get_by_role("button", name="Approve", exact=True).click()
    group = page.get_by_role("group", name="Approve the plan")
    typed = group.get_by_label(f"Type {SECOND} to approve")
    typed.wait_for()
    confirm = group.get_by_role("button", name="Approve", exact=True)
    # Another name does not approve: neither the button nor Enter sends it.
    for wrong in ("", "Second", "someone else"):
        typed.fill(wrong)
        assert confirm.get_attribute("aria-disabled") == "true", wrong
    typed.press("Enter")
    confirm.click(force=True)  # a click a person can still make on it
    page.wait_for_load_state("networkidle")
    assert _fact(page, "Status") == "Waiting for review"
    # The name as set, in any case and with spaces at either end, approves.
    typed.fill("  second reviewer ")
    assert confirm.get_attribute("aria-disabled") is None
    confirm.click()
    page.locator(".srcstatus", has_text="Approved").wait_for()
    assert _fact(page, "Status") == "Approved"
    assert _fact(page, "Approved by").startswith(f"{SECOND}, ")
    assert page.get_by_role("button", name="Withdraw").count() == 1
    _no_alarm(page)

    # Withdrawn elsewhere: opening Build reads where the plan stands again.
    _api(page, "POST", "/withdraw")
    _tab(page, "Build")
    page.get_by_text("The plan is not approved yet").wait_for()
    _tab(page, "Approval")
    page.locator(".srcstatus", has_text="Draft").wait_for()
    _approve_on_screen(page)

    # Build: a refused build is said in the Build panel, where Build pack is
    # the way to try again.
    _tab(page, "Build")
    page.route(f"**/api/accelerator/sources/{SOURCE}/build", lambda route: route.fulfill(
        status=409, json={"detail": "A pack is being built already: wait for it to finish.",
                          "refusal": "busy"}))
    try:
        page.get_by_role("button", name="Build pack").click()
        panel = page.locator("section[aria-label='Build']")
        panel.locator(".banner--alarm", has_text="being built already").wait_for()
        assert page.locator(".srcsetup > .banner--alarm").count() == 0
        assert panel.get_by_role("button", name="Build pack").get_attribute(
            "aria-disabled") is None
    finally:
        page.unroute(f"**/api/accelerator/sources/{SOURCE}/build")

    # Then built: the result is announced and focused, and the files listed.
    page.get_by_role("button", name="Build pack").click()
    page.locator(".banner--affirm", has_text="is built and in place").wait_for(timeout=LONG)
    _said(page, "pack is built and in place")
    page.wait_for_function("() => document.activeElement?.id === 'src-build-result'")
    _no_alarm(page)
    # The list is read again once the build ends.
    page.wait_for_function("() => document.querySelector('table.srcfiles') && "
                           "!document.querySelector('table.srcfiles').textContent"
                           ".includes('Not built')")
    files = page.locator("table.srcfiles tbody tr")
    names = [r.locator("td").first.inner_text().strip() for r in files.all()]
    assert "pack.json" in names and f"extract/{SOURCE}.sql" in names, names
    assert "Not built" not in page.locator("table.srcfiles").inner_text()
    page.get_by_role("button", name=f"View extract/{SOURCE}.sql").click()
    shown = page.get_by_role("region", name=f"Text of extract/{SOURCE}.sql")
    shown.wait_for()
    assert "-- table: " in shown.inner_text()
    # Shown as text: the file's own words, not markup the page draws; it can
    # be scrolled by keyboard, and Escape closes it, back to its View button.
    assert shown.evaluate("el => el.children.length") == 0
    assert shown.get_attribute("tabindex") == "0"
    shown.focus()
    shown.press("Escape")
    page.wait_for_function("() => !document.querySelector('.srcviewer')")
    page.wait_for_function("id => document.activeElement?.id === id",
                           arg=f"src-view-extract_{SOURCE}_sql")
    with page.expect_download() as saved:
        page.get_by_role("button", name="Download README.txt").click()
    assert saved.value.suggested_filename == "README.txt"
    _said(page, "README.txt downloaded")
    steps = page.locator("ol.srcsteps").inner_text()
    for words in ("SDG_PACKS_DIR=", "SDG_PACK=ui_bank", f"SDG_EXTRACT_SOURCE={SOURCE}",
                  "table setup", "Run the extract", "Restart SDM"):
        assert words in steps, words
    _no_alarm(page)

    _load_on_screen(page)
    _plain_words_on_every_tab(page)

    # Withdrawn, the plan is a draft again, and Build says it is not approved.
    _tab(page, "Approval")
    page.get_by_role("button", name="Withdraw").click()
    page.locator(".srcstatus", has_text="Draft").wait_for()
    assert _fact(page, "Status") == "Draft"
    _tab(page, "Build")
    page.get_by_text("The plan is not approved yet").wait_for()
    _no_alarm(page)


def _store_rows(table: str) -> int:
    import pyodbc

    from sdg.config import get_settings

    assert re.fullmatch(r"[A-Za-z0-9_]+", table), table
    with pyodbc.connect(get_settings().mssql.connection_string(STORE)) as conn:
        return int(conn.cursor().execute(f"SELECT COUNT(*) FROM sdm.[{table}]").fetchone()[0])


def _load_on_screen(page) -> None:
    """Build's next step: the Load tab creates the tables in one click, loads
    them (the login's refusal met and gone past), shows each table's rows and
    the checks, and offers Refresh after."""
    page.get_by_role("button", name="Go to Load").click()
    panel = page.locator("section[aria-label='Load']")
    panel.locator("table.srcload").first.wait_for()
    tables = [r.get_attribute("data-table")
              for r in panel.locator("table.srcload tbody tr[data-table]").all()]
    assert len(tables) == 2, tables
    assert panel.locator("table.srcload", has_text="Not created").count() == 1
    assert "Reads every row" not in panel.inner_text()
    assert panel.get_by_role("button", name="Load now").count() == 0

    # A login that may not create tables: the script is offered for the DBA.
    status_url = f"**/api/accelerator/sources/{SOURCE}/load"
    real = _api(page, "GET", "/load")
    page.route(status_url, lambda route: route.fulfill(
        json={**real, "store": {**real["store"], "may_create": False}})
        if route.request.method == "GET" else route.fallback())
    try:
        _tab(page, "Build")
        _tab(page, "Load")
        panel.get_by_text("Give the script to the DBA").wait_for()
        assert panel.get_by_role("button", name="Create tables").count() == 0
        with page.expect_download() as saved:
            panel.get_by_role("button", name="Download sql/store_tables.sql").click()
        assert saved.value.suggested_filename == "store_tables.sql"
    finally:
        page.unroute(status_url)
    _tab(page, "Build")
    _tab(page, "Load")

    panel.get_by_role("button", name="Create tables").click()
    _said(page, "The tables are ready")
    # Focus moves on to the next step.
    page.wait_for_function("() => document.activeElement?.id === 'src-load'")
    assert panel.locator("table.srcload").first.inner_text().count("Ready") == 2
    assert panel.get_by_role("button", name="Create tables").count() == 0
    _no_alarm(page)

    panel.get_by_role("button", name="Load now").click()
    # The login's refusal is announced, and focus is on the button that goes on.
    _said(page, "Load with this login goes on")
    page.wait_for_function("() => document.activeElement?.id === 'src-go-on'")
    page.get_by_role("button", name="Load with this login").click()
    result = page.locator("#src-load-result")
    result.wait_for(timeout=LONG)
    page.wait_for_function("() => document.activeElement?.id === 'src-load-result'")
    shown = result.inner_text()
    assert "Loaded " in shown and "rows" in shown, shown
    for table in tables:
        assert table in shown and _store_rows(table) > 0, table
    checks = result.locator("ul.srcchecks li")
    assert checks.count() >= 4
    assert result.locator(".chip", has_text="Needs a look").count() == 0, shown
    _said(page, "every check passed")
    _no_alarm(page)
    # Logged, so the button is Refresh now, and analysis waits on a restart.
    panel.get_by_role("button", name="Refresh").wait_for()
    assert "Last loads" in panel.inner_text()
    assert "SDG_PACK=ui_bank" in panel.inner_text()

    before = {table: _store_rows(table) for table in tables}
    panel.get_by_role("button", name="Refresh").click()
    page.get_by_role("button", name="Load with this login").click()
    page.wait_for_function("() => document.activeElement?.id === 'src-load-result'",
                           timeout=LONG)
    assert {table: _store_rows(table) for table in tables} == before
    _no_alarm(page)
    _stop_on_screen(page, tables)


def _stop_on_screen(page, tables: list[str]) -> None:
    """Stop, on a load the server is made to say is running: the stop
    request reaches the job, the screen says it stops after the table it is
    on, and the stopped result says the tables it did not reach keep their
    rows."""
    job = {"id": "fakeload01", "kind": "accelerator", "label": f"Load {SOURCE}",
           "source": SOURCE, "state": "running", "done": 1, "total": 2,
           "note": f"{tables[0]}: 400 rows", "elapsed_seconds": 3.0, "eta_seconds": None,
           "result": None, "error": None}
    stopped = {**job, "state": "done", "done": 2, "result": {
        "source": SOURCE, "stopped": True, "failed": 1, "notes": [], "checks": [],
        "load": {"seconds": 3.0, "rows": 400, "tables": [
            {"table": tables[0], "rows": 400, "skipped": 0, "seconds": 3.0, "error": None},
            {"table": tables[1], "rows": 0, "skipped": 0, "seconds": 0.0,
             "error": "Not run: the extract was stopped before this table. Its rows are as "
                      "they were."}]}}}
    asked: list[str] = []

    def polled(route):
        route.fulfill(json=stopped if asked else job)

    def stop(route):
        asked.append(route.request.url)
        route.fulfill(json={**job, "asked": True})

    page.route(f"**/api/accelerator/sources/{SOURCE}/load",
               lambda route: route.fulfill(json=job) if route.request.method == "POST"
               else route.fallback())
    page.route("**/api/accelerator/jobs/fakeload01", polled)
    page.route("**/api/accelerator/jobs/fakeload01/stop", stop)
    try:
        panel = page.locator("section[aria-label='Load']")
        # The load is answered as started (the login's refusal was met above).
        panel.get_by_role("button", name="Refresh").click()
        panel.get_by_role("button", name="Stop").click()
        _said(page, "Stopping after the table being loaded")
        assert asked and asked[0].endswith("/jobs/fakeload01/stop")
        result = page.locator("#src-load-result")
        result.locator(".banner", has_text="The load was stopped").wait_for(timeout=LONG)
        assert "Not run" in result.inner_text()
        _said(page, "The load was stopped")
    finally:
        page.unroute(f"**/api/accelerator/sources/{SOURCE}/load")
        page.unroute("**/api/accelerator/jobs/fakeload01")
        page.unroute("**/api/accelerator/jobs/fakeload01/stop")


def test_a_source_that_cannot_be_read_is_said_by_its_codes(opened):
    page, _ = opened
    _go(page, "Source setup", "Sources")
    page.get_by_label("Add a source by name").fill(MISSING)
    page.get_by_role("button", name="Add", exact=True).click()
    row = page.locator("label.srcsource", has_text=MISSING)
    row.wait_for()
    assert "Added this session" in row.inner_text()
    assert row.get_by_role("radio").is_checked()

    page.get_by_role("button", name="Read source").click()
    alarm = page.locator(".banner--alarm")
    alarm.wait_for(timeout=LONG)
    said = alarm.inner_text()
    # The fixed sentence, with the driver's kind and codes, never its words.
    assert f"{MISSING} could not be read (" in said, said
    assert "Cannot open database" not in said and "[Microsoft]" not in said, said
    assert not CLI.search(said), said


def test_a_proposal_older_than_the_landscape_is_locked(opened):
    page, work = opened
    # Approved first, as another person's screen would: an approved plan can
    # still be withdrawn once the proposal is older than the read.
    _read_and_named(page)
    _api(page, "POST", "/pack-name", {"id": "ui_bank", "name": "UI bank"})
    if _api(page, "GET", "/approval")["status"] != "draft":
        _api(page, "POST", "/withdraw")
    shown = _api(page, "POST", "/submit")
    _api(page, "POST", "/approve", {"plan_hash": shown["plan_hash"], "approver": SECOND})
    profile = work / "accelerator" / SOURCE / "profile.json"
    held = json.loads(profile.read_text(encoding="utf-8"))
    held["profiled"] = "2099-01-01T00:00:00+00:00"
    profile.write_text(json.dumps(held), encoding="utf-8")

    page.reload()
    page.wait_for_load_state("networkidle")
    _go(page, "Source setup", "Sources")
    _choose(page, SOURCE)
    _tab(page, "Hierarchy")
    notice = page.locator(".banner--caution", has_text="earlier read of the source")
    notice.wait_for()
    assert notice.get_by_role("button", name="Propose again").count() == 1
    assert not CLI.search(notice.inner_text())

    # Every control is locked, and a click changes nothing.
    box = page.locator("li.srclevel").first.get_by_label("Selected")
    assert box.get_attribute("aria-disabled") == "true"
    checked = box.is_checked()
    box.click(force=True)  # a click a person can still make on a locked box
    page.wait_for_load_state("networkidle")
    assert box.is_checked() == checked
    assert page.locator(".srckind").first.get_by_role(
        "button", name="Save name").get_attribute("aria-disabled") == "true"
    _no_alarm(page)

    # Locked controls say why: they point to the notice's words.
    _tab(page, "Approval")
    save = page.get_by_role("button", name="Save pack name")
    assert save.get_attribute("aria-disabled") == "true"
    assert "src-stale-reason" in (save.get_attribute("aria-describedby") or "")
    assert page.locator("#src-stale-reason").inner_text().startswith("This proposal was made")
    # Withdrawing only takes the plan back to draft, so it stays open.
    withdraw = page.get_by_role("button", name="Withdraw")
    assert withdraw.get_attribute("aria-disabled") is None
    withdraw.click()
    page.locator(".srcstatus", has_text="Draft").wait_for()
    _no_alarm(page)
    _plain_words_on_every_tab(page)

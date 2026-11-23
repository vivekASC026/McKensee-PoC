"""Layout approval as a control: with a second approver named, an approval
needs the approver's name typed, is recorded in that name and confirmed by the
computer's user, holds only for the fields approved, and every change of the
setting is logged. Layouts a test saves are deleted by their ids.
"""

from __future__ import annotations

import getpass
import json

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app

client = TestClient(app)

FIELDS = [{"name": "MEMBER_ID"}, {"name": "FIRST_NAME"}]


def _saved(name: str) -> int:
    from sdg.analyzer.services import layout_service

    saved = client.post("/api/layouts/save", json={
        "name": name, "kind": "csv", "delimiter": "|", "fields": FIELDS,
        "sample_name": None, "purpose": "generation",
    })
    assert saved.status_code == 200, saved.text
    return next(row["id"] for row in layout_service.list_layouts() if row["name"] == name)


def _work():
    from sdg.api.app import service

    return service.work


@pytest.mark.db
def test_an_approval_needs_the_approvers_name_typed_and_records_who_confirmed(second_approver):
    from sdg.analyzer.services import layout_service

    me = getpass.getuser()
    layout_id = _saved("SDMT approval typed")
    try:
        assert layout_service.get_layout(layout_id)["created_by"] == me

        for typed in (None, "", "someone else", "SDMT approve"):
            refused = client.post("/api/layouts/approve",
                                  json={"layout_id": layout_id, "approver": typed})
            assert refused.status_code == 403, (typed, refused.text)
            assert "approver's name" in refused.json()["detail"]
        assert layout_service.get_layout(layout_id)["status"] == "pending_review"

        # Any case, spaces around it: the same name.
        done = client.post("/api/layouts/approve", json={
            "layout_id": layout_id, "approver": "  " + second_approver.upper() + " "})
        assert done.status_code == 200, done.text
        held = layout_service.get_layout(layout_id)
        assert held["status"] == "approved"
        assert held["approved_by"] == second_approver
        assert held["confirmed_by"] == me
        listed = next(r for r in client.get("/api/layouts").json()["layouts"]
                      if r["id"] == layout_id)
        assert listed["confirmed_by"] == me and listed["approved_by"] == second_approver

        # Only a layout waiting for review is approved: not twice, and an
        # archived one is not brought back.
        again = client.post("/api/layouts/approve",
                            json={"layout_id": layout_id, "approver": second_approver})
        assert again.status_code == 409, again.text
        assert client.delete(f"/api/layouts/{layout_id}").json()["action"] == "archived"
        archived = client.post("/api/layouts/approve",
                               json={"layout_id": layout_id, "approver": second_approver})
        assert archived.status_code == 409, archived.text
        assert layout_service.get_layout(layout_id)["status"] == "archived"
    finally:
        layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_with_no_second_approver_an_approval_is_the_computers_user():
    from sdg.analyzer.services import layout_service

    before = client.get("/api/layouts/approval").json()["secondApprover"]
    client.put("/api/layouts/approval", json={"secondApprover": None})
    layout_id = None
    try:
        layout_id = _saved("SDMT approval nobody named")
        assert client.post("/api/layouts/approve",
                           json={"layout_id": layout_id}).status_code == 200
        held = layout_service.get_layout(layout_id)
        assert held["approved_by"] == getpass.getuser() == held["confirmed_by"]
    finally:
        client.put("/api/layouts/approval", json={"secondApprover": before})
        if layout_id is not None:
            layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_keep_approval_holds_only_for_an_edit_that_changes_nothing_written(second_approver):
    from sdg.analyzer.services import layout_service

    layout_id = _saved("SDMT approval kept")
    renamed = "SDMT approval kept renamed"
    try:
        def approve():
            assert client.post("/api/layouts/approve", json={
                "layout_id": layout_id, "approver": second_approver}).status_code == 200

        approve()
        # A new name only: still approved.
        named = client.put(f"/api/layouts/{layout_id}",
                           json={"name": renamed, "keep_approval": True})
        assert named.status_code == 200, named.text
        assert named.json()["status"] == "approved"
        same = layout_service.get_layout(layout_id)["fields"]
        unchanged = client.put(f"/api/layouts/{layout_id}",
                               json={"fields": same, "keep_approval": True})
        assert unchanged.json()["status"] == "approved", unchanged.text

        # Fields changed (a field renamed, one added): back to review, whatever
        # keep_approval says, and the layout no longer writes.
        for fields in ([{"name": "MEMBER_ID"}, {"name": "LAST_NAME"}],
                       FIELDS + [{"name": "DOB"}]):
            changed = client.put(f"/api/layouts/{layout_id}",
                                 json={"fields": fields, "keep_approval": True})
            assert changed.status_code == 200, changed.text
            assert changed.json()["status"] == "pending_review", changed.json()
            assert changed.json()["reapproval_required"] is True
            held = layout_service.get_layout(layout_id)
            assert held["approved_by"] is None and held["confirmed_by"] is None
            refused = client.post("/api/export", json={
                "format": "csv", "subject": "members", "layout_id": layout_id,
                "root_limit": 40, "rows": 2})
            assert refused.status_code == 409, refused.text
            approve()

        # The delimiter changed: back to review too.
        piped = client.put(f"/api/layouts/{layout_id}",
                           json={"delimiter": ",", "keep_approval": True})
        assert piped.json()["status"] == "pending_review", piped.json()
    finally:
        layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_keep_approval_is_unchanged_when_no_approval_is_required():
    """No second approver: approval writes nothing different, and an edit with
    keep_approval keeps the status it had, as before."""
    from sdg.analyzer.services import layout_service

    layout_id = _saved("SDMT approval not required")
    try:
        assert client.post("/api/layouts/approve",
                           json={"layout_id": layout_id}).status_code == 200
        edited = client.put(f"/api/layouts/{layout_id}", json={
            "fields": FIELDS + [{"name": "DOB"}], "keep_approval": True})
        assert edited.json()["status"] == "approved", edited.json()
    finally:
        layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_every_change_of_the_approval_setting_is_logged(second_approver):
    log = _work() / "layout-approval-log.jsonl"
    before = log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    assert client.put("/api/layouts/approval",
                      json={"secondApprover": "SDMT other approver"}).status_code == 200
    # Set to what it already is: nothing changed, nothing logged.
    client.put("/api/layouts/approval", json={"secondApprover": "SDMT other approver"})
    assert client.put("/api/layouts/approval",
                      json={"secondApprover": ""}).status_code == 200

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[: len(before)] == before, "the log was rewritten, not appended to"
    added = [json.loads(line) for line in lines[len(before):]]
    assert [(e["old"], e["new"]) for e in added] == [
        (second_approver, "SDMT other approver"), ("SDMT other approver", None)], added
    for entry in added:
        assert entry["who"] == getpass.getuser()
        assert entry["at"][:4].isdigit() and "T" in entry["at"]
    # The log stays when another database is chosen, as the setting does.
    from sdg.api.service import Service

    assert "layout-approval-log.jsonl" in Service.STAYS


@pytest.mark.db
def test_an_unreadable_approval_setting_requires_approval():
    """A damaged setting file must not switch the control off."""
    from sdg.analyzer.services import layout_service

    setting = _work() / "layout-approval.json"
    kept = setting.read_bytes() if setting.exists() else None
    layout_id = None
    try:
        client.put("/api/layouts/approval", json={"secondApprover": None})
        layout_id = _saved("SDMT approval unreadable")
        asked = {"format": "csv", "subject": "members", "layout_id": layout_id,
                 "root_limit": 40, "rows": 2}
        assert client.post("/api/export", json=asked).status_code == 200

        setting.write_text("{not json", encoding="utf-8")
        assert client.get("/api/layouts/approval").json()["required"] is True
        assert client.get("/api/layouts").json()["approvalRequired"] is True
        refused = client.post("/api/export", json=asked)
        assert refused.status_code == 409, refused.text
        approve = client.post("/api/layouts/approve", json={"layout_id": layout_id})
        assert approve.status_code == 409, approve.text
        assert "can't be read" in approve.json()["detail"]

        # Setting it again repairs it, and the repair is logged.
        assert client.put("/api/layouts/approval",
                          json={"secondApprover": None}).status_code == 200
        last = json.loads((_work() / "layout-approval-log.jsonl")
                          .read_text(encoding="utf-8").splitlines()[-1])
        assert last["old"] == "(unreadable)" and last["new"] is None
        assert client.post("/api/export", json=asked).status_code == 200
    finally:
        if kept is None:
            setting.unlink(missing_ok=True)
        else:
            setting.write_bytes(kept)
        if layout_id is not None:
            layout_service.delete_layout(layout_id)


def test_the_approvers_name_matches_by_one_rule_on_screen_and_server():
    """Spaces at either end ignored, letters lower-cased by Unicode's default
    mapping -- Python's lower() and the screen's toLowerCase() agree -- and
    nothing looser: "straße" is not "STRASSE" (casefold would say it is)."""
    from sdg.api.routers.layouts import same_name

    assert same_name("  sdmt APPROVER ", "SDMT approver")
    assert same_name("ÁLVARO ÑÚÑEZ", "álvaro ñúñez")
    assert not same_name("STRASSE", "straße")
    assert not same_name("SDMT approve", "SDMT approver")
    assert not same_name(None, "SDMT approver") and not same_name("", "SDMT approver")


@pytest.mark.db
@pytest.mark.parametrize("named", [5, ["SDMT approver"], {"name": "x"}, True])
def test_an_approver_setting_that_is_not_a_name_requires_approval(named):
    setting = _work() / "layout-approval.json"
    kept = setting.read_bytes() if setting.exists() else None
    try:
        setting.write_text(json.dumps({"secondApprover": named}), encoding="utf-8")
        said = client.get("/api/layouts/approval").json()
        assert said["required"] is True and said["secondApprover"] is None, said
    finally:
        if kept is None:
            setting.unlink(missing_ok=True)
        else:
            setting.write_bytes(kept)


@pytest.mark.db
def test_a_change_of_the_setting_that_cannot_be_logged_is_not_made(second_approver, monkeypatch):
    import sdg.api.routers.layouts as layouts_router

    def unloggable(old, new):
        raise OSError("the log can't be written")

    monkeypatch.setattr(layouts_router, "_log_setting", unloggable)
    failing = TestClient(app, raise_server_exceptions=False)
    refused = failing.put("/api/layouts/approval", json={"secondApprover": "SDMT someone"})
    assert refused.status_code == 500
    assert client.get("/api/layouts/approval").json()["secondApprover"] == second_approver


@pytest.mark.db
def test_a_rule_changed_alone_sends_the_layout_back_to_review(second_approver):
    from sdg.analyzer.services import layout_service

    rules = [r["id"] for r in layout_service.get_rules()][:2]
    assert len(rules) == 2, "the suite's store has generation rules"
    layout_id = _saved("SDMT approval rule only")
    try:
        first = [{"name": "MEMBER_ID", "rule_id": rules[0]}, {"name": "FIRST_NAME"}]
        client.put(f"/api/layouts/{layout_id}", json={"fields": first})
        assert client.post("/api/layouts/approve", json={
            "layout_id": layout_id, "approver": second_approver}).status_code == 200
        other = [{"name": "MEMBER_ID", "rule_id": rules[1]}, {"name": "FIRST_NAME"}]
        changed = client.put(f"/api/layouts/{layout_id}",
                             json={"fields": other, "keep_approval": True})
        assert changed.status_code == 200, changed.text
        assert changed.json()["status"] == "pending_review", changed.json()
        assert layout_service.get_layout(layout_id)["approved_by"] is None
    finally:
        layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_an_edit_keeps_the_author_and_records_who_edited(monkeypatch):
    import sdg.api.app as app_module
    from sdg.analyzer.services import layout_service

    me = getpass.getuser()
    layout_id = _saved("SDMT approval edited by")
    try:
        monkeypatch.setattr(app_module, "_who", lambda: "SDMT editor")
        edited = client.put(f"/api/layouts/{layout_id}",
                            json={"fields": FIELDS + [{"name": "DOB"}]})
        assert edited.status_code == 200, edited.text
        held = layout_service.get_layout(layout_id)
        assert held["created_by"] == me, "an edit overwrote the layout's author"
        assert held["updated_by"] == "SDMT editor"
        listed = next(r for r in client.get("/api/layouts").json()["layouts"]
                      if r["id"] == layout_id)
        assert listed["created_by"] == me and listed["updated_by"] == "SDMT editor"
    finally:
        layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_saving_over_a_layout_keeps_the_author_and_records_who_saved(monkeypatch):
    import sdg.api.app as app_module
    from sdg.analyzer.services import layout_service

    me = getpass.getuser()
    layout_id = _saved("SDMT approval saved over")
    try:
        authored = layout_service.get_layout(layout_id)
        monkeypatch.setattr(app_module, "_who", lambda: "SDMT saver")
        again = client.post("/api/layouts/save", json={
            "name": "SDMT approval saved over", "kind": "csv", "delimiter": "|",
            "fields": FIELDS + [{"name": "DOB"}], "sample_name": None,
            "purpose": "generation"})
        assert again.status_code == 200, again.text
        held = layout_service.get_layout(layout_id)
        assert len(held["fields"]) == 3, "the save did not replace the layout"
        assert held["created_by"] == me, "saving over replaced the layout's author"
        assert held["created_at"] == authored["created_at"]
        assert held["updated_by"] == "SDMT saver"
    finally:
        layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_an_archived_layout_is_restored_before_it_is_edited(second_approver):
    from sdg.analyzer.services import layout_service

    layout_id = _saved("SDMT approval archived edit")
    try:
        assert client.post("/api/layouts/approve", json={
            "layout_id": layout_id, "approver": second_approver}).status_code == 200
        assert client.delete(f"/api/layouts/{layout_id}").json()["action"] == "archived"
        before = layout_service.get_layout(layout_id)

        refused = client.put(f"/api/layouts/{layout_id}",
                             json={"fields": FIELDS + [{"name": "DOB"}]})
        assert refused.status_code == 409, refused.text
        assert "Restore" in refused.json()["detail"]
        assert layout_service.get_layout(layout_id) == before, "the archived layout changed"

        assert client.post(f"/api/layouts/{layout_id}/restore").status_code == 200
        edited = client.put(f"/api/layouts/{layout_id}",
                            json={"fields": FIELDS + [{"name": "DOB"}]})
        assert edited.status_code == 200, edited.text
    finally:
        layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_layouts_that_cannot_be_read_are_said_never_listed_as_none(monkeypatch):
    """No right to add a column SDM needs: the screen says to run the upgrade
    script, rather than showing no layouts."""
    from sdg.analyzer.services import layout_service

    monkeypatch.setattr(layout_service, "ADDED_COLUMNS", {
        "confirmed_by": "THROW 50000, N'ALTER TABLE permission denied', 1;"})
    monkeypatch.setattr(layout_service, "_missing_columns", lambda cur: ["confirmed_by"])
    expected = "Run 01_sdm_upgrade.sql: sdm.layouts needs the confirmed_by column"

    with pytest.raises(layout_service.LayoutsTableOutdated, match=expected):
        layout_service.list_layouts()
    listed = client.get("/api/layouts")
    assert listed.status_code == 503, listed.text
    assert listed.json()["detail"] == expected
    approved = client.get("/api/layouts/approved")
    assert approved.status_code == 503 and approved.json()["detail"] == expected

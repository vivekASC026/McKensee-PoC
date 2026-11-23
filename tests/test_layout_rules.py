"""Which rule fills each field of a layout, chosen rather than only inferred.

A field read from a sample bound to a rule only if its name was the rule's or
one of its aliases. Anything else was written blank, and nothing on the screens
could say what it was. These cover the whole of the fix, as somebody uses it:

    a sample's unmatched field is given a rule, and the file fills it;
    a field can be chosen blank, whatever it is called;
    a name given a rule is remembered, and the next sample binds by itself;
    a saved layout is edited -- rule, width -- and goes back to review;
    the file keeps the layout's delimiter, the sample's start positions and
    the sample's date format.

Layouts and aliases made here are removed by the ids and names the server
returned for them, and no others.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app
from sdg.writers import export

client = TestClient(app)


# ---------------------------------------------------------------------------
# The writers, without a database
# ---------------------------------------------------------------------------

def test_a_fixed_width_row_keeps_the_gaps_its_sample_had():
    fields = [
        {"name": "A", "start": 0, "length": 3},
        {"name": "B", "start": 5, "length": 2},
        {"name": "C", "start": 7, "length": 4, "type": "int"},
    ]
    line = "".join(export.stream_fixed_width([{"A": "ab", "B": "xyz", "C": 7}], fields))
    assert line.rstrip("\r\n") == "ab   xy   7"


def test_a_layout_without_starts_writes_its_fields_end_to_end():
    fields = [{"name": "A", "length": 3}, {"name": "B", "length": 2}]
    line = "".join(export.stream_fixed_width([{"A": "a", "B": "b"}], fields))
    assert line.rstrip("\r\n") == "a  b "


def test_a_date_is_written_the_way_the_sample_wrote_it():
    assert export.layout_value(date(1980, 1, 31), {"date_format": "MM/DD/YYYY"}) == "01/31/1980"
    assert export.layout_value("1980-01-31", {"date_format": "YYYYMMDD"}) == "19800131"
    # Not a date, or no format: left as it is.
    assert export.layout_value("Dana", {"date_format": "YYYYMMDD"}) == "Dana"
    assert export.layout_value(date(1980, 1, 31), {}) == date(1980, 1, 31)
    assert export.layout_value(None, {"date_format": "YYYYMMDD"}) is None


def test_a_field_chosen_blank_names_no_element_whatever_it_is_called(monkeypatch):
    from sdg.analyzer.services import layout_service
    from sdg.api import app as app_module

    monkeypatch.setattr(layout_service, "get_all_rules",
                        lambda: [{"id": 7, "field_name": "FIRST_NAME"}])
    names = app_module._layout_elements([
        {"name": "FIRST_NAME", "blank": True},
        {"name": "GIVEN", "rule_id": 7},
        {"name": "SEX"},
    ])
    assert names == {"FIRST_NAME": None, "GIVEN": "FIRST_NAME", "SEX": "SEX"}


# ---------------------------------------------------------------------------
# Through the API, against the rule repository
# ---------------------------------------------------------------------------

def _rule(field_name: str) -> dict:
    rules = client.get("/api/layouts/rules").json()["rules"]
    found = next((r for r in rules if r["field_name"] == field_name), None)
    if found is None:
        pytest.skip(f"the rule repository has no {field_name}")
    return found


def _choose(field: dict, rule: dict | None) -> dict:
    """What the screen sends when a rule is picked for a field, or blank."""
    if rule is None:
        return {**field, "rule_id": None, "blank": True, "is_default": True,
                "matched_by": "blank"}
    return {**field, "rule_id": rule["id"], "blank": False, "is_default": False,
            "matched_by": "chosen", "rule_label": rule.get("field_label")}


def _layout_id(name: str) -> int:
    from sdg.analyzer.services import layout_service

    return next(row["id"] for row in layout_service.list_layouts() if row["name"] == name)


@pytest.mark.db
def test_a_sample_field_given_a_rule_is_filled_and_its_name_remembered():
    """A pipe-delimited sample whose names the repository has never seen."""
    from sdg.analyzer.services import layout_service

    member, first, dob = _rule("MEMBER_ID"), _rule("FIRST_NAME"), _rule("DOB")
    sample = (
        b"SDMT_MBR_NO|SDMT_GIVEN|SEX|SDMT_BDAY|SDMT_PAD\n"
        b"X0000000001|Dana|F|01/31/1980|zz\n"
        b"X0000000002|Lee|M|12/02/1975|zz\n"
    )
    parsed = client.post("/api/layouts/parse",
                         files={"file": ("sdmt-pipe.txt", sample, "text/plain")})
    assert parsed.status_code == 200, parsed.text
    body = parsed.json()
    assert body["kind"] == "csv" and body["delimiter"] == "|"
    by_name = {f["name"]: f for f in body["fields"]}
    for unknown in ("SDMT_MBR_NO", "SDMT_GIVEN", "SDMT_BDAY", "SDMT_PAD"):
        assert by_name[unknown]["is_default"], f"{unknown} matched something already"
    assert by_name["SEX"]["matched_by"] == "exact"
    assert by_name["SDMT_BDAY"]["date_format"] == "MM/DD/YYYY"

    chosen = {"SDMT_MBR_NO": member, "SDMT_GIVEN": first, "SDMT_BDAY": dob,
              "SDMT_PAD": None, "SEX": None}
    fields = [_choose(f, chosen[f["name"]]) for f in body["fields"]]

    name = "SDMT pipe layout with chosen rules"
    taught: list[str] = []
    layout_id = None
    try:
        saved = client.post("/api/layouts/save", json={
            "name": name, "kind": "csv", "delimiter": "|", "fields": fields,
            "sample_name": "sdmt-pipe.txt", "purpose": "generation",
        })
        assert saved.status_code == 200, saved.text
        layout_id = _layout_id(name)
        taught = saved.json()["aliased"]
        assert sorted(taught) == ["SDMT_BDAY", "SDMT_GIVEN", "SDMT_MBR_NO"], saved.json()
        assert saved.json()["n_default"] == 2

        # The next sample calling them the same binds by itself.
        again = client.post("/api/layouts/parse",
                            files={"file": ("sdmt-pipe.txt", sample, "text/plain")}).json()
        rebound = {f["name"]: f for f in again["fields"]}
        assert rebound["SDMT_GIVEN"]["rule_id"] == first["id"]
        assert rebound["SDMT_GIVEN"]["matched_by"] == "alias"

        assert client.post("/api/layouts/approve",
                           json={"layout_id": layout_id}).status_code == 200
        written = client.post("/api/export", json={
            "format": "csv", "subject": "members", "layout_id": layout_id,
            "root_limit": 40, "rows": 3,
        })
        assert written.status_code == 200, written.text
        lines = [line for line in written.text.split("\r\n") if line]
        assert lines[0] == "SDMT_MBR_NO|SDMT_GIVEN|SEX|SDMT_BDAY|SDMT_PAD", lines[0]
        assert len(lines) > 1, "nothing was written"
        for line in lines[1:]:
            cells = line.split("|")
            assert len(cells) == 5, f"not split by the layout's pipe: {line!r}"
            mbr, given, sex, bday, pad = cells
            assert mbr and given, f"a chosen rule wrote nothing: {line!r}"
            assert sex == "" and pad == "", f"a field chosen blank was filled: {line!r}"
            assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", bday), (
                f"the date is not in the sample's MM/DD/YYYY: {bday!r}")
    finally:
        if layout_id is not None:
            layout_service.delete_layout(layout_id)
        for alias in taught:
            layout_service.remove_alias(alias)


@pytest.mark.db
def test_a_saved_layout_is_edited_and_goes_back_to_review(second_approver):
    """A fixed-width sample, saved blank, then given rules and a wider field."""
    from sdg.analyzer.services import layout_service

    member, first, dob = _rule("MEMBER_ID"), _rule("FIRST_NAME"), _rule("DOB")
    sample = (
        b"SDMT_FX_ID  SDMT_FX_NM  SDMT_FX_DT\n"
        b"A0000000001 Dana        19800131\n"
        b"A0000000002 Lee         19751202\n"
    )
    parsed = client.post("/api/layouts/parse",
                         files={"file": ("sdmt-fixed.dat", sample, "text/plain")}).json()
    assert parsed["kind"] == "fixed"
    assert [f["name"] for f in parsed["fields"]] == ["SDMT_FX_ID", "SDMT_FX_NM", "SDMT_FX_DT"]
    starts = [f["start"] for f in parsed["fields"]]

    name = "SDMT fixed layout edited"
    taught: list[str] = []
    layout_id = None
    try:
        saved = client.post("/api/layouts/save", json={
            "name": name, "kind": "fixed", "delimiter": None,
            "fields": parsed["fields"], "sample_name": "sdmt-fixed.dat",
            "purpose": "generation",
        })
        assert saved.status_code == 200, saved.text
        layout_id = _layout_id(name)
        assert client.post("/api/layouts/approve",
                           json={"layout_id": layout_id,
                                 "approver": second_approver}).status_code == 200

        # The editor: rules for all three, and the name field two wider, which
        # moves the date along by two.
        detail = client.get(f"/api/layouts/{layout_id}").json()
        ident, nm, dt = detail["fields"]
        nm_width = nm["length"] + 2
        fields = [
            _choose(ident, member),
            {**_choose(nm, first), "length": nm_width, "max_width": nm_width},
            {**_choose(dt, dob), "start": dt["start"] + 2},
        ]
        edited = client.put(f"/api/layouts/{layout_id}", json={"fields": fields})
        assert edited.status_code == 200, edited.text
        assert edited.json()["status"] == "pending_review"
        assert edited.json()["reapproval_required"] is True
        taught = edited.json()["aliased"]

        refused = client.post("/api/export", json={
            "format": "fixed", "layout_id": layout_id, "root_limit": 40, "rows": 2,
        })
        assert refused.status_code == 409, "an edited layout wrote before approval"

        assert client.post("/api/layouts/approve",
                           json={"layout_id": layout_id,
                                 "approver": second_approver}).status_code == 200
        written = client.post("/api/export", json={
            "format": "fixed", "subject": "members", "layout_id": layout_id,
            "root_limit": 40, "rows": 3,
        })
        assert written.status_code == 200, written.text
        rows = [line for line in written.text.split("\r\n") if line]
        assert rows, "nothing was written"
        date_at = starts[2] + 2
        for row in rows:
            assert len(row) == date_at + dt["length"], row
            assert row[: ident["length"]].strip(), f"member id blank: {row!r}"
            assert row[ident["length"]: starts[1]].strip() == "", (
                f"the sample's gap was not kept: {row!r}")
            assert row[starts[1]: starts[1] + nm_width].strip(), f"name blank: {row!r}"
            assert re.fullmatch(r"\d{8}", row[date_at: date_at + 8]), (
                f"the date is not at {date_at} in the sample's YYYYMMDD: {row!r}")
    finally:
        if layout_id is not None:
            layout_service.delete_layout(layout_id)
        for alias in taught:
            layout_service.remove_alias(alias)


@pytest.mark.db
def test_a_layout_with_two_fields_of_one_name_is_refused():
    from sdg.analyzer.services import layout_service

    name = "SDMT duplicate names"
    response = client.post("/api/layouts/save", json={
        "name": name, "kind": "csv", "delimiter": ",",
        "fields": [{"name": "SDMT_A"}, {"name": "sdmt_a"}], "purpose": "generation",
    })
    if response.status_code == 200:
        # Saved when it should not have been: removed, so it is not left behind.
        layout_service.delete_layout(_layout_id(name))
    assert response.status_code == 422, response.text


@pytest.mark.db
def test_a_layout_saved_as_new_is_a_copy_and_dropping_takes_it_off_the_list():
    from sdg.analyzer.services import layout_service

    original, copy = "SDMT drop original", "SDMT drop copy"
    fields = [{"name": "SDMT_D_A", "length": 4}, {"name": "SDMT_D_B", "length": 2}]
    made: list[int] = []
    try:
        assert client.post("/api/layouts/save", json={
            "name": original, "kind": "fixed", "fields": fields, "purpose": "generation",
        }).status_code == 200
        made.append(_layout_id(original))

        taken = client.post("/api/layouts/save", json={
            "name": original, "kind": "fixed", "fields": fields[:1],
            "purpose": "generation", "as_new": True,
        })
        assert taken.status_code == 409, "saving as new replaced an existing layout"
        assert client.get(f"/api/layouts/{made[0]}").json()["fields"] == fields

        assert client.post("/api/layouts/save", json={
            "name": copy, "kind": "fixed", "fields": fields[:1],
            "purpose": "generation", "as_new": True,
        }).status_code == 200
        made.append(_layout_id(copy))

        # Not approved: deleted outright.
        dropped = client.delete(f"/api/layouts/{made[1]}")
        assert dropped.status_code == 200 and dropped.json()["action"] == "deleted"
        made.pop()

        # Approved: archived, keeping its approval, and off the list.
        client.post("/api/layouts/approve", json={"layout_id": made[0]})
        dropped = client.delete(f"/api/layouts/{made[0]}")
        assert dropped.json()["action"] == "archived"
        listed = [row["name"] for row in client.get("/api/layouts").json()["layouts"]]
        assert original not in listed
    finally:
        for layout_id in made:
            layout_service.delete_layout(layout_id)


@pytest.mark.db
def test_the_rules_are_listed_when_no_plan_is_built(monkeypatch):
    """A database just read has no plan: the rules screen still lists the
    rules, and says nothing is added from a plan."""
    from sdg.api.app import service
    from sdg.api.service import Service

    monkeypatch.setattr(Service, "plan", property(lambda self: None))
    assert service.plan is None
    preview = client.get("/api/rules/seed")
    assert preview.status_code == 200, preview.text
    assert preview.json()["has_plan"] is False and preview.json()["n_missing"] == 0
    assert client.get("/api/rules").status_code == 200

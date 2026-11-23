"""Files worked through the same steps as a database.

Connect names a folder; Discover & Profile reads and measures every delimited
or fixed-width file in it; Map and Review reads any of them as a layout sample.
The folder is remembered in work/ but with no one database's files, so it
stays when another database is chosen and never makes a plan stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sdg.api.app import app, service

client = TestClient(app)

PIPE = (
    "MEMBER_NO|GIVEN|SEX|BORN\n"
    "X1|Dana|F|01/31/1980\n"
    "X2|Lee|M|12/02/1975\n"
    "X3||F|03/15/1990\n"
)
FIXED = (
    "SDMT_ID     SDMT_NM     SDMT_DT\n"
    "A0000000001 Dana        19800131\n"
    "A0000000002 Lee         19751202\n"
)


@pytest.fixture
def folder(tmp_path: Path):
    (tmp_path / "members.txt").write_text(PIPE)
    (tmp_path / "extract.dat").write_text(FIXED)
    (tmp_path / "notes.pdf").write_text("not a data file")
    setting = service.work / "files-source.json"
    before = setting.read_text() if setting.exists() else None
    yield tmp_path
    if before is None:
        setting.unlink(missing_ok=True)
    else:
        setting.write_text(before)


def test_a_folder_is_named_and_its_data_files_listed(folder):
    stale_before = service.plan_stale()
    listed = client.put("/api/files/source", json={"folder": str(folder)})
    assert listed.status_code == 200, listed.text
    assert [f["name"] for f in listed.json()["files"]] == ["extract.dat", "members.txt"]
    assert client.get("/api/files/source").json()["folder"] == str(folder.resolve())
    assert service.plan_stale() == stale_before, "naming a folder made the plan stale"
    assert "files-source.json" in service.STAYS

    refused = client.put("/api/files/source", json={"folder": str(folder / "nowhere")})
    assert refused.status_code == 422


def test_every_file_is_read_and_each_field_measured(folder):
    client.put("/api/files/source", json={"folder": str(folder)})
    files = {f["name"]: f for f in client.get("/api/files/profile").json()["files"]}

    members = files["members.txt"]
    assert members["kind"] == "csv" and members["delimiter"] == "|"
    assert members["row_count"] == 3
    fields = {f["name"]: f for f in members["fields"]}
    assert fields["GIVEN"]["empty_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert fields["SEX"]["distinct"] == 2
    assert fields["BORN"]["date_format"] == "MM/DD/YYYY"

    extract = files["extract.dat"]
    assert extract["kind"] == "fixed" and extract["row_count"] == 2
    assert [f["name"] for f in extract["fields"]] == ["SDMT_ID", "SDMT_NM", "SDMT_DT"]
    assert extract["fields"][0]["samples"] == ["A0000000001", "A0000000002"]


def test_a_file_in_the_folder_is_read_as_a_layout_sample(folder):
    client.put("/api/files/source", json={"folder": str(folder)})
    parsed = client.post("/api/files/parse", json={"name": "members.txt"})
    assert parsed.status_code == 200, parsed.text
    body = parsed.json()
    assert body["sample_name"] == "members.txt" and body["delimiter"] == "|"
    assert all("matched_by" in f for f in body["fields"]), "fields were not given rules"

    # Only a file of the folder, by its name.
    for name in ("../members.txt", str(folder / "members.txt"), "notes.pdf", "missing.txt"):
        assert client.post("/api/files/parse", json={"name": name}).status_code in (404, 422)

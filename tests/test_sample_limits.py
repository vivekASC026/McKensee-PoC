"""Uploaded samples have a size limit, and a download's name always fits its
header: an over-sized sample is refused with a sentence before it is read,
and a name in any script downloads under an ASCII name plus its real one.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from sdg.api.app import _attachment, app
from sdg.api.limits import MAX_SAMPLE_BYTES

client = TestClient(app)

# Just over the limit, and an X12 envelope so it would otherwise be read.
HUGE = "ISA*00*" + "X" * MAX_SAMPLE_BYTES


def _refused(response) -> None:
    assert response.status_code == 413, response.text[:300]
    assert "larger than 20 MB" in response.json()["detail"]


def test_an_oversized_sample_is_refused_by_every_way_in():
    """Every way a sample comes in: detected, read (as the sample or as the
    master configuration), saved or changed as a configuration, written
    unsaved, and uploaded as a flat-file layout sample."""
    _refused(client.post("/api/samples/detect", json={"sample": HUGE}))
    _refused(client.post("/api/samples/read", json={"sample": HUGE, "auto": True}))
    from sdg.writers.samples import standard

    small = standard.files("hl7-adt")["sample"]
    _refused(client.post("/api/samples/read", json={"sample": small, "master": HUGE}))
    rows = [{"segment_id": "NM1", "position": 3, "algorithm": "LAST_NAME"}]
    _refused(client.post("/api/samples/configs", json={
        "name": "SDMT too big", "sample": HUGE, "rows": rows}))
    _refused(client.put("/api/samples/configs/999999999", json={
        "name": "SDMT too big", "sample": HUGE, "rows": rows}))
    _refused(client.post("/api/export", json={
        "format": "unsaved", "subject": "members", "pattern_id": 1,
        "sample_config": {"sample": HUGE, "rows": rows}}))
    _refused(client.post("/api/layouts/parse", files={
        "file": ("too-big.csv", ("A,B\n" + "x,y\n" * (MAX_SAMPLE_BYTES // 4 + 1)).encode(),
                 "text/csv")}))


def test_the_limit_is_bytes_so_a_multibyte_sample_reaches_it_sooner():
    """Measured in UTF-8 bytes: "é" is two bytes, so half the limit's count
    of them is exactly the limit (read), and one more is over (refused) --
    with far fewer characters than the limit."""
    at_limit = "é" * (MAX_SAMPLE_BYTES // 2)
    assert len(at_limit.encode("utf-8")) == MAX_SAMPLE_BYTES
    read = client.post("/api/samples/detect", json={"sample": at_limit})
    assert read.status_code == 200, read.text[:300]
    over = at_limit + "é"
    assert len(over) < MAX_SAMPLE_BYTES < len(over.encode("utf-8"))
    _refused(client.post("/api/samples/detect", json={"sample": over}))
    # The same rule for a layout sample uploaded as a file.
    _refused(client.post("/api/layouts/parse", files={
        "file": ("multibyte.csv", ("A\n" + over).encode("utf-8"), "text/csv")}))


def test_a_sample_at_the_limit_is_read():
    """The limit is a size, not a format check: a sample of exactly the limit
    is accepted (read as a flat file), and a small one still reads."""
    at_limit = client.post("/api/samples/detect", json={"sample": "x" * MAX_SAMPLE_BYTES})
    assert at_limit.status_code == 200, at_limit.text[:300]
    assert at_limit.json() == {"format": "flat"}
    from sdg.writers.samples import standard

    small = standard.files("hl7-adt")["sample"]
    assert len(small.encode("utf-8")) < MAX_SAMPLE_BYTES
    detected = client.post("/api/samples/detect", json={"sample": small})
    assert detected.status_code == 200, detected.text
    assert detected.json()["format"] == "hl7v2"


def _header_parts(header: str) -> tuple[str, dict[str, str]]:
    """The header read by the standard library's MIME parser (quoted strings
    and RFC 2231 extended values understood): its disposition, its plain
    filename, and its filename* decoded, where it has one."""
    import email.message
    import email.utils

    message = email.message.Message()
    message["Content-Disposition"] = header
    params = message.get_params(header="content-disposition")
    disposition, parts = params[0][0], {}
    for key, value in params[1:]:
        extended = isinstance(value, tuple)
        if extended:
            assert value[0] == "UTF-8", value
        parts["filename*" if extended else key] = email.utils.collapse_rfc2231_value(value)
    return disposition, parts


@pytest.mark.parametrize("name", [
    "Beneficiário ✓ 834.txt", "日本語のファイル.hl7", "tab\there.txt", "semi;colon.csv",
    "C:\\Users\\x\\\"quoted\".edi",
])
def test_a_download_name_in_any_script_fits_its_header(name):
    header = _attachment(name)
    header.encode("latin-1")  # what the server sends a header as
    # Carried by a real response: the header is encodable.
    Response(b"", headers={"Content-Disposition": header})
    disposition, parts = _header_parts(header)
    assert disposition == "attachment"
    plain = parts["filename"]
    assert plain and all(32 <= ord(c) < 127 for c in plain), plain
    assert not any(c in plain for c in '"\\/;'), plain
    assert "\r" not in header and "\n" not in header and "\t" not in header
    if "filename*" in parts:
        real = parts["filename*"]
        assert "/" not in real and "\\" not in real and '"' not in real
        assert real.replace("\t", "") == real
    if name == "日本語のファイル.hl7":
        assert parts["filename*"] == name
        assert plain.endswith(".hl7")


def test_a_name_with_a_semicolon_keeps_it_only_in_the_encoded_name():
    """A semicolon separates a header's parameters: the plain name never holds
    one, and the encoded name carries it percent-encoded -- the exact header."""
    header = _attachment("semi;colon.csv")
    assert header == "attachment; filename=\"semi_colon.csv\"; filename*=UTF-8''semi%3Bcolon.csv"
    assert _header_parts(header) == ("attachment", {"filename": "semi_colon.csv",
                                                    "filename*": "semi;colon.csv"})


def test_a_plain_name_keeps_its_simple_header():
    assert _attachment("834-member.txt") == 'attachment; filename="834-member.txt"'
    assert _attachment("../../etc/passwd") == 'attachment; filename="passwd"'


@pytest.mark.db
def test_an_unsaved_configuration_named_in_any_script_downloads():
    """The name typed for an unsaved configuration goes into the file name;
    a name outside Latin-1 failed the download with a server error."""
    from sdg.analyzer import db
    from sdg.writers.samples import standard

    pattern = next((p for p in db.get_all_patterns()
                    if p.get("profile_json") and "claims_pattern" not in (p["profile_json"] or "")),
                   None)
    # The suite's Pattern data always holds members patterns; none is a broken
    # fixture, said as a failure rather than a skip that hides this test.
    assert pattern is not None, (
        "Pattern data holds no members pattern: run the suite against the seeded store")
    sample = standard.files("hl7-adt")["sample"]
    read = client.post("/api/samples/read", json={
        "sample": sample, "sampleName": "mine.hl7", "auto": True}).json()
    rows = [{**r, "algorithm": r["algorithm"] or r["suggested"]} for r in read["rows"]]
    written = client.post("/api/export", json={
        "format": "unsaved", "subject": "members", "pattern_id": pattern["id"],
        "members": 2, "record": False,
        "sample_config": {"name": "Admissões 日本", "sample": sample, "sampleName": "mine.hl7",
                          "basedOn": read["basedOn"], "rows": rows, "vary": read["vary"]}})
    assert written.status_code == 200, written.text
    header = written.headers["content-disposition"]
    assert "filename*=UTF-8''" in header, header
    assert "Admiss" in unquote(header.split("filename*=UTF-8''", 1)[1])

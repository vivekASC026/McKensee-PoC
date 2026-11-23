"""Health-only formats are offered only by a pack that names their modules.

X12 (834, 835, 837P), SNIP, FHIR, HL7 v2, HL7 v3 and CDA are written by code
only the health business uses. A pack offers each by naming its module; one
that does not gets "... is not offered by the active business pack" from every
route that would read or write it, and the screens do not show it.

The pack used here is the health pack with its modules taken away, so every
other route works as it does for health and only the offering differs.
"""

from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from sdg import packs
from sdg.api.app import app
from sdg.writers import samples
from sdg.writers.samples import standard
from tests.test_pack_constants import ROOT

client = TestClient(app)
DEFAULTS = ROOT / "sdg" / "writers" / "samples" / "defaults"
NOT_OFFERED = "is not offered by the active business pack (Plain)"


def health_offering(modules: list[str], folder, monkeypatch) -> packs.Pack:
    """The health pack offering only these modules, as "Plain", and made active."""
    shutil.copytree(ROOT / "sdg" / "packs" / "health", folder / "plain")
    body = json.loads((folder / "plain" / "pack.json").read_text(encoding="utf-8"))
    body.update(id="plain", name="Plain", modules=modules,
                formats={"members": ["csv"], "claims": ["csv"]})
    (folder / "plain" / "pack.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setenv("SDG_PACKS_DIR", str(folder))
    monkeypatch.setenv("SDG_PACK", "plain")
    packs.load.cache_clear()
    return packs.active()


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """The health pack offering no health module, and only CSV files."""
    yield health_offering([], tmp_path, monkeypatch)
    packs.load.cache_clear()


def _refused(response, what: str) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"{what} {NOT_OFFERED}."


def test_the_health_pack_offers_every_format_it_lists():
    """Each format in the health pack's menus is written by a module it offers."""
    health = packs.load("health")
    by_id = {d["id"]: d["format"] for d in standard._every()}
    for entity, listed in health.formats.items():
        for fmt in listed:
            kind, _, name = fmt.partition(":")
            if kind == "edi":
                assert health.has_module("x12"), fmt
            elif kind == "standard":
                assert name in by_id, f"{entity}: no standard file {name}"
                assert any(health.has_module(m) for m in samples.PACK_MODULES[by_id[name]]), fmt
            else:
                assert fmt == "csv", f"{entity}: a format this test does not know: {fmt}"
    assert {"x12", "snip", "fhir", "hl7v2", "hl7v3", "cda"} <= set(health.data["modules"])


def test_the_health_pack_offers_every_sample_format():
    assert all(samples.offered(f) for f in samples.FORMATS)
    assert [d["id"] for d in standard.listing()] == [d["id"] for d in standard._every()]


def test_x12_is_refused_where_it_is_not_offered(plain):
    assert plain.id == "plain" and not plain.has_module("x12")
    _refused(client.get("/api/edi/formats"), "X12 EDI")
    with pytest.raises(packs.NotOffered, match="X12 EDI is not offered"):
        from sdg.writers.edi_formats import load_format

        load_format("834")


def test_snip_is_refused_where_it_is_not_offered(plain):
    _refused(client.get("/api/edi/snip"), "SNIP checking")
    x12 = (DEFAULTS / "x12-834.txt").read_bytes()
    _refused(client.post("/api/edi/snip/check-file", files={"file": ("a.txt", x12)}),
             "SNIP checking")


@pytest.mark.parametrize("sample, label", [
    ("x12-834.txt", "X12"),
    ("fhir-members-bundle.json", "FHIR"),
    ("hl7-adt-a01.hl7", "HL7 v2"),
    ("cda-ccd.xml", "HL7 v3"),
])
def test_a_sample_of_a_format_not_offered_is_refused_by_name(plain, sample, label):
    text = (DEFAULTS / sample).read_text(encoding="utf-8")
    fmt = next(m for m in samples.FORMATS.values() if m.detect(text))
    assert fmt.LABEL.startswith(label)
    _refused(client.post("/api/samples/detect", json={"sample": text}), fmt.LABEL)
    _refused(client.post("/api/samples/read", json={"sample": text, "auto": True}), fmt.LABEL)


def test_a_flat_sample_is_still_read_where_nothing_is_offered(plain):
    body = client.post("/api/samples/detect", json={"sample": "ID,NAME\n1,Ann\n"}).json()
    assert body == {"format": "flat"}


def test_no_standard_file_or_saved_configuration_is_listed_where_none_is_offered(plain):
    assert client.get("/api/samples/defaults").json() == {"defaults": []}
    assert client.get("/api/samples/configs").json() == {"configs": []}
    # Named by id, a standard file is refused by its format, not lost.
    _refused(client.post("/api/samples/read", json={"defaultId": "fhir-members"}),
             samples.FORMATS["fhir"].LABEL)


def test_the_screens_are_told_what_is_offered(plain):
    body = client.get("/api/pack").json()
    assert body["id"] == "plain"
    assert body["modules"] == []
    assert body["formats"] == {"members": ["csv"], "claims": ["csv"]}


def test_the_screens_show_a_format_tab_only_where_it_is_offered():
    """Output.tsx's tabs name the modules the server checks: the same names."""
    import re

    text = (ROOT / "ui" / "src" / "views" / "Output.tsx").read_text(encoding="utf-8")
    block = text[text.index("const FILE_TABS"):text.index("];", text.index("const FILE_TABS"))]
    tabs = {m.group(1): set(re.findall(r'"([\w-]+)"', m.group(2)))
            for m in re.finditer(r'\["([\w-]+)", "[^"]+", \[([^\]]*)\]\]', block)}
    assert tabs["layouts"] == set()
    assert tabs["edi"] == {"x12"}
    assert tabs["fhir"] == set(samples.PACK_MODULES["fhir"])
    assert tabs["hl7v2"] == set(samples.PACK_MODULES["hl7v2"])
    assert tabs["cda"] == set(samples.PACK_MODULES["cda"])
    assert tabs["edi-sample"] == {m for ms in samples.PACK_MODULES.values() for m in ms}


@pytest.mark.parametrize("fmt, what", [
    ("edi:834", "X12 EDI"), ("edi:835", "X12 EDI"), ("edi834", "X12 EDI"),
    ("standard:fhir-members", "FHIR"), ("standard:hl7-adt", "HL7 v2"),
    ("standard:cda-ccd", "HL7 v3"),
])
def test_a_file_in_a_format_not_offered_is_not_written(plain, fmt, what):
    """Nothing is written or kept: the export, and its checks, refuse by name."""
    from sdg.analyzer.services import generation_requests as gr

    label = next((m.LABEL for m in samples.FORMATS.values() if m.LABEL.startswith(what)), what)
    before = gr.list_requests() if hasattr(gr, "list_requests") else None
    _refused(client.post("/api/export", json={"format": fmt, "record": False}), label)
    check = "/api/edi/snip/check" if fmt.startswith("edi") else "/api/samples/check"
    _refused(client.post(check, json={"export": {"format": fmt, "record": False}}), label)
    if before is not None:
        assert gr.list_requests() == before


def test_snip_checks_of_text_are_refused_where_not_offered(plain):
    from sdg.writers.snip import validate

    with pytest.raises(packs.NotOffered, match="SNIP checking is not offered"):
        validate((DEFAULTS / "x12-834.txt").read_text(encoding="utf-8"))


def test_a_saved_configuration_of_a_format_not_offered_is_refused(plain, monkeypatch):
    """Read by id, saved or saved over: refused by its format, and nothing is stored."""
    from sdg.analyzer.services import sample_gen_configs

    monkeypatch.setattr(sample_gen_configs, "get_config", lambda config_id: {
        "id": config_id, "name": "kept", "format": "fhir", "kind": "Bundle",
        "config": {}, "sample_text": "{}", "sample_name": "a.json", "based_on": None})
    stored = []
    monkeypatch.setattr(sample_gen_configs, "save_as", lambda **kw: stored.append(kw))
    monkeypatch.setattr(sample_gen_configs, "update", lambda *a, **kw: stored.append(kw))
    _refused(client.get("/api/samples/configs/7"), samples.FORMATS["fhir"].LABEL)
    x12 = (DEFAULTS / "x12-834.txt").read_text(encoding="utf-8")
    body = {"name": "mine", "sample": x12, "rows": [{"at": "NM1*03", "algorithm": "x"}]}
    _refused(client.post("/api/samples/configs", json=body), samples.FORMATS["x12"].LABEL)
    _refused(client.put("/api/samples/configs/7", json=body), samples.FORMATS["x12"].LABEL)
    assert stored == []


def test_write_again_says_not_offered_not_missing(plain, monkeypatch):
    """A kept X12 file written again under a pack without X12: 404, by name,
    not the 409 a run with missing history gets."""
    import sdg.api.app as api_app

    def refusing(*args, **kwargs):
        packs.require("x12", "X12 EDI")

    monkeypatch.setattr(api_app, "_request_stream", refusing)
    from sdg.analyzer.services import generation_requests as gr

    monkeypatch.setattr(gr, "get_file", lambda file_id: {
        "id": file_id, "request_id": 1, "format": "edi:834", "params": {"format": "edi:834"},
        "snapshots": {},
        "subject": "members", "file_name": "834.txt", "digest": "x"}, raising=False)
    got = client.post("/api/generation-requests/files/5/again")
    assert got.status_code == 404, got.text
    assert got.json()["detail"] == f"X12 EDI {NOT_OFFERED}."


@pytest.mark.parametrize("modules, offered, refused", [
    (["hl7v3"], ["cda-ccd.xml"], ["x12-834.txt", "fhir-members-bundle.json"]),
    (["cda"], ["cda-ccd.xml"], ["hl7-adt-a01.hl7"]),
    (["x12"], ["x12-834.txt"], ["fhir-members-bundle.json", "cda-ccd.xml"]),
    (["fhir", "hl7v2"], ["fhir-members-bundle.json", "hl7-adt-a01.hl7"], ["x12-837.txt"]),
])
def test_a_pack_offering_some_modules_offers_only_their_formats(
        tmp_path, monkeypatch, modules, offered, refused):
    health_offering(modules, tmp_path, monkeypatch)
    try:
        for sample in offered:
            text = (DEFAULTS / sample).read_text(encoding="utf-8")
            got = client.post("/api/samples/detect", json={"sample": text})
            assert got.status_code == 200, (sample, got.text)
        for sample in refused:
            text = (DEFAULTS / sample).read_text(encoding="utf-8")
            assert client.post("/api/samples/detect", json={"sample": text}).status_code == 404
        listed = {d["format"] for d in client.get("/api/samples/defaults").json()["defaults"]}
        assert listed == {f for f in samples.FORMATS if samples.offered(f)}
    finally:
        packs.load.cache_clear()


def test_paid_claims_need_adjudication_beside_x12(tmp_path, monkeypatch):
    """The 835 says what each claim paid: offered only with adjudication."""
    from sdg.writers.edi_formats import formats, load_format

    health_offering(["x12"], tmp_path, monkeypatch)
    try:
        assert load_format("834").id == "834"
        with pytest.raises(packs.NotOffered, match=r"\(paid claims\) is not offered"):
            load_format("835")
        assert "835" not in {f.id for f in formats()}
        assert "835" not in {f["id"] for f in client.get("/api/edi/formats").json()["formats"]}
    finally:
        packs.load.cache_clear()


def test_a_file_from_a_pattern_is_refused_where_every_file_is_set_up(plain):
    """A download, a check and a write again all set a file up in one place:
    a pack that does not offer generating from patterns refuses it there."""
    from sdg.api import app as app_module

    for asked in ({"pattern_id": 1}, {"scenarios": [{"pattern_id": 1, "label": "A"}]}):
        request = app_module.ExportRequest(format="csv", subject="members", **asked)
        with pytest.raises(packs.NotOffered, match="Generating from patterns is not offered"):
            app_module._request_stream(request)


@pytest.fixture
def analysis_only(tmp_path, monkeypatch):
    """The health pack analyzing patterns, writing nothing from them."""
    yield health_offering(["pattern_analysis"], tmp_path, monkeypatch)
    packs.load.cache_clear()


@pytest.mark.parametrize("method, path", [
    ("post", "/api/patterns/to-plan"),
    ("post", "/api/story/to-plan"),
    ("get", "/api/patterns/1/claim-shape"),
    ("post", "/api/generation-requests/1/claims-check"),
])
def test_plans_and_checks_from_a_pattern_are_refused_where_not_offered(
        analysis_only, method, path):
    """Each route that writes a plan from a pattern, or measures one for a
    file, refuses by name where the pack offers no generating from patterns,
    before reading anything."""
    got = client.post(path, json={}) if method == "post" else client.get(path)
    _refused(got, "Generating from patterns")

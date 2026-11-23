"""SDM's API answers only its own screens, on this machine's own names.

A web page on another site, in the same browser, could otherwise call the API:
read real rows, start a load. Every /api call must carry the token the server
put in its own page, name the machine it runs on, and not come from a page on
another site. The suite's own clients send the token (conftest.py); these
checks send what an outsider would.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from sdg.api import guard
from sdg.api.app import UI_DIST, app

outsider = TestClient(app, headers={"X-SDM-Token": ""})
screens = TestClient(app)


def test_a_call_without_the_token_is_refused():
    refused = outsider.get("/api/state")
    assert refused.status_code == 401
    assert "SDM's screens" in refused.json()["detail"]
    assert outsider.post("/api/generate").status_code == 401
    wrong = TestClient(app, headers={"X-SDM-Token": "guessed"})
    assert wrong.get("/api/state").status_code == 401
    assert screens.get("/api/state").status_code == 200


def test_the_api_schema_is_only_for_the_screens():
    assert outsider.get("/api/openapi.json").status_code == 401
    assert "paths" in screens.get("/api/openapi.json").json()
    assert "paths" not in outsider.get("/openapi.json").text


def test_another_name_for_this_machine_is_refused():
    # A site pointing its own name at this machine (DNS rebinding) is not it.
    refused = screens.get("/api/state", headers={"host": "attacker.example:8777"})
    assert refused.status_code == 403 and "SDG_ALLOWED_HOSTS" in refused.json()["detail"]
    assert screens.get("/", headers={"host": "attacker.example"}).status_code == 403
    for own in ("localhost:8777", "127.0.0.1:8777", "[::1]:8777"):
        assert screens.get("/api/state", headers={"host": own}).status_code == 200, own


def test_a_page_on_another_site_is_refused():
    refused = screens.post("/api/generate", headers={"origin": "https://attacker.example"})
    assert refused.status_code == 403
    same = screens.get("/api/state", headers={"origin": "http://localhost:8777"})
    assert same.status_code == 200


def test_the_screens_page_carries_the_token():
    if not UI_DIST.exists():
        return
    page = outsider.get("/")
    assert page.status_code == 200
    assert f'<meta name="sdm-token" content="{guard.TOKEN}">' in page.text
    # Every route of the interface is the same page, token and all.
    assert guard.TOKEN in outsider.get("/generation/files").text


def test_the_page_never_serves_a_file_outside_the_interface():
    if not UI_DIST.exists():
        return
    for path in ("/%2E%2E/%2E%2E/pyproject.toml", "/..%2F..%2Fpyproject.toml"):
        answered = outsider.get(path)
        assert "[project]" not in answered.text, path


def test_the_api_documentation_page_is_open_and_nothing_else_is():
    assert outsider.get("/api/docs").status_code == 200
    assert outsider.get("/api/generation-requests").status_code == 401


def test_a_file_that_cannot_begin_is_refused_with_a_sentence(monkeypatch):
    from sdg.api import app as api

    def failing():
        raise ValueError("the rule for MEME_SEX cannot be read")
        yield ""  # a generator, as a file is

    monkeypatch.setattr(api, "_request_stream",
                        lambda request, replay=None: (failing(), "text/plain", "f.txt", None))
    # As the browser sees it: the error is the response, not a stream cut off.
    browser = TestClient(app, raise_server_exceptions=False)
    refused = browser.post("/api/export", json={"format": "csv"})
    assert refused.status_code == 500
    assert "MEME_SEX" in refused.json()["detail"]

    def whole():
        yield "a,b\n"
        yield "1,2\n"

    monkeypatch.setattr(api, "_request_stream",
                        lambda request, replay=None: (whole(), "text/plain", "f.txt", None))
    written = screens.post("/api/export", json={"format": "csv"})
    assert written.status_code == 200 and written.text == "a,b\n1,2\n"

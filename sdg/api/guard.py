"""Who may call SDM's API: its own screens, on this machine's own names.

The API reads real rows and writes into databases, and it listens on a port
any web page in the same browser can send requests to. So every /api request
must carry the token this server made when it started (`X-SDM-Token`), which
only its own page is given -- in a <meta> tag of the index.html it serves. A
page on another site cannot read that page, so it cannot send the token.

The host checked as well: a site that points its own name at 127.0.0.1 (DNS
rebinding) would otherwise be the same origin as SDM and could read the token
from the page. A request is answered only when it names SDM by one of this
machine's names -- localhost, its own host name or addresses -- or a name in
SDG_ALLOWED_HOSTS (comma separated). A request from a page on another site
(its Origin) is refused too.

A script that calls the API sets SDG_API_TOKEN before starting the server and
sends it; otherwise the token is new each time the server starts.
"""

from __future__ import annotations

import hmac
import os
import secrets
import socket
from functools import lru_cache

from fastapi.responses import JSONResponse

HEADER = "X-SDM-Token"
META = "sdm-token"

TOKEN = os.getenv("SDG_API_TOKEN", "").strip() or secrets.token_urlsafe(32)

# The API's own documentation page, and nothing else under /api, is open: it
# reads nothing and writes nothing. Calls made from it need the token.
_OPEN = {"/api/docs", "/api/docs/oauth2-redirect"}


@lru_cache(maxsize=1)
def _own_names() -> frozenset[str]:
    names = {"localhost", "127.0.0.1", "::1", "[::1]"}
    try:
        host = socket.gethostname()
        names |= {host, socket.getfqdn()}
        names |= set(socket.gethostbyname_ex(host)[2])
    except OSError:
        pass
    return frozenset(n.lower() for n in names if n)


def allowed_hosts() -> frozenset[str]:
    extra = {n.strip().lower() for n in os.getenv("SDG_ALLOWED_HOSTS", "").split(",")}
    return _own_names() | frozenset(extra - {""})


def _host_of(value: str) -> str:
    """'host:port', '[::1]:8777' or a URL's authority, as the host alone."""
    value = value.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1].split("/", 1)[0]
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def refusal(path: str, headers) -> JSONResponse | None:
    """Why this request is not answered, or None when it is."""
    hosts = allowed_hosts()
    host = _host_of(headers.get("host", ""))
    if host not in hosts:
        return JSONResponse(status_code=403, content={"detail": (
            f"SDM answers only by this machine's own names, not {host or 'no name'}. "
            f"To reach it by another name, add it to SDG_ALLOWED_HOSTS in .env.")})
    origin = headers.get("origin")
    if origin and origin != "null" and _host_of(origin) not in hosts:
        return JSONResponse(status_code=403, content={"detail": (
            "A page on another site cannot use SDM.")})
    if path.startswith("/api") and path not in _OPEN:
        sent = headers.get(HEADER.lower(), "")
        if not hmac.compare_digest(sent.encode(), TOKEN.encode()):
            return JSONResponse(status_code=401, content={"detail": (
                "Open SDM from its own address to use it: this request did not "
                "come from SDM's screens.")})
    return None


def with_token(html: str) -> str:
    """The screens' page, carrying the token they send back."""
    tag = f'<meta name="{META}" content="{TOKEN}">'
    return html.replace("<head>", "<head>" + tag, 1) if "<head>" in html else tag + html

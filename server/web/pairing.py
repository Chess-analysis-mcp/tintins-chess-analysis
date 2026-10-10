"""Pairing credential for network access ("Allow other devices on my network to connect").

Bound to loopback, only this computer can reach the board. Bound to the network, anyone on the Wi-Fi
could otherwise call the API (spend the user's Claude quota, read game history). So when the server
listens beyond loopback, every request from ANOTHER device must carry a secret that only the person at
this computer can hand over: it is shown as a QR code / link in the 📱 Phone popover (the token is only
ever returned to this computer). Scanning it sets an HttpOnly, SameSite=Strict cookie on the phone.

The token lives in memory only and is regenerated on every launch, so a restart un-pairs phones (they
re-scan) and there's no secret on disk. Requests from this computer never need it.
"""
from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from server import config
from server.web.local_client import is_local_client

COOKIE_NAME = "chess_pair"
QUERY_PARAM = "pair"

_token: str = secrets.token_urlsafe(32)


def get_token() -> str:
    return _token


def reset_token() -> str:
    """Issue a fresh token (invalidates every paired device). Used by tests."""
    global _token
    _token = secrets.token_urlsafe(32)
    return _token


def required() -> bool:
    """True when the server is configured to listen beyond loopback, so remote clients must pair."""
    return config._reachable_from_network(config.WEB_HOST)


def _matches(candidate: str | None) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), _token.encode("utf-8"))


def with_token(url: str | None) -> str | None:
    """The pairing link for the QR code: the board address plus the token. Local callers only."""
    return f"{url}/?{QUERY_PARAM}={_token}" if url else None


_UNPAIRED_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair this device</title>
<style>body{font-family:system-ui,sans-serif;max-width:30em;margin:12vh auto;padding:0 1.2em;line-height:1.5}</style>
</head><body>
<h1>Pair this device</h1>
<p>This board needs a pairing key before another device can use it.</p>
<p>On the computer running the app, press <b>📱 Phone</b> at the top of the board and scan the QR code
with this device's camera.</p>
<p>(Typing the address by hand isn't enough, and the key changes whenever the app is restarted.)</p>
</body></html>"""


def _unpaired(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": "Not paired. On the computer running the app, press 📱 Phone and scan the QR code."},
            status_code=401,
        )
    return HTMLResponse(_UNPAIRED_HTML, status_code=401)


def _origin_matches_host(request: Request) -> bool:
    """For state-changing requests from another device: Origin, if sent, must be the board itself."""
    origin = request.headers.get("origin")
    if origin is None:
        return True  # non-browser client, or an old browser; SameSite=Strict covers the cookie side
    if "://" not in origin:  # "null" and other opaque values
        return False
    host = request.headers.get("host", "").strip().lower()
    return bool(host) and origin.split("://", 1)[1].strip().lower() == host


def check(request: Request) -> Response | None:
    """Gate a request from another device. None = let it through; otherwise the response to send.

    Callers skip this when `required()` is false; this computer's own requests always pass.
    """
    if is_local_client(request):
        return None
    supplied = request.query_params.get(QUERY_PARAM)
    if supplied is not None and request.method in ("GET", "HEAD"):
        if not _matches(supplied):
            return _unpaired(request)
        # Valid: remember it in a cookie and drop the key from the address bar (history, referrers).
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE_NAME, _token, httponly=True, samesite="strict", path="/")
        return resp
    if not _matches(request.cookies.get(COOKIE_NAME)):
        return _unpaired(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_matches_host(request):
        return PlainTextResponse("Forbidden: cross-origin request.", status_code=403)
    return None

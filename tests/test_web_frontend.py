"""Regression tests for the static frontend mount.

A wheel install (`uv run` for the MCP server) installs only the `server` package; the frontend
used to live as a repo-root sibling of `server/`, so it was NOT shipped in the wheel and the board
served the API but 404'd at `/` ({"detail":"Not Found"}). pyproject force-includes it as
`server/_frontend/` and `app._resolve_frontend_dir` finds it there (or in the source tree). These
tests guard both halves: the resolver locates a real directory, and `/` actually serves the UI.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server.web import app as app_module


def test_frontend_dir_resolves_to_a_real_directory():
    # Whatever the install layout, the resolver must find shipped assets — not None (which would
    # mean the board has no UI and `/` 404s).
    resolved = app_module._resolve_frontend_dir()
    assert resolved is not None, "frontend assets not found — the board would 404 at '/'"
    assert (resolved / "index.html").is_file()


def test_root_serves_the_board_not_a_404():
    client = TestClient(app_module.create_app())
    # The bug: API up, UI missing. Assert the UI is actually mounted.
    assert client.get("/api/app-config").status_code == 200
    root = client.get("/")
    assert root.status_code == 200, "GET / should serve index.html, not 404"
    assert "<!doctype html>" in root.text.lower()


def test_doctor_endpoint_reports_dependency_status():
    # The setup banner (checkSetup() in main.js) reads /api/doctor; it must always return the three
    # checks with a boolean `ok`, and flag `claude` as optional so a missing CLI never reads as a
    # blocker.
    client = TestClient(app_module.create_app())
    r = client.get("/api/doctor")
    assert r.status_code == 200
    checks = r.json()["checks"]
    for name in ("python", "stockfish", "claude"):
        assert isinstance(checks[name]["ok"], bool)
    assert checks["claude"].get("optional") is True


def test_packaged_frontend_location_matches_pyproject_force_include():
    # The wheel ships assets at server/_frontend/ (pyproject [tool.hatch...force-include]); the
    # resolver checks that path first. Keep the two in lockstep so a rename can't silently break
    # installed copies while source checkouts keep working.
    server_pkg = Path(app_module.__file__).resolve().parent.parent  # .../server
    assert server_pkg.name == "server"
    # The resolver's packaged candidate is server/_frontend (a sibling of server/web/).
    assert app_module._resolve_frontend_dir() in (
        server_pkg / "_frontend",
        server_pkg.parent / "frontend",
    )


# --- Offline-capable frontend (vendored chessground/chess.js, no CDN) -----------------------------


def test_frontend_has_no_cdn_dependencies():
    # The board must render fully offline: chessground + chess.js (JS and CSS) are vendored under
    # frontend/vendor and referenced by relative path. A reintroduced CDN URL would silently break
    # offline use, so guard index.html + main.js against the old hosts.
    client = TestClient(app_module.create_app())
    index = client.get("/").text
    main_js = client.get("/main.js").text
    for ref in ("cdn.jsdelivr.net", "esm.sh", "unpkg.com"):
        assert ref not in index, f"index.html still references {ref} (breaks offline use)"
        assert ref not in main_js, f"main.js still references {ref} (breaks offline use)"


def test_vendored_assets_are_served():
    # The relative paths index.html/main.js import must actually resolve from the static mount.
    client = TestClient(app_module.create_app())
    for path in (
        "/vendor/chessground.min.js",
        "/vendor/chess.min.js",
        "/vendor/chessground.base.css",
        "/vendor/chessground.brown.css",
        "/vendor/chessground.cburnett.css",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} should be served, not {r.status_code}"
        assert r.content, f"{path} is empty"
    # The JS must carry a JavaScript MIME or the browser rejects the ES module import.
    ct = client.get("/vendor/chessground.min.js").headers["content-type"]
    assert ct.startswith(("text/javascript", "application/javascript")), ct


def test_js_served_with_javascript_mime_even_if_registry_says_text_plain(monkeypatch):
    # Windows footgun: some installs map `.js` -> `text/plain` in the registry, which makes the
    # browser refuse `<script type="module">` (and thus the vendored chessground/chess.js imports),
    # blanking the offline board. `server.web.app` calls mimetypes.add_type at import to override
    # that. Simulate the hostile mapping, reload the module, and confirm the override wins.
    import importlib
    import mimetypes

    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    importlib.reload(app_module)
    try:
        assert mimetypes.guess_type("vendor/chessground.min.js")[0] == "text/javascript"
        assert mimetypes.guess_type("main.js")[0] == "text/javascript"
    finally:
        importlib.reload(app_module)  # restore clean module state for the rest of the suite


def test_connectivity_endpoint_reports_shape(monkeypatch):
    # The offline banner (checkOnline() in main.js) reads /api/connectivity; it must always return
    # booleans for `online` and `local_llm` so the page can decide the warning wording. Mock the
    # probe so the test never hits the network (and exercises the offline branch deterministically).
    from server.web import routes_board

    monkeypatch.setattr(routes_board, "_probe_online", lambda: False)
    client = TestClient(app_module.create_app())
    r = client.get("/api/connectivity")
    assert r.status_code == 200
    body = r.json()
    assert body["online"] is False
    assert isinstance(body["local_llm"], bool)


# --- Local-only request guard (CSRF / DNS-rebinding defence) -------------------------------------


def test_same_origin_request_is_allowed():
    # The board's own frontend sends Origin: http://127.0.0.1:<port> — must pass.
    client = TestClient(app_module.create_app())
    r = client.get("/api/app-config", headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_cross_origin_request_is_blocked():
    # A page on evil.com that makes the browser POST to the board must be rejected before any route
    # runs (so it can't spend Claude quota). The Host stays local; the Origin gives it away.
    client = TestClient(app_module.create_app())
    r = client.post("/api/chat", headers={"origin": "https://evil.com"}, json={"question": "hi"})
    assert r.status_code == 403


def test_non_local_host_is_blocked():
    # DNS rebinding: the IP resolves to 127.0.0.1 but the browser still sends the attacker's Host.
    client = TestClient(app_module.create_app())
    r = client.get("/api/app-config", headers={"host": "attacker.example"})
    assert r.status_code == 403


def test_opaque_origin_allowed_for_reads_blocked_for_writes():
    # The file:// loading splash (Origin: null) polls /api/app-config — allow that GET; but a
    # sandboxed iframe also gets Origin: null, so never let it reach a state-changing POST.
    client = TestClient(app_module.create_app())
    assert client.get("/api/app-config", headers={"origin": "null"}).status_code == 200
    assert client.post("/api/chat", headers={"origin": "null"}, json={"question": "hi"}).status_code == 403

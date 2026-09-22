"""Settings routes: read/update the user-editable config so the app is standalone.

`GET /api/settings` returns the current effective values for the Settings panel; `POST /api/settings`
persists a patch to `<DATA_DIR>/settings.json`, applies it to the live `config`, and handles the one
change with a side effect — a new Stockfish path, which is validated then triggers an engine restart.
"""
from __future__ import annotations

import shutil

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server.core import engine
from server.core import settings as settings_mod
from server.web.local_client import is_local_client

router = APIRouter()

# Where Ollama serves by default; used when the Settings field is still blank so "Detect" works
# with one click on a stock install.
_OLLAMA_DEFAULT_URL = "http://localhost:11434"


class SettingsPatch(BaseModel):
    username: str | None = None
    chesscom_username: str | None = None
    chesscom_sync: bool | None = None
    chesscom_sync_max: str | None = None
    aliases: str | None = None
    lichess_token: str | None = None
    profile_recent: str | None = None
    profile_lifetime: str | None = None
    player_elo: str | None = None
    stockfish_path: str | None = None
    coach_ai_auto: bool | None = None
    coach_ai_persist: bool | None = None
    personalize_history: bool | None = None
    puzzle_animations: bool | None = None
    puzzle_auto_advance: bool | None = None
    puzzle_mistake_interleave: bool | None = None
    local_llm_base_url: str | None = None
    local_llm_model: str | None = None
    web_host: str | None = None


def _stockfish_ok(path: str) -> bool:
    return bool(shutil.which(config.clean_path(path)))


@router.get("/settings")
def get_settings(request: Request) -> dict:
    """Current effective settings + a couple of read-only status flags for the panel."""
    eff = settings_mod.effective()
    # With network access on, other devices can open Settings too; don't hand them the saved token.
    token_hidden = bool(eff["lichess_token"]) and not is_local_client(request)
    if token_hidden:
        eff["lichess_token"] = ""
    return {
        "settings": eff,
        "stockfish_ok": _stockfish_ok(eff["stockfish_path"]),
        "data_dir": config.DATA_DIR,
        "web_port": config.WEB_PORT,
        "lan_ip": config.lan_ip(),  # for the network-access hint ("on your phone, open http://...")
        "lichess_token_hidden": token_hidden,
    }


@router.get("/phone-access")
def get_phone_access() -> dict:
    """For the 📱 Phone popover: can a phone on this Wi-Fi open the board right now, and where."""
    return config.phone_access()


def _probe_models(resp: httpx.Response) -> list[str] | None:
    """Parse one model-listing response, or None when this isn't a usable model endpoint.

    Covers the two shapes a local model server uses: Ollama's native `{"models": [{"name": ...}]}`
    and the OpenAI-compatible `{"data": [{"id": ...}]}` (LM Studio, llama.cpp, LiteLLM). An empty
    Ollama list is a genuine answer ("running, nothing pulled yet"), so it's accepted; a
    `data`-shaped list that yields no usable names (a different API's listing) is a miss, so the
    next endpoint is still probed.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    models = body.get("models")
    if isinstance(models, list):
        return [m["name"] for m in models if isinstance(m, dict) and m.get("name")]
    data = body.get("data")
    if isinstance(data, list):
        names = [
            m.get("name") or m.get("id")
            for m in data
            if isinstance(m, dict) and (m.get("name") or m.get("id"))
        ]
        return names or None
    return None


@router.get("/ollama/models")
def ollama_models(url: str = "") -> dict:
    """List the models a local model server offers, so the Settings panel can offer a picker.

    Works with any local server the local-LLM field accepts, not just Ollama: it probes the
    Ollama-native `GET /api/tags` first, then the OpenAI-compatible `GET /models` (and `/v1/models`
    for a bare host). `url` is the optional base URL the user typed; blank falls back to the saved
    local-LLM URL, then Ollama's default port. Never raises — a server that's down or unknown just
    returns `{ok: false}` with a friendly hint.
    """
    base = (url or config.LOCAL_LLM_BASE_URL or _OLLAMA_DEFAULT_URL).strip().rstrip("/")
    # Bare host (Ollama's default) gets all three probes; a base that already ends in /v1
    # (LM Studio, LiteLLM) is probed at /models, which is its /v1/models.
    paths = ["/api/tags", "/models"] if base.endswith("/v1") else ["/api/tags", "/models", "/v1/models"]
    last_error = ""
    for path in paths:
        try:
            resp = httpx.get(f"{base}{path}", timeout=3.0)
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code < 200 or resp.status_code >= 300:
                continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        models = _probe_models(resp)
        if models is not None:
            return {"ok": True, "base_url": base, "models": models}
        last_error = "a page with no model list"
    return {
        "ok": False,
        "base_url": base,
        "models": [],
        "error": (
            f"No model server found at {base} (last probe: {last_error}). Is it running? "
            "(Ollama: `ollama serve`; LM Studio / LiteLLM: their web UI / proxy)"
        ),
    }


@router.post("/settings")
def post_settings(patch: SettingsPatch, request: Request) -> JSONResponse:
    """Persist + apply a settings patch. Returns the new effective settings (or a 400 on bad input)."""
    data = {k: v for k, v in patch.model_dump().items() if v is not None}

    # Another device never saw the real token (GET hides it), so its blank field must not wipe it.
    if data.get("lichess_token") == "" and not is_local_client(request):
        del data["lichess_token"]

    # A host the server can't bind would stop the app from starting next launch: reject it now.
    if "web_host" in data:
        host = config.normalize_web_host(data["web_host"])
        if host is None:
            return JSONResponse(
                {"error": f"Invalid network address '{data['web_host']}'. Use 127.0.0.1 or 0.0.0.0."},
                status_code=400,
            )
        data["web_host"] = host

    # A new Stockfish path is the only setting with a side effect: validate it, then restart the
    # engine pool so the next analysis uses it. An unusable path is rejected before anything changes.
    new_path = config.clean_path(data.get("stockfish_path"))
    restart_engine = bool(new_path) and (shutil.which(new_path) or new_path) != config.STOCKFISH_PATH
    if new_path and not _stockfish_ok(new_path):
        return JSONResponse(
            {"error": f"Stockfish not found or not executable at '{new_path}'."}, status_code=400
        )

    eff = settings_mod.update(data)
    if restart_engine:
        try:
            engine.restart()
        except Exception:  # pragma: no cover - defensive; next analysis would surface a real error
            pass
    return JSONResponse({"settings": eff, "stockfish_ok": _stockfish_ok(eff["stockfish_path"])})


@router.post("/fix-stockfish-arch")
def post_fix_stockfish_arch() -> JSONResponse:
    """Swap an Intel-under-Rosetta Stockfish for the native arm64 build (Apple Silicon only).

    Downloads the official arm64 static engine to the managed path (forcing the arch + a fresh
    download even if a wrong-arch binary is already on PATH), pins that path in Settings so it wins,
    and restarts the engine pool. No-op-with-error when there's nothing to fix.
    """
    import os
    import subprocess
    import sys

    report = config.stockfish_arch_report()
    if not report.get("can_fix"):
        return JSONResponse(
            {"ok": False, "error": "No architecture mismatch to fix on this machine."},
            status_code=409,
        )

    script = os.path.join(config.PROJECT_ROOT, "scripts", "download_stockfish.py")
    env = {
        **os.environ,
        "CHESS_FORCE_STOCKFISH_ARCH": "arm64",
        "CHESS_FORCE_STOCKFISH_DOWNLOAD": "1",
    }
    try:
        proc = subprocess.run(
            [sys.executable, script], capture_output=True, text=True, timeout=300, env=env
        )
    except Exception as exc:  # noqa: BLE001 - surface any launch/timeout failure to the banner
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    new_path = (proc.stdout or "").strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    if proc.returncode != 0 or not new_path or config.macho_arch(new_path) != "arm64":
        detail = (proc.stderr or "").strip() or "Could not download the arm64 Stockfish build."
        return JSONResponse({"ok": False, "error": detail}, status_code=500)

    # Pin the native engine so it wins over the Intel one on PATH, then restart the pool.
    eff = settings_mod.update({"stockfish_path": new_path})
    try:
        engine.restart()
    except Exception:  # pragma: no cover - defensive; next analysis would surface a real error
        pass
    return JSONResponse({"ok": True, "path": new_path, "settings": eff})

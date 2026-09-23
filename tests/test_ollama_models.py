"""Model detection (Settings -> Engine & AI -> Detect models): lists what a local model server
offers, for BOTH Ollama's native API and the OpenAI-compatible listing (LM Studio, llama.cpp,
LiteLLM). All network is mocked — nothing hits a real server."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from server import config
from server.web import app as app_module
from server.web import routes_settings

OLLAMA_TAGS = {"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3.1:8b"}]}
OPENAI_MODELS = {"data": [{"id": "Qwen/Qwen3.8-27B"}, {"id": "mistral-small"}]}


class _Resp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def client(monkeypatch):
    """Capture every outgoing GET and replay a per-URL canned response (or raise)."""
    calls: list[str] = []
    canned: dict[str, object] = {}  # url -> _Resp, or an Exception to raise

    def fake_get(url, timeout=None):
        calls.append(url)
        resp = canned.get(url, _Resp(404))
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(routes_settings.httpx, "get", fake_get)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "", raising=False)
    yield TestClient(app_module.create_app()), calls, canned


def _detect(c, url: str = ""):
    return c.get("/api/ollama/models", params={"url": url}).json()


# --- Ollama-native listing -----------------------------------------------------------------------


def test_detects_ollama_native_tags(client):
    c, calls, canned = client
    canned["http://127.0.0.1:11434/api/tags"] = _Resp(200, OLLAMA_TAGS)
    res = _detect(c, "http://127.0.0.1:11434")
    assert res["ok"] is True
    assert res["base_url"] == "http://127.0.0.1:11434"
    assert res["models"] == ["qwen2.5-coder:7b", "llama3.1:8b"]
    # Found it on the first probe — no further requests.
    assert calls == ["http://127.0.0.1:11434/api/tags"]


def test_ollama_running_with_no_models_is_a_genuine_empty(client):
    c, calls, canned = client
    canned["http://127.0.0.1:11434/api/tags"] = _Resp(200, {"models": []})
    res = _detect(c, "http://127.0.0.1:11434")
    assert res["ok"] is True
    assert res["models"] == []
    assert calls == ["http://127.0.0.1:11434/api/tags"]


# --- OpenAI-compatible listing (LM Studio / LiteLLM / llama.cpp) ---------------------------------


def test_detects_openai_models_via_v1_base(client):
    """The user's real setup: a LiteLLM proxy at http://127.0.0.1:8000/v1."""
    c, calls, canned = client
    canned["http://127.0.0.1:8000/v1/api/tags"] = _Resp(404)
    canned["http://127.0.0.1:8000/v1/models"] = _Resp(200, OPENAI_MODELS)
    res = _detect(c, "http://127.0.0.1:8000/v1")
    assert res["ok"] is True
    assert res["base_url"] == "http://127.0.0.1:8000/v1"
    assert res["models"] == ["Qwen/Qwen3.8-27B", "mistral-small"]
    # A /v1 base is probed at /models (== its /v1/models); it must NOT re-probe /v1/models.
    assert calls == ["http://127.0.0.1:8000/v1/api/tags", "http://127.0.0.1:8000/v1/models"]


def test_bare_host_falls_back_to_v1_models(client):
    c, calls, canned = client
    canned["http://127.0.0.1:8000/api/tags"] = _Resp(404)
    canned["http://127.0.0.1:8000/models"] = _Resp(404)
    canned["http://127.0.0.1:8000/v1/models"] = _Resp(200, OPENAI_MODELS)
    res = _detect(c, "http://127.0.0.1:8000")
    assert res["ok"] is True
    assert res["models"] == ["Qwen/Qwen3.8-27B", "mistral-small"]
    assert calls == [
        "http://127.0.0.1:8000/api/tags",
        "http://127.0.0.1:8000/models",
        "http://127.0.0.1:8000/v1/models",
    ]


def test_data_listing_with_no_names_keeps_probing(client):
    """A 2xx page that's neither Ollama nor OpenAI-shaped must not stop the search."""
    c, calls, canned = client
    canned["http://127.0.0.1:1234/api/tags"] = _Resp(200, {"hello": "world"})
    canned["http://127.0.0.1:1234/models"] = _Resp(404)
    canned["http://127.0.0.1:1234/v1/models"] = _Resp(200, OPENAI_MODELS)
    res = _detect(c, "http://127.0.0.1:1234")
    assert res["ok"] is True
    assert res["models"] == ["Qwen/Qwen3.8-27B", "mistral-small"]
    assert len(calls) == 3


def test_data_listing_uses_name_when_present(client):
    c, _, canned = client
    canned["http://127.0.0.1:1234/api/tags"] = _Resp(404)
    canned["http://127.0.0.1:1234/models"] = _Resp(
        200, {"data": [{"name": "by-name"}, {"id": "by-id"}]}
    )
    res = _detect(c, "http://127.0.0.1:1234")
    assert res["models"] == ["by-name", "by-id"]


# --- URL handling --------------------------------------------------------------------------------


def test_blank_url_falls_back_to_saved_local_llm_url(client, monkeypatch):
    c, calls, canned = client
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:8000/v1", raising=False)
    canned["http://127.0.0.1:8000/v1/api/tags"] = _Resp(404)
    canned["http://127.0.0.1:8000/v1/models"] = _Resp(200, OPENAI_MODELS)
    res = _detect(c, "")
    assert res["ok"] is True
    assert res["base_url"] == "http://127.0.0.1:8000/v1"
    assert calls[0] == "http://127.0.0.1:8000/v1/api/tags"


def test_blank_url_defaults_to_ollama_port_when_nothing_saved(client):
    c, calls, canned = client
    canned["http://localhost:11434/api/tags"] = _Resp(200, OLLAMA_TAGS)
    res = _detect(c, "")
    assert res["ok"] is True
    assert res["base_url"] == "http://localhost:11434"
    assert calls == ["http://localhost:11434/api/tags"]


def test_trailing_slash_is_stripped(client):
    c, calls, canned = client
    canned["http://127.0.0.1:11434/api/tags"] = _Resp(200, OLLAMA_TAGS)
    res = _detect(c, "http://127.0.0.1:11434/")
    assert res["base_url"] == "http://127.0.0.1:11434"
    assert calls == ["http://127.0.0.1:11434/api/tags"]


# --- failures ------------------------------------------------------------------------------------


def test_all_probes_404_reports_not_found_with_last_error(client):
    c, calls, canned = client
    canned["http://127.0.0.1:8000/v1/api/tags"] = _Resp(404, text="<html>nope</html>")
    canned["http://127.0.0.1:8000/v1/models"] = _Resp(404, text="<html>nope</html>")
    res = _detect(c, "http://127.0.0.1:8000/v1")
    assert res["ok"] is False
    assert res["models"] == []
    assert "No model server found at http://127.0.0.1:8000/v1" in res["error"]
    assert "HTTP 404" in res["error"]
    assert len(calls) == 2


def test_connection_refused_is_reported_not_raised(client):
    """Nothing listening: report it after ONE probe, since the rest share that host:port."""
    c, calls, canned = client
    canned["http://127.0.0.1:9999/v1/api/tags"] = httpx.ConnectError("connection refused")
    canned["http://127.0.0.1:9999/v1/models"] = httpx.ConnectError("connection refused")
    res = _detect(c, "http://127.0.0.1:9999/v1")
    assert res["ok"] is False
    assert res["models"] == []
    assert "connection refused" in res["error"]
    # A dead port costs one timeout, not one per path.
    assert calls == ["http://127.0.0.1:9999/v1/api/tags"]


def test_connect_timeout_also_stops_the_search(client):
    c, calls, canned = client
    canned["http://10.0.0.9:8000/api/tags"] = httpx.ConnectTimeout("timed out")
    res = _detect(c, "http://10.0.0.9:8000")
    assert res["ok"] is False
    assert "ConnectTimeout" in res["error"]
    assert calls == ["http://10.0.0.9:8000/api/tags"]


def test_read_timeout_keeps_probing_the_other_paths(client):
    """A live host that stalls on ONE path is not a dead host, so the search continues."""
    c, calls, canned = client
    canned["http://127.0.0.1:8000/api/tags"] = httpx.ReadTimeout("slow")
    canned["http://127.0.0.1:8000/models"] = _Resp(200, OPENAI_MODELS)
    res = _detect(c, "http://127.0.0.1:8000")
    assert res["ok"] is True
    assert res["models"] == ["Qwen/Qwen3.8-27B", "mistral-small"]
    assert calls == ["http://127.0.0.1:8000/api/tags", "http://127.0.0.1:8000/models"]


def test_non_json_success_page_is_skipped(client):
    c, calls, canned = client
    canned["http://127.0.0.1:8000/api/tags"] = _Resp(200, text="Ollama v0.x is up!")
    canned["http://127.0.0.1:8000/models"] = _Resp(404)
    canned["http://127.0.0.1:8000/v1/models"] = _Resp(200, OPENAI_MODELS)
    res = _detect(c, "http://127.0.0.1:8000")
    assert res["ok"] is True
    assert res["models"] == ["Qwen/Qwen3.8-27B", "mistral-small"]
    assert len(calls) == 3

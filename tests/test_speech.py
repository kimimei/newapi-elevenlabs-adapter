# -*- coding: utf-8 -*-
"""MVP 测试：链路（Mock 模式）+ 转换逻辑（纯函数）。

跑法：cd elevenlabs-adapter && python -m pytest tests -v
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as m  # noqa: E402
from app.elevenlabs_client import resolve_voice_and_model  # noqa: E402
from app.voices import Voices  # noqa: E402

client = TestClient(m.app)

TEST_VOICES = Voices({
    "aliases": {"eleven-rachel": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model": "eleven_multilingual_v2"}},
    "voices": {"rachel": "21m00Tcm4TlvDq8ikWAM"},
    "model_alias": {"tts-1": "eleven_multilingual_v2"},
})


@pytest.fixture(autouse=True)
def _mock_mode():
    """默认 Mock 模式，避免测试发真实请求；单测自行关掉验证 401。"""
    m.MOCK = True
    yield
    m.MOCK = True


# ---------- 纯函数：音色/模型解析 ----------

def test_resolve_alias_model_implies_voice():
    voice_id, model_id, _ = resolve_voice_and_model("eleven-rachel", None, TEST_VOICES)
    assert voice_id == "21m00Tcm4TlvDq8ikWAM"
    assert model_id == "eleven_multilingual_v2"


def test_resolve_body_voice_overrides_alias():
    voice_id, _, _ = resolve_voice_and_model("eleven-rachel", "rachel", TEST_VOICES)
    assert voice_id == "21m00Tcm4TlvDq8ikWAM"


def test_resolve_native_elevenlabs_model_keeps_model():
    _, model_id, _ = resolve_voice_and_model("eleven_v3", "rachel", TEST_VOICES)
    assert model_id == "eleven_v3"


def test_resolve_openai_alias_model():
    _, model_id, _ = resolve_voice_and_model("tts-1", "rachel", TEST_VOICES)
    assert model_id == "eleven_multilingual_v2"


def test_resolve_no_voice_returns_none():
    voice_id, _, _ = resolve_voice_and_model("tts-1", None, TEST_VOICES)
    assert voice_id is None


def test_voice_id_passthrough():
    voice_id = TEST_VOICES.voice_to_id("21m00Tcm4TlvDq8ikWAM")
    assert voice_id == "21m00Tcm4TlvDq8ikWAM"


# ---------- 链路（Mock 模式） ----------

def test_speech_mock_ok():
    r = client.post("/v1/audio/speech", json={"model": "eleven-rachel", "input": "你好，世界"})
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"  # WAV 头
    assert r.headers["X-Mock"] == "true"
    assert r.headers["X-Usage-Input-Characters"] == "5"
    assert r.headers["X-ElevenLabs-Voice"] == "21m00Tcm4TlvDq8ikWAM"
    assert r.headers["X-ElevenLabs-Model"] == "eleven_multilingual_v2"


def test_speech_voice_by_name():
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "rachel"})
    assert r.status_code == 200
    assert r.headers["X-ElevenLabs-Voice"] == "21m00Tcm4TlvDq8ikWAM"


def test_speech_missing_input():
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": ""})
    assert r.status_code == 400
    assert "input" in r.json()["error"]["message"]


def test_speech_no_voice_and_no_alias():
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi"})
    assert r.status_code == 400
    assert "voice_id" in r.json()["error"]["message"]


def test_speech_unknown_response_format():
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "rachel", "response_format": "aac"})
    assert r.status_code == 400
    assert "aac" in r.json()["error"]["message"]


def test_speech_stream_not_implemented():
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "rachel", "stream": True})
    assert r.status_code == 501


def test_speech_auth_required_when_not_mock(monkeypatch):
    """无 Key 且非 Mock → 401（隔离本地 config.json，避免真发请求）。"""
    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: None)
    m.MOCK = False
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "rachel"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "authentication_error"


def test_speech_upstream_error_passthrough(monkeypatch):
    """上游 401 原样透传（打桩 ElevenLabs 调用，不发真实请求）。"""
    import httpx as _httpx

    async def fake_call(*a, **k):
        return _httpx.Response(
            401, json={"detail": {"message": "Invalid API key", "status": "invalid_api_key"}}
        )

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy_for_test")
    monkeypatch.setattr(m, "call_elevenlabs_tts", fake_call)
    m.MOCK = False
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "rachel"})
    assert r.status_code == 401
    assert "Invalid API key" in r.json()["error"]["message"]


def test_speech_with_bearer_but_mock_ok():
    r = client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "hi", "voice": "rachel"},
        headers={"Authorization": "Bearer sk_test_dummy_0000000000000000"},
    )
    assert r.status_code == 200


# ---------- 元信息端点 ----------

def test_models_default_is_engines_only():
    """默认接入项只有标准引擎；音色封装别名属于补充包，不在默认列表里。"""
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()["data"]}
    assert "eleven_multilingual_v2" in ids
    assert "eleven-rachel" not in ids
    assert "tts-1" not in ids


def test_models_with_presets_all():
    r = client.get("/v1/models", params={"presets": "all"})
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()["data"]}
    assert "eleven-rachel" in ids
    assert "tts-1" in ids
    assert "eleven_multilingual_v2" in ids


def test_voices():
    r = client.get("/v1/voices")
    assert r.status_code == 200
    names = {v["name"] for v in r.json()["voices"]}
    assert "rachel" in names


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

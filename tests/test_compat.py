# -*- coding: utf-8 -*-
"""兼容性回归：WAV 容器、speed 钳制、错误结构、密钥归一化、状态码映射。

这些点都是「接入中转站时才会暴露」的问题，普通链路测试覆盖不到。
"""
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as m  # noqa: E402
from app.elevenlabs_client import clamp_speed, resolve_api_key  # noqa: E402

client = TestClient(m.app)

PCM = b"\x01\x00\x02\x00" * 200  # 裸 PCM，非 WAV 容器


@pytest.fixture(autouse=True)
def _reset():
    m.MOCK = True
    yield
    m.MOCK = True


def _fake_upstream(content=PCM, status=200, json_body=None):
    async def _call(*a, **k):
        if json_body is not None:
            return httpx.Response(status, json=json_body)
        return httpx.Response(status, content=content)
    return _call


def _post(monkeypatch, **body):
    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _fake_upstream(**body.pop("_upstream", {})))
    m.MOCK = False
    payload = {"model": "tts-1", "input": "你好", "voice": "rachel"}
    payload.update(body)
    return client.post("/v1/audio/speech", json=payload)


# ---------- WAV 容器 ----------

def test_wav_wraps_raw_pcm_into_riff(monkeypatch):
    """OpenAI 的 wav 语义是带容器的 WAV，上游给的是裸 PCM。"""
    r = _post(monkeypatch, response_format="wav")
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"
    assert r.content[8:12] == b"WAVE"
    assert r.headers["content-type"].startswith("audio/wav")
    # RIFF 头 44 字节 + 原始 PCM
    assert len(r.content) == 44 + len(PCM)


def test_pcm_stays_raw(monkeypatch):
    """pcm 语义是裸流，不能套容器。"""
    r = _post(monkeypatch, response_format="pcm")
    assert r.status_code == 200
    assert r.content == PCM
    assert "L16" in r.headers["content-type"]


# ---------- speed 钳制 ----------

def test_speed_clamped_high(monkeypatch):
    r = _post(monkeypatch, speed=3.0)
    assert r.status_code == 200
    assert r.headers["X-Speed-Clamped"] == "1.2"


def test_speed_clamped_low(monkeypatch):
    r = _post(monkeypatch, speed=0.3)
    assert r.status_code == 200
    assert r.headers["X-Speed-Clamped"] == "0.7"


def test_speed_in_range_not_clamped(monkeypatch):
    r = _post(monkeypatch, speed=1.0)
    assert r.status_code == 200
    assert "X-Speed-Clamped" not in r.headers


def test_clamp_speed_pure():
    assert clamp_speed(None) == (None, False)
    assert clamp_speed(1.0) == (1.0, False)
    assert clamp_speed(0.1) == (0.7, True)
    assert clamp_speed(9.9) == (1.2, True)


# ---------- 错误结构 ----------

def test_validation_error_uses_openai_shape():
    """FastAPI 默认返回 {"detail": [...]}，中转站解析不了。"""
    r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "rachel", "speed": 99})
    assert r.status_code == 400
    body = r.json()
    assert "error" in body
    assert isinstance(body["error"]["message"], str)
    assert "detail" not in body


def test_upstream_422_mapped_to_400(monkeypatch):
    r = _post(monkeypatch, _upstream={"status": 422, "json_body": {"detail": "bad voice"}})
    assert r.status_code == 400


def test_upstream_500_mapped_to_502(monkeypatch):
    r = _post(monkeypatch, _upstream={"status": 500, "json_body": {"detail": "boom"}})
    assert r.status_code == 502


def test_upstream_401_kept_as_401(monkeypatch):
    """401 必须保留：中转站据此判定渠道密钥失效并自动禁用渠道。"""
    r = _post(monkeypatch, _upstream={"status": 401, "json_body": {"detail": {"message": "Invalid API key"}}})
    assert r.status_code == 401


# ---------- 密钥归一化 ----------

def test_key_plain_bearer():
    assert resolve_api_key("Bearer sk_live_abc") == "sk_live_abc"


def test_key_strips_openai_style_prefix():
    """部分中转站会给渠道密钥套一层 sk- 短横线外壳。"""
    assert resolve_api_key("Bearer sk-abc123") == "abc123"


def test_key_keeps_elevenlabs_underscore_prefix():
    """ElevenLabs 原生 key 是 sk_ 下划线，必须保留。"""
    assert resolve_api_key("Bearer sk_abc123") == "sk_abc123"


def test_key_multi_takes_first():
    assert resolve_api_key("Bearer key1,key2") == "key1"


def test_key_from_x_api_key_header():
    assert resolve_api_key(None, "Bearer xkey") == "xkey"
    assert resolve_api_key(None, None, "ak") == "ak"


def test_key_priority_authorization_first():
    assert resolve_api_key("Bearer primary", "secondary") == "primary"

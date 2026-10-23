# -*- coding: utf-8 -*-
"""安全回归：路径穿越、请求体超限、IP 白名单、本地 Key 回落。

这几条对应 SECURITY.md 里「已修」的四项，任何一项被改回去都必须让测试变红。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import elevenlabs_client as ec  # noqa: E402
from app import main as m  # noqa: E402
from app.elevenlabs_client import resolve_api_key, resolve_voice_and_model  # noqa: E402
from app.voices import Voices  # noqa: E402

client = TestClient(m.app)

VOICES = Voices({
    "aliases": {"eleven-rachel": {"voice_id": "21m00Tcm4TlvDq8ikWAM",
                                  "model": "eleven_multilingual_v2"}},
    "voices": {"rachel": "21m00Tcm4TlvDq8ikWAM"},
    "model_alias": {"tts-1": "eleven_multilingual_v2"},
})


@pytest.fixture(autouse=True)
def _mock_mode():
    m.MOCK = True
    yield
    m.MOCK = True


# ---------- 1. voice_id 路径穿越 ----------

def test_voice_id_with_slash_rejected():
    """voice_id 会拼进上游 URL 路径，带 "/" 就能改写路径。"""
    with pytest.raises(ValueError):
        resolve_voice_and_model("eleven_v3", "aaaaaaaaaaaaaa/../x", VOICES)


def test_voice_id_with_dotdot_rejected():
    with pytest.raises(ValueError):
        resolve_voice_and_model("eleven_v3", "..%2f..%2fetc%2fpasswd", VOICES)


def test_api_rejects_bad_voice_id_with_400():
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "你好", "voice": "aaaaaaaaaaaaaa/../x"})
    assert r.status_code == 400
    assert "voice_id" in r.text


def test_legit_voice_id_still_works():
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "你好", "voice": "21m00Tcm4TlvDq8ikWAM"})
    assert r.status_code == 200
    assert r.headers.get("X-Usage-Input-Characters") == "2"


def test_alias_voice_id_also_validated():
    """别名分支提前 return，voice_id 必须在里面再校验一次。

    voices.json 是仓内可编辑文件：这里放一个带斜杠的 voice_id，
    若不校验，body.voice 的那道正则就被绕过了。
    """
    from app.voices import Voices as V
    bad = V({"aliases": {"probe-bad": {"voice_id": "../../evil", "model": "eleven_v3"}},
             "voices": {}, "model_alias": {}})
    with pytest.raises(ValueError) as ei:
        resolve_voice_and_model("probe-bad", None, bad)
    assert "aliases[probe-bad]" in str(ei.value)


def test_shipped_voices_json_all_pass_validation():
    """仓内 voices.json 里每一条 voice_id 都必须自带合规，不能依赖运行时才发现。"""
    from app.voices import load_voices
    v = load_voices()
    bad = [vid for vid in list(v.voices.values())
           + [a.get("voice_id") for a in v.aliases.values()] if vid]
    offenders = [vid for vid in bad if not ec._VOICE_ID_RE.match(vid)]
    assert offenders == [], "不合规 voice_id: %r" % (offenders[:5],)


def test_voice_name_still_resolves():
    vid, _, _ = resolve_voice_and_model("eleven_v3", "rachel", VOICES)
    assert vid == "21m00Tcm4TlvDq8ikWAM"


# ---------- 2. 请求体上限 ----------

def test_oversize_body_returns_413(monkeypatch):
    monkeypatch.setattr(m, "MAX_BODY_BYTES", 1024)
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "字" * 2000})
    assert r.status_code == 413
    assert "上限" in r.text


def test_small_body_not_blocked(monkeypatch):
    monkeypatch.setattr(m, "MAX_BODY_BYTES", 4096)
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "你好", "voice": "rachel"})
    assert r.status_code == 200


# ---------- 3. IP 白名单 ----------

def test_ip_allowlist_blocks_unknown(monkeypatch):
    monkeypatch.setattr(m, "ALLOW_IPS", ["10.9.9.9"])
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "你好", "voice": "rachel"})
    assert r.status_code == 403
    assert "白名单" in r.text


def test_ip_allowlist_empty_means_open(monkeypatch):
    monkeypatch.setattr(m, "ALLOW_IPS", [])
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "你好", "voice": "rachel"})
    assert r.status_code == 200


# ---------- 4. 本地明文 Key 回落默认关闭 ----------

def test_local_key_fallback_disabled_by_default(monkeypatch):
    monkeypatch.setattr(ec, "ALLOW_LOCAL_KEY", False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert resolve_api_key(None, None, None) is None


def test_header_key_still_preferred(monkeypatch):
    monkeypatch.setattr(ec, "ALLOW_LOCAL_KEY", False)
    assert resolve_api_key("Bearer sk_abc") == "sk_abc"

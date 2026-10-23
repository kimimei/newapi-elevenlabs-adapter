# -*- coding: utf-8 -*-
"""模型目录：标准引擎 vs 补充包。

核心约定（改坏会直接影响接入面）：
  1. 默认 /v1/models 只有标准引擎，不含任何音色/场景封装别名；
  2. 补充包未启用时「不列出」，但**仍可调用**（软门禁），避免打断已注册渠道；
  3. 旧格式 voices 文件整体折叠成默认启用的 legacy 包，行为不倒退。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as m  # noqa: E402
from app.voices import DEFAULT_ENGINES, Voices  # noqa: E402

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _mock_and_reset_presets(monkeypatch):
    m.MOCK = True
    monkeypatch.setattr(m, "PRESETS", "none")
    yield
    m.MOCK = True


def _ids(**kw):
    return [d["id"] for d in client.get("/v1/models", **kw).json()["data"]]


# ---------- 默认接入面 ----------

def test_default_models_are_engines_only():
    assert _ids() == DEFAULT_ENGINES


def test_default_contains_no_alias():
    ids = set(_ids())
    for name in ("zh-tw-ladaisy", "zh-yingshi", "zh-boyin", "eleven-rachel", "tts-1"):
        assert name not in ids


# ---------- 包选择 ----------

def test_pack_cn_only():
    ids = set(_ids(params={"presets": "cn"}))
    assert "zh-yingshi" in ids
    assert "zh-影视" in ids
    assert "zh-tw-ladaisy" not in ids
    assert all(e in ids for e in DEFAULT_ENGINES)


def test_pack_tw_only():
    ids = set(_ids(params={"presets": "tw"}))
    assert {"zh-tw-ladaisy", "zh-tw-ziyue", "zh-台湾甜嗲", "zh-台湾自然"} <= ids
    assert "zh-yingshi" not in ids


def test_pack_multi_select():
    ids = set(_ids(params={"presets": "cn,tw"}))
    assert "zh-yingshi" in ids and "zh-tw-ladaisy" in ids


def test_pack_all_covers_every_pack_model():
    ids = set(_ids(params={"presets": "all"}))
    for pack in m.VOICES.packs.values():
        assert set(pack.models) <= ids


def test_presets_env_applies():
    m.PRESETS = "cn"
    assert "zh-yingshi" in set(_ids())


def test_unknown_pack_returns_400():
    r = client.get("/v1/models", params={"presets": "jp"})
    assert r.status_code == 400
    assert "jp" in r.json()["error"]["message"]


def test_none_is_allowed():
    assert _ids(params={"presets": "none"}) == DEFAULT_ENGINES


# ---------- 软门禁：不列出 ≠ 不可用 ----------

def test_alias_still_callable_when_pack_disabled():
    """线上渠道已注册的别名不能因为默认收窄而 500/400。"""
    r = client.post("/v1/audio/speech", json={"model": "zh-tw-ladaisy", "input": "你好"})
    assert r.status_code == 200
    assert r.headers["X-ElevenLabs-Voice"] == "8RCmdqih4CKjJ7Bd3ZxT"
    assert r.headers["X-ElevenLabs-Model"] == "eleven_v3"


# ---------- 包清单端点 ----------

def test_presets_endpoint_lists_packs_disabled_by_default():
    body = client.get("/v1/presets").json()
    assert body["engines"] == DEFAULT_ENGINES
    packs = {p["id"]: p for p in body["data"]}
    assert set(packs) == {"compat", "en", "cn", "tw"}
    assert all(p["enabled"] is False for p in packs.values())
    assert "zh-tw-ladaisy" in packs["tw"]["models"]


def test_presets_endpoint_reflects_enabled():
    body = client.get("/v1/presets", params={"presets": "cn"}).json()
    packs = {p["id"]: p for p in body["data"]}
    assert packs["cn"]["enabled"] is True
    assert packs["tw"]["enabled"] is False


# ---------- 音色列表跟随包 ----------

def test_voices_hides_alias_entries_of_disabled_packs():
    names = {v["name"] for v in client.get("/v1/voices").json()["voices"]}
    assert "rachel" in names          # voices 字典（音色名）常驻
    assert "zh-boyin" not in names    # 别名条目跟随包


def test_voices_shows_alias_entries_when_enabled():
    names = {v["name"] for v in client.get("/v1/voices", params={"presets": "cn"}).json()["voices"]}
    assert "zh-boyin" in names


# ---------- 旧格式兼容 ----------

def test_legacy_format_folds_into_enabled_pack():
    v = Voices({
        "aliases": {"eleven-rachel": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model": "eleven_multilingual_v2"}},
        "voices": {"rachel": "21m00Tcm4TlvDq8ikWAM"},
        "model_alias": {"tts-1": "eleven_multilingual_v2"},
    })
    ids = set(v.models_for(None))
    assert "eleven-rachel" in ids
    assert "tts-1" in ids
    assert "eleven_multilingual_v2" in ids
    assert set(DEFAULT_ENGINES) <= ids
    # 解析视图不受影响
    assert v.aliases["eleven-rachel"]["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
    assert v.model_alias["tts-1"] == "eleven_multilingual_v2"

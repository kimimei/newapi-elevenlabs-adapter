# -*- coding: utf-8 -*-
"""错误翻译 / 音色目录 / 在线试听 的回归。

对应 T4（错误可执行化）、T6（音色目录 + preview）、T1（试听护栏）。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import demo as demo_mod  # noqa: E402
from app import main as m  # noqa: E402
from app import voice_catalog  # noqa: E402
from app.errors import explain  # noqa: E402

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _mock():
    m.MOCK = True
    yield
    m.MOCK = True


# ---------- T4 错误可执行化 ----------

def test_every_error_has_fix():
    r = client.post("/v1/audio/speech", json={"model": "eleven_v3", "input": ""})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body.get("fix"), "错误体必须带 fix"
    assert body["fix"].startswith(("把", "检查", "在", "补", "去掉", "按"))


def test_stream_is_ignored_with_header():
    """stream=true 不再 501，而是忽略参数返回音频 + X-Stream-Ignored 告知。

    反证：把 main.py 里的 501 分支加回去，此测试立即变红。
    """
    r = client.post("/v1/audio/speech",
                    json={"model": "eleven_v3", "input": "你好", "voice": "rachel",
                          "stream": True})
    assert r.status_code == 200
    assert r.headers.get("X-Stream-Ignored") == "true"


def test_bad_voice_id_fix_mentions_voice_field():
    r = client.post("/v1/audio/speech",
                    json={"model": "eleven_v3", "input": "你好", "voice": "aaaaaaaaaaaaaa/../x"})
    assert r.status_code == 400
    assert "voice_id" in r.json()["error"]["message"]


@pytest.mark.parametrize("status,keyword", [
    (401, "sk_"), (404, "voice_id"), (429, "并发"), (422, "language_code"), (501, "stream"),
])
def test_explain_maps_status_to_fix(status, keyword):
    _, fix = explain(status, "")
    assert keyword in fix or keyword in explain(status, "")[0]


def test_explain_prefers_upstream_text():
    msg, fix = explain(422, '{"detail":"Model \'eleven_v4\' does not support language_code \'zh-TW\'"}')
    assert "zh-TW" in fix


# ---------- T6 音色目录 ----------

def test_voices_include_alias_metadata():
    r = client.get("/v1/voices?presets=all")
    assert r.status_code == 200
    data = r.json()
    zh = [v for v in data["voices"] if v.get("pack") == "cn"]
    assert zh and zh[0]["lang"] == "zh"
    assert data["preview"] is False
    assert "label" in zh[0]


def test_voices_lang_filter():
    r = client.get("/v1/voices?presets=all&lang=zh")
    assert all(v.get("lang", "").startswith("zh") for v in r.json()["voices"])
    assert r.json()["count"] > 0


def test_voices_preview_cached(monkeypatch):
    voice_catalog.clear_cache()
    calls = {"n": 0}

    async def fake(api_key, ttl=3600):
        calls["n"] += 1
        return {"4AfodMgwXps9oZFhHzoj": "https://cdn.example/p.mp3"}

    monkeypatch.setattr(m, "fetch_previews", fake)
    r1 = client.get("/v1/voices?presets=all&with_preview=1")
    r2 = client.get("/v1/voices?presets=all&with_preview=1")
    assert r1.json()["preview"] is True
    hit = [v for v in r1.json()["voices"] if v.get("preview_url")]
    assert hit, "应带上 preview_url"
    assert calls["n"] >= 1


# ---------- T1 试听护栏 ----------

def test_demo_disabled_by_default():
    assert demo_mod.DEMO_ENABLED is False
    r = client.post("/v1/demo/speech", json={"text": "你好"})
    assert r.status_code == 404


def test_demo_rate_limited(monkeypatch):
    monkeypatch.setattr(demo_mod, "DEMO_ENABLED", True)
    monkeypatch.setattr(demo_mod, "DEMO_PER_HOUR", 2)
    monkeypatch.setattr(demo_mod, "_hits", {})
    monkeypatch.setattr(demo_mod, "resolve_api_key", lambda *a: "sk_fake")
    monkeypatch.setattr(demo_mod, "DEMO_MAX_CHARS", 20)

    async def fake_tts(*a, **kw):
        class R:
            status_code = 200
            content = b"ID3fakeaudio"
        return R()

    monkeypatch.setattr(demo_mod, "call_elevenlabs_tts", fake_tts)
    codes = [client.post("/v1/demo/speech", json={"text": "你好"}).status_code
             for _ in range(3)]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429


def test_demo_truncates_text(monkeypatch):
    monkeypatch.setattr(demo_mod, "DEMO_ENABLED", True)
    monkeypatch.setattr(demo_mod, "DEMO_MAX_CHARS", 5)
    monkeypatch.setattr(demo_mod, "_hits", {})
    monkeypatch.setattr(demo_mod, "resolve_api_key", lambda *a: "sk_fake")
    seen = {}

    async def fake_tts(text, *a, **kw):
        seen["text"] = text

        class R:
            status_code = 200
            content = b"ID3"
        return R()

    monkeypatch.setattr(demo_mod, "call_elevenlabs_tts", fake_tts)
    r = client.post("/v1/demo/speech", json={"text": "一二三四五六七八九十"})
    assert r.status_code == 200
    assert seen["text"] == "一二三四五"


def test_demo_restricts_voice(monkeypatch):
    monkeypatch.setattr(demo_mod, "DEMO_ENABLED", True)
    monkeypatch.setattr(demo_mod, "_hits", {})
    monkeypatch.setattr(demo_mod, "resolve_api_key", lambda *a: "sk_fake")
    monkeypatch.setattr(demo_mod, "DEMO_VOICES", ["zh-yingshi"])
    used = {}

    async def fake_tts(text, model, voice_id, *a, **kw):
        used["vid"] = voice_id

        class R:
            status_code = 200
            content = b"ID3"
        return R()

    monkeypatch.setattr(demo_mod, "call_elevenlabs_tts", fake_tts)
    client.post("/v1/demo/speech", json={"text": "你好", "voice": "ANYTHING"})
    assert used["vid"] == "PSvrh6w41Qm19oaaHLca", "非白名单音色应回落到第一个"


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "试听" in r.text


# ---------- 本轮代码复查新增 ----------

def test_demo_empty_voices_falls_back(monkeypatch):
    """ADAPTER_DEMO_VOICES 写成"," 时不能 IndexError->500。

    取 DEMO_VOICES[0] 是硬编码的下标访问，配置项一空就炸。
    兜底逻辑在import 期执行，所以必须重新 load 模块才能验证 ——
    只 monkeypatch 属性再断言常量非空，是测不出回退的。
    """
    import importlib
    import app.demo as d
    monkeypatch.setenv("ADAPTER_DEMO_VOICES", ",")
    monkeypatch.setenv("ADAPTER_DEMO_ENABLED", "1")
    reloaded = importlib.reload(d)
    try:
        assert reloaded.DEMO_VOICES, "空配置未回落到默认音色"
        # 真正走一遍取首音色的路径，确保不抛
        vid, _, _ = reloaded.resolve_voice_and_model(
            reloaded.DEMO_VOICES[0], None, reloaded.VOICES)
        assert vid
    finally:
        monkeypatch.undo()
        importlib.reload(d)


def test_demo_hits_table_gets_pruned():
    """限流表只增不减的话，扫一遍来源 IP 就能把内存撑起来。"""
    import app.demo as d
    d._hits.clear()
    now = __import__("time").time()
    # 造 2000 个互不相同的过期 IP（阈值是1024，key 必须真的超过它）
    for i in range(2000):
        d._hits["172.%d.%d.%d" % (i // 65536 % 256, i // 256 % 256, i % 256)] = [now - 7200]
    d._hits["fresh"] = [now]
    ok, _ = d._allow("10.0.0.0")
    assert ok
    assert len(d._hits) < 1000, "过期 IP 未被清理：%d" % len(d._hits)
    assert "fresh" in d._hits, "活跃 IP 被误删"
    d._hits.clear()


def test_demo_error_body_is_valid_json():
    """带引号/反斜杠的文案不能把 JSON 拼坏。"""
    import json
    import app.demo as d
    body = d._err('bad "voice" \\ path')
    json.loads(body)   # 解析不了就会抛


def test_demo_voice_with_quote_yields_parseable_json(monkeypatch):
    """端到端：voice 值里塞引号/反斜杠，响应体仍必须能被 json.loads 解析。

    只测 _err() 等于测「函数存在」；手拼字面量的写法照样能过。
    必须真的打一个请求，让 voice 原值流进报错文案。
    """
    import json
    import app.demo as d
    monkeypatch.setattr(d, "DEMO_ENABLED", True)
    monkeypatch.setattr(d, "DEMO_VOICES", ['bad"voice\\x'])
    monkeypatch.setattr(d, "VOICES", type("V", (), {
        "aliases": {'bad"voice\\x': {"voice_id": "../../evil", "model": "eleven_v3"}},
        "voice_to_id": staticmethod(lambda x: None),
    })())
    r = client.post("/v1/demo/speech", json={"text": "试听"})
    assert r.status_code in (400, 200)
    json.loads(r.text)      # 旧的手拼写法在这里会抛JSONDecodeError


def test_mock_declares_real_format(monkeypatch):
    """MOCK 模式固定回 WAV：请求 mp3 时必须明写，别让下游误判成格式转换 bug。"""
    m.MOCK = True
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "测试", "voice": "rachel",
        "response_format": "mp3"})
    assert r.status_code == 200
    assert r.headers.get("X-Mock") == "true"
    assert r.headers.get("X-Mock-Format") == "wav"
    assert r.headers.get("X-Mock-Format-Mismatch") == "mp3"


def test_mock_wav_request_has_no_mismatch_header():
    m.MOCK = True
    r = client.post("/v1/audio/speech", json={
        "model": "eleven_v3", "input": "测试", "voice": "rachel",
        "response_format": "wav"})
    assert r.status_code == 200
    assert "X-Mock-Format-Mismatch" not in r.headers


def test_version_matches_changelog():
    """main.py 的 version 必须在 CHANGELOG.md 中有对应条目，避免版本号漂移。"""
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parent.parent
    ver = re.search(r'version="([^"]+)"', (root / "app" / "main.py").read_text("utf-8")).group(1)
    txt = (root / "CHANGELOG.md").read_text("utf-8")
    assert "## [%s]" % ver in txt, "app/main.py version=%s，但 CHANGELOG 没有 ## [%s] 条目" % (ver, ver)


def test_openai_voice_names_all_resolve():
    """OpenAI 官方 6 个音色必须全部可解析。

    为什么要有这条：几乎所有 work 工具（Dify / n8n / Coze / LangChain）接入
    OpenAI 兼容 TTS 时都默认传 voice="alloy"（OpenAI 文档示例值）。
    这 6 个名字若解析失败，接入方第一次调用就 400。
    反证：从 voices.json 删掉任一 OpenAI 音色映射，此测试立即变红。
    """
    from app.voices import load_voices

    v = load_voices()
    for name in ("alloy", "echo", "fable", "onyx", "nova", "shimmer"):
        vid = v.voice_to_id(name)
        assert vid, f"OpenAI 音色 {name} 应解析出 voice_id，实际为空"
        assert len(vid) >= 15, f"{name} 的 voice_id 异常: {vid}"


def test_elevenlabs_tts_alias_resolves_to_real_engine():
    """elevenlabs-tts 必须映射到真实引擎，不能原样透传给上游。

    这条锁的是一个真实缺陷：曾经有个分支把 "elevenlabs-tts" 当引擎名
    直接发给 ElevenLabs，上游返回
    "An invalid ID has been received: 'elevenlabs-tts'" -> 整条兼容入口 400。
    反证：把该直通分支加回 elevenlabs_client.py，此测试立即变红。
    """
    from app.elevenlabs_client import resolve_voice_and_model
    from app.voices import load_voices

    v = load_voices()
    _, model_id, _ = resolve_voice_and_model("elevenlabs-tts", None, v)
    assert model_id != "elevenlabs-tts", "elevenlabs-tts 被原样透传给上游，上游会报 invalid ID"
    assert model_id.startswith("eleven_"), f"应映射到 eleven_* 引擎名，实际 {model_id}"

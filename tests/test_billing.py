# -*- coding: utf-8 -*-
"""计费口径 + 超时重试策略 的回归。

背景（2026-10-20 实测）：
  1. 上游 TTS 响应头 `character-cost` 是真实扣费数，实测 cost = floor(len/2)。
     适配器原来用 len(input) 回传，比上游多报一倍 → 客户被双倍扣费。
  2. 上游「合成完才返回」，客户端超时后上游已扣费。原来超时也走重试，
     实测 3000 字 × 3 次重试白烧 4335 字符。现在超时一律不重试。

这些用例钉住以上两条行为，防止以后被"优化"回去。
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import elevenlabs_client as ec  # noqa: E402


# ---------- extract_character_cost ----------

def _resp(headers):
    return httpx.Response(200, headers=headers, content=b"x")


def test_extract_character_cost_normal():
    assert ec.extract_character_cost(_resp({"character-cost": "963"})) == 963


def test_extract_character_cost_case_insensitive_header():
    # HTTP 头大小写不敏感，不同代理可能改写大小写
    r = httpx.Response(200, content=b"x")
    r.headers["Character-Cost"] = "5"
    assert ec.extract_character_cost(r) == 5


def test_extract_character_cost_missing_returns_none():
    assert ec.extract_character_cost(_resp({})) is None


def test_extract_character_cost_garbage_returns_none():
    # 上游异常 / 代理塞了非数字时不能炸，要退回本地估算
    assert ec.extract_character_cost(_resp({"character-cost": "abc"})) is None
    assert ec.extract_character_cost(_resp({"character-cost": ""})) is None


def test_extract_character_cost_float_string():
    # 见过上游给 "1.0" 这类值
    assert ec.extract_character_cost(_resp({"character-cost": "1.0"})) == 1


def test_extract_character_cost_negative_rejected():
    assert ec.extract_character_cost(_resp({"character-cost": "-3"})) is None


def test_extract_character_cost_zero_is_valid():
    """0 是合法值（输入极短时上游真会返回 0），不能被当成"没拿到"。"""
    assert ec.extract_character_cost(_resp({"character-cost": "0"})) == 0


# ---------- 超时绝不重试 ----------

class _CountingClient:
    """假 AsyncClient：记录被调用了几次，可指定每次抛什么。"""

    def __init__(self, exc=None, resp=None):
        self.calls = 0
        self.exc = exc
        self.resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.resp


@pytest.fixture
def patched_client(monkeypatch):
    """替换 httpx.AsyncClient，同时把重试次数设成非 0，确保测得出"没重试"。"""
    made = {}

    def _factory(exc=None, resp=None):
        holder = _CountingClient(exc=exc, resp=resp)
        made["c"] = holder
        monkeypatch.setattr(ec.httpx, "AsyncClient", lambda **kw: holder)
        return holder

    monkeypatch.setattr(ec, "MAX_RETRIES", 3)
    return _factory, made


def _call():
    return ec.call_elevenlabs_tts("测试文本", "eleven_v3", "voiceid123",
                                  "key-abc", "mp3_44100_128", None, None)


def test_timeout_is_never_retried(patched_client):
    """核心：超时只调一次。重试就是在重复扣上游的钱。"""
    factory, made = patched_client
    factory(exc=httpx.ReadTimeout("read timed out"))
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_call())
    assert made["c"].calls == 1, "超时后重试了 %d 次 —— 这会重复扣费" % made["c"].calls


def test_connect_error_still_retries(patched_client):
    """连接失败上游没合成，重试是安全的，必须保留（不然网络抖一下就失败）。"""
    factory, made = patched_client
    factory(exc=httpx.ConnectError("connection refused"))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(_call())
    assert made["c"].calls == 4, "MAX_RETRIES=3 应共试 4 次，实际 %d" % made["c"].calls


def test_5xx_still_retries(patched_client):
    factory, made = patched_client
    factory(resp=httpx.Response(500, content=b"boom"))
    r = asyncio.run(_call())
    assert r.status_code == 500
    assert made["c"].calls == 4


def test_4xx_not_retried(patched_client):
    factory, made = patched_client
    factory(resp=httpx.Response(401, content=b"no"))
    r = asyncio.run(_call())
    assert r.status_code == 401
    assert made["c"].calls == 1


# ---------- 端到端：X-Usage-Input-Characters 必须取上游真值 ----------

PCM = b"\x01\x00\x02\x00" * 200


def _run_speech(monkeypatch, upstream_headers, text="你好世界我们走吧"):
    """打真实 /v1/audio/speech，只把上游调用换成带指定响应头的桩。"""
    from fastapi.testclient import TestClient
    from app import main as m

    def _fake(*a, **k):
        return httpx.Response(200, content=PCM, headers=upstream_headers)

    async def _fake_async(*a, **k):
        return _fake()

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _fake_async)
    monkeypatch.setattr(m, "MOCK", False)
    try:
        c = TestClient(m.app)
        return c.post("/v1/audio/speech",
                      json={"model": "tts-1", "voice": "rachel", "input": text})
    finally:
        monkeypatch.setattr(m, "MOCK", True)


def test_rule_header_is_ascii_safe(monkeypatch):
    """响应头用的计费依据必须是纯 ASCII。

    HTTP 头只能是 latin-1，任何非 ASCII 写进 header 都会抛
    UnicodeEncodeError，把整个响应打挂（不是丢一个头，是整个请求 500）。
    这个坑差点漏掉 —— 账本文本允许中文，但头不行，所以拆成两个函数。
    """
    from app import billing
    for mode in ("chars", "upstream"):
        monkeypatch.setattr(billing, "BILLING_MODE", mode)
        for src in ("input", "guard", "upstream"):
            for status in (200, 502):
                s = billing.rule_header(1000, 900, 480, status, src)
                s.encode("latin-1")  # 不抛异常即通过
                assert s.isascii(), "响应头必须是纯 ASCII：%r" % s


def test_rule_text_allows_chinese_for_ledger():
    """账本是 UTF-8 文件，允许中文说明 —— 但只能进账本，不能进 headers。"""
    from app import billing
    s = billing.rule_text(0, 1000, None, 502)
    assert "未计费" in s
    with pytest.raises(UnicodeEncodeError):
        s.encode("latin-1")   # 证明它确实不能直接塞进 header


def test_bill_chars_is_input_length_by_default(monkeypatch):
    """计费基数 = 客户提交的原始字符数 —— 与 new-api 同源。

    new-api 用 `utf8.RuneCountInString(input)`，收的就是这个数。
    我们账本记别的数，客户拿后台日志一对就发现差额，
    且差额方向固定为我方少收（new-api 照收，我们少记）。

    注意：输入 1000 字符里含 900 个零宽那种极端情况，收 1000 是**与 new-api 一致**，
    不是隐性收费 —— 客户在 new-api 后台看到的也是 1000。
    """
    from app import billing
    monkeypatch.setattr(billing, "BILLING_MODE", "input")
    n, src = billing.bill_chars_by_mode(100, 40, 480, 200)
    assert (n, src) == (100, "input")


def test_bill_chars_chars_mode_uses_sent(monkeypatch):
    """chars 口径：按实际合成字符数计费（可切换的备选口径）。

    这不是默认值，因为它与 new-api 口径不一致、会产生 raw-sent 差额。
    保留它是为了在「必须按合成量收费」的场景下有路可走，且能一键回滚。
    """
    from app import billing
    monkeypatch.setattr(billing, "BILLING_MODE", "chars")
    n, src = billing.bill_chars_by_mode(100, 40, 480, 200)
    assert (n, src) == (40, "chars")


def test_bill_chars_ignores_input_when_clean(monkeypatch):
    """输入与合成一致时，两者当然是同一个数。"""
    from app import billing
    monkeypatch.setattr(billing, "BILLING_MODE", "input")
    assert billing.bill_chars_by_mode(1000, 1000, 480, 200) == (1000, "input")


def test_bill_never_exceeds_submitted_chars(monkeypatch):
    """核心承诺：绝不超过客户提交的字符数。

    1000 字里 900 个零宽 → 最多收 1000（= 客户提交的量）。
    """
    from app import billing
    monkeypatch.setattr(billing, "BILLING_MODE", "input")
    n, _ = billing.bill_chars_by_mode(1000, 100, 480, 200)
    assert n == 1000


def test_bill_is_monotonic(monkeypatch):
    """input 口径下计费只随提交量变化，与清理强度无关。

    清理强度改变的是**成本**（sent），不是**计费**（bill）。
    这条不变式保证「加强清理不会突然多收钱」。
    """
    from app import billing
    monkeypatch.setattr(billing, "BILLING_MODE", "input")
    for sent in range(0, 1001, 13):
        n, _ = billing.bill_chars_by_mode(1000, sent, 480, 200)
        assert n == 1000, "计费必须恒等于提交量，与清理强度无关"


def test_bill_chars_upstream_mode(monkeypatch):
    """开关切到 upstream 时按上游积分计费，供自用/大客户透明结算。"""
    from app import billing
    monkeypatch.setattr(billing, "BILLING_MODE", "upstream")
    n, src = billing.bill_chars(1000, 480)
    assert (n, src) == (480, "upstream")


def test_bill_chars_upstream_mode_falls_back(monkeypatch):
    """upstream 模式但拿不到积分时，退回字符数并如实标注来源。

    绝不能悄悄把估算值当真实扣费报出去 —— 那会让对账彻底失去意义。
    退回时用 input 口径（与 new-api 一致），并如实标注来源。
    """
    from app import billing
    monkeypatch.setattr(billing, "BILLING_MODE", "upstream")
    n, src = billing.bill_chars(1000, None)
    assert (n, src) == (1000, "input")


def test_usage_header_reports_input_by_default(monkeypatch):
    """端到端：默认模式下 8 字文本报 8，而不是上游的 3。"""
    r = _run_speech(monkeypatch, {"character-cost": "3"})
    assert r.status_code == 200
    assert r.headers["X-Usage-Input-Characters"] == "8"
    assert r.headers["X-Usage-Source"] == "input"

def test_upstream_credits_always_exposed(monkeypatch):
    """上游真实积分必须始终透出，作为"没有超收"的自证材料。"""
    r = _run_speech(monkeypatch, {"character-cost": "3"})
    assert r.headers.get("X-Usage-Upstream-Credits") == "3"


def test_upstream_credits_absent_when_upstream_silent(monkeypatch):
    """上游没给积分时不编数字 —— 缺就是缺。"""
    r = _run_speech(monkeypatch, {})
    assert "X-Usage-Upstream-Credits" not in r.headers


def test_request_id_header_present(monkeypatch):
    """每个响应都要有 request_id，三层对账靠它精确配对，而不是猜时间戳。"""
    r = _run_speech(monkeypatch, {"character-cost": "3"})
    rid = r.headers.get("X-Request-Id")
    assert rid and len(rid) >= 8


def test_usage_header_no_gap_when_equal(monkeypatch):
    """口径一致时不加多余头，避免噪音。"""
    r = _run_speech(monkeypatch, {"character-cost": "8"})
    assert r.headers["X-Usage-Input-Characters"] == "8"
    assert "X-Usage-Local-Chars" not in r.headers
def test_default_retries_is_zero():
    """默认不重试：TTS 按次计费，默认值必须站在"别乱花钱"这一侧。"""
    import importlib
    import os
    old = os.environ.pop("ELEVENLABS_MAX_RETRIES", None)
    try:
        from app import config
        importlib.reload(config)
        assert config.MAX_RETRIES == 0, "默认重试次数应为 0，实际 %r" % config.MAX_RETRIES
    finally:
        if old is not None:
            os.environ["ELEVENLABS_MAX_RETRIES"] = old
        importlib.reload(config)


def test_default_timeout_covers_long_text():
    """实测 33ms/字。默认超时必须能覆盖几秒字级别的文本，否则长文本必失败。"""
    import importlib
    import os
    old = os.environ.pop("ELEVENLABS_TIMEOUT", None)
    try:
        from app import config
        importlib.reload(config)
        # 300s / 0.033 ≈ 9000 字
        assert config.TIMEOUT >= 300, (
            "默认超时 %rs 太小：实测 33ms/字，2000 字就要 98s" % config.TIMEOUT)
    finally:
        if old is not None:
            os.environ["ELEVENLABS_TIMEOUT"] = old
        importlib.reload(config)


def test_jobs_max_chars_matches_timeout():
    """JOBS_MAX_CHARS 必须落在超时能覆盖的范围内，否则该配置形同虚设。"""
    import importlib
    import os
    old1 = os.environ.pop("ELEVENLABS_TIMEOUT", None)
    old2 = os.environ.pop("ADAPTER_JOBS_MAX_CHARS", None)
    try:
        from app import config
        importlib.reload(config)
        coverable = config.TIMEOUT / 0.033
        assert config.JOBS_MAX_CHARS <= coverable * 1.1, (
            "JOBS_MAX_CHARS=%d 超出超时能覆盖的 %.0f 字 —— 必然超时失败"
            % (config.JOBS_MAX_CHARS, coverable))
    finally:
        if old1 is not None:
            os.environ["ELEVENLABS_TIMEOUT"] = old1
        if old2 is not None:
            os.environ["ADAPTER_JOBS_MAX_CHARS"] = old2
        importlib.reload(config)

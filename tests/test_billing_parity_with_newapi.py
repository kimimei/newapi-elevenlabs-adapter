# -*- coding: utf-8 -*-
"""A 方案的验收门禁：账本口径必须与 new-api 逐笔相等。

**这是整个计费链路最硬的一条约束，单独成文件钉死。**

背景（rc.44 源码取证）：

    relaykit/dto/audio.go     GetTokenCountMeta() -> CombineText: r.Input
    service/token_counter.go   utf8.RuneCountInString(meta.CombineText)
    relay/channel/openai/audio.go  usage.PromptTokens = info.GetEstimatePromptTokens()

即 new-api 计的 = **原始 input 的 Unicode 字符数，不 strip**。
Go 的 RuneCountInString 与 Python 的 len() 都是 Unicode 字符数，两侧单位一致。

所以适配器账本的 bill_chars 必须 == len(原始 input)，且在真实 HTTP 链路上
逐笔成立。这不是"差不多"，是**逐笔相等**。

本文件用真实端点 + 真实账本核对，覆盖 new-api 口径最容易出偏差的形态：
首尾空白、CRLF、零宽、BOM、emoji ZWJ、代理对（UTF-16 长度 ≠ 字符数）。
"""
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing  # noqa: E402

PCM = b"\x01\x00\x02\x00" * 200

# 覆盖 new-api 口径最容易出偏差的形态
SAMPLES = [
    "你好世界",                    # 干净
    "  你好  ",                    # 首尾空格（new-api 不 strip → 6）
    "你好\n",                      # 尾随换行
    "\n你好",                      # 首部换行
    "你\u200b好\u200b世界",         # 零宽
    "\ufeff你好世界\ufeff",          # BOM
    "你    好",                    # 连续空格
    "第一段\r\n第二段",            # CRLF
    "👨\u200d👩\u200d👧一家人",      # emoji ZWJ
    "**粗体**和[链接](http://x.com)",  # Markdown
    "a b c",
    "混排 text 混排 123",           # 中英数混排
    "𝔘𝔫𝔦𝔠𝔬𝔡𝔢 𝟄𝔢𝔠𝔥",             # 代理对：UTF-16 长度 24 ≠ 字符数 12
]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "_last_cleanup_day", None)
    return tmp_path


def _rows(tmp_path):
    out = []
    for f in sorted(tmp_path.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _post(monkeypatch, text):
    from fastapi.testclient import TestClient
    from app import main as m

    async def _fake(payload_text, *a, **k):
        return httpx.Response(200, content=PCM, headers={"character-cost": "1"})

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _fake)
    monkeypatch.setattr(m, "MOCK", False)
    try:
        c = TestClient(m.app)
        return c.post("/v1/audio/speech",
                      json={"model": "tts-1", "voice": "rachel", "input": text})
    finally:
        monkeypatch.setattr(m, "MOCK", True)


@pytest.mark.parametrize("text", SAMPLES)
def test_ledger_equals_newapi_rune_count(monkeypatch, ledger, text):
    """账本 bill_chars == len(原始 input) —— 逐笔相等，零容差。

    这是 new-api 的计费口径（`utf8.RuneCountInString(input)`，不 strip）。
    两边单位一致（都是 Unicode 字符数），所以差额只能是口径不同造成的。
    """
    r = _post(monkeypatch, text)
    assert r.status_code == 200
    row = _rows(ledger)[-1]
    na = len(text)          # new-api 口径：原始 input 的 Unicode 字符数
    assert row["bill_chars"] == na, (
        "账本 %d != new-api 口径 %d（原文 %r）" % (row["bill_chars"], na, text))
    assert row["in_chars"] == na
    assert row["bill_source"] == "input"


@pytest.mark.parametrize("text", SAMPLES)
def test_response_header_equals_ledger(monkeypatch, ledger, text):
    """响应头与账本必须一致 —— 客户看到的和账上记的是同一个数。"""
    r = _post(monkeypatch, text)
    assert r.status_code == 200
    row = _rows(ledger)[-1]
    assert int(r.headers["X-Usage-Input-Characters"]) == row["bill_chars"]
    assert r.headers["X-Usage-Source"] == "input"


def test_surrogate_pair_counted_as_characters(monkeypatch, ledger):
    """代理对按**字符**计，不是 UTF-16 码元，也不是字节。

    '𝔘𝔫𝔦𝔠𝔬𝔡𝔢' 是 6 个字符（UTF-16 占 12 个码元，UTF-8 占 24 字节）。
    Go 的 RuneCountInString 与 Python 的 len() 都得 6 ——
    若哪一侧按字节或码元算，就会差一倍，且这种样本在真实中文流量里不出现，
    靠肉眼很难发现。
    """
    text = "𝔘𝔫𝔦𝔠𝔬𝔡𝔢 𝟄𝔢𝔠𝔥"
    assert len(text) == 12                          # 字符数（= new-api 的口径）
    assert len(text.encode("utf-8")) == 45          # UTF-8 字节数
    assert len(text.encode("utf-16-le")) // 2 == 23  # UTF-16 码元数
    r = _post(monkeypatch, text)
    assert r.status_code == 200
    assert r.headers["X-Usage-Input-Characters"] == "12", "按字符计，不是字节/码元"


def test_whitespace_not_stripped(monkeypatch, ledger):
    """首尾空白**不 strip** —— new-api 收多少我们就记多少。

    客户提交 '  你好  ' 共 6 个字符，new-api 就收 6。
    若我们 strip 成 4，逐笔对账就稳定差 2，且这种偏差每次都出现、
    但金额极小，很容易被当成"正常容差"放过。
    """
    r = _post(monkeypatch, "  你好  ")
    assert r.status_code == 200
    assert r.headers["X-Usage-Input-Characters"] == "6"
    row = _rows(ledger)[-1]
    assert row["bill_chars"] == 6
    assert row["sent_chars"] == 2, "实际合成 2（成本依据）"


def test_chars_mode_still_available_for_rollback(monkeypatch, ledger):
    """回滚路径必须一直可用：改一个环境变量就能切回 sent 口径。

    保留旧口径不是为了继续用，是为了在"必须按合成量收费"的场景下有路可走，
    且出事时能立刻回退。
    """
    from app import billing as b
    monkeypatch.setattr(b, "BILLING_MODE", "chars")
    r = _post(monkeypatch, "你\u200b好\u200b世界")
    assert r.status_code == 200
    assert r.headers["X-Usage-Input-Characters"] == "4"
    assert r.headers["X-Usage-Source"] == "chars"
    row = _rows(ledger)[-1]
    assert row["bill_chars"] == 4
    assert row["bill_source"] == "chars"

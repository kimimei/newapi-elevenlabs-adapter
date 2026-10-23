# -*- coding: utf-8 -*-
"""清理链路端到端：提交文本 → 清理 → 送上游 → 计费，四个数必须自洽。

这里验证的不是"清理函数对不对"（那是 test_text_clean.py 的事），
而是**清理函数有没有真的接进主链路** —— 最典型的失效方式是：
清理了但上游收的还是原文，或者计费按原文算了。
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing  # noqa: E402

PCM = b"\x01\x00\x02\x00" * 200


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "_last_cleanup_day", None)
    return tmp_path


def _rows(tmp_path):
    import json
    out = []
    for f in sorted(tmp_path.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _post(monkeypatch, text, capture=None):
    """打真实端点，桩掉上游，把实际收到的文本抓下来。"""
    from fastapi.testclient import TestClient
    from app import main as m

    async def _fake(payload_text, *a, **k):
        if capture is not None:
            capture.append(payload_text)
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


def test_upstream_receives_cleaned_text(monkeypatch):
    """最关键的一条：上游收到的必须是清理后的文本。

    只测清理函数是不够的 —— 忘了把 text 传给上游，测试照样全绿，
    但客户付了 10 个字、听了 8 个字。
    """
    got = []
    r = _post(monkeypatch, "你\u200b好\u200b世界", capture=got)
    assert r.status_code == 200
    assert got == ["你好世界"], "上游收到的应该是清理后的文本，实际 %r" % got


def test_billing_uses_input_length(monkeypatch, ledger):
    """计费 = 客户提交的原始字符数（与 new-api 同源）—— 这才是能对账的账。

    "你\u200b好\u200b世界" 客户提交 6 个字符，new-api 就收 6 个
    （RuneCount(input)，不 strip、不剔除零宽）。我们记 6，两边逐笔相等。

    实际只合成 4 个（sent=4）—— 差的 2 个是**我方上游成本的节省**，
    不是给客户的让利。账本两个数都留，毛利才算得准。
    """
    r = _post(monkeypatch, "你\u200b好\u200b世界")
    assert r.status_code == 200
    assert r.headers["X-Usage-Input-Characters"] == "6", "必须与 new-api 的 RuneCount(input) 一致"
    row = _rows(ledger)[0]
    assert row["in_chars"] == 6, "原始输入 = 计费依据"
    assert row["sent_chars"] == 4, "实际合成 = 成本依据"
    assert row["bill_chars"] == 6
    assert row["bill_source"] == "input"


def test_never_exceeds_submitted_chars(monkeypatch, ledger):
    """极端情况：100 字符里 98 个是不可见字符，收 100（= 提交的量）。

    这是账本能站住的底线 —— 我们收的数绝不超过客户提交的数，
    且与 new-api 收的**完全一样**。客户在后台看到的也是 100。
    """
    text = "你好" + "\u200b" * 98   # 100 字符，只有 2 个真字
    r = _post(monkeypatch, text)
    assert r.status_code == 200
    row = _rows(ledger)[0]
    assert row["in_chars"] == 100
    assert row["sent_chars"] == 2
    assert row["bill_chars"] == 100
    assert row["bill_source"] == "input"

def test_clean_report_visible_in_headers(monkeypatch):
    """客户能从响应头看到删了多少，不用猜。"""
    r = _post(monkeypatch, "你\u200b好\u200b世界")
    assert "X-Text-Clean" in r.headers
    assert r.headers["X-Text-Raw-Chars"] == "6"
    assert r.headers["X-Text-Sent-Chars"] == "4"


def test_no_clean_headers_when_nothing_removed(monkeypatch):
    """没东西可删时不加多余头，避免噪音。"""
    r = _post(monkeypatch, "你好世界")
    assert "X-Text-Clean" not in r.headers


def test_all_invisible_input_rejected(monkeypatch):
    """全是零宽字符的输入清理后为空 —— 必须拒绝，否则白扣上游费用。"""
    got = []
    r = _post(monkeypatch, "\u200b\ufeff\u200b", capture=got)
    assert r.status_code == 400
    assert got == [], "不该把空文本送给上游"


def test_whitespace_only_rejected(monkeypatch):
    r = _post(monkeypatch, "   \n\t  ")
    assert r.status_code == 400


def test_emoji_survives_pipeline(monkeypatch):
    """emoji 经过整条链路不能被拆散。"""
    family = "👨\u200d👩\u200d👧"
    got = []
    r = _post(monkeypatch, family, capture=got)
    assert r.status_code == 200
    assert got == [family], "emoji 组合字符被破坏了"


def test_failed_request_records_raw_and_clean(monkeypatch, ledger):
    """失败时也要能看出客户提交了多少、清理后多少。"""
    from fastapi.testclient import TestClient
    from app import main as m

    async def _timeout(*a, **k):
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _timeout)
    monkeypatch.setattr(m, "MOCK", False)
    try:
        c = TestClient(m.app)
        r = c.post("/v1/audio/speech",
                   json={"model": "tts-1", "voice": "rachel",
                         "input": "你\u200b好世界"})
        assert r.status_code == 502
    finally:
        monkeypatch.setattr(m, "MOCK", True)

    row = _rows(ledger)[0]
    assert row["status"] == 502
    assert row["bill_chars"] == 0
    assert row["raw_chars"] == 5
    assert row["cleaned_chars"] == 4

# -*- coding: utf-8 -*-
"""清理省下的成本，以及这个数字必须真实可核对。

**先说清语义**（口径切换后容易读错）：

    input 口径（默认）下，客户按**提交的原始字符数**付费 —— 与 new-api 一致；
    而上游只按**实际合成的字符数**收钱。
    所以 raw − sent 这段「省下来的」是**我方上游成本的节省**，不是给客户的让利。

    把它宣传成「替客户省钱」是错的：客户账单没有变少，变少的是我们的成本。
    虚报比不宣称更糟 —— 客户拿两边的账单一比就穿帮。

所以断言的重点是：
  1. `saved_chars` 必须等于「原始输入 - 实际合成」，不能凭空放大
  2. 计费恒等于提交量，清理强度改变不能影响计费（省成本不能变成改计费）
  3. `/v1/usage` 不能泄露密钥、IP 等运营侧信息
"""
import json
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


def _post(monkeypatch, text, capture=None, token="sk_test"):
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
                      headers={"Authorization": "Bearer " + token},
                      json={"model": "tts-1", "voice": "rachel", "input": text})
    finally:
        monkeypatch.setattr(m, "MOCK", True)


def _rows(tmp_path):
    out = []
    for f in sorted(tmp_path.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ---------- 本次节省可见 ----------

def test_saved_chars_header(monkeypatch):
    """单次请求就要让客户看到「这次省了多少」。"""
    r = _post(monkeypatch, "你\u200b好\u200b世界")   # 6 → 4
    assert r.status_code == 200
    assert r.headers["X-Text-Saved-Chars"] == "2"


def test_no_saved_header_when_clean(monkeypatch):
    """没东西可省时不加头，避免噪音。"""
    r = _post(monkeypatch, "你好世界")
    assert "X-Text-Saved-Chars" not in r.headers


def test_saved_does_not_change_billing(monkeypatch, ledger):
    """清理掉的字符是**我方成本节省**，不是给客户的折扣 —— 两个数是独立的。

    input 口径下客户按提交的 6 个字符付费（与 new-api 一致），
    但上游只收 4 个字符的实际合成量 —— 差额的 2 个是成本节省。
    """
    r = _post(monkeypatch, "你\u200b好\u200b世界")
    assert r.headers["X-Usage-Input-Characters"] == "6"
    assert r.headers["X-Text-Saved-Chars"] == "2"
    row = _rows(ledger)[0]
    assert row["in_chars"] == 6
    assert row["sent_chars"] == 4
    assert row["bill_chars"] == 6, "计费与 new-api 同源 = 客户提交的量"
    assert row["bill_source"] == "input"


# ---------- 累计节省统计 ----------

def test_stats_accumulate_saved(monkeypatch, ledger):
    """账本汇总的 saved_chars 必须等于逐笔（原始 - 合成）之和。"""
    _post(monkeypatch, "你\u200b好\u200b世界")      # 6 → 4，省 2
    _post(monkeypatch, "你   好")                    # 5 → 3，省 2
    _post(monkeypatch, "你好世界")                   # 4 → 4，省 0
    s = billing.stats()
    assert s["saved_chars"] == 4
    assert s["in_chars"] == 15
    assert s["saved_ratio"] == pytest.approx(4 / 15, abs=1e-4)


def test_stats_saved_never_exceeds_input(monkeypatch, ledger):
    """saved_chars 不可能大于输入总量 —— 否则统计逻辑有 bug。"""
    for t in ["你\u200b好", "你好   世界", "第一段\n\n第二段"]:
        _post(monkeypatch, t)
    s = billing.stats()
    assert 0 <= s["saved_chars"] <= s["in_chars"]


# ---------- 按客户聚合 ----------

def test_usage_by_token_groups(monkeypatch, ledger):
    """按令牌聚合，能看出每个客户省了多少。"""
    _post(monkeypatch, "你\u200b好\u200b世界", token="sk_aaaaaa")
    _post(monkeypatch, "你好   世界", token="sk_bbbbbb")
    rows = billing.usage_by_token(days=1)
    assert len(rows) == 2
    # _token_label 剥掉 sk_ 前缀后留前 6 位
    assert {r["token"] for r in rows} == {"sk_aaa…", "sk_bbb…"}
    for r in rows:
        assert r["saved_chars"] > 0
        assert r["in_chars"] > r["sent_chars"]


def test_usage_by_token_truncates_key(monkeypatch, ledger):
    """聚合结果里的令牌必须是截断的，不能出现完整密钥。"""
    _post(monkeypatch, "你\u200b好", token="sk_" + "x" * 40)
    rows = billing.usage_by_token(days=1)
    assert len(rows[0]["token"]) <= 8
    assert "x" * 10 not in rows[0]["token"]


# ---------- /v1/usage 接口 ----------

def test_usage_endpoint_returns_summary(monkeypatch, ledger):
    _post(monkeypatch, "你\u200b好\u200b世界")
    from fastapi.testclient import TestClient
    from app import main as m
    c = TestClient(m.app)
    r = c.get("/v1/usage?days=1")
    assert r.status_code == 200
    d = r.json()
    assert d["today"]["requests"] == 1
    assert d["today"]["saved_chars"] == 2
    assert d["window"]["saved_chars"] == 2
    assert d["by_caller"]


def test_usage_endpoint_does_not_leak_secrets(monkeypatch, ledger):
    """/v1/usage 只给聚合数字，不能把密钥、IP、原文吐出去。

    账本里有 token 前 6 位和客户端 IP，那是运营侧排查用的，
    不该经由这个面向客户的接口暴露。
    """
    _post(monkeypatch, "你\u200b好\u200b世界", token="sk_SECRETKEY123456")
    from fastapi.testclient import TestClient
    from app import main as m
    c = TestClient(m.app)
    body = c.get("/v1/usage?days=1").text
    assert "SECRETKEY" not in body
    assert "127.0.0.1" not in body
    assert "testclient" not in body.lower()


def test_usage_endpoint_clamps_days(monkeypatch, ledger):
    """days 参数不能任意放大，避免扫全盘文件。"""
    from fastapi.testclient import TestClient
    from app import main as m
    c = TestClient(m.app)
    assert c.get("/v1/usage?days=99999").status_code == 200
    assert c.get("/v1/usage?days=-5").status_code == 200


# ---------- 省的钱要真实 ----------

def test_saved_matches_real_reduction(monkeypatch):
    """端到端核对：省下的数字 == 原文长度差，不能虚报。

    这是对外宣称的核心指标，虚报比不宣称更糟。
    """
    samples = [
        "你\u200b好\u200b世界",           # 零宽
        "\ufeff你好世界",                  # BOM
        "第一段\r\n第二段",                # CRLF
        "你好   世界",                     # 多空格
        "  两边空白  ",                    # 首尾
    ]
    for text in samples:
        got = []
        r = _post(monkeypatch, text, capture=got)
        if r.status_code != 200:
            continue
        expected = len(text) - len(got[0])
        actual = int(r.headers.get("X-Text-Saved-Chars", "0"))
        assert actual == expected, (
            "省下的数不符：原文 %r 声明 %d，实际差 %d" % (text, actual, expected)
        )

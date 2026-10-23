# -*- coding: utf-8 -*-
"""计费账本的回归测试。

账本是三层对账的地基：
    L1  bill_chars vs new-api logs.prompt_tokens   → 中转站漏记/重复记
    L2  up_credits vs 上游 /v1/history             → 上游扣费异常 / Key 被挪用
    L3  bill_chars × 售价 vs up_credits × 成本     → 实际毛利率

最容易被"优化"掉、也最贵的一条：**失败请求也必须记账**。
上游是「合成完才返回」，超时失败的上游照样扣钱，如果没有这一行，
就永远查不出钱花在哪了。
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


@pytest.fixture
def tmp_path_ledger(tmp_path, monkeypatch):
    """把账本目录指向临时目录，避免污染真实 logs/。"""
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


def test_success_row_has_both_numbers(tmp_path_ledger):
    """成功请求同时记录「向客户计费」和「上游真实消耗」两个数。"""
    billing.record(request_id="r1", in_chars=1000, up_credits=480, status=200,
                   elapsed_ms=7000, model="zh-boyin", voice="yun")
    rows = _rows(tmp_path_ledger)
    assert len(rows) == 1
    r = rows[0]
    assert r["in_chars"] == 1000
    assert r["bill_chars"] == 1000      # 默认 chars 模式
    assert r["up_credits"] == 480       # 上游真值
    assert r["status"] == 200
    assert r["rid"] == "r1"


def test_failed_request_is_recorded(tmp_path_ledger):
    """核心：失败的请求也要留痕。

    超时失败的上游已经合成并扣费，这是最贵的一类失败。
    没有这行账，L2 对账就永远看不出钱花在哪。
    """
    billing.record(request_id="r2", in_chars=3000, up_credits=None, status=502,
                   elapsed_ms=120000, error="ReadTimeout: read timed out")
    rows = _rows(tmp_path_ledger)
    assert len(rows) == 1
    assert rows[0]["status"] == 502
    assert rows[0]["in_chars"] == 3000
    assert "ReadTimeout" in rows[0]["error"]


def test_validation_error_is_recorded(tmp_path_ledger):
    """连 400 参数错误也记 —— 排查"客户说调用失败"时，账本能直接定位。"""
    billing.record(request_id="r3", in_chars=0, up_credits=None,
                   status=400, elapsed_ms=2, error="input 为空")
    rows = _rows(tmp_path_ledger)
    assert rows and rows[0]["status"] == 400


def test_up_credits_none_stays_none(tmp_path_ledger):
    """拿不到上游积分就写 null，不能填 0 或估算值冒充真实扣费。"""
    billing.record(request_id="r4", in_chars=500, up_credits=None,
                   status=200, elapsed_ms=100)
    assert _rows(tmp_path_ledger)[0]["up_credits"] is None


def test_ledger_off_switch(tmp_path_ledger, monkeypatch):
    """关掉开关时一行都不写（不建议，但要有）。"""
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", True)
    billing.record(request_id="r5", in_chars=10, up_credits=5, status=200, elapsed_ms=1)
    assert _rows(tmp_path_ledger) == []


def test_write_failure_does_not_raise(tmp_path_ledger, monkeypatch):
    """账本写失败绝不能把业务请求带崩 —— 记账是旁路，不是主链路。"""
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(billing.os, "makedirs", boom)
    billing.record(request_id="r6", in_chars=10, up_credits=5, status=200, elapsed_ms=1)


def test_token_is_truncated(tmp_path_ledger):
    """账本里的密钥标签必须截断，账本不能变成第二份密钥泄露源。"""
    billing.record(request_id="r7", in_chars=10, up_credits=5, status=200,
                   elapsed_ms=1, token_name="QnSxCN…")
    assert _rows(tmp_path_ledger)[0]["token"] == "QnSxCN…"


def test_stats_aggregates_and_margin(tmp_path_ledger, monkeypatch):
    """汇总：能算出今天的请求数、成功失败、以及毛利率。

    毛利率**只算成功请求** —— 失败请求不收费但上游可能扣了钱，
    混进来会把毛利率算歪（那是"白扣"损失，不是毛利）。

    ⚠️ 2026-10-22 口径修正：旧版断言 `gross_margin_ratio == 0.52`
    （= 1 - 1440/3000），那是**拿积分除字符**，两个不同量纲的比值，
    数学上无意义。现在必须先把两边换成美元，且**未配单价时返回 None**。
    量纲正确性的详细用例见 `tests/test_margin_dimension.py`。
    """
    # 未配单价 → 必须返回 None（"宁可不给，也不给错数"）
    monkeypatch.setattr(billing, "UP_CREDITS_PER_USD", 0.0)
    monkeypatch.setattr(billing, "CHARS_PER_USD", 0.0)

    billing.record(request_id="a", in_chars=1000, up_credits=480, status=200, elapsed_ms=1)
    billing.record(request_id="b", in_chars=2000, up_credits=960, status=200, elapsed_ms=1)
    billing.record(request_id="c", in_chars=3000, up_credits=None, status=502, elapsed_ms=1)
    s = billing.stats()
    assert s["requests"] == 3
    assert s["succeeded"] == 2
    assert s["failed"] == 1
    assert s["in_chars"] == 6000
    assert s["up_credits"] == 1440
    # 只算成功：1440 积分 / 3000 字符 = 0.48 —— 同量纲，有意义
    assert s["ok_bill_chars"] == 3000
    assert s["ok_sent_chars"] == 3000
    assert s["credits_per_char"] == pytest.approx(0.48, abs=1e-6)
    assert s["gross_margin_ratio"] is None, (
        "未配 ADAPTER_UP_CREDITS_PER_USD / ADAPTER_CHARS_PER_USD 时必须返回 "
        "None；返回数字说明又在拿积分除字符")

    # 配上单价后，毛利率必须按美元算
    monkeypatch.setattr(billing, "UP_CREDITS_PER_USD", 4545.4545)
    monkeypatch.setattr(billing, "CHARS_PER_USD", 5000.0)
    s = billing.stats()
    # 成本 1440/4545.4545 = $0.3168；收入 3000/5000 = $0.6
    # 毛利率 = 1 - 0.3168/0.6 = 1 - 0.528 = 0.472
    assert s["gross_margin_ratio"] == pytest.approx(0.472, abs=0.01)


def test_old_ledger_purged(tmp_path_ledger, monkeypatch):
    """超过保留天数的账本自动清理，避免无限增长。"""
    old = tmp_path_ledger / "2020-01-01.jsonl"
    old.write_text('{"x":1}\n', encoding="utf-8")
    monkeypatch.setattr(billing, "BILLING_LOG_DAYS", 30)
    billing.record(request_id="r8", in_chars=1, up_credits=1, status=200, elapsed_ms=1)
    assert not old.exists(), "过期账本文件应被删除"


def test_daily_file_split(tmp_path_ledger):
    """按天切分，便于按天核对与清理。"""
    billing.record(request_id="r9", in_chars=1, up_credits=1, status=200, elapsed_ms=1)
    files = list(tmp_path_ledger.glob("*.jsonl"))
    assert len(files) == 1
    # 文件名必须是 YYYY-MM-DD.jsonl
    assert len(files[0].stem) == 10 and files[0].stem[4] == "-"


def test_failed_request_bills_zero(tmp_path_ledger):
    """失败请求的 bill_chars 必须是 0，否则账本和面板永远对不上。

    new-api 对非 200 响应不扣费。如果账本仍按 len(input) 记账，
    L1 就会显示出恒定的差额，只能靠"容差"掩盖 —— 客户一核对就是争议。
    in_chars 仍保留原始提交量，不影响追溯。
    """
    billing.record(request_id="f1", in_chars=3000, up_credits=None,
                   status=502, elapsed_ms=120000, error="ReadTimeout")
    r = _rows(tmp_path_ledger)[0]
    assert r["bill_chars"] == 0, "失败请求不能计费"
    assert r["bill_source"] == "failed"
    assert r["in_chars"] == 3000, "原始提交量必须留痕"


def test_bill_rule_is_explained(tmp_path_ledger):
    """每笔账都要有一句「怎么算出来的」，客户质疑时不用翻代码。"""
    billing.record(request_id="r1", in_chars=1000, up_credits=480, status=200, elapsed_ms=1)
    billing.record(request_id="r2", in_chars=3000, up_credits=None, status=502, elapsed_ms=1)
    rows = {r["rid"]: r for r in _rows(tmp_path_ledger)}
    assert "bill=len(input)=1000" in rows["r1"]["bill_rule"]
    assert "mode=input" in rows["r1"]["bill_rule"]
    assert "upstream=480" in rows["r1"]["bill_rule"]
    assert "未计费" in rows["r2"]["bill_rule"]


def test_count_chars_strips_edges():
    """首尾空白不计费：上游合成前会忽略，计入就是多收钱。

    '你好\\n' 是 3 个字符，但真正合成的只有 2 个。
    """
    assert billing.count_chars("你好") == 2
    assert billing.count_chars("你好\n") == 2
    assert billing.count_chars("  你好  ") == 2
    assert billing.count_chars("你好\n\n") == 2


def test_count_chars_keeps_inner_spaces():
    """中间的空格照常计 —— 它会真的产生停顿。"""
    assert billing.count_chars("你 好") == 3


def test_count_chars_empty_and_none():
    assert billing.count_chars("") == 0
    assert billing.count_chars(None) == 0


# ---------- 端到端：失败请求真的落到账本里 ----------

def test_upstream_timeout_writes_ledger_row(tmp_path_ledger, monkeypatch):
    """端到端验证：上游超时时账本必须有一行 502。

    这条覆盖的是最贵的事故场景 —— 钱花了、音频没拿到、也没人知道。
    """
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
                   json={"model": "tts-1", "voice": "rachel", "input": "你好世界"})
        assert r.status_code == 502
    finally:
        monkeypatch.setattr(m, "MOCK", True)

    rows = _rows(tmp_path_ledger)
    assert rows, "超时请求必须写账本"
    assert rows[0]["status"] == 502
    assert rows[0]["in_chars"] == 4
    assert rows[0]["bill_chars"] == 0, "失败的请求不能计费"
    assert rows[0].get("rid")


def test_error_response_carries_request_id(monkeypatch):
    """错误响应也必须带 X-Request-Id。

    客户说"这次失败还被扣钱了"，没有 rid 就无从查起 ——
    这是账单争议里最被动的一种局面。
    """
    from fastapi.testclient import TestClient
    from app import main as m

    c = TestClient(m.app)
    r = c.post("/v1/audio/speech", json={"model": "tts-1", "input": ""})
    assert r.status_code == 400
    rid = r.headers.get("X-Request-Id")
    assert rid and len(rid) >= 8, "错误响应必须带 rid，实际头：%s" % dict(r.headers)


def test_bad_format_response_carries_request_id(monkeypatch):
    """另一条错误路径也要带 rid，不能只有个别分支记得。"""
    from fastapi.testclient import TestClient
    from app import main as m

    c = TestClient(m.app)
    r = c.post("/v1/audio/speech",
               json={"model": "tts-1", "voice": "rachel", "input": "你好",
                     "response_format": "aac"})
    assert r.status_code == 400
    assert r.headers.get("X-Request-Id")


def test_leading_trailing_whitespace_not_billed(tmp_path_ledger, monkeypatch):
    """端到端：'你好\\n' 计 3 个字符（= new-api 的口径），但只合成 2 个。

    ⚠️ 这条测试记录了口径切换后的真实行为，别当成 bug：

    new-api 计的是 `RuneCount(input)`，**不 strip** —— 客户提交 3 个字符，
    它就收 3 个字符的钱。我们账本跟它一致，所以也是 3。

    但上游只合成 2 个字符（尾随换行不发音），所以那 1 个字符的差额
    是**上游成本的节省**，不是多收客户。
    """
    from fastapi.testclient import TestClient
    from app import main as m

    async def _ok(*a, **k):
        return httpx.Response(200, content=PCM, headers={"character-cost": "1"})

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _ok)
    monkeypatch.setattr(m, "MOCK", False)
    try:
        c = TestClient(m.app)
        r = c.post("/v1/audio/speech",
                   json={"model": "tts-1", "voice": "rachel", "input": "你好\n"})
        assert r.status_code == 200
    finally:
        monkeypatch.setattr(m, "MOCK", True)

    # 与 new-api 同源：不 strip，3 个字符就记 3
    assert r.headers["X-Usage-Input-Characters"] == "3", "必须与 new-api 的 RuneCount(input) 一致"
    row = _rows(tmp_path_ledger)[0]
    assert row["in_chars"] == 3
    assert row["sent_chars"] == 2, "实际合成的量 = 成本依据"
    assert row["bill_chars"] == 3
    assert row["bill_source"] == "input"


def test_usage_rule_header_present(monkeypatch):
    """成功响应带计费依据，客户可直接看到算式。"""
    from fastapi.testclient import TestClient
    from app import main as m

    async def _ok(*a, **k):
        return httpx.Response(200, content=PCM, headers={"character-cost": "1"})

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _ok)
    monkeypatch.setattr(m, "MOCK", False)
    try:
        c = TestClient(m.app)
        r = c.post("/v1/audio/speech",
                   json={"model": "tts-1", "voice": "rachel", "input": "你好世界"})
        assert r.status_code == 200
    finally:
        monkeypatch.setattr(m, "MOCK", True)

    rule = r.headers.get("X-Usage-Rule", "")
    assert "bill=input=4" in rule
    assert "mode=input" in rule
    assert "upstream=1" in rule

# -*- coding: utf-8 -*-
"""账本的「流式标记」与「真实模型 id」回归测试。

## 为什么补这两个字段

2026-11-11 在生产账本（36 行真实数据）里发现两件事：

1. **没有 stream 字段** → 上线流式之后，无法回答「有多少比例在用流式」，
   更没法单独对流式请求出首块 SLA。业务方问起来只能靠猜。
2. **model 字段是客户端写的音色别名**（`rachel` / `lin` / `alloy`），
   不是上游真实模型（`eleven_flash_v2_5` / `eleven_multilingual_v2`）。
   而 flash 档实测约 0.24 积分/字符、v2/v3 档约 0.48 —— **差一倍**。
   拿别名当模型算成本，毛利只能算错。

本文件的测试在旧代码上**必须失败**（见 commit message 里的反证记录），
否则等于没测。
"""
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing  # noqa: E402
from app import main as m  # noqa: E402


class ASGIRequest:
    """最小的 ASGI 调用器：不启服务、不占端口，直接压 app。"""

    def __init__(self, payload, path="/v1/audio/speech"):
        self.body = json.dumps(payload).encode()
        self.path = path
        self.messages = []

    async def receive(self):
        if not getattr(self, "sent", False):
            self.sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await asyncio.Event().wait()

    async def send(self, message):
        self.messages.append(message)

    async def run(self):
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(self.body)).encode())]
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": self.path, "raw_path": self.path.encode(), "query_string": b"",
                 "root_path": "", "server": ("testserver", 80),
                 "client": ("testclient", 1234), "headers": headers}
        await asyncio.wait_for(m.app(scope, self.receive, self.send), 10)

    @property
    def status(self):
        return next(msg for msg in self.messages
                    if msg["type"] == "http.response.start")["status"]


def setup(monkeypatch, tmp_path):
    """Mock 模式 + 临时账本目录。Mock 不需要上游 Key，也不花额度。"""
    monkeypatch.setattr(m, "MOCK", True)
    monkeypatch.setattr(m, "ALLOW_IPS", [])
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 0)
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "_last_cleanup_day", None)


def ledger():
    return [json.loads(line)
            for path in sorted(Path(billing.BILLING_LOG_DIR).glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_sse_request_is_marked_stream(monkeypatch, tmp_path):
    """stream_format=sse 的请求，账本必须 stream=true、stream_format=sse。"""
    async def case():
        setup(monkeypatch, tmp_path)
        req = ASGIRequest({"model": "tts-1", "voice": "rachel",
                           "input": "你好世界", "stream_format": "sse"})
        await req.run()
        assert req.status == 200
        row = ledger()[0]
        assert row["stream"] is True
        assert row["stream_format"] == "sse"
        assert row["stream_ignored"] is False
    asyncio.run(case())


def test_audio_request_is_marked_not_stream(monkeypatch, tmp_path):
    """普通请求必须显式 stream=false —— 否则统计时分不清「非流式」和「旧行没这个字段」。"""
    async def case():
        setup(monkeypatch, tmp_path)
        req = ASGIRequest({"model": "tts-1", "voice": "rachel", "input": "你好世界"})
        await req.run()
        assert req.status == 200
        row = ledger()[0]
        assert row["stream"] is False
        assert row["stream_format"] == "audio"
    asyncio.run(case())


def test_stream_true_without_format_is_flagged_ignored(monkeypatch, tmp_path):
    """`stream: true` 但没给 stream_format=sse 时按非流式返回，必须留标记。

    Dify / n8n 这类客户端默认带 stream:true，它们的首包延迟天然很差。
    没有这个标记，事后会被误判成适配器性能问题。
    """
    async def case():
        setup(monkeypatch, tmp_path)
        req = ASGIRequest({"model": "tts-1", "voice": "rachel",
                           "input": "你好世界", "stream": True})
        await req.run()
        assert req.status == 200
        row = ledger()[0]
        assert row["stream"] is False
        assert row["stream_ignored"] is True
    asyncio.run(case())


def test_ledger_records_real_upstream_model_id(monkeypatch, tmp_path):
    """model 是别名、model_id 必须是上游真模型，两者并存。"""
    async def case():
        setup(monkeypatch, tmp_path)
        req = ASGIRequest({"model": "tts-1", "voice": "rachel", "input": "你好世界"})
        await req.run()
        row = ledger()[0]
        assert row["model"] == "rachel", "model 字段保留客户端原值，便于还原调用方式"
        assert row["model_id"] and row["model_id"].startswith("eleven_"), \
            "model_id 必须是上游真模型 id，否则无法按档位算成本"
        assert row["model_id"] != row["model"]
    asyncio.run(case())


def _write_rows(day: str, rows):
    Path(billing.BILLING_LOG_DIR, "%s.jsonl" % day).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def _row(**kw):
    base = {"status": 200, "in_chars": 30, "sent_chars": 30, "bill_chars": 30,
            "up_credits": 7, "model": "rachel", "model_id": "eleven_flash_v2_5",
            "stream": True, "stream_format": "sse", "stream_ignored": False,
            "token": "sk_abc", "ts": 1}
    base.update(kw)
    return base


def test_stats_reports_sse_ratio_and_per_model_cost(monkeypatch, tmp_path):
    """聚合要能直接回答「流式占比」和「每个模型档位的单价」。"""
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    day = date.today().strftime("%Y-%m-%d")
    _write_rows(day, [
        _row(),
        _row(),
        _row(stream=False, stream_format="audio", up_credits=14,
             model_id="eleven_multilingual_v2"),
        _row(status=400, up_credits=None, bill_chars=0, stream=False,
             stream_format="audio", model_id="eleven_multilingual_v2"),
    ])

    s = billing.stats(day=day)
    assert s["requests"] == 4
    assert s["sse_requests"] == 2
    assert s["audio_requests"] == 2
    assert s["sse_ratio"] == 0.5

    by = s["by_model"]
    assert set(by) == {"eleven_flash_v2_5", "eleven_multilingual_v2"}
    assert by["eleven_flash_v2_5"]["requests"] == 2
    assert by["eleven_flash_v2_5"]["credits_per_char"] == round(14 / 60, 4)
    assert by["eleven_multilingual_v2"]["credits_per_char"] == round(14 / 30, 4)
    assert by["eleven_flash_v2_5"]["credits_per_char"] < \
        by["eleven_multilingual_v2"]["credits_per_char"], "flash 档必须显著便宜"


def test_window_stats_matches_single_day(monkeypatch, tmp_path):
    """单日 stats 与 1 天窗口必须逐项相等 —— 两处逻辑分叉过一次，不能再分叉。"""
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    day = date.today().strftime("%Y-%m-%d")
    _write_rows(day, [_row(), _row(stream=False, stream_format="audio")])
    a = billing.stats(day=day)
    b = billing.window_stats(days=1)
    for k in ("requests", "sse_requests", "audio_requests", "sse_ratio",
              "bill_chars", "up_credits", "credits_per_char", "by_model"):
        assert a[k] == b[k], "字段 %s 在两处聚合里不一致" % k


def test_legacy_rows_without_stream_field_do_not_crash(monkeypatch, tmp_path):
    """旧行（无 stream / model_id）不能把聚合打挂，按非流式算。"""
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    day = date.today().strftime("%Y-%m-%d")
    legacy = {"status": 200, "in_chars": 10, "sent_chars": 10, "bill_chars": 10,
              "up_credits": 5, "model": "shimmer", "token": "sk_old", "ts": 1}
    _write_rows(day, [legacy])
    s = billing.stats(day=day)
    assert s["requests"] == 1
    assert s["audio_requests"] == 1
    assert s["sse_requests"] == 0
    assert "shimmer" in s["by_model"], "缺 model_id 时退回 model，聊胜于无"
    assert s["by_model"]["shimmer"]["credits_per_char"] == 0.5


def test_usage_endpoint_exposes_stream_and_model(monkeypatch, tmp_path):
    """/v1/usage 是白名单返回，新增字段必须显式加进去才看得到。

    这里直接调 async 函数，不起服务：验证的是返回结构，不是传输层。
    """
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    day = date.today().strftime("%Y-%m-%d")
    _write_rows(day, [
        _row(),
        _row(stream=False, stream_format="audio", up_credits=14,
             model_id="eleven_multilingual_v2"),
    ])
    out = asyncio.run(m.usage(days=1))

    today = out["today"]
    assert today["sse_requests"] == 1
    assert today["audio_requests"] == 1
    assert today["sse_ratio"] == 0.5
    assert "eleven_flash_v2_5" in today["by_model"]
    assert today["by_model"]["eleven_flash_v2_5"]["credits_per_char"] is not None

    win = out["window"]
    assert win["sse_requests"] == 1
    assert "eleven_multilingual_v2" in win["by_model"]

    caller = out["by_caller"][0]
    assert caller["sse_requests"] == 1
    assert caller["sse_ratio"] == 0.5
    assert caller["models"] == ["eleven_flash_v2_5", "eleven_multilingual_v2"]


def test_usage_endpoint_exposes_stream_and_model(monkeypatch, tmp_path):
    """/v1/usage 是白名单返回，新增字段必须显式加进去才看得到。

    这里直接调 async 函数，不起服务：验证的是返回结构，不是传输层。
    """
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    day = date.today().strftime("%Y-%m-%d")
    _write_rows(day, [
        _row(),
        _row(stream=False, stream_format="audio", up_credits=14,
             model_id="eleven_multilingual_v2"),
    ])
    out = asyncio.run(m.usage(days=1))

    today = out["today"]
    assert today["sse_requests"] == 1
    assert today["audio_requests"] == 1
    assert today["sse_ratio"] == 0.5
    assert "eleven_flash_v2_5" in today["by_model"]
    assert today["by_model"]["eleven_flash_v2_5"]["credits_per_char"] is not None

    win = out["window"]
    assert win["sse_requests"] == 1
    assert "eleven_multilingual_v2" in win["by_model"]

    caller = out["by_caller"][0]
    assert caller["sse_requests"] == 1
    assert caller["sse_ratio"] == 0.5
    assert caller["models"] == ["eleven_flash_v2_5", "eleven_multilingual_v2"]

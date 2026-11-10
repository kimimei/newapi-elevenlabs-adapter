# -*- coding: utf-8 -*-
"""针对 PR #2 四处缺口的回归测试（本地修复分支 pr-2-fix）。

1. 流式也记上游 character-cost（原来恒为 None，L2 对账整批空白）
2. /healthz 不占并发槽、也不会被 429（长流会把 Dockerfile HEALTHCHECK 打死）
3. IP 白名单优先于并发闸门（未授权来源不该先看到 429）
4. Mock 模式能跑 SSE（原来直接 400，测试环境根本走不到流式链路）
"""
import asyncio
import base64
import json
from pathlib import Path

import httpx

from app import billing
from app import main as m
from app.elevenlabs_client import open_elevenlabs_tts_stream


class AudioStream(httpx.AsyncByteStream):
    def __init__(self, gate=None):
        self.gate = gate

    async def __aiter__(self):
        yield b"ID3-first"
        if self.gate:
            await self.gate.wait()
        yield b"last-audio"

    async def aclose(self):
        pass


class ASGIRequest:
    def __init__(self, payload, path="/v1/audio/speech", method="POST"):
        self.body = json.dumps(payload).encode()
        self.path = path
        self.method = method
        self.messages = []

    async def receive(self):
        if not getattr(self, "sent", False):
            self.sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await asyncio.Event().wait()

    async def send(self, message):
        self.messages.append(message)

    async def run(self):
        headers = [] if self.method == "GET" else [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(self.body)).encode())]
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": self.method, "scheme": "http",
                 "path": self.path, "raw_path": self.path.encode(), "query_string": b"",
                 "root_path": "", "server": ("testserver", 80),
                 "client": ("testclient", 1234), "headers": headers}
        await asyncio.wait_for(m.app(scope, self.receive, self.send), 5)

    @property
    def start(self):
        return next(msg for msg in self.messages if msg["type"] == "http.response.start")

    @property
    def status(self):
        return self.start["status"]

    @property
    def headers(self):
        return dict(self.start["headers"])

    @property
    def content(self):
        return b"".join(msg.get("body", b"") for msg in self.messages)

    @property
    def events(self):
        return [json.loads(line[6:]) for line in self.content.decode().splitlines()
                if line.startswith("data: ")]


def setup(monkeypatch, tmp_path, concurrency=1, billing_mode="input", mock=False):
    monkeypatch.setattr(m, "MOCK", mock)
    monkeypatch.setattr(m, "ALLOW_IPS", [])
    monkeypatch.setattr(m, "MAX_CONCURRENCY", concurrency)
    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **k: "offline_key")
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "BILLING_MODE", billing_mode)
    m._CONCURRENCY[0] = 0


def patch_upstream(monkeypatch, gate=None, cost="3"):
    def handler(request):
        headers = {"request-id": "up-rid-1"}
        if cost is not None:
            headers["character-cost"] = cost
        return httpx.Response(200, stream=AudioStream(gate), headers=headers)

    async def open_stream(*args, **kwargs):
        return await open_elevenlabs_tts_stream(
            *args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(m, "open_elevenlabs_tts_stream", open_stream)


def ledger():
    return [json.loads(line) for path in Path(billing.BILLING_LOG_DIR).glob("*.jsonl")
            for line in path.read_text().splitlines()]


SSE_PAYLOAD = {"model": "tts-1", "voice": "rachel", "input": "你好世界",
               "stream_format": "sse", "stream": True}


def test_sse_records_upstream_credits(monkeypatch, tmp_path):
    """上游给了 character-cost=3，账本就必须是 3，不能是 None。"""
    async def case():
        setup(monkeypatch, tmp_path)
        patch_upstream(monkeypatch, cost="3")
        req = ASGIRequest(SSE_PAYLOAD)
        await req.run()
        rows = ledger()
        assert len(rows) == 1
        assert rows[0]["status"] == 200
        assert rows[0]["up_credits"] == 3, "流式也必须记上游真实积分"
        assert rows[0]["up_rid"] == "up-rid-1"
        assert req.headers[b"x-usage-upstream-credits"] == b"3"
    asyncio.run(case())


def test_sse_upstream_mode_bills_by_credits(monkeypatch, tmp_path):
    """BILLING_MODE=upstream 时，流式请求要按上游积分计费，而不是偷偷退回字符数。"""
    async def case():
        setup(monkeypatch, tmp_path, billing_mode="upstream")
        patch_upstream(monkeypatch, cost="3")
        req = ASGIRequest(SSE_PAYLOAD)
        await req.run()
        row = ledger()[0]
        assert row["bill_source"] == "upstream"
        assert row["bill_chars"] == 3
        assert "upstream" in row["bill_rule"]
    asyncio.run(case())


def test_healthz_bypasses_full_concurrency_gate(monkeypatch, tmp_path):
    """一个流占满唯一槽位时，/healthz 仍须 200，且不占槽。"""
    async def case():
        setup(monkeypatch, tmp_path, concurrency=1)
        gate = asyncio.Event()
        patch_upstream(monkeypatch, gate=gate)
        stream_req = ASGIRequest(SSE_PAYLOAD)
        task = asyncio.create_task(stream_req.run())
        await asyncio.sleep(0.2)
        assert m._inflight() == 1

        health = ASGIRequest({}, path="/healthz", method="GET")
        await health.run()
        assert health.status == 200, "健康检查不能被并发闸门打成 429"
        assert m._inflight() == 1, "豁免路径不该占用业务槽位"

        gate.set()
        await task
        assert m._inflight() == 0
    asyncio.run(case())


def test_whitelist_403_wins_over_429(monkeypatch, tmp_path):
    """并发打满时，未授权来源必须仍是 403，而不是先看到 429。"""
    async def case():
        setup(monkeypatch, tmp_path, concurrency=1)
        gate = asyncio.Event()
        patch_upstream(monkeypatch, gate=gate)
        stream_req = ASGIRequest(SSE_PAYLOAD)
        task = asyncio.create_task(stream_req.run())
        await asyncio.sleep(0.2)
        assert m._inflight() == 1

        monkeypatch.setattr(m, "ALLOW_IPS", ["203.0.113.9"])
        blocked = ASGIRequest(SSE_PAYLOAD)
        await blocked.run()
        assert blocked.status == 403, "白名单必须在并发闸门之外"

        monkeypatch.setattr(m, "ALLOW_IPS", [])
        full = ASGIRequest(SSE_PAYLOAD)
        await full.run()
        assert full.status == 429, "白名单放行后仍应被并发闸门挡住"

        gate.set()
        await task
    asyncio.run(case())


def test_mock_sse_streams_wav_deltas(monkeypatch, tmp_path):
    """Mock 模式能走 SSE：两段 delta 的字节拼起来是一段完整 WAV，且标明 X-Mock-Format。"""
    async def case():
        setup(monkeypatch, tmp_path, mock=True)
        req = ASGIRequest(SSE_PAYLOAD)
        await req.run()
        assert req.status == 200
        assert req.headers[b"content-type"].startswith(b"text/event-stream")
        assert req.headers[b"x-mock-format"] == b"wav"
        events = req.events
        assert [e["type"] for e in events] == ["speech.audio.delta", "speech.audio.delta",
                                               "speech.audio.done"]
        audio = b"".join(base64.b64decode(e["audio"]) for e in events[:-1])
        assert audio.startswith(b"RIFF"), "mock 产出的必须是 WAV，不是伪装的 MP3"
        assert events[-1]["usage"]["input_tokens"] == len("你好世界")
        rows = ledger()
        assert len(rows) == 1 and rows[0]["status"] == 200
    asyncio.run(case())

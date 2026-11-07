"""离线 MockTransport + 直接 ASGI 驱动；不用会缓冲完整响应的 ASGITransport 证明首包。"""
import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from app import billing, main as m
from app.elevenlabs_client import open_elevenlabs_tts_stream


class AudioStream(httpx.AsyncByteStream):
    def __init__(self, gate=None, fail=False):
        self.gate = gate
        self.fail = fail
        self.closed = False
        self.tail = False
        self.started = False

    async def __aiter__(self):
        self.started = True
        yield b"ID3-first"
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise httpx.ReadError("断流测试")
        self.tail = True
        yield b"last-audio"

    async def aclose(self):
        self.closed = True


class ASGIRequest:
    def __init__(self, payload=None, fail_send=False):
        payload = payload or {"model": "tts-1", "voice": "rachel", "input": "  中😀\u200b文  ",
                              "stream_format": "sse", "stream": True}
        self.body = json.dumps(payload).encode()
        self.messages = []
        self.first = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.fail_send = fail_send
        self.received = False

    async def receive(self):
        if not self.received:
            self.received = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "http.response.body" and b"speech.audio.delta" in message.get("body", b""):
            self.first.set()
            if self.fail_send:
                raise OSError("客户端已关闭")

    async def run(self):
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/v1/audio/speech",
                 "raw_path": b"/v1/audio/speech", "query_string": b"", "root_path": "",
                 "server": ("testserver", 80), "client": ("testclient", 1234),
                 "headers": [(b"content-type", b"application/json"),
                             (b"content-length", str(len(self.body)).encode())]}
        await asyncio.wait_for(m.app(scope, self.receive, self.send), 5)

    @property
    def start(self):
        return next(msg for msg in self.messages if msg["type"] == "http.response.start")

    @property
    def content(self):
        return b"".join(msg.get("body", b"") for msg in self.messages)

    @property
    def events(self):
        return [json.loads(line[6:]) for line in self.content.decode().splitlines() if line.startswith("data: ")]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "MOCK", False)
    monkeypatch.setattr(m, "ALLOW_IPS", [])
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 1)
    monkeypatch.setattr(m, "resolve_api_key", lambda *args: "offline_test_key")
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "BILLING_MODE", "input")
    m._CONCURRENCY[0] = 0
    yield
    m._CONCURRENCY[0] = 0


def ledger():
    return [json.loads(line) for path in Path(billing.BILLING_LOG_DIR).glob("*.jsonl")
            for line in path.read_text().splitlines()]


def upstream(monkeypatch, stream, status=200):
    calls, clients = [], []

    def handler(request):
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"detail": {"message": "测试上游失败"}},
                                  headers={"request-id": "up-test"})
        return httpx.Response(200, stream=stream, headers={"request-id": "up-test"})

    async def open_stream(*args):
        result = await open_elevenlabs_tts_stream(*args, transport=httpx.MockTransport(handler))
        clients.append(result[1])
        return result

    monkeypatch.setattr(m, "open_elevenlabs_tts_stream", open_stream)
    return calls, clients


def test_first_delta_before_tail_and_slot_held_until_end(monkeypatch):
    async def case():
        gate = asyncio.Event()
        audio = AudioStream(gate)
        calls, clients = upstream(monkeypatch, audio)
        req = ASGIRequest()
        task = asyncio.create_task(req.run())
        try:
            await asyncio.wait_for(req.first.wait(), 2)
            assert not audio.tail, "首包必须在上游尾块放行前发出"
            assert m._inflight() == 1
            assert ledger() == [], "不能在路由返回时提前记成功"
            second = ASGIRequest()
            await second.run()
            assert second.start["status"] == 429
            gate.set()
            await task
        finally:
            gate.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert req.start["status"] == 200
        headers = dict(req.start["headers"])
        assert headers[b"content-type"].startswith(b"text/event-stream")
        assert b"content-length" not in headers
        assert b"x-stream-ignored" not in headers
        events = req.events
        assert [e["type"] for e in events] == ["speech.audio.delta", "speech.audio.delta", "speech.audio.done"]
        assert b"".join(base64.b64decode(e["delta"]) for e in events[:-1]) == b"ID3-firstlast-audio"
        raw = json.loads(req.body)["input"]
        assert events[-1]["usage"]["input_tokens"] == len(raw)
        assert events[-1]["usage"]["total_tokens"] == len(raw)
        assert audio.closed and clients[0].is_closed
        assert m._inflight() == 0
        assert len(calls) == 1
        request = calls[0]
        assert request.url.path.endswith("/stream")
        assert request.url.params["output_format"] == "mp3_44100_128"
        payload = json.loads(request.content)
        assert payload["text"] != raw  # 清理不能改变计费依据
        rows = ledger()
        assert len(rows) == 1
        assert rows[0]["status"] == 200
        assert rows[0]["bill_chars"] == rows[0]["in_chars"] == len(raw)
        assert rows[0]["sent_chars"] == len(payload["text"])
        assert rows[0]["up_credits"] is None and rows[0]["up_rid"] == "up-test"
    asyncio.run(case())


@pytest.mark.parametrize("status,expected", [(401, 401), (402, 403), (422, 400), (429, 429), (503, 502)])
def test_upstream_header_failure_is_json_before_sse(monkeypatch, status, expected):
    async def case():
        calls, clients = upstream(monkeypatch, AudioStream(), status)
        req = ASGIRequest()
        await req.run()
        assert req.start["status"] == expected
        assert dict(req.start["headers"])[b"content-type"].startswith(b"application/json")
        assert "测试上游失败" in json.loads(req.content)["error"]["message"]
        assert len(calls) == 1 and clients[0].is_closed
        rows = ledger()
        assert len(rows) == 1 and rows[0]["status"] == expected
        assert rows[0]["bill_chars"] == 0 and rows[0]["up_credits"] is None
        assert m._inflight() == 0
    asyncio.run(case())


def test_midstream_error_emits_error_not_done_and_single_failed_row(monkeypatch):
    async def case():
        audio = AudioStream(fail=True)
        calls, clients = upstream(monkeypatch, audio)
        req = ASGIRequest()
        await req.run()
        assert [e["type"] for e in req.events] == ["speech.audio.delta", "error"]
        assert audio.closed and clients[0].is_closed and len(calls) == 1
        rows = ledger()
        assert len(rows) == 1 and rows[0]["status"] == 502
        assert rows[0]["bill_chars"] == 0 and rows[0]["up_credits"] is None
        assert m._inflight() == 0
    asyncio.run(case())


@pytest.mark.parametrize("method", ["disconnect", "cancel", "send_failure"])
def test_client_interruption_closes_upstream_and_records_cancel(monkeypatch, method):
    async def case():
        audio = AudioStream(asyncio.Event())
        calls, clients = upstream(monkeypatch, audio)
        req = ASGIRequest(fail_send=method == "send_failure")
        task = asyncio.create_task(req.run())
        await asyncio.wait_for(req.first.wait(), 2)
        if method == "disconnect":
            req.disconnect.set()
        elif method == "cancel":
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert audio.closed and clients[0].is_closed and len(calls) == 1
        assert b"speech.audio.done" not in req.content
        rows = ledger()
        assert len(rows) == 1 and rows[0]["status"] == 499
        assert rows[0]["bill_chars"] == 0 and rows[0]["up_credits"] is None
        assert m._inflight() == 0
    asyncio.run(case())


@pytest.mark.parametrize("fmt", ["wav", "pcm", "opus"])
def test_sse_rejects_non_mp3_without_opening_upstream(monkeypatch, fmt):
    async def case():
        calls, _ = upstream(monkeypatch, AudioStream())
        req = ASGIRequest({"input": "你好", "voice": "rachel", "stream_format": "sse", "response_format": fmt})
        await req.run()
        assert req.start["status"] == 400
        assert b"RIFF" in req.content and calls == []
        assert len(ledger()) == 1 and ledger()[0]["bill_chars"] == 0
    asyncio.run(case())


def test_open_stream_does_not_read_body_and_preserves_payload():
    async def case():
        audio = AudioStream()
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, stream=audio)
        response, client = await open_elevenlabs_tts_stream(
            "原文", "eleven_v3", "test_voice_123", "offline_key", "mp3_44100_128", 3.0,
            {"lang": "zh", "voice_settings": {"stability": 0.5}}, httpx.MockTransport(handler))
        try:
            assert not audio.started and not response.is_stream_consumed
            request = captured[0]
            assert request.method == "POST"
            assert request.url.path.endswith("/text-to-speech/test_voice_123/stream")
            payload = json.loads(request.content)
            assert payload["text"] == "原文" and payload["model_id"] == "eleven_v3"
            assert payload["language"] == "zh"
            assert payload["voice_settings"] == {"stability": 0.5, "speed": 1.2}
            assert request.headers["xi-api-key"] == "offline_key"
        finally:
            await response.aclose()
            await client.aclose()
        assert audio.closed and client.is_closed
    asyncio.run(case())


def test_slot_held_while_final_downstream_send_is_blocked(monkeypatch):
    async def case():
        upstream(monkeypatch, AudioStream())
        req = ASGIRequest()
        original_send = req.send
        reached = asyncio.Event()
        release = asyncio.Event()
        async def slow_send(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                reached.set()
                await release.wait()
            await original_send(message)
        req.send = slow_send
        task = asyncio.create_task(req.run())
        try:
            await asyncio.wait_for(reached.wait(), 2)
            assert m._inflight() == 1, "内存转发完成不能提前释放实际下游发送的槽"
            second = ASGIRequest()
            await second.run()
            assert second.start["status"] == 429
        finally:
            release.set()
            await task
        assert m._inflight() == 0
    asyncio.run(case())


def test_open_stream_network_failure_never_retries():
    async def case():
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ConnectError("离线失败")
        with pytest.raises(httpx.ConnectError):
            await open_elevenlabs_tts_stream("你好", "eleven_v3", "test_voice_123", "offline_key",
                                            "mp3_44100_128", None, transport=httpx.MockTransport(handler))
        assert len(calls) == 1
    asyncio.run(case())

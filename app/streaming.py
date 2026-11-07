"""SSE 音频传输；资源和账本属于整个 ASGI 响应，而不是路由返回时刻。"""
import base64
import json
import logging

import anyio
from starlette.responses import StreamingResponse

logger = logging.getLogger("elevenlabs-adapter")


def _event(kind: str, **data) -> bytes:
    payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
    return f"event: {kind}\ndata: {payload}\n\n".encode("utf-8")


class SpeechStreamResponse(StreamingResponse):
    def __init__(self, upstream, client, raw_chars: int, bill, up_rid: str | None):
        self.upstream = upstream
        self.client = client
        self.bill = bill
        self.up_rid = up_rid
        self.outcome = 499
        self.failure = "客户端取消流式请求"
        self.raw_chars = raw_chars
        self.finished = False
        super().__init__(self._events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    async def _events(self):
        try:
            # 不指定 chunk_size：不能等缓冲区装满才给下游首块。
            async for chunk in self.upstream.aiter_bytes():
                if chunk:
                    yield _event("speech.audio.delta", audio=base64.b64encode(chunk).decode("ascii"))
        except Exception as exc:
            self.outcome = 502
            self.failure = f"{type(exc).__name__}: {exc}"
            logger.warning("上游音频流中断：%s", self.failure)
            yield _event("error", error={"message": "上游音频流中断", "type": "upstream_error",
                                         "code": "upstream_error"})
            return
        yield _event("speech.audio.done", usage={
            "input_tokens": self.raw_chars, "output_tokens": 0, "total_tokens": self.raw_chars,
            "input_token_details": {"text_tokens": self.raw_chars, "audio_tokens": 0},
        })
        self.outcome = 200
        self.failure = None

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            # 包括客户端取消和 send() 失败；头已经发出，不能改 HTTP 状态。
            self.outcome = 499
            self.failure = "客户端取消流式请求"
            raise

    async def finish(self, interrupted: bool = False):
        if self.finished:
            return
        self.finished = True
        if interrupted:
            self.outcome = 499
            self.failure = "客户端取消流式请求"
        # disconnect 会取消任务；关闭连接必须受保护，且即使尚未迭代首块也要执行。
        with anyio.CancelScope(shield=True):
            try:
                await self.upstream.aclose()
            finally:
                try:
                    await self.client.aclose()
                finally:
                    # 不拿输入长度冒充上游积分；失败/取消仍按既有规则记 0。
                    self.bill(self.outcome, up_credits=None, up_rid=self.up_rid, error=self.failure)

# -*- coding: utf-8 -*-
"""在线试听（Demo）—— 免注册、有护栏。

为什么要有护栏
--------------
这个端点用的是**服务端配置的 Key**，等于把额度公开。没有限制的话，
一条 `for` 循环就能把一个月额度刷光。四道闸门：

  1) 默认关闭（ADAPTER_DEMO_ENABLED=1 才开）
  2) 单次文本 ≤ 20 字符（够听音色，不够白嫖配音）
  3) 每 IP 每小时 N 次（默认 20）
  4) 只允许目录里指定的几个音色（不允许传任意 voice_id）
"""
import io
import json
import logging
import os
import time
import wave

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .config import MOCK
from .elevenlabs_client import call_elevenlabs_tts, resolve_api_key, resolve_voice_and_model
from .voices import load_voices

logger = logging.getLogger("elevenlabs-adapter.demo")

router = APIRouter()
VOICES = load_voices()

DEMO_ENABLED = os.getenv("ADAPTER_DEMO_ENABLED", "").lower() in ("1", "true", "yes")
DEMO_MAX_CHARS = int(os.getenv("ADAPTER_DEMO_MAX_CHARS") or 20)
DEMO_PER_HOUR = int(os.getenv("ADAPTER_DEMO_PER_HOUR") or 20)
_DEFAULT_DEMO_VOICES = ["zh-yingshi", "zh-boyin", "zh-pangbai"]
DEMO_VOICES = [v.strip() for v in
               (os.getenv("ADAPTER_DEMO_VOICES")
                or ",".join(_DEFAULT_DEMO_VOICES)).split(",") if v.strip()]

# ADAPTER_DEMO_VOICES 写成 "," 或全空白时会解析出空列表，
# 下面取 DEMO_VOICES[0] 会 IndexError -> 500。这里兜底回默认音色并告警。
if not DEMO_VOICES:
    DEMO_VOICES = list(_DEFAULT_DEMO_VOICES)
    logger.warning(
        "ADAPTER_DEMO_VOICES 解析为空（检查是否只有分隔符/空格），已回落到默认音色：%s",
        ",".join(_DEFAULT_DEMO_VOICES))

_hits = {}          # ip -> [timestamp, ...]


def _err(message: str, fix: str | None = None) -> str:
    """统一生成 OpenAI 风格错误体。

    必须走 json.dumps：文案里可能带 voice 原值/上游原文，
    手拼 JSON 字面量遇到引号、反斜杠、换行就产出无法解析的响应体。
    """
    err = {"message": message}
    if fix:
        err["fix"] = fix
    return json.dumps({"error": err}, ensure_ascii=False)


def _mock_wav(seconds=1.0, rate=24000):
    """Mock 模式返回静音 WAV：让本地/演示环境不依赖 Key 也能点通。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _allow(ip):
    now = time.time()
    bucket = [t for t in _hits.get(ip, []) if now - t < 3600]
    if len(bucket) >= DEMO_PER_HOUR:
        _hits[ip] = bucket
        return False, 0
    bucket.append(now)
    _hits[ip] = bucket
    # 顺手清掉没请求过的 IP：dict 只增不减的话，扫一遍来源 IP 就能把内存撑起来
    if len(_hits) > 1024:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] >= 3600]:
            _hits.pop(k, None)
    return True, DEMO_PER_HOUR - len(bucket)


class DemoRequest(BaseModel):
    text: str = Field(..., max_length=200)
    voice: str | None = None


@router.get("/v1/demo/config")
async def demo_config():
    return {
        "enabled": DEMO_ENABLED,
        "max_chars": DEMO_MAX_CHARS,
        "per_hour": DEMO_PER_HOUR,
        "voices": [{"name": v, "label": _label(v)} for v in DEMO_VOICES],
    }


def _label(name):
    spec = VOICES.aliases.get(name) or {}
    return spec.get("label") or name


@router.post("/v1/demo/speech")
async def demo_speech(req: DemoRequest, request: Request):
    if not DEMO_ENABLED:
        return Response(_err("在线试听未开启", "在 .env 设置 ADAPTER_DEMO_ENABLED=1"),
                        status_code=404, media_type="application/json")

    ip = request.client.host if request.client else "unknown"
    ok, left = _allow(ip)
    if not ok:
        return Response(_err("试听次数已用完",
                             "每 IP 每小时 %d 次，稍后再试" % DEMO_PER_HOUR),
                        status_code=429, media_type="application/json",
                        headers={"Retry-After": "3600"})

    text = (req.text or "").strip()
    if not text:
        return Response(_err("text 不能为空"), status_code=400,
                        media_type="application/json")
    if len(text) > DEMO_MAX_CHARS:
        text = text[:DEMO_MAX_CHARS]

    voice = req.voice if req.voice in DEMO_VOICES else DEMO_VOICES[0]
    try:
        voice_id, model_id, extra = resolve_voice_and_model(voice, None, VOICES)
    except ValueError as e:
        # 用 json.dumps 而不是 %s 拼串：报错文案里带 voice 原值，
        # 直接插进 JSON 字面量会产出非法 JSON（引号/反斜杠即失效）。
        return Response(_err(str(e)), status_code=400, media_type="application/json")

    if MOCK:
        return Response(content=_mock_wav(), media_type="audio/wav",
                        headers={"X-Mock": "true", "X-Demo-Chars": str(len(text))})

    key = resolve_api_key(None, None, None)
    if not key:
        return Response(_err("服务端未配置 Key", "设置 ELEVENLABS_API_KEY 后重启"),
                        status_code=500, media_type="application/json")

    try:
        up = await call_elevenlabs_tts(text, model_id, voice_id, key, "mp3_44100_128", None, extra)
    except Exception as e:                          # noqa: BLE001
        logger.warning("demo upstream call failed (voice=%s): %s", voice, e)
        return Response(_err("上游调用失败，稍后再试"), status_code=502,
                        media_type="application/json")
    if up.status_code != 200:
        # 不把上游原文回给公众端：可能含账户/配额等内部信息。细节只进服务端日志。
        logger.warning("demo upstream returned %d: %s", up.status_code, up.text[:300])
        return Response(_err("上游返回 %d" % up.status_code), status_code=502,
                        media_type="application/json")

    return Response(content=up.content, media_type="audio/mpeg",
                    headers={"X-Demo-Chars": str(len(text)),
                             "X-Demo-Quota-Left": str(left),
                             "Cache-Control": "no-store"})

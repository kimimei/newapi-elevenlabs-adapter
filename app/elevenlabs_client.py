# -*- coding: utf-8 -*-
"""ElevenLabs 上游调用与 OpenAI 请求转换。

核心转换（"不好集成"的根因都在这里解决）：
  OpenAI: POST /v1/audio/speech  {model, input, voice, response_format, speed}
  ElevenLabs: POST /v1/text-to-speech/{voice_id}  {text, model_id, voice_settings, output_format}
  鉴权: Authorization: Bearer <key>  ->  xi-api-key: <key>
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import API_BASE, ALLOW_LOCAL_KEY, DEFAULT_MODEL, MAX_RETRIES, MOCK, TIMEOUT

logger = logging.getLogger("elevenlabs-adapter")

# OpenAI response_format -> (ElevenLabs output_format, Content-Type)
# ElevenLabs 枚举无 aac/flac，故这两项不支持。
# wav 在适配器内由裸 PCM 包成 RIFF 容器（见 main.py），pcm 保持裸流。
FORMAT_MAP = {
    "mp3": ("mp3_44100_128", "audio/mpeg"),
    "opus": ("opus_48000_128", "audio/ogg"),
    "wav": ("pcm_24000", "audio/wav"),
    "pcm": ("pcm_24000", "audio/L16; rate=24000"),
    "aac": None,
    "flac": None,
}

# voice_id 会被拼进上游 URL 路径，必须严格校验：
# 一旦允许 "/"、".." 之类的字符，调用方就能把请求指到 api.elevenlabs.io 的任意路径上，
# 且用的是服务端配置的 Key（路径穿越 / 越权调用）。
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")

# ElevenLabs voice_settings.speed 的实际接受区间
SPEED_MIN, SPEED_MAX = 0.7, 1.2


def clamp_speed(speed: float | None) -> tuple[float | None, bool]:
    """把 OpenAI 语速区间(0.25–4.0)钳制到 ElevenLabs 可接受区间(0.7–1.2)。

    返回 (实际下发值, 是否发生钳制)。上游对越界值直接报 400，
    故此处不报错而是钳制，并通过响应头告知调用方。
    """
    if speed is None:
        return None, False
    if speed < SPEED_MIN:
        return SPEED_MIN, True
    if speed > SPEED_MAX:
        return SPEED_MAX, True
    return speed, False


def _normalize_key(raw: str | None) -> str | None:
    """清洗渠道密钥：去空白、剥 Bearer、剥 sk- 外壳、多 key 取第一个。

    中转站透传密钥的形态并不统一：
      - new-api / one-api：Authorization: Bearer sk_xxx（ElevenLabs 原生 sk_ 下划线）
      - 部分面板：多 key 用逗号分隔，或额外套一层 sk- 短横线前缀
    只剥短横线 sk-（OpenAI 风格），保留下划线 sk_（ElevenLabs 原生）。
    """
    if not raw:
        return None
    k = raw.strip()
    if k.lower().startswith("bearer "):
        k = k[7:].strip()
    if "," in k:
        k = k.split(",", 1)[0].strip()
    if k.startswith("sk-"):
        k = k[3:]
    return k or None


def resolve_api_key(
    authorization: str | None,
    x_api_key: str | None = None,
    api_key_header: str | None = None,
) -> str | None:
    """Key 解析优先级：
    1) Authorization: Bearer <key>（new-api / one-api 主路径）
    2) x-api-key / api-key 头（部分中转站与 OpenAI 官方 SDK 的变体）
    3) 环境变量 ELEVENLABS_API_KEY
    4) 本地 ~/.elevenlabs/config.json 明文 api_key（兼容内部配置）
    """
    for raw in (authorization, x_api_key, api_key_header):
        k = _normalize_key(raw)
        if k:
            return k
    k = os.getenv("ELEVENLABS_API_KEY")
    if k:
        return k
    if not ALLOW_LOCAL_KEY:
        return None
    try:
        cfg_path = Path.home() / ".elevenlabs" / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            # key_manager v2 用 "enc": "dpapi" 标记加密；旧版明文无此字段
            enc = cfg.get("enc") or cfg.get("encrypted")
            k = cfg.get("api_key")
            if k and not enc:
                return k
            if k and enc:
                logger.warning(
                    "检测到 ~/.elevenlabs/config.json 为加密存储(enc=%s)，MVP 不支持解密；"
                    "请改用请求头 Authorization: Bearer <key> 或环境变量 ELEVENLABS_API_KEY", enc
                )
    except Exception:
        pass
    return None


def _check_voice_id(voice_id: str | None, source: str) -> str | None:
    """voice_id 会被拼进上游 URL 路径，任何来源都必须过这一关。"""
    if voice_id and not _VOICE_ID_RE.match(voice_id):
        raise ValueError(
            "voice_id 不合法（来源：%s）：只允许字母、数字、下划线、连字符（10–64 位）。"
            "收到：%r" % (source, voice_id[:40]))
    return voice_id


def resolve_voice_and_model(model: str, voice: str | None, voices) -> tuple[str | None, str, dict]:
    """返回 (voice_id, model_id, extra)。voice_id 可能为 None（此时调用方应报 400）。

    extra 为预设别名携带的默认配置：{"lang": "zh", "voice_settings": {...}}。

    判定顺序：
      1) body.voice 显式指定（音色名或 voice_id）
      2) model 命中 aliases（模型名即音色，可含中文预设）
      3) model 本身是 ElevenLabs 模型名（eleven_*）
      4) model 命中 model_alias（OpenAI 兼容名）
    """
    voice_id = None
    if voice:
        voice_id = _check_voice_id(voices.voice_to_id(voice), "body.voice")

    alias = voices.aliases.get(model)
    if alias:
        extra = {}
        if alias.get("lang"):
            extra["lang"] = alias["lang"]
        if alias.get("voice_settings"):
            extra["voice_settings"] = dict(alias["voice_settings"])
        # 别名分支提前 return，voice_id 必须在这里再校验一次。
        # voices.json 是仓内文件但可编辑——一旦有人写进一个带斜杠的 voice_id，
        # 这条路径就成了绕过 body.voice 校验的口子。
        picked = _check_voice_id(voice_id or alias.get("voice_id"), "aliases[%s]" % model)
        return picked, alias.get("model") or DEFAULT_MODEL, extra

    if model.startswith("eleven_") or model in ("elevenlabs-tts",):
        return voice_id, model, {}

    return voice_id, voices.model_alias.get(model, DEFAULT_MODEL), {}


def build_payload(text: str, model_id: str, output_format: str, speed: float | None,
                  extra: dict | None = None) -> dict:
    payload = {"text": text, "model_id": model_id, "output_format": output_format}
    extra = extra or {}
    if extra.get("lang"):
        payload["language"] = extra["lang"]
    voice_settings = dict(extra.get("voice_settings") or {})
    clamped, _ = clamp_speed(speed)
    if clamped is not None:
        voice_settings["speed"] = clamped
    if voice_settings:
        payload["voice_settings"] = voice_settings
    return payload


async def call_elevenlabs_tts(text: str, model_id: str, voice_id: str, api_key: str,
                              output_format: str, speed: float | None,
                              extra: dict | None = None) -> httpx.Response:
    """调用 POST /v1/text-to-speech/{voice_id}，返回原始 httpx.Response。

    **超时一律不重试**（关键）：上游是「合成完才返回」，客户端超时只代表自己不等了，
    上游那一侧通常已经合成完毕并扣了费。重试 = 再扣一次。实测 87% 的字符消耗
    来自超时重试（3000 字 × 120s 超时 × 3 次 = 扣 3 次钱，只拿到 0 份音频）。

    其余失败（网络中断 / 5xx / 429）仍按 ELEVENLABS_MAX_RETRIES 重试：
    这些情况下上游大概率没有完成合成，重试相对安全。
    """
    # 双保险：即使上游校验被绕过，也不让 voice_id 改写路径
    url = f"{API_BASE}/text-to-speech/{quote(voice_id, safe='')}"
    payload = build_payload(text, model_id, output_format, speed, extra)
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    attempts = max(0, MAX_RETRIES) + 1
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for i in range(attempts):
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException:
                # 不重试。上游可能已经合成并计费，重试就是重复付费。
                logger.error(
                    "upstream timeout after %.0fs, NOT retrying "
                    "(TTS is billed on completion upstream; retry would charge again). "
                    "Shorten the input or raise ELEVENLABS_TIMEOUT.",
                    TIMEOUT)
                raise
            except httpx.HTTPError as e:
                last_exc = e
                if i == attempts - 1:
                    raise
                logger.warning("upstream network error (attempt %d/%d): %s", i + 1, attempts, e)
            else:
                if resp.status_code < 500 and resp.status_code != 429:
                    return resp
                if i == attempts - 1:
                    return resp
                logger.warning("upstream %d (attempt %d/%d), retrying",
                               resp.status_code, i + 1, attempts)
            await asyncio.sleep(0.4 * (3 ** i))
    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable")


def extract_character_cost(resp: httpx.Response) -> int | None:
    """读上游回传的 character-cost —— 这是**上游实际扣掉的额度**，不是我们估的。

    ElevenLabs 在 TTS 响应头里给 `character-cost`。实测它与输入长度的关系是
    `cost = floor(len(text) / 2)`（中文英文都一样），即上游按半字符计费。
    自己用 len(input) 估会多报一倍，客户按这个数被扣钱就是双倍收费。

    拿不到（老版本上游 / 代理剥头 / MOCK）时返回 None，由调用方决定兜底。
    """
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    raw = headers.get("character-cost") or headers.get("Character-Cost")
    if raw is None:
        return None
    try:
        v = int(float(raw.strip()))
    except (ValueError, AttributeError):
        return None
    return v if v >= 0 else None


def upstream_error_message(resp: httpx.Response) -> str:
    """提取 ElevenLabs 错误体中的可读信息（detail 可能是对象或字符串）。"""
    try:
        body = resp.json()
    except Exception:
        return f"ElevenLabs 上游错误 HTTP {resp.status_code}"
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        return f"ElevenLabs: {detail.get('message') or detail.get('status') or str(detail)}"
    if isinstance(detail, str):
        return f"ElevenLabs: {detail}"
    return f"ElevenLabs 上游错误 HTTP {resp.status_code}"

# -*- coding: utf-8 -*-
"""音色目录增强：给每个音色补一个可试听的 preview_url。

ElevenLabs 的 `GET /v1/voices` 会返回 `preview_url`（上游 CDN 链接）。
中转站客户选音色时最大的障碍是「听得见才敢选」，所以这里把它带出来。

约束
----
* 缓存 1 小时：上游这个接口有频率限制，且 preview_url 本身有效期很长
* 拿不到就当没有（返回空 dict），**不让目录接口因为上游抖动而 500**
* 只读，不落盘；Key 用完即弃，不记录
"""
import logging
import time

import httpx

from .config import API_BASE

logger = logging.getLogger("elevenlabs-adapter")

_TTL = 3600
_cache = {"ts": 0.0, "data": {}}


async def fetch_previews(api_key: str, ttl: int = _TTL) -> dict:
    """返回 {voice_id: preview_url}。失败时返回上一次的缓存（可能为空）。"""
    if not api_key:
        return {}
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < ttl:
        return _cache["data"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                API_BASE + "/voices",
                params={"page_size": 100},
                headers={"xi-api-key": api_key},
            )
        if resp.status_code != 200:
            logger.warning("取音色预览失败：HTTP %s", resp.status_code)
            return _cache["data"]
        body = resp.json()
        data = {}
        for v in body.get("voices") or []:
            vid = v.get("voice_id")
            if vid:
                data[vid] = v.get("preview_url")
        _cache["ts"] = now
        _cache["data"] = data
        return data
    except Exception as e:                     # noqa: BLE001
        logger.warning("取音色预览异常：%s", e)
        return _cache["data"]


def clear_cache():
    """测试用。"""
    _cache["ts"] = 0.0
    _cache["data"] = {}

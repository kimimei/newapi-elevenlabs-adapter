# -*- coding: utf-8 -*-
"""T5 · 长文本异步任务。

为什么需要
----------
配音客户的第一单通常就是几千字（一条口播、一段有声书章节）。
同步接口在两种情况下必然失败：

  1. 中转站 / 网关的请求超时通常 60~120秒，几千字的合成远超这个时间
  2. ElevenLabs 单次请求的字符上限本身就在几千字量级

于是客户看到的是「提交成功 → 等30 秒 → 504」，第一次接触就流失。

怎么做
------
    POST /v1/audio/speech/jobs      提交任务，立刻返回 job_id
    GET  /v1/audio/speech/jobs/{id} 轮询状态，结果好了给音频
    DELETE /v1/audio/speech/jobs/{id} 取消排队中的任务

为什么不内置队列
----------------
Redis / Celery 对一个「给中转站用的适配器」太重了。这个实现只用标准库：
内存任务表 + asyncio后台协程。代价说在前面：

  * 进程重启，排队中的任务全部丢失（已完成的不受影响，因为结果在调用方手里）
  * 多副本部署时，轮询请求必须打到提交时的那个副本（否则查不到 job）

  多副本的正确用法：要么单副本，要么前面挂 sticky session。
  真要横向扩展，把 `jobs` 换成 Redis 实现即可 —— 接口契约不用变。

默认关闭：ADAPTER_JOBS_ENABLED=1 才开。
"""
import asyncio
import logging
import os
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .config import (
    JOBS_ENABLED,
    TEXT_CLEAN,
    JOBS_MAX_CHARS,
    JOBS_MAX_PENDING,
    JOBS_TTL_SECONDS,
    JOBS_WORKERS,
)
from .elevenlabs_client import (
    FORMAT_MAP,
    call_elevenlabs_tts,
    clamp_speed,
    resolve_api_key,
    resolve_voice_and_model,
    upstream_error_message,
)
from .errors import explain
from . import billing
from . import text_clean

logger = logging.getLogger("elevenlabs-adapter.jobs")

router = APIRouter()

# job_id -> 任务字典
_jobs: dict[str, dict] = {}
# 排队中的任务（用于实现取消）
_pending: dict[str, asyncio.Future] = {}


class JobRequest(BaseModel):
    model: str = Field(default="eleven_multilingual_v2")
    input: str = Field(..., description="要合成的文本，可以很长")
    voice: str | None = None
    response_format: str = Field(default="mp3")
    speed: float | None = Field(default=None, ge=0.25, le=4.0)
    text_clean: str | None = Field(
        default=None,
        description="文本清理强度 off / safe / normalize；不传用服务端默认")


def _err(message: str, code: str = "invalid_request_error",
         fix: str | None = None, status: int = 400,
         headers: dict | None = None) -> JSONResponse:
    err = {"message": message, "type": code, "code": code}
    if fix:
        err["fix"] = fix
    return JSONResponse(status_code=status, content={"error": err}, headers=headers)


def _purge_expired() -> None:
    """清掉过期任务。

    不清的话，长跑实例的内存会被音频字节一格格吃光 ——
    一个 5000 字的任务结果就是几MB 到几十 MB。
    """
    now = time.time()
    for jid in [j for j, v in _jobs.items()
                if v.get("finished_at") and now - v["finished_at"] > JOBS_TTL_SECONDS]:
        _jobs.pop(jid, None)


def _public(job: dict) -> dict:
    """对外视图。不含音频字节本身 —— 那是另一个端点的事。"""
    view = {
        "id": job["id"],
        "status": job["status"],
        "chars": job["chars"],
        "created_at": job["created_at"],
        "model": job.get("model"),
        "voice": job.get("voice"),
        "response_format": job.get("response_format"),
    }
    if job["status"] == "succeeded":
        view["finished_at"] = job.get("finished_at")
        view["audio_bytes"] = len(job.get("audio") or b"")
        view["duration_sec"] = job.get("duration_sec")
        # 计费口径：upstream = 上游 character-cost 真实扣费；local-estimate = len(input) 估算
        view["usage_chars"] = job.get("usage_chars", job["chars"])
        view["usage_source"] = job.get("usage_source", "local-estimate")
        view["download_url"] = "/v1/audio/speech/jobs/%s/audio" % job["id"]
        view["expires_in"] = int(JOBS_TTL_SECONDS - (time.time() - job["finished_at"]))
    elif job["status"] == "failed":
        view["error"] = job.get("error")
        view["fix"] = job.get("fix")
    elif job["status"] == "cancelled":
        view["error"] = "任务已取消"
    return view


def _estimate_seconds(chars: int) -> float:
    """粗估耗时。中文 TTS 大致 15~25 字/秒（含上游排队），给个量级就够。

    没有这个字段，客户提交完2000 字看到 queued 不知道要等多久，
    只会以为卡死了然后反复提交 —— 那才是真正打爆并发的原因。
    """
    return round(max(3.0, chars / 20.0), 1)


def _audio_seconds(content: bytes, response_format: str) -> float | None:
    """从音频字节估时长。拿不到就返回 None（不编数字）。

    mp3 用文件头里的 Xing/Info 帧的帧数；估不出来（VBR 无 Xing 头）就放弃，
    总比给个错的时长好 —— 客户按错误时长做的剪辑卡点会全乱。
    """
    try:
        if response_format == "wav" and content[:4] == b"RIFF":
            import struct
            # RIFF 头之后 24 字节是 fmt/data 块；data 块大小在偏移 40
            data_size = struct.unpack("<I", content[40:44])[0]
            rate = 24000           # 适配器把 pcm_24000 包成 WAV，采样率固定
            return round(data_size / 2 / rate, 2) if rate else None
        if response_format == "mp3" and content[:3] == b"ID3":
            return None
        # 通用估算：mp3 128kbps ≈ 16 KB/s
        if response_format == "mp3":
            return round(len(content) / 16000.0, 2)
    except Exception:                          # noqa: BLE001
        return None
    return None


def _header(resp, name: str) -> str | None:
    """安全读响应头。测试桩 / 非 httpx 对象没有 headers 时返回 None 而不是炸。"""
    h = getattr(resp, "headers", None)
    if h is None:
        return None
    try:
        return h.get(name) or h.get(name.title())
    except AttributeError:
        return None


def _bill_job(job: dict, spec: dict, model_id: str | None, voice_id: str | None,
              status: int, up_credits: int | None, up_rid: str | None,
              error: str | None) -> None:
    """把异步任务写进同一本账。失败任务同样要记 —— 那才是白扣钱的凭据。"""
    started = job.get("started_at") or job.get("created_at") or time.time()
    # in_chars 必须是**客户提交的原始长度**（未 strip）。
    # count_chars() 会 strip，用它会让 input 口径与 new-api 逐笔对不上。
    raw_len = spec.get("raw_chars")
    if raw_len is None:
        raw_len = len(spec.get("input") or "")
    billing.record(
        request_id="job:" + job["id"],
        in_chars=raw_len,
        up_credits=up_credits,
        status=status,
        elapsed_ms=int((time.time() - started) * 1000),
        model=spec.get("model"),
        voice=voice_id or spec.get("voice"),
        upstream_request_id=up_rid,
        client_ip=job.get("client_ip"),
        token_name=job.get("token_name"),
        error=error,
        extra={"async": True,
               "raw_chars": raw_len,
               "cleaned_chars": spec.get("cleaned_chars"),
               "clean_rules": spec.get("clean_rules")},
    )


async def _run_job(job_id: str, spec: dict, voices, msvc_lock: asyncio.Semaphore) -> None:
    """后台执行一个任务。任何异常都要落到 job 上，不能让协程静默死掉。"""
    job = _jobs[job_id]
    try:
        async with msvc_lock:
            if job["status"] == "cancelled":
                return
            job["status"] = "running"
            job["started_at"] = time.time()

            voice_id, model_id, extra = resolve_voice_and_model(
                spec["model"], spec["voice"], voices)
            if not voice_id:
                job["status"] = "failed"
                job["error"] = "无法确定 voice_id：请求里要带 voice 字段"
                job["fix"] = 'body 加 voice，例如 {"model":"eleven_v3","voice":"yun"}'
                job["finished_at"] = time.time()
                return

            fmt = FORMAT_MAP.get(spec["response_format"])
            if fmt is None:
                job["status"] = "failed"
                job["error"] = "response_format '%s' 不支持" % spec["response_format"]
                job["fix"] = "改成 mp3 / opus / wav / pcm"
                job["finished_at"] = time.time()
                return

            output_format, content_type = fmt
            api_key = spec["api_key"]
            up = await call_elevenlabs_tts(
                spec["input"], model_id, voice_id, api_key,
                output_format, spec["speed"], extra)

            up_rid = _header(up, "request-id") or _header(up, "x-trace-id")
            if up.status_code != 200:
                _, fix = explain(up.status_code, up.text)
                job["status"] = "failed"
                job["error"] = upstream_error_message(up)
                job["fix"] = fix
                job["finished_at"] = time.time()
                logger.warning("job %s failed: %s %s", job_id, up.status_code, up.text[:300])
                _bill_job(job, spec, model_id, voice_id, up.status_code,
                          None, up_rid, up.text[:200])
                return

            audio = up.content
            if spec["response_format"] == "wav":
                from .main import _pcm_to_wav
                audio = _pcm_to_wav(audio)

            job["audio"] = audio
            job["content_type"] = content_type
            job["model_id"] = model_id
            job["voice_id"] = voice_id
            job["duration_sec"] = _audio_seconds(audio, spec["response_format"])
            # 计费口径与同步接口一致：bill_chars 按模式，up_credits 是上游真值
            # ⚠️ 第一个参数必须是**客户提交的原始长度**（未 strip，与 new-api 同源），
            # 第二个参数是**实际合成长度**（成本依据）。传反了会让异步与同步两个价。
            from .elevenlabs_client import extract_character_cost
            cost = extract_character_cost(up)
            bill_n, bill_src = billing.bill_chars_by_mode(
                spec.get("raw_chars") if spec.get("raw_chars") is not None
                else len(spec.get("input") or ""),
                spec.get("cleaned_chars") if spec.get("cleaned_chars") is not None
                else len(spec.get("input") or ""),
                cost, 200)
            job["usage_chars"] = bill_n
            job["usage_source"] = bill_src
            job["upstream_credits"] = cost
            job["status"] = "succeeded"
            job["finished_at"] = time.time()
            job["attempts"] = job.get("attempts", 0) + 1
            _bill_job(job, spec, model_id, voice_id, 200, cost, up_rid, None)

    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["finished_at"] = time.time()
        _bill_job(job, spec, None, None, 499, None, None, "cancelled")
        raise
    except Exception as e:                      # noqa: BLE001
        logger.exception("job %s crashed", job_id)
        job["status"] = "failed"
        job["error"] = "内部错误：%s" % type(e).__name__
        job["fix"] = "看适配器日志里的 job %s 那一行栈；若是上游超时，调大 ELEVENLABS_TIMEOUT" % job_id
        job["finished_at"] = time.time()
        # 超时崩溃的上游可能已扣费，必须留痕
        _bill_job(job, spec, None, None, 502, None, None,
                  "%s: %s" % (type(e).__name__, e))


def _queue_depth() -> int:
    return sum(1 for v in _jobs.values() if v["status"] == "queued")


_worker_lock: asyncio.Semaphore | None = None
_voices_cache = None


def _sem() -> asyncio.Semaphore:
    """懒初始化。模块级 asyncio 原语不能在建loop 前创建（会绑错事件循环）。"""
    global _worker_lock
    if _worker_lock is None:
        _worker_lock = asyncio.Semaphore(JOBS_WORKERS)
    return _worker_lock


def _voices():
    """音色表只读一次。load_voices() 每次都重新解析 JSON，
    每个任务调一遍就是几百次磁盘读。"""
    global _voices_cache
    if _voices_cache is None:
        from .voices import load_voices
        _voices_cache = load_voices()
    return _voices_cache


@router.get("/v1/audio/speech/jobs")
async def list_jobs():
    """任务列表（运维视角：看队列深度与失败原因）。"""
    _purge_expired()
    if not JOBS_ENABLED:
        return _err("异步任务接口未开启", fix="设置 ADAPTER_JOBS_ENABLED=1 后重启",
                    status=404)
    by_status: dict[str, int] = {}
    for v in _jobs.values():
        by_status[v["status"]] = by_status.get(v["status"], 0) + 1
    return {
        "object": "list",
        "total": len(_jobs),
        "queue_depth": _queue_depth(),
        "workers": JOBS_WORKERS,
        "by_status": by_status,
        "data": [_public(v) for v in
                 sorted(_jobs.values(), key=lambda x: x["created_at"], reverse=True)[:50]],
    }


@router.post("/v1/audio/speech/jobs")
async def submit_job(req: JobRequest, request: Request):
    """提交长文本任务，立刻返回 job_id。"""
    if not JOBS_ENABLED:
        return _err("异步任务接口未开启",
                    fix="中转站客户需要长文本时，在适配器上设置 ADAPTER_JOBS_ENABLED=1 后重启；"
                        "否则请按 2000 字分段调用同步接口",
                    status=404)

    # 与同步接口同一套规则：客户可加强（normalize），不能削弱服务端下限
    clean_mode = text_clean.resolve_mode(req.text_clean, TEXT_CLEAN)
    text, clean_report = text_clean.clean(req.input, clean_mode)
    if not text:
        return _err("input 不能为空",
                    fix="把要合成的文本放进 input 字段；纯空白/纯零宽字符也会被拒绝")
    sent_chars = len(text)
    chars = billing.count_chars(req.input)
    if JOBS_MAX_CHARS and sent_chars > JOBS_MAX_CHARS:
        return _err("input 超长：%d 字符，单任务上限 %d 字符" % (sent_chars, JOBS_MAX_CHARS),
                    fix="调大 ADAPTER_JOBS_MAX_CHARS，或分成多个任务提交",
                    status=400)

    _purge_expired()
    depth = _queue_depth()
    if depth >= JOBS_MAX_PENDING:
        return _err("队列已满：排队中 %d / 上限 %d" % (depth, JOBS_MAX_PENDING),
                    code="rate_limit_error",
                    fix="稍后重试；或调大 ADAPTER_JOBS_MAX_PENDING",
                    status=429, headers={"Retry-After": "10"})

    api_key = resolve_api_key(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        request.headers.get("api-key"),
    )
    if not api_key:
        return _err("缺少 ElevenLabs API Key",
                    code="authentication_error",
                    fix="在请求头加 Authorization: Bearer sk_xxx；"
                        "中转站场景下这个 Key 是渠道密钥，不是客户令牌",
                    status=401)

    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    spec = {
        "model": req.model,
        "voice": req.voice,
        "input": text,
        "speed": req.speed,
        "response_format": req.response_format,
        "api_key": api_key,
        "raw_chars": clean_report["raw_len"],
        "cleaned_chars": sent_chars,
        "clean_rules": ",".join(clean_report["rules"]) or None,
        "clean_mode": clean_mode,
    }
    _jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "chars": chars,
        "created_at": now,
        "model": req.model,
        "voice": req.voice,
        "response_format": req.response_format,
        # 记账要能按客户/来源聚合，只留 Key 前几位
        "client_ip": request.client.host if request.client else None,
        "token_name": (api_key[:6] + "…") if api_key and len(api_key) > 6 else api_key,
    }

    loop = asyncio.get_running_loop()
    # 注意传的是 _voices() 的结果 —— 传函数本身会让后台协程拿到
    # 'function' object has no attribute 'voice_to_id'，任务直接 failed
    fut = loop.create_task(_run_job(job_id, spec, _voices(), _sem()))
    _pending[job_id] = fut
    fut.add_done_callback(lambda _f, jid=job_id: _pending.pop(jid, None))

    logger.info("job %s queued: %d chars, queue_depth=%d", job_id, chars, depth + 1)

    return JSONResponse(
        status_code=202,
        content={"id": job_id, "status": "queued", "chars": chars,
                 "poll_url": "/v1/audio/speech/jobs/%s" % job_id,
                 "estimated_seconds": _estimate_seconds(chars),
                 "queue_position": depth + 1,
                 "queue_depth": depth + 1,
                 "workers": JOBS_WORKERS},
        headers={"Location": "/v1/audio/speech/jobs/%s" % job_id},
    )


@router.get("/v1/audio/speech/jobs/{job_id}")
async def get_job(job_id: str):
    _purge_expired()
    if not JOBS_ENABLED:
        return _err("异步任务接口未开启", fix="设置 ADAPTER_JOBS_ENABLED=1 后重启",
                    status=404)
    job = _jobs.get(job_id)
    if not job:
        return _err("job_id 不存在或已过期：%s" % job_id,
                    fix="确认 job_id 没抄错；结果在 ADAPTER_JOBS_TTL_SECONDS 后会被清理（默认 1800 秒）",
                    status=404)
    return _public(job)


@router.get("/v1/audio/speech/jobs/{job_id}/audio")
async def get_job_audio(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _err("job_id 不存在或已过期：%s" % job_id, status=404)
    if job["status"] != "succeeded":
        return _err("任务尚未完成（当前状态：%s）" % job["status"],
                    fix="按 poll_url 轮询到 status=succeeded 再下载", status=409)
    headers = {
        "X-Usage-Input-Characters": str(job["chars"]),
        "Content-Disposition": 'attachment; filename="%s.%s"'
                               % (job_id, job.get("response_format", "mp3")),
        "Cache-Control": "no-store",
    }
    if job.get("voice_id"):
        headers["X-ElevenLabs-Voice"] = job["voice_id"]
    if job.get("model_id"):
        headers["X-ElevenLabs-Model"] = job["model_id"]
    return Response(content=job["audio"],
                    media_type=job.get("content_type", "audio/mpeg"),
                    headers=headers)


@router.delete("/v1/audio/speech/jobs/{job_id}")
async def cancel_job(job_id: str):
    """取消排队中的任务。已经在跑的停不了（上游请求无法撤回）。"""
    if not JOBS_ENABLED:
        return _err("异步任务接口未开启", fix="设置 ADAPTER_JOBS_ENABLED=1 后重启",
                    status=404)
    job = _jobs.get(job_id)
    if not job:
        return _err("job_id 不存在或已过期：%s" % job_id, status=404)
    if job["status"] in ("succeeded", "failed", "cancelled"):
        return _err("任务已结束（状态 %s），无法取消" % job["status"],
                    fix="只有排队中的任务可以取消", status=409)
    fut = _pending.get(job_id)
    if fut and not fut.done():
        fut.cancel()
    job["status"] = "cancelled"
    job["finished_at"] = time.time()
    return {"id": job_id, "status": "cancelled"}
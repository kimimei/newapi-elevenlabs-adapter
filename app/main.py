# -*- coding: utf-8 -*-
"""ElevenLabs -> OpenAI 兼容适配器（MVP）。

对外暴露 OpenAI 兼容的 /v1/audio/speech，内部调用 ElevenLabs TTS。
new-api / one-api 等中转站把它注册为「OpenAI」类型渠道即可上架 ElevenLabs。

端点：
  POST /v1/audio/speech   文本转语音（非流式；stream=true 暂返回 501）
  GET  /v1/models         OpenAI 格式模型列表（默认只含标准引擎，可加 ?presets=）
  GET  /v1/presets        补充包清单（各包是否启用、含哪些模型）
  GET  /v1/voices          音色列表
  GET  /healthz            健康检查
"""
import io
import logging
import os
import time
import uuid
import wave
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import (
    ALLOW_IPS,
    BILLING_LOG_DAYS,
    MAX_BODY_BYTES,
    MAX_CHARS,
    MAX_CONCURRENCY,
    MOCK,
    PRESETS,
    TEXT_CLEAN,
)
from .elevenlabs_client import (
    FORMAT_MAP,
    call_elevenlabs_tts,
    clamp_speed,
    extract_character_cost,
    resolve_api_key,
    resolve_voice_and_model,
    upstream_error_message,
)
from .voices import load_voices
from .errors import explain
from .voice_catalog import fetch_previews
from .demo import router as demo_router
from .jobs import router as jobs_router
from . import billing
from . import text_clean

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("elevenlabs-adapter")

app = FastAPI(title="ElevenLabs OpenAI-Compatible Adapter", version="0.4.0")

VOICES = load_voices()
if MOCK:
    logger.warning("ELEVENLABS_MOCK=1：不调用 ElevenLabs 上游，返回本地生成的占位音频（仅用于链路验证）")

_ENABLED_PACKS, _UNKNOWN_PACKS = VOICES.parse_packs(PRESETS)
if _UNKNOWN_PACKS:
    logger.warning("ADAPTER_PRESETS 中有无法识别的包：%s（可用：%s）",
                   ",".join(sorted(_UNKNOWN_PACKS)), ",".join(VOICES.packs) or "无")
logger.info("标准引擎 %d 个：%s", len(VOICES.engines), ", ".join(VOICES.engines))
logger.info("补充包：%s（ADAPTER_PRESETS=%s，未启用的包只是不列出，仍可调用）",
            ", ".join(f"{pid}={'on' if pid in _ENABLED_PACKS else 'off'}" for pid in VOICES.packs) or "无",
            PRESETS)

# 上游状态码 -> 对下游(OpenAI 语义)的状态码
# 401 原样保留：中转站据此判定「渠道密钥失效」并自动禁用渠道，这是期望行为
_UPSTREAM_STATUS_MAP = {401: 401, 402: 403, 403: 403, 404: 404, 422: 400, 429: 429}


def _normalize_status(code: int) -> int:
    if code in _UPSTREAM_STATUS_MAP:
        return _UPSTREAM_STATUS_MAP[code]
    if 500 <= code < 600:
        return 502
    return 400


class SpeechRequest(BaseModel):
    model: str = Field(default="eleven_multilingual_v2", description="OpenAI 模型名 / ElevenLabs 模型名 / 音色别名")
    input: str = Field(..., description="要合成的文本")
    voice: str | None = Field(default=None, description="音色名或 voice_id（ElevenLabs 语义）")
    response_format: str = Field(default="mp3", description="mp3/opus/wav/pcm（aac/flac 不受 ElevenLabs 支持）")
    speed: float | None = Field(default=None, ge=0.25, le=4.0,
                                description="语速（OpenAI 区间 0.25–4.0），适配器钳制到 ElevenLabs 的 0.7–1.2")
    stream: bool = Field(default=False, description="MVP 暂不支持流式")
    text_clean: str | None = Field(
        default=None,
        description="文本清理强度，可选 off / safe / normalize。"
                    "不传则用服务端 ADAPTER_TEXT_CLEAN（默认 safe）。"
                    "normalize 会额外去掉 Markdown 标记（**粗体**→粗体），"
                    "省更多字符但可能误伤本身在讲 Markdown 的文本，需自行确认")


def _openai_error(status: int, message: str, code: str = "invalid_request_error",
                  fix: str | None = None, headers: dict | None = None) -> JSONResponse:
    """OpenAI 错误结构 + 一个 `fix` 字段（人看的「下一步改哪一行」）。

    `fix` 是纯增量字段：老客户端只读 message，不受影响。
    """
    err = {"message": message, "type": code, "code": code}
    if fix:
        err["fix"] = fix
    return JSONResponse(status_code=status, content={"error": err}, headers=headers)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """把 FastAPI 默认的 {"detail": [...]} 转成 OpenAI 错误结构。

    中转站普遍按 {"error": {"message": ...}} 解析错误体，结构不符时
    往往只显示「未知错误」，排查成本很高。
    """
    errs = exc.errors()
    if errs:
        first = errs[0]
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        message = f"{loc}: {first.get('msg', '参数校验失败')}" if loc else first.get("msg", "参数校验失败")
    else:
        message = "参数校验失败"
    return _openai_error(400, message,
                        fix="检查请求体字段类型：input 必须是非空字符串，"
                            "speed 需在 0.25–4.0，stream 需为布尔值")


def _too_large(size: int) -> JSONResponse:
    return _openai_error(
        413, "请求体过大：%s 字节，上限 %d 字节。长文本请用异步任务接口。" % (size, MAX_BODY_BYTES),
        fix="短文本按 2000 字左右切分分次调用；长文本改用 POST /v1/audio/speech/jobs "
            "（提交后轮询），或调大 ADAPTER_MAX_BODY_BYTES（默认 2 MiB）")


# 进程内并发计数。上游按并发限流，放太多进去只会让 ElevenLabs 挨个返 429。
# 单进程计数，多副本部署时每个副本各算各的 —— 这是刻意的取舍：
# 引入分布式计数要额外依赖（Redis），对单实例适配器不划算。
_CONCURRENCY = [0]


def _inflight() -> int:
    return _CONCURRENCY[0]


# 白名单豁免路径。健康检查必须豁免：白名单填错时唯一的自查手段就是 /healthz，
# 把它也拦住等于让人瞎填（实测踩过：配错后连健康检查都403，无法判断服务死活）。
_EXEMPT_PATHS = ("/healthz",)


@app.middleware("http")
async def _guard(request: Request, call_next):
    """三道前置闸门：来源 IP 白名单 + 请求体上限（含 chunked）+ 并发上限。

    没有这几道闸门，把 8123 暴露到公网等于把上游 Key 借给全互联网：
    任何人都能 POST 一段超长文本，既刷额度也打内存。

    关于请求体上限：**只看 Content-Length 是能被绕过的** ——
    客户端用 Transfer-Encoding: chunked 逐块发送时压根不带 Content-Length，
    于是检查直接跳过，超大请求照样进到FastAPI 的解析里把内存打满。
    所以无 Content-Length 时改为流式计数，边收边判，超了立刻掐断。

    关于 IP 白名单：**比的是 TCP 对端地址（request.client.host），不是 XFF**。
    nginx 终止 TLS 转发过来时，这里看到的是反代自己的地址（同机 127.0.0.1，
    跨机则是反代的 IP），不是真实客户端 IP。所以 ADAPTER_ALLOW_IPS 在反代形态下
    要填**反代的地址** —— 这正是白名单该做的事（只允许自己的反代来找适配器）。
    用 scripts/check_proxy_ip.py 可实测确认。
    """
    exempt = request.url.path in _EXEMPT_PATHS
    if ALLOW_IPS and not exempt:
        host = request.client.host if request.client else ""
        if host not in ALLOW_IPS:
            return _openai_error(
                403, "来源 IP 不在白名单（ADAPTER_ALLOW_IPS）：%s" % host,
                code="permission_error",
                fix="填**发起连接的地址**，不是真实客户端 IP："
                    "反代转发时这里看到的是反代的地址（同机 127.0.0.1、"
                    "容器网关 172.17.0.1 / 172.18.0.1）；"
                    "用 python scripts/check_proxy_ip.py 可实测确认该填谁")

    # --- 并发闸门 ---
    # 上游按并发限流。放 200 个请求同时进来，ElevenLabs 会挨个返回 429，
    # 客户看到的是"随机失败"。在入口就挡住并说清楚并发上限，体验差别很大。
    if MAX_CONCURRENCY > 0:
        n = _inflight()
        if n >= MAX_CONCURRENCY:
            return _openai_error(
                429, "并发已满：当前 %d / 上限 %d。队列已满，请稍后重试。" % (n, MAX_CONCURRENCY),
                code="rate_limit_error",
                fix="客户端加退避重试（1-3 秒）；或调大 ADAPTER_MAX_CONCURRENCY；"
                    "批量任务请串行提交",
                headers={"Retry-After": "3",
                         "X-Adapter-Concurrency": "%d/%d" % (n, MAX_CONCURRENCY)})
        _CONCURRENCY[0] += 1

    try:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return _too_large(int(cl))
        if not (cl and cl.isdigit()):
            # 无 Content-Length（chunked 传输）：流式计数，超限立刻掐断。
            # 收上来的块必须拼回 request._body，否则下游 FastAPI 读到空 body。
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    return _too_large(len(body))
            request._body = bytes(body)
        return await call_next(request)
    finally:
        if MAX_CONCURRENCY > 0:
            _CONCURRENCY[0] = max(0, _CONCURRENCY[0] - 1)


def _pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    """裸 PCM(16-bit mono) 包一层 RIFF 头。

    OpenAI 的 response_format="wav" 语义是**带容器的 WAV**，而 ElevenLabs
    的 pcm_24000 是裸流。直接透传会导致多数播放器/客户端解不出来。
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _mock_wav(seconds: float = 1.0, rate: int = 24000) -> bytes:
    """Mock 模式生成 1 秒静音 WAV（标准库 wave，无外部依赖）。"""
    return _pcm_to_wav(b"\x00\x00" * int(rate * seconds), rate)


def _mock_audio() -> tuple[bytes, str]:
    """Mock 模式固定返回静音 WAV（标准库只能造 WAV）。

    请求 mp3/opus 时仍返回 WAV 字节，但如实把真实容器写进 X-Mock-Format，
    并保持 Content-Type=audio/wav —— 与其谎报成 mp3 让下游追查解码失败，
    不如明说「这是 mock，格式不匹配」。
    """
    return _mock_wav(), "audio/wav"


@app.post("/v1/audio/speech")
async def audio_speech(req: SpeechRequest, request: Request):
    t0 = time.time()
    rid = uuid.uuid4().hex[:16]
    # ---- 计费基数 ----
    # 两个数，缺一不可：
    #   raw_chars  = 客户提交的原始长度（**未 strip**，与 new-api 的
    #                RuneCount(input) 逐字相等）→ input 口径的计费依据
    #   sent_chars = 实际送上游合成的长度 → **成本依据**（上游只收这个）
    # 清理掉的字符（raw − sent）不再计费，但那部分**我方上游也不付钱**，
    # 所以它是我方成本节省，**不是给客户的让利**。两者混为一谈就会算错毛利。
    # 档位：客户可通过 text_clean 参数主动加强（normalize），但不能削弱服务端下限。
    clean_mode = text_clean.resolve_mode(req.text_clean, TEXT_CLEAN)
    text, clean_report = text_clean.clean(req.input, clean_mode)
    raw_chars = clean_report["raw_len"]          # 客户提交的原始长度
    sent_chars = len(text)                        # 实际合成 = 成本依据
    in_chars = raw_chars                          # 兼容账本字段名
    # 记账上下文：所有 return 分支都经过 _bill()，失败请求同样留痕
    # 注意：raw_chars / clean_rules 不是 record() 的具名参数，走 extra 传入
    ctx = {
        "model": req.model if not req.voice else (req.voice or req.model),
        "voice": req.voice,
        "client_ip": request.client.host if request.client else None,
        "token_name": _token_label(request),
    }
    clean_extra = {
        "raw_chars": raw_chars,
        "cleaned_chars": sent_chars,
        "clean_rules": ",".join(clean_report["rules"]) or None,
    }

    def _bill(status: int, up_credits: int | None = None, up_rid: str | None = None,
              error: str | None = None) -> dict:
        """记一行账，返回应附加到响应的头。

        错误响应也必须带 X-Request-Id —— 客户说"这次失败还被扣钱了"时，
        没有 rid 就无从查起。
        """
        billing.record(
            request_id=rid, in_chars=in_chars, up_credits=up_credits, status=status,
            elapsed_ms=int((time.time() - t0) * 1000),
            upstream_request_id=up_rid, error=error, extra=clean_extra, **ctx)
        return {"X-Request-Id": rid}

    if not text:
        # 注意：判断的是清理后的结果。全是零宽字符/空白的 input 清理后为空，
        # 这种请求送给上游只会白扣费，直接拒绝。
        return _openai_error(400, "input 不能为空",
                            fix="把要合成的文本放进 input 字段；纯空格、纯零宽字符也会被拒绝",
                            headers=_bill(400, error="input 为空"))
    if MAX_CHARS and sent_chars > MAX_CHARS:
        # 按实际送合成的量判断：上游的上限约束的是真正发过去的文本，
        # 用原始长度判断会让客户用不可见字符绕过限制。
        return _openai_error(
            400,
            f"input 超长：{sent_chars} 字符，单次上限 {MAX_CHARS} 字符，请分段提交",
            fix="按 2000 字左右切分后逐段调用，再用音频拼接；"
                "或调大 ADAPTER_MAX_CHARS（注意上游单次也有上限）",
            headers=_bill(400, error="input 超长"),
        )
    if req.stream:
        return _openai_error(501, "MVP 暂不支持流式（stream=true），请使用非流式请求",
                            code="not_implemented",
                            headers=_bill(501, error="stream 不支持"),
                            fix="去掉请求体里的 \"stream\": true，或改成 false 走非流式")

    fmt = FORMAT_MAP.get(req.response_format)
    if fmt is None:
        return _openai_error(400, f"response_format '{req.response_format}' 不支持；ElevenLabs 仅支持 mp3/opus/wav/pcm",
                            fix="把 response_format 改成 mp3（最通用）或 opus/wav/pcm；"
                                "aac/flac 上游不支持，不会静默降级",
                            headers=_bill(400, error="response_format 不支持"))

    try:
        voice_id, model_id, extra = resolve_voice_and_model(req.model, req.voice, VOICES)
    except ValueError as e:
        return _openai_error(400, str(e),
                            fix="voice 传音色名（如 yun）或 15 位以上的 voice_id；"
                                "model 传引擎名（eleven_v3）或已注册的别名，不要传 \"tts-1\" 之外的自造名",
                            headers=_bill(400, error="voice_id 非法"))
    ctx["voice"] = voice_id
    if not voice_id:
        return _openai_error(
            400,
            "无法确定 voice_id：请在请求中传 voice（音色名或 voice_id），"
            "或把模型配置为 'eleven-<name>' 形式的音色别名（见 app/voices.json）",
            fix="补一个 voice 字段：{\"model\":\"eleven_v3\",\"voice\":\"yun\"}；"
                "model 用引擎名时 voice 是必填的",
            headers=_bill(400, error="无法确定 voice_id"),
        )

    api_key = resolve_api_key(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        request.headers.get("api-key"),
    )
    if not api_key and not MOCK:
        return _openai_error(
            401,
            "缺少 ElevenLabs API Key：请通过 Authorization: Bearer <key> 传入，"
            "或设置环境变量 ELEVENLABS_API_KEY",
            code="authentication_error",
            fix="在请求头加 Authorization: Bearer sk_xxx；"
                "中转站场景下这个 Key 是渠道密钥，不是客户令牌",
            headers=_bill(401, error="缺少 API Key"),
        )

    output_format, content_type = fmt
    speed_used, speed_clamped = clamp_speed(req.speed)
    upstream_cost = None  # 上游真实扣费（character-cost 头），拿不到则保持 None
    up_rid = None
    if MOCK:
        data, media_type = _mock_audio()
        resp = Response(content=data, media_type=media_type)
        resp.headers["X-Mock"] = "true"
        # 请求 mp3/opus 时这里会是 wav：明写出来，别让下游以为是适配器格式转换出问题
        resp.headers["X-Mock-Format"] = "wav"
        if req.response_format not in ("wav", "pcm"):
            resp.headers["X-Mock-Format-Mismatch"] = req.response_format
    else:
        try:
            up = await call_elevenlabs_tts(text, model_id, voice_id, api_key, output_format, req.speed, extra)
        except httpx.HTTPError as e:
            logger.error("upstream call failed: %s", e)
            # 超时上游可能已扣费（这是最贵的一类失败），账本要留痕
            _, fix = explain(502, str(e))
            return _openai_error(502, f"调用 ElevenLabs 失败：{e}",
                                 code="upstream_error", fix=fix,
                                 headers=_bill(502, error="%s: %s" % (type(e).__name__, e)))
        except Exception as e:  # noqa: BLE001
            # 非 httpx 的异常（DNS 解析失败、连接被拒、JSON 解析炸了…）会让
            # FastAPI 直接吐 500 且没有 error.fix —— 客户侧只看到"未知错误"，
            # 排查成本全压在支持邮件里。统一转成 502 + 一句能执行的话。
            logger.exception("upstream call crashed")
            _, fix = explain(502, "%s: %s" % (type(e).__name__, e))
            return _openai_error(
                502, "调用 ElevenLabs 失败：%s: %s" % (type(e).__name__, e),
                code="upstream_error",
                fix=fix or "看适配器日志的异常栈；常见原因是服务器出网不通或 DNS 解析失败",
                headers=_bill(502, error="%s: %s" % (type(e).__name__, e)))
        up_rid = _resp_header(up, "request-id") or _resp_header(up, "x-trace-id")
        if up.status_code != 200:
            logger.error("upstream error: %s %s", up.status_code, up.text[:500])
            # 把上游原文翻译成一句可执行的话，同时保留原始 message 供排查
            _, fix = explain(up.status_code, up.text)
            return _openai_error(
                _normalize_status(up.status_code),
                upstream_error_message(up),
                code="upstream_error",
                fix=fix,
                headers=_bill(_normalize_status(up.status_code), up_rid=up_rid,
                              error=up.text[:200] or "upstream %d" % up.status_code),
            )
        body = up.content
        # OpenAI 的 wav 语义是「带 RIFF 容器的 WAV」，上游给的是裸 PCM
        if req.response_format == "wav":
            body = _pcm_to_wav(body)
        resp = Response(content=body, media_type=content_type)
        upstream_cost = extract_character_cost(up)

    # ---- 计费口径 ----
    # 三个数，各有出处：
    #   bill_chars  = 向客户收费的数量 = 按 `BILLING_MODE` 决定
    #                 （input 口径 = 客户提交的原始字符数，与 new-api 同源）
    #   sent_chars  = 实际送上游合成的字符数 = **成本依据**
    #   up_credits  = 上游真实扣的积分（拿不到则为 None，绝不拿估算值顶替）
    #
    # **收钱的是 new-api**（按 RuneCount(input) 扣 quota），我们只是记账。
    # 所以口径必须与 new-api 同源 —— 账本记 sent 而 new-api 记 raw，
    # 客户拿后台日志一对就发现差额，且差额方向固定为我方少收。
    bill_n, bill_src = billing.bill_chars_by_mode(in_chars, sent_chars, upstream_cost, 200)
    resp.headers["X-Usage-Input-Characters"] = str(bill_n)
    resp.headers["X-Usage-Source"] = bill_src
    # 计费依据：一句话把算式写清楚，客户质疑时不用翻代码
    # 用 rule_header（纯 ASCII）—— HTTP 头只允许 latin-1，中文字符会把响应打挂
    resp.headers["X-Usage-Rule"] = billing.rule_header(
        bill_n, in_chars, upstream_cost, 200, bill_src)
    if upstream_cost is not None:
        resp.headers["X-Usage-Upstream-Credits"] = str(upstream_cost)
    resp.headers["X-Request-Id"] = rid
    resp.headers["X-ElevenLabs-Model"] = model_id
    resp.headers["X-ElevenLabs-Voice"] = voice_id
    # 清理透明化 + 价值可见：客户能自己核对「提交多少、合成多少」
    resp.headers["X-Text-Clean-Mode"] = clean_mode
    if clean_report["removed"]:
        resp.headers["X-Text-Clean"] = text_clean.describe(clean_report)
        resp.headers["X-Text-Raw-Chars"] = str(raw_chars)
        resp.headers["X-Text-Sent-Chars"] = str(sent_chars)
        # 清理掉的字符数。
        # ⚠️ 语义随口径变：input 口径下这部分**仍然计入客户账单**
        #（与 new-api 一致），但上游只收 sent —— 所以它是我方成本节省，
        # 不是给客户的让利。别把它当成"客户少付了这么多"。
        resp.headers["X-Text-Saved-Chars"] = str(max(0, raw_chars - sent_chars))
    if speed_clamped:
        resp.headers["X-Speed-Clamped"] = str(speed_used)
    _bill(200, up_credits=upstream_cost, up_rid=up_rid)
    return resp


def _resp_header(resp, name: str) -> str | None:
    """安全读上游响应头。测试桩 / 非 httpx 对象没有 headers 时返回 None 而不是炸。"""
    h = getattr(resp, "headers", None)
    if h is None:
        return None
    try:
        return h.get(name) or h.get(name.title())
    except AttributeError:
        return None


def _token_label(request: Request) -> str | None:
    """从中转站透传的 Key 里取个可读标签，只留前 6 位，不落完整密钥。

    账本要能按客户/渠道聚合，但又不能变成密钥泄露的二次来源。
    """
    raw = (request.headers.get("authorization")
           or request.headers.get("x-api-key")
           or request.headers.get("api-key") or "")
    raw = raw.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if raw.startswith("sk-"):
        raw = raw[3:]
    return (raw[:6] + "…") if len(raw) > 6 else (raw or None)


def _pack_spec(override: str | None) -> str:
    """查询参数优先于环境变量，便于接入时不重启预览。"""
    return PRESETS if override is None else override


@app.get("/v1/models")
async def list_models(presets: str | None = None):
    """默认只返回标准引擎；补充包需 ADAPTER_PRESETS 开启或本次请求指定。

    例：/v1/models?presets=all 可预览全部（不改变服务端默认行为）。
    """
    spec = _pack_spec(presets)
    _, unknown = VOICES.parse_packs(spec)
    if unknown:
        return _openai_error(
            400,
            f"未知的 presets: {','.join(sorted(unknown))}；可用值 all / none / {','.join(VOICES.packs)}",
        )
    data = [
        {"id": i, "object": "model", "created": 0, "owned_by": "elevenlabs"}
        for i in VOICES.models_for(spec)
    ]
    return {"object": "list", "data": data}


@app.get("/v1/presets")
async def list_presets(presets: str | None = None):
    """补充包清单：接入方据此决定要不要把某个包加进渠道模型列表。"""
    spec = _pack_spec(presets)
    enabled, unknown = VOICES.parse_packs(spec)
    if unknown:
        return _openai_error(
            400,
            f"未知的 presets: {','.join(sorted(unknown))}；可用值 all / none / {','.join(VOICES.packs)}",
        )
    return {
        "object": "list",
        "engines": VOICES.engine_ids,
        "data": [
            {
                "id": pid,
                "label": p.label,
                "enabled": pid in enabled,
                "models": list(p.models),
            }
            for pid, p in VOICES.packs.items()
        ],
    }


@app.get("/v1/voices")
async def list_voices(presets: str | None = None, lang: str | None = None,
                      with_preview: int = 0, request: Request = None):
    """音色目录。

    `with_preview=1` 时附带 `preview_url`（上游 CDN 试听链接，缓存 1 小时）。
    `lang=zh` 只返回声明了该语言的别名（基础音色无语言声明，会被过滤掉）。
    """
    enabled, _ = VOICES.parse_packs(_pack_spec(presets))

    previews = {}
    if with_preview:
        key = resolve_api_key(
            request.headers.get("authorization") if request else None,
            request.headers.get("x-api-key") if request else None,
            request.headers.get("api-key") if request else None,
        ) if request else None
        previews = await fetch_previews(key)

    data = []
    if not lang:
        for name, vid in VOICES.voices.items():
            data.append({"voice_id": vid, "name": name, "kind": "voice",
                         "preview_url": previews.get(vid)})
    for name, spec in VOICES.aliases.items():
        pack = VOICES.alias_pack(name)
        if pack not in enabled:
            continue
        if lang and not str(spec.get("lang") or "").lower().startswith(lang.lower()):
            continue
        data.append({
            "voice_id": spec["voice_id"], "name": name, "kind": "alias",
            "model": spec.get("model"), "pack": pack, "lang": spec.get("lang"),
            "label": spec.get("label"), "preview_url": previews.get(spec["voice_id"]),
        })
    return {"voices": data, "count": len(data), "preview": bool(with_preview)}


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(demo_router)
app.include_router(jobs_router)


@app.get("/v1/usage")
async def usage(days: int = 30):
    """用量看板。

    数据全部来自本地账本，不调用上游、不消耗额度。

    **只返回聚合数字，不返回原文** —— 账本里有 token 前 6 位与 IP，
    这些属于运营侧的排查信息，不该经由这个接口暴露给调用方。

    ⚠️ `saved_chars` 的语义随计费口径变（`bill_source` 可查当前口径）：
      input 口径（默认）下，客户按 `in_chars` 付费（与 new-api 一致），
      而 `saved_chars` = in_chars − sent_chars 是**我方上游成本的节省**，
      **不是**客户少付的钱。把两者混为一谈会得出错误的毛利。
      chars 口径下，saved_chars 才是真正的「没向客户收的部分」。
    """
    days = max(1, min(days, BILLING_LOG_DAYS))
    today = billing.stats()
    win = billing.window_stats(days)
    by_token = billing.usage_by_token(days)

    total_in = sum(r["in_chars"] for r in by_token)
    total_sent = sum(r["sent_chars"] for r in by_token)
    total_saved = sum(r["saved_chars"] for r in by_token)

    return {
        "window_days": days,
        "today": {
            "requests": today["requests"],
            "succeeded": today["succeeded"],
            "failed": today["failed"],
            "bill_chars": today["bill_chars"],
            "saved_chars": today["saved_chars"],
            "saved_ratio": today["saved_ratio"],
            # 毛利率（真·美元口径）：需配置 ADAPTER_UP_CREDITS_PER_USD /
            # ADAPTER_CHARS_PER_USD，未配置时恒为 None（宁可不给不给错数）
            "gross_margin_ratio": today.get("gross_margin_ratio"),
            "credits_per_char": today.get("credits_per_char"),
            "up_cost_usd": today.get("up_cost_usd"),
            "revenue_usd": today.get("revenue_usd"),
        },
        "window": {
            "requests": sum(r["requests"] for r in by_token),
            "in_chars": total_in,
            "sent_chars": total_sent,
            "saved_chars": total_saved,
            "saved_ratio": round(total_saved / total_in, 4) if total_in else 0.0,
            # 窗口级毛利率：跨天累计成本/计费，逻辑同 billing.window_stats()
            "gross_margin_ratio": win.get("gross_margin_ratio"),
            "credits_per_char": win.get("credits_per_char"),
            "up_cost_usd": win.get("up_cost_usd"),
            "revenue_usd": win.get("revenue_usd"),
        },
        "by_caller": [
            {
                "caller": r["token"],
                "requests": r["requests"],
                "in_chars": r["in_chars"],
                "sent_chars": r["sent_chars"],
                "saved_chars": r["saved_chars"],
                "saved_ratio": r["saved_ratio"],
            }
            for r in by_token
        ],
        "note": "计费 = 实际合成字符数；saved_chars 是清理帮你省下的、没有向你收费的部分",
    }


@app.get("/")
async def index():
    """试听 + 配置生成器（纯静态，不依赖 Key）。"""
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(str(page))
    return {"service": "elevenlabs-adapter", "models": "/v1/models", "health": "/healthz"}


@app.get("/healthz")
async def healthz():
    from .config import JOBS_ENABLED, JOBS_MAX_PENDING, JOBS_WORKERS
    out = {"status": "ok", "mock": MOCK,
           "catalog": VOICES.engine_ids,
           "demo": os.getenv("ADAPTER_DEMO_ENABLED", "").lower() in ("1", "true", "yes")}
    if JOBS_ENABLED:
        # 队列深度放进健康检查：接了监控的话，"任务堆积"能在告警里看见，
        # 而不是等客户来说"我的单子卡住了"
        from .jobs import _purge_expired, _queue_depth
        _purge_expired()
        out["jobs"] = {"enabled": True, "queue_depth": _queue_depth(),
                       "max_pending": JOBS_MAX_PENDING, "workers": JOBS_WORKERS}
    return out

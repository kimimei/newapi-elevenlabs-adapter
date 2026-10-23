# -*- coding: utf-8 -*-
"""示例脚本共用的小工具：读配置、发请求、认错误、存音频。

只用 Python 标准库（3.9+）—— 跑通一次不该先花半小时装环境。
要接进生产，看 `tts_openai_sdk.py`：换成官方 SDK 只差三行。

配置优先级：命令行参数 > 环境变量 > examples/.env
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(os.path.dirname(HERE), ".env")      # examples/.env
DEFAULT_BASE = "http://127.0.0.1:8123/v1"
DEFAULT_MODEL = "eleven_v3"
DEFAULT_VOICE = "yun"

# aac / flac 上游不支持，传了直接 400 —— 不会静默降级，所以这里也不列
FORMATS = ("mp3", "opus", "wav", "pcm")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_env(path: str = ENV_FILE) -> None:
    """极简 .env 解析。

    已存在的环境变量优先 —— 这样 CI 里 `FOO=bar python tts.py` 能压过文件，
    不用为了跑一次脚本去改文件。
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def normalize_base(url: str) -> str:
    """把 base 规整成 `scheme://host[:port]/v1`。

    漏 `/v1` 是最常见的 404 来源（真实路径会变成 /audio/speech）。
    这里自动补齐并提示一句 —— 静默修补会让用户以为自己填对了。
    """
    url = (url or DEFAULT_BASE).strip().rstrip("/")
    if url.endswith("/audio/speech"):          # 粘了完整路径进来
        url = url[: -len("/audio/speech")]
    if not url.endswith("/v1"):
        print("· base_url 未以 /v1 结尾，已自动补全为 %s/v1" % url, file=sys.stderr)
        url += "/v1"
    return url


def die(msg: str, code: int = 2) -> None:
    print("\n✗ " + msg, file=sys.stderr)
    raise SystemExit(code)


def hget(headers: dict, name: str) -> str | None:
    """大小写无关地取响应头。

    urllib 会给成 `X-Request-Id` 也可能给 `x-request-id`，
    按名字精确取会时灵时不灵 —— 那类 bug 极难查。
    """
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


def human_bytes(n: int) -> str:
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class Client(object):
    """够用的 HTTP 客户端：不重试、不重定向。

    不重试是刻意的 —— 上游是「合成完才返回」，客户端超时的时候上游往往已经
    扣了费，重试就是二次扣费。宁可失败让人看见，也不要静默烧额度。
    """

    def __init__(self, base_url: str = "", token: str = "",
                 timeout: float = 180.0, quiet: bool = False):
        self.base = normalize_base(base_url or os.getenv("ADAPTER_BASE_URL") or DEFAULT_BASE)
        self.token = (token or os.getenv("ADAPTER_TOKEN") or "").strip()
        self.timeout = timeout
        self.quiet = quiet

    # -- 传输 ---------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers.items()), resp.read(), time.time() - t0
        except urllib.error.HTTPError as err:
            # HTTPError 也是响应：错误体里才是真正的信息，不能直接抛掉
            body = err.read() if hasattr(err, "read") else b""
            return err.code, dict((err.headers or {}).items()), body, time.time() - t0
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", err)
            die(
                "连不上 %s（%s）\n"
                "  1) 服务起来了吗： curl -s http://127.0.0.1:8123/healthz\n"
                "  2) 容器在跑吗：   docker ps --filter name=elevenlabs-adapter\n"
                "  3) 地址填对了吗： base 必须含 /v1，且端口就是你映射的那个\n"
                "  4) 远程机器：     确认防火墙/安全组放行了端口" % (url, reason)
            )
        except Exception as err:                              # noqa: BLE001
            die("请求 %s 时出错：%s: %s" % (url, type(err).__name__, err))

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, payload: dict):
        return self._request("POST", path, payload)

    def delete(self, path: str):
        return self._request("DELETE", path)

    # -- 业务 ---------------------------------------------------------------
    def speech(self, text: str, model: str, voice: str, fmt: str = "mp3",
               speed: float | None = None, text_clean: str | None = None):
        payload = {"model": model, "input": text, "response_format": fmt}
        if voice:
            payload["voice"] = voice
        if speed is not None:
            payload["speed"] = speed
        if text_clean:
            payload["text_clean"] = text_clean
        return self.post("/audio/speech", payload)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def explain(status: int, body: bytes) -> str:
    """把状态码翻成「下一步改哪里」。

    优先用适配器返回的 error.fix —— 那是它自己知道的最准的修法；
    拿不到（反代返回的 HTML、网关 502）再按状态码兜底。
    """
    detail = ""
    structured = False
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
        err = obj.get("error") or {}
        detail = err.get("message") or obj.get("message") or ""
        fix = err.get("fix")
        structured = bool(err) or bool(obj.get("detail"))
        if fix:
            detail = "%s\n  → %s" % (detail, fix)
    except Exception:                                          # noqa: BLE001
        detail = body[:200].decode("utf-8", "replace").strip()

    hints = {
        400: "请求参数不对：看上面的 message；response_format 只支持 mp3/opus/wav/pcm",
        401: "令牌不对。直连自建实例填上游 Key；经中转站填面板发的令牌 —— 两者不能互换",
        403: "上游拒绝了：额度用完 / Key 无该模型权限 / 账号被限制",
        404: "路径或模型不存在：检查 base 是否多了或少了 /v1；模型是否已注册进渠道",
        409: "资源还没就绪（异步任务的音频未生成完）",
        429: "限流：降低并发或稍后重试，注意响应里的 Retry-After",
        501: "传了 stream=true，本适配器不支持流式，去掉该参数",
        502: "上游异常或超时：文本太长请改用异步任务接口，或分段提交",
    }
    hint = hints.get(status, "未预期的状态码，看响应体原文")
    if not structured and status >= 500:
        # 适配器的错误体一定是 {"error": {...}}。不是这个结构，说明响应来自
        # nginx / 云网关 / 占了同一端口的别的服务 —— 先查地址，别去查上游。
        hint = ("这个响应不是适配器的错误结构，多半来自反代/网关或别的服务："
                "先确认端口上跑的是不是适配器（curl -s http://主机:端口/healthz）\n  " + hint)
    elif fix:
        # 适配器已经说清了怎么改，就不要再叠一句通用猜测 —— 两句不同的建议
        # 并列时，人会照着错的那条改
        hint = ""
    return "%s\n  %s" % (detail or "(无响应体)", hint) if hint else (detail or "(无响应体)")


def show_trace(headers: dict, elapsed: float, nbytes: int, out_path: str | None = None) -> None:
    """打印这次调用的「凭据」：请求 id + 计费 + 实际下发的模型音色。

    这些头就是和客服/财务对账时唯一能拿出来的东西，脚本顺手打出来，
    省得用户去翻 DevTools。
    """
    rid = hget(headers, "x-request-id") or "-"
    bill = hget(headers, "x-usage-input-characters") or "-"
    credits = hget(headers, "x-usage-upstream-credits") or "-"
    model = hget(headers, "x-elevenlabs-model") or "-"
    voice = hget(headers, "x-elevenlabs-voice") or "-"

    print("  X-Request-Id        %s" % rid)
    print("  计费字符 / 上游积分  %s / %s" % (bill, credits))
    print("  实际下发 model/voice %s / %s" % (model, voice))
    print("  耗时 / 体积          %.2fs / %s" % (elapsed, human_bytes(nbytes)))

    for extra in ("x-usage-rule", "x-speed-clamped", "x-text-clean", "x-mock-format-mismatch"):
        val = hget(headers, extra)
        if val:
            print("  %-22s %s" % (extra, val))
    if out_path:
        print("  已保存              %s" % out_path)


def save_audio(body: bytes, headers: dict, out_path: str) -> bool:
    """按 Content-Type 判断是不是音频，再落盘。

    不判断的后果很常见：请求失败时 `-o out.mp3` 把 JSON 错误体写进 .mp3，
    文件只有几百字节，播放器打不开，然后一路往「格式转换」方向排错。
    """
    ctype = (hget(headers, "content-type") or "").lower()
    if not ctype.startswith("audio/"):
        die("服务端返回的不是音频（Content-Type: %s），内容如下：\n%s"
            % (ctype or "缺失", body[:600].decode("utf-8", "replace")))
        return False
    d = os.path.dirname(os.path.abspath(out_path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(body)
    return True


def read_text(args) -> str:
    """取待合成文本：位置参数 > --file > stdin。"""
    text = " ".join(args.text).strip() if getattr(args, "text", None) else ""
    if getattr(args, "file", None):
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text.strip():
        die("没有要合成的文本。用法：python tts.py \"你好，世界\"   或   --file script.txt")
    return text


# ---------------------------------------------------------------------------
# argparse 公共参数
# ---------------------------------------------------------------------------

def add_common_args(p: argparse.ArgumentParser, out_default: str = "out.mp3") -> argparse.ArgumentParser:
    p.add_argument("text", nargs="*", help="要合成的文本（也可以不传，改用 --file 或管道）")
    p.add_argument("-f", "--file", help="从文本文件读取（UTF-8），适合长稿")
    p.add_argument("-o", "--out", default=out_default, help="输出文件路径（默认 %s）" % out_default)
    p.add_argument("--format", default="mp3", choices=FORMATS,
                   help="音频格式，默认 mp3；pcm 是裸流、wav 会补 RIFF 头")
    p.add_argument("--speed", type=float, default=None,
                   help="语速 0.25–4.0，服务端会钳制到 0.7–1.2 并回 X-Speed-Clamped")
    p.add_argument("--model", default=os.getenv("ADAPTER_MODEL") or DEFAULT_MODEL,
                   help="引擎名，默认 %s" % DEFAULT_MODEL)
    p.add_argument("--voice", default=os.getenv("ADAPTER_VOICE") or DEFAULT_VOICE,
                   help="音色名或 voice_id，默认 %s" % DEFAULT_VOICE)
    p.add_argument("--text-clean", default=None, choices=("off", "safe", "normalize"),
                   help="文本清理强度；不改用服务端默认（通常 safe）")
    p.add_argument("-b", "--base-url", default=os.getenv("ADAPTER_BASE_URL") or "",
                   help="适配器地址，含 /v1")
    p.add_argument("-k", "--token", default=os.getenv("ADAPTER_TOKEN") or "",
                   help="令牌（也可用环境变量 ADAPTER_TOKEN）")
    p.add_argument("--timeout", type=float,
                   default=float(os.getenv("REQUEST_TIMEOUT") or 180),
                   help="客户端超时秒数，默认 180")
    p.add_argument("-q", "--quiet", action="store_true", help="只输出错误")
    return p


def client_from_args(args) -> Client:
    return Client(args.base_url, args.token, args.timeout, args.quiet)

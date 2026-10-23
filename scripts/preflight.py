#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接入自检（preflight）—— 12 项红绿，一条命令回答「接对了吗、会不会算错钱」。

设计约束
--------
* 只用标准库：客户拿到就能跑，不需要 pip install。
* 兼容 Python 3.6（CentOS 7 自带版本），不依赖 dataclasses / typing 新语法。
* 默认**不产生合成调用**（不花钱）：第 6/7/12 项的"实调"部分只在 --live 时执行。
* 只读：默认不修改任何配置；--fix-hint 只打印修复建议，不动手。

12 项
-----
 1 适配器 /healthz 可达
 2 上游 ElevenLabs Key 有效（GET /v1/models，不消耗字符）
 3 适配器 /v1/models 非空
 4 目标模型在目录内
 5 音色可解析为 voice_id
 6 返回音频可解码            [--live]
 7 响应头带字符数             [--live]
 8 new-api 渠道已注册该模型
 9 abilities 行存在
10 ModelRatio 已配
11 AudioRatio = 0            ← 唯一「配错看不出来、但持续多收钱」的项
12 日志 quota == 字符 × 倍率  [--live 用本次调用，否则用最近一条日志]

用法
----
  python3 preflight.py --adapter http://127.0.0.1:8123 --key sk_xxx \
      --db /opt/newapi-verify/data/one-api.db --model eleven_v3 --voice rachel

  # 连 new-api 一起验（会产生 1 次约 2 个字符的计费）
  python3 preflight.py ... --live --newapi http://127.0.0.1:8300 --token sk-xxx

  python3 preflight.py ... --json        # 机读，给 CI / 面板用

退出码：0 全绿 / 1 有失败 / 2 参数或环境错误
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

OK, FAIL, SKIP, WARN = "ok", "fail", "skip", "warn"
UPSTREAM_MODELS = "https://api.elevenlabs.io/v1/models"
DEFAULT_DB = os.environ.get("NEWAPI_DB", "./one-api.db")


# --------------------------------------------------------------------------
# 终端
# --------------------------------------------------------------------------

def _force_utf8_stdio():
    """cron / 无 LANG 环境下 Python 3.6 会退回 ASCII，中文直接崩。"""
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            return
        except Exception:                                   # noqa: BLE001
            pass
    import io
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if stream is not None and hasattr(stream, "buffer") and enc != "utf8":
            setattr(sys, name, io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


_force_utf8_stdio()

COLOR = {"ok": "\033[32m", "fail": "\033[31m", "warn": "\033[33m", "skip": "\033[90m"}
RESET = "\033[0m"
SYMBOL = {OK: "✓", FAIL: "✗", WARN: "!", SKIP: "–"}


def enable_color():
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:                                     # Windows 10+ 打开 VT 处理
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
        return True
    except Exception:                        # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http(url, method="GET", body=None, headers=None, timeout=15):
    """返回 (status, headers_dict, bytes)。异常返回 (0, {}, 错误文本)。"""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:                    # noqa: BLE001
            payload = b""
        return e.code, dict(e.headers or {}), payload
    except Exception as e:                   # noqa: BLE001
        return 0, {}, str(e).encode("utf-8", "replace")


def as_json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:                        # noqa: BLE001
        return None


def brief(raw, limit=160):
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def vwidth(text):
    """中文按 2 列算，否则表格会错位。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def pad(text, width):
    gap = width - vwidth(text)
    return text + (" " * gap if gap > 0 else "")


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------

def open_db(path):
    if not path or not os.path.exists(path):
        return None, "数据库不存在：%s" % (path or "-")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con, ""
    except Exception as e:                   # noqa: BLE001
        return None, "打开失败：%s" % e


def read_options(con, keys):
    out = {}
    q = "SELECT key, value FROM options WHERE key IN (%s)" % ",".join("?" * len(keys))
    for row in con.execute(q, list(keys)):
        try:
            out[row["key"]] = json.loads(row["value"])
        except Exception:                    # noqa: BLE001
            out[row["key"]] = {}
    return out


def pick_channel(con, model):
    """优先返回已含目标模型的渠道；否则返回第一个启用的渠道。"""
    rows = list(con.execute(
        'SELECT id, name, models, "group", status FROM channels ORDER BY id'))
    for r in rows:
        if model in [m.strip() for m in (r["models"] or "").split(",") if m.strip()]:
            return r, rows
    for r in rows:
        if r["status"] == 1:
            return r, rows
    return (rows[0] if rows else None), rows


# --------------------------------------------------------------------------
# 检查项
# --------------------------------------------------------------------------

def result(idx, title, status, detail="", fix=""):
    return {"id": idx, "title": title, "status": status, "detail": detail, "fix": fix}


def check_1_health(adapter):
    code, _, raw = http(adapter.rstrip("/") + "/healthz", timeout=5)
    if code == 200:
        body = as_json(raw) or {}
        mock = body.get("mock")
        detail = "status=%s" % body.get("status", "?")
        if mock:
            detail += "，** MOCK 模式，返回的是静音占位音频 **"
            return result(1, "适配器健康", WARN, detail,
                          "生产环境务必设 ELEVENLABS_MOCK=0，否则客户拿到的是静音文件")
        return result(1, "适配器健康", OK, detail)
    if code == 0:
        return result(1, "适配器健康", FAIL, "连不上：%s" % brief(raw),
                      "确认容器已启动、端口可达：curl <adapter>/healthz")
    return result(1, "适配器健康", FAIL, "HTTP %d：%s" % (code, brief(raw)),
                  "看适配器日志；路径应为 /healthz，不是 /v1/healthz")


def check_2_upstream_key(key, skip=False):
    if skip:
        return result(2, "上游 Key 有效", SKIP, "已按 --skip-upstream 跳过",
                      "客户环境不出网时用这个开关；出网环境建议保留此项")
    if not key:
        return result(2, "上游 Key 有效", SKIP, "未提供 --key",
                      "加 --key sk_xxx（ElevenLabs 的 Key，不是中转站令牌）")
    if not key.startswith("sk_"):
        return result(2, "上游 Key 有效", WARN, "Key 不像 ElevenLabs 原生格式（应以 sk_ 开头）",
                      "确认填的是上游 Key，而不是 new-api 的 sk- 令牌")
    code, _, raw = http(UPSTREAM_MODELS, headers={"xi-api-key": key}, timeout=15)
    if code == 200:
        body = as_json(raw) or []
        n = len(body) if isinstance(body, list) else len(body.get("models", []))
        return result(2, "上游 Key 有效", OK, "上游可用模型 %d 个（未消耗字符）" % n)
    if code == 401:
        return result(2, "上游 Key 有效", FAIL, "401 未授权",
                      "Key 失效/被删/带空格。去 ElevenLabs 后台重新复制")
    if code == 0:
        return result(2, "上游 Key 有效", SKIP, "网络不可达：%s" % brief(raw),
                      "服务器出网被墙时，此项无法验证；不影响其余检查")
    return result(2, "上游 Key 有效", FAIL, "HTTP %d：%s" % (code, brief(raw)),
                  "检查 Key 权限与账户状态")


def check_3_models(adapter):
    code, _, raw = http(adapter.rstrip("/") + "/v1/models?presets=all", timeout=8)
    if code != 200:
        return result(3, "模型目录非空", FAIL,
                      "HTTP %d：%s" % (code or 0, brief(raw)),
                      "适配器未就绪或 /v1/models 报错，先修第 1 项"), []
    body = as_json(raw) or {}
    ids = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
    if not ids:
        return result(3, "模型目录非空", FAIL, "返回 0 个模型",
                      "检查 app/voices.json 的 engines 段是否为空"), []
    return result(3, "模型目录非空", OK, "%d 个（含未启用包）" % len(ids)), ids


def check_4_model_in_catalog(model, ids):
    if not ids:
        return result(4, "目标模型在目录", SKIP, "目录为空，跳过")
    if model in ids:
        return result(4, "目标模型在目录", OK, model)
    near = [m for m in ids if model.lower() in m.lower() or m.lower() in model.lower()][:5]
    hint = ("相近的可选项：%s" % "、".join(near)) if near else "目录里没有相近名字"
    return result(4, "目标模型在目录", FAIL, "目录里没有 %s；%s" % (model, hint),
                  "模型名拼错，或该别名在 voices.json 中不存在。注意 engine 名是 eleven_v3 不是 v3")


def check_5_voice(adapter, voice):
    if not voice:
        return result(5, "音色可解析", SKIP, "未指定 --voice")
    if len(voice) >= 15 and " " not in voice:
        return result(5, "音色可解析", OK, "按 voice_id 直接透传（%s…）" % voice[:8])
    code, _, raw = http(adapter.rstrip("/") + "/v1/voices?presets=all", timeout=8)
    if code != 200:
        return result(5, "音色可解析", WARN, "取不到音色表（HTTP %d）" % code,
                      "适配器版本较老可能无 /v1/voices；可用 voice_id 直接传")
    names = [v.get("name") for v in ((as_json(raw) or {}).get("voices") or [])]
    if voice in names:
        return result(5, "音色可解析", OK, "%s（目录内）" % voice)
    return result(5, "音色可解析", FAIL, "音色表里没有「%s」" % voice,
                  "改用目录内音色，或直接传 15 位以上的 voice_id")


def check_6_7_live(adapter, key, model, voice, live):
    """一次最小调用同时验「音频可解码」和「字符回传」。"""
    if not live:
        return (result(6, "音频可解码", SKIP, "未加 --live（默认不产生合成费用）"),
                result(7, "字符数回传", SKIP, "未加 --live"))
    if not key:
        return (result(6, "音频可解码", SKIP, "缺少 --key"),
                result(7, "字符数回传", SKIP, "缺少 --key"))
    text = "测试"
    body = {"model": model, "input": text, "voice": voice or "rachel", "response_format": "mp3"}
    code, headers, raw = http(adapter.rstrip("/") + "/v1/audio/speech", method="POST",
                              body=body, headers={"Authorization": "Bearer " + key},
                              timeout=60)
    if code != 200:
        fail = (result(6, "音频可解码", FAIL, "HTTP %d：%s" % (code or 0, brief(raw)),
                       "按错误码修：401 换 Key / 404 模型不存在 / 422 参数 / 429 并发"),
                result(7, "字符数回传", SKIP, "调用未成功"))
        return fail

    head = raw[:4]
    if head[:4] == b"RIFF" or head[:2] == b"\xff\xfb" or head[:3] == b"ID3":
        r6 = result(6, "音频可解码", OK, "mp3/wav 头正确，%d 字节" % len(raw))
    elif raw[:1] in (b"{", b"["):
        r6 = result(6, "音频可解码", FAIL, "返回的是 JSON 不是音频：%s" % brief(raw),
                    "多为上游报错被当成音频返回；看适配器日志")
    else:
        r6 = result(6, "音频可解码", WARN, "文件头不认识（%s），%d 字节"
                    % (head.hex(), len(raw)),
                    "确认 response_format 与上游 output_format 映射")

    chars = headers.get("X-Usage-Input-Characters") or headers.get("x-usage-input-characters")
    if chars and chars.strip() == str(len(text)):
        r7 = result(7, "字符数回传", OK, "X-Usage-Input-Characters=%s" % chars)
    elif chars:
        r7 = result(7, "字符数回传", WARN, "回传 %s，实际发送 %d" % (chars, len(text)),
                    "字符统计口径不一致，对账会有偏差")
    else:
        r7 = result(7, "字符数回传", FAIL, "响应头没有 X-Usage-Input-Characters",
                    "没有这个头，new-api 只能按 tokenizer 估算 → 中文计费必然偏差")
    return r6, r7


def check_8_channel(con, channel, model):
    if con is None:
        return result(8, "渠道已注册该模型", SKIP, "无数据库，跳过（只验适配器侧）")
    if channel is None:
        return result(8, "渠道已注册该模型", FAIL, "没有任何渠道",
                      "先在 new-api 添加渠道，类型选 OpenAI")
    models = [m.strip() for m in (channel["models"] or "").split(",") if m.strip()]
    if model in models:
        return result(8, "渠道已注册该模型", OK,
                      "渠道 #%d %s，共 %d 个模型" % (channel["id"], channel["name"], len(models)))
    return result(8, "渠道已注册该模型", FAIL,
                  "渠道 #%d 未包含 %s" % (channel["id"], model),
                  "后台 → 渠道 → 编辑 → 模型列表补上；或改库后重启 new-api")


def check_9_abilities(con, channel, model):
    if con is None:
        return result(9, "abilities 行存在", SKIP, "无数据库")
    if channel is None:
        return result(9, "abilities 行存在", SKIP, "无渠道")
    groups = [g.strip() for g in (channel["group"] or "default").split(",") if g.strip()]
    found = []
    for g in groups:
        row = con.execute(
            'SELECT enabled FROM abilities WHERE channel_id=? AND model=? AND "group"=?',
            (channel["id"], model, g)).fetchone()
        if row:
            found.append("%s(enabled=%s)" % (g, row["enabled"]))
    if found:
        return result(9, "abilities 行存在", OK, "、".join(found))
    return result(9, "abilities 行存在", FAIL,
                  "渠道模型里有 %s，但 abilities 无对应行 → 调用必 404" % model,
                  "补 abilities 行后重启 new-api；删渠道模型时也要同步删")


def check_10_11_ratio(con, model, options, completion_hint=None):
    if con is None:
        return (result(10, "ModelRatio 已配", SKIP, "无数据库"),
                result(11, "AudioRatio = 0", SKIP, "无数据库"))
    ratios = options.get("ModelRatio") or {}
    audios = options.get("AudioRatio") or {}
    qpu = options.get("QuotaPerUnit") or 500000
    try:
        qpu = float(qpu)
    except Exception:                        # noqa: BLE001
        qpu = 500000.0

    r = ratios.get(model)
    if r is None:
        r10 = result(10, "ModelRatio 已配", FAIL, "ModelRatio 里没有 %s" % model,
                     "新增模型必须先配倍率再上架，否则按默认倍率计费或直接报错")
    elif not isinstance(r, (int, float)) or r <= 0:
        r10 = result(10, "ModelRatio 已配", FAIL, "值异常：%r" % (r,),
                     "倍率必须为正数")
    else:
        usd = float(r) * 1000.0 / qpu
        r10 = result(10, "ModelRatio 已配", OK,
                     "%s = %s（隐含 $%.4f/千字符）" % (model, r, usd))

    a = audios.get(model)
    if a is None:
        detail = "AudioRatio 里没有该模型（new-api 缺省按 1 处理）"
        if completion_hint:
            detail += "；按近期日志 completion/prompt≈%.2f 估算，**实收约为应收的 %.2f 倍**" % (
                completion_hint, 1 + completion_hint)
        r11 = result(11, "AudioRatio = 0", FAIL, detail,
                     "音频伪 token 不应二次计价。在定价面板把该模型 AudioRatio 设为 0 后重启")
    elif float(a) != 0:
        detail = "当前 AudioRatio=%s" % a
        if completion_hint:
            detail += "；按近期日志 completion/prompt≈%.2f 估算，**实收约为应收的 %.2f 倍**" % (
                completion_hint, 1 + completion_hint * float(a))
        r11 = result(11, "AudioRatio = 0", FAIL, detail,
                     "这是唯一「配错看不出来、但持续多收钱」的项，必须改 0")
    else:
        r11 = result(11, "AudioRatio = 0", OK, "已归零")
    return r10, r11


def check_12_reconcile(con, model, ratio, live_info=None):
    if con is None:
        return result(12, "计费对账", SKIP, "无数据库")
    if not isinstance(ratio, (int, float)):
        return result(12, "计费对账", SKIP, "倍率缺失，无法对账")

    if live_info:
        chars = live_info["chars"]
        row = live_info["row"]
        src = "本次调用"
    else:
        row = con.execute(
            "SELECT id, quota, prompt_tokens, content FROM logs "
            "WHERE model_name=? ORDER BY id DESC LIMIT 1", (model,)).fetchone()
        chars = row["prompt_tokens"] if row else 0
        src = "最近一条日志 #%s" % (row["id"] if row else "-")

    if not row:
        return result(12, "计费对账", WARN, "该模型还没有调用日志",
                      "先跑一次 --live，或让客户调一次再看")
    expected = float(chars) * float(ratio)
    actual = float(row["quota"])
    if not chars:
        return result(12, "计费对账", WARN, "%s 记录字符数为 0" % src)
    if abs(actual - expected) < 0.5:
        return result(12, "计费对账", OK,
                      "%s：%d 字符 × %s = %d quota，实际 %d ✓"
                      % (src, chars, ratio, expected, actual))
    return result(12, "计费对账", FAIL,
                  "%s：应为 %d，实际 %d（偏差 %.1f%%）"
                  % (src, expected, actual, (actual - expected) * 100.0 / expected),
                  "看日志 content 里的 audio ratio 是否非 0；或该模型另有补全倍率")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def estimate_completion_ratio(con, model):
    """估算 completion/prompt：优先本模型日志，没有则用近期同类日志的均值。

    这个值只用于回答「AudioRatio 非 0 时大概多收几倍」，不参与判定。
    """
    row = con.execute(
        "SELECT prompt_tokens, completion_tokens FROM logs WHERE model_name=? "
        "ORDER BY id DESC LIMIT 1", (model,)).fetchone()
    if row and row["prompt_tokens"]:
        return float(row["completion_tokens"]) / float(row["prompt_tokens"])
    rows = list(con.execute(
        "SELECT prompt_tokens, completion_tokens FROM logs "
        "WHERE prompt_tokens > 0 AND completion_tokens > 0 "
        "ORDER BY id DESC LIMIT 50"))
    if not rows:
        return None
    vals = sorted(float(r["completion_tokens"]) / float(r["prompt_tokens"]) for r in rows)
    return vals[len(vals) // 2]


def run(args):
    con, db_err = open_db(args.db)
    model = args.model
    options = read_options(con, ("ModelRatio", "AudioRatio", "QuotaPerUnit")) if con else {}
    channel, all_channels = pick_channel(con, model) if con else (None, [])
    ratios = options.get("ModelRatio") or {}
    ratio = ratios.get(model)

    results = []
    results.append(check_1_health(args.adapter))

    results.append(check_2_upstream_key(args.key, args.skip_upstream))

    r3, ids = check_3_models(args.adapter)
    results.append(r3)
    results.append(check_4_model_in_catalog(model, ids))
    results.append(check_5_voice(args.adapter, args.voice))

    r6, r7 = check_6_7_live(args.adapter, args.key, model, args.voice, args.live)
    results.append(r6)
    results.append(r7)

    r8 = check_8_channel(con, channel, model)
    results.append(r8)
    results.append(check_9_abilities(con, channel, model))

    # 用日志的 completion/prompt 估算「AudioRatio 非 0 时多收多少」
    hint = estimate_completion_ratio(con, model) if con is not None else None
    r10, r11 = check_10_11_ratio(con, model, options, hint)
    results.append(r10)
    results.append(r11)

    live_info, live_err = None, None
    if args.live and args.newapi and args.token and con is not None:
        live_info, live_err = reconcile_via_newapi(
            con, args.newapi, args.token, model, args.voice)
    if live_err:
        results.append(live_err)
    else:
        results.append(check_12_reconcile(con, model, ratio, live_info))

    return results, con, db_err, all_channels


def reconcile_via_newapi(con, newapi, token, model, voice):
    """经 new-api 调一次，返回 ({chars, row}, None) 或 (None, 失败结果)。"""
    base = newapi.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    max_id = con.execute("SELECT COALESCE(MAX(id),0) FROM logs").fetchone()[0]
    code, headers, raw = http(base + "/v1/audio/speech", method="POST",
                              body={"model": model, "input": "测试",
                                    "voice": voice or "rachel",
                                    "response_format": "mp3"},
                              headers={"Authorization": "Bearer " + token}, timeout=90)
    if code != 200:
        return None, result(
            12, "计费对账", FAIL,
            "经 new-api 调用失败 HTTP %d：%s" % (code or 0, brief(raw)),
            "先确保渠道/令牌可用；也可去掉 --newapi，用最近一条日志对账")
    chars = headers.get("X-Usage-Input-Characters") or headers.get("x-usage-input-characters")
    row = None
    for _ in range(10):
        row = con.execute(
            "SELECT id, quota, prompt_tokens FROM logs WHERE id > ? ORDER BY id DESC LIMIT 1",
            (max_id,)).fetchone()
        if row:
            break
        time.sleep(0.3)
    if not row:
        return None, result(
            12, "计费对账", WARN,
            "调用成功但没有读到新日志行（日志异步落盘？）",
            "等几秒重跑，或手动看后台日志")
    return {"chars": int(chars) if chars and chars.isdigit() else row["prompt_tokens"],
            "row": row}, None


def render(results, args, db_err, channels):
    color = args.color
    tally = {OK: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for r in results:
        tally[r["status"]] += 1

    print("")
    print(" ElevenLabs → OpenAI 接入自检")
    print(" " + "-" * 60)
    print(" 适配器   %s" % args.adapter)
    print(" 数据库   %s%s" % (args.db, ("（%s）" % db_err) if db_err else ""))
    print(" 模型     %s    音色  %s" % (args.model, args.voice or "-"))
    print(" 模式     %s" % ("实调（会产生约 2 个字符的费用）" if args.live else "只读（默认，不花钱）"))
    print("")

    for r in results:
        sym = SYMBOL[r["status"]]
        paint = COLOR[r["status"]] if color else ""
        tail = RESET if color else ""
        head = "%s[%s]%s %2d  %s" % (paint, sym, tail, r["id"], pad(r["title"], 18))
        print(" %s %s" % (head, r["detail"]))

    print("")
    print(" " + "-" * 60)
    line = " 结果：%d 通过 / %d 失败 / %d 警告 / %d 跳过" % (
        tally[OK], tally[FAIL], tally[WARN], tally[SKIP])
    if color:
        line = (COLOR[FAIL] if tally[FAIL] else COLOR[OK]) + line + RESET
    print(line)

    fixes = [r for r in results if r["status"] in (FAIL, WARN) and r["fix"]]
    if fixes:
        print("")
        print(" 需要处理：")
        for r in fixes:
            print("   %2d. %s" % (r["id"], r["fix"]))
    print("")
    return 1 if tally[FAIL] else 0


def main():
    p = argparse.ArgumentParser(
        description="ElevenLabs → OpenAI 接入自检（12 项）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python3 preflight.py --adapter http://127.0.0.1:8123 --key sk_xxx "
               "--db %s --model eleven_v3 --voice rachel" % DEFAULT_DB)
    p.add_argument("--adapter", default=os.getenv("ADAPTER_URL", "http://127.0.0.1:8123"),
                   help="适配器地址（只到端口，不带 /v1）")
    p.add_argument("--key", default=os.getenv("ELEVENLABS_API_KEY", ""),
                   help="ElevenLabs 上游 Key（sk_ 开头）")
    p.add_argument("--db", default=os.getenv("PS_DB", DEFAULT_DB),
                   help="new-api 的 SQLite 路径")
    p.add_argument("--model", default="eleven_v3", help="要检查的模型名")
    p.add_argument("--voice", default="", help="要检查的音色（名或 voice_id）")
    p.add_argument("--live", action="store_true", help="执行真实合成验证（6/7/12，会产生少量费用）")
    p.add_argument("--skip-upstream", action="store_true",
                   help="跳过上游 Key 校验（服务器不出网时用）")
    p.add_argument("--newapi", default="", help="new-api 地址（配合 --live --token 做端到端对账）")
    p.add_argument("--token", default="", help="new-api 令牌（sk- 开头）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--no-color", action="store_true", help="关闭颜色")
    args = p.parse_args()
    args.color = enable_color() and not args.no_color

    results, con, db_err, channels = run(args)

    if args.json:
        payload = {
            "ok": not any(r["status"] == FAIL for r in results),
            "adapter": args.adapter,
            "db": args.db,
            "db_error": db_err,
            "model": args.model,
            "voice": args.voice,
            "live": bool(args.live),
            "checks": results,
            "channels": [{"id": c["id"], "name": c["name"],
                          "models": len([m for m in (c["models"] or "").split(",") if m.strip()])}
                         for c in (channels or [])],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1

    return render(results, args, db_err, channels)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

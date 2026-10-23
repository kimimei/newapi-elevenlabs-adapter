#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对数报告（reconcile）—— 一页纸证明「字符 → quota → 美元」四段一致。

为什么需要这份东西
------------------
中转站最怕的不是「接不上」，是「接上了算错钱」。口头说对齐没有用，
要给出可复核的数字链：

    发送字符数  →  适配器回传字符数  →  new-api 日志 quota  →  折算美元
         │              │                    │                  │
      客户端自己数    X-Usage-Input-      logs.prompt_tokens   quota / QuotaPerUnit
                      Characters          ×  ModelRatio

任意一段对不上，报告就判定失败并指出断在哪一段。

两种模式
--------
1) 实测（默认）：经 new-api 发 N 次真实请求，逐笔读回日志。会产生少量字符费用。
2) 复盘（--from-logs）：不调用、不花钱，直接分析历史日志，用于存量客户补报告。

用法
----
  # 实测（推荐，约 140 个字符 ≈ $0.011）
  python3 scripts/reconcile.py --db ./one-api.db \
      --newapi http://127.0.0.1:8300 --token sk-xxx \
      --model eleven_v3 --voice yun --out report.html

  # 复盘（零成本）
  python3 scripts/reconcile.py --db ./one-api.db \
      --model eleven_v3 --from-logs --out report.html

输出：HTML（A4 打印样式，可直接另存 PDF）+ 同名 JSON。
"""

import argparse
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_DB = "./one-api.db"
SAMPLE_TEXT = (
    "配音对账样本，用于验证字符计费链路是否与官方计价一致。"
    "本段文字仅作测试用途，不含任何业务信息。"
)


def _force_utf8_stdio():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            return
        except Exception:                     # noqa: BLE001
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "buffer"):
            setattr(sys, name, io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


_force_utf8_stdio()


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def http(url, body=None, headers=None, timeout=90):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = dict(headers or {})
    if data:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:                    # noqa: BLE001
        return 0, {}, str(e).encode("utf-8", "replace")


def open_db(path):
    if not os.path.exists(path):
        raise SystemExit("数据库不存在：%s" % path)
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def load_ctx(con, model):
    opts = {}
    for row in con.execute("SELECT key, value FROM options "
                           "WHERE key IN ('ModelRatio','AudioRatio','QuotaPerUnit')"):
        try:
            opts[row["key"]] = json.loads(row["value"])
        except Exception:                     # noqa: BLE001
            opts[row["key"]] = {}
    ratios = opts.get("ModelRatio") or {}
    audios = opts.get("AudioRatio") or {}
    qpu = float(opts.get("QuotaPerUnit") or 500000)

    ch = con.execute(
        'SELECT id, name, "group" FROM channels '
        'WHERE status=1 AND ("," || models || ",") LIKE ? LIMIT 1',
        ("%," + model + ",%",)).fetchone()

    return {
        "model": model,
        "ratio": ratios.get(model),
        "audio_ratio": audios.get(model),
        "qpu": qpu,
        "channel_id": ch["id"] if ch else None,
        "channel_name": ch["name"] if ch else "-",
    }


def make_text(n):
    s = (SAMPLE_TEXT * 40)[:n]
    return s if len(s) == n else (s + "测" * (n - len(s)))


# --------------------------------------------------------------------------
# 采样
# --------------------------------------------------------------------------

def call_once(newapi, token, model, voice, n, con):
    text = make_text(n)
    base = newapi.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    max_id = con.execute("SELECT COALESCE(MAX(id),0) FROM logs").fetchone()[0]
    t0 = time.time()
    code, headers, raw = http(base + "/v1/audio/speech", method_body(text, model, voice),
                              {"Authorization": "Bearer " + token})
    elapsed = int((time.time() - t0) * 1000)
    if code != 200:
        return {"sent": n, "error": "HTTP %s: %s" % (code, raw[:200].decode("utf-8", "replace"))}

    hdr = headers.get("X-Usage-Input-Characters") or headers.get("x-usage-input-characters")
    row = None
    for _ in range(20):
        row = con.execute(
            "SELECT id, quota, prompt_tokens, completion_tokens FROM logs "
            "WHERE id > ? ORDER BY id DESC LIMIT 1", (max_id,)).fetchone()
        if row:
            break
        time.sleep(0.25)
    return {
        "sent": n,
        "header_chars": int(hdr) if hdr and hdr.isdigit() else None,
        "bytes": len(raw),
        "ms": elapsed,
        "quota": row["quota"] if row else None,
        "prompt_tokens": row["prompt_tokens"] if row else None,
        "log_id": row["id"] if row else None,
    }


def method_body(text, model, voice):
    return {"model": model, "input": text, "voice": voice or "rachel",
            "response_format": "mp3"}


def sample_from_logs(con, model, limit):
    rows = list(con.execute(
        "SELECT id, quota, prompt_tokens, completion_tokens FROM logs "
        "WHERE model_name=? ORDER BY id DESC LIMIT ?", (model, limit)))
    out = []
    for r in reversed(rows):
        out.append({"sent": r["prompt_tokens"], "header_chars": None,
                    "bytes": None, "ms": None, "quota": r["quota"],
                    "prompt_tokens": r["prompt_tokens"], "log_id": r["id"]})
    return out


# --------------------------------------------------------------------------
# 计算
# --------------------------------------------------------------------------

def analyze(samples, ctx):
    ratio = ctx["ratio"]
    qpu = ctx["qpu"]
    rows = []
    for s in samples:
        if s.get("error"):
            rows.append(dict(s, ok=False, reason="调用失败"))
            continue
        chars = s.get("header_chars") or s.get("prompt_tokens") or s.get("sent")
        quota = s.get("quota")
        expected = float(chars) * float(ratio) if (chars and ratio) else None
        dev = None
        if expected and quota is not None:
            dev = (float(quota) - expected) * 100.0 / expected
        rows.append({
            "sent": s["sent"], "header_chars": s.get("header_chars"),
            "prompt_tokens": s.get("prompt_tokens"), "quota": quota,
            "expected": expected, "dev_pct": dev,
            "usd": (float(quota) / qpu) if quota is not None else None,
            "usd_expected": (expected / qpu) if expected is not None else None,
            "bytes": s.get("bytes"), "ms": s.get("ms"), "log_id": s.get("log_id"),
            "ok": dev is not None and abs(dev) < 1e-9,
        })
    passed = bool(rows) and all(r["ok"] for r in rows)
    max_dev = max([abs(r["dev_pct"]) for r in rows if r["dev_pct"] is not None] or [0])
    return rows, passed, max_dev


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

HTML = u"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ElevenLabs 计费对数报告 · %(model)s</title>
<style>
  @page { size: A4; margin: 16mm 14mm; }
  * { box-sizing: border-box; }
  body { font: 13px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         color: #1c1c22; margin: 0; padding: 24px; background: #fff; }
  .wrap { max-width: 780px; margin: 0 auto; }
  header { border-bottom: 2px solid #1c1c22; padding-bottom: 10px; margin-bottom: 18px; }
  h1 { font-size: 19px; margin: 0 0 4px; letter-spacing: .2px; }
  .sub { color: #6c6c76; font-size: 12px; }
  .verdict { border: 1px solid %(verdict_border)s; background: %(verdict_bg)s;
             padding: 12px 14px; border-radius: 6px; margin: 16px 0 20px; }
  .verdict b { font-size: 15px; color: %(verdict_fg)s; }
  .verdict p { margin: 6px 0 0; font-size: 12.5px; color: #3c3c46; }
  h2 { font-size: 13px; margin: 22px 0 8px; color: #1c1c22;
       border-left: 3px solid #1c1c22; padding-left: 8px; }
  table { width: 100%%; border-collapse: collapse; font-size: 12px; }
  th, td { border: 1px solid #e3e3e8; padding: 6px 8px; text-align: right; }
  th { background: #f6f6f8; font-weight: 600; color: #3c3c46; }
  td.l, th.l { text-align: left; }
  td.ok { color: #1f7a3d; }
  td.bad { color: #c62828; font-weight: 600; }
  .kv { width: 100%%; font-size: 12px; }
  .kv td { border: none; border-bottom: 1px solid #f0f0f3; padding: 5px 0; }
  .kv td { text-align: left; }
  .kv td:first-child { color: #6c6c76; width: 34%%; }
  .note { font-size: 11.5px; color: #6c6c76; margin-top: 10px; line-height: 1.6; }
  .warn { background: #fdf3e7; border-left: 3px solid #d98324; padding: 10px 12px;
          font-size: 12px; margin: 14px 0; }
  footer { margin-top: 26px; border-top: 1px solid #e3e3e8; padding-top: 10px;
           font-size: 11px; color: #8a8a94; }
  code { background: #f4f4f6; padding: 1px 5px; border-radius: 3px; font-size: 11.5px; }
  @media print { body { padding: 0; } .wrap { max-width: none; } }
</style></head><body><div class="wrap">

<header>
  <h1>ElevenLabs 计费对数报告</h1>
  <div class="sub">模型 <b>%(model)s</b> ｜ 音色 %(voice)s ｜ 生成时间 %(ts)s ｜ 模式 %(mode)s</div>
</header>

<div class="verdict">
  <b>%(verdict)s</b>
  <p>%(verdict_detail)s</p>
</div>

<h2>一、%(segments)s %(tag)s</h2>
<table>
  <tr><th class="l">环节</th><th class="l">来源</th><th>本次取值</th></tr>
  <tr><td class="l">① 发送字符数</td><td class="l">客户端请求 input 长度</td><td>%(chars_sent)s</td></tr>
  <tr><td class="l">② 回传字符数</td><td class="l">响应头 X-Usage-Input-Characters</td><td>%(chars_back)s</td></tr>
  <tr><td class="l">③ 计费字符数</td><td class="l">new-api 日志 prompt_tokens</td><td>%(chars_billed)s</td></tr>
  <tr><td class="l">④ 扣减额度</td><td class="l">new-api 日志 quota</td><td>%(quota)s</td></tr>
  <tr><td class="l">⑤ 折算金额</td><td class="l">quota ÷ QuotaPerUnit</td><td>%(usd)s USD</td></tr>
  <tr><td class="l">⑥ 官方应收</td><td class="l">字符 ÷ 1000 × 单价</td><td>%(usd_expected)s USD</td></tr>
</table>
<p class="note">%(chain)s</p>

<h2>二、逐笔明细</h2>
<table>
  <tr>
    <th>#</th><th>日志</th><th>发送</th><th>回传</th><th>计费字符</th>
    <th>quota</th><th>应缴</th><th>偏差</th><th>耗时</th>
  </tr>
  %(rows)s
</table>
<p class="note">实测模式下「发送」为本次请求字符数；复盘模式下取自历史日志。</p>

<h2>三、环境参数</h2>
<table class="kv">
  <tr><td>模型倍率 ModelRatio</td><td>%(ratio)s　（隐含 %(implied)s / 千字符）</td></tr>
  <tr><td>音频倍率 AudioRatio</td><td>%(audio)s</td></tr>
  <tr><td>QuotaPerUnit</td><td>%(qpu)s　（1 USD = %(qpu)s quota）</td></tr>
  <tr><td>渠道</td><td>%(channel)s</td></tr>
  <tr><td>计费公式</td><td>quota = 计费字符数 × ModelRatio × GroupRatio（group=1）</td></tr>
</table>

%(risk)s

<h2>四、结论与建议</h2>
<p class="note">%(conclusion)s</p>

<footer>
  本报告由 <code>scripts/reconcile.py</code> 于 %(ts)s 生成，数据取自 %(datasource)s，可复核。<br>
  复现命令：<code>%(cmd)s</code><br>
  单价随 ElevenLabs 官方调整而变，跨月对账请以当期官方定价为准。
</footer>

</div></body></html>
"""


def fmt(v, dash="-"):
    return dash if v is None else str(v)


def render(rows, passed, max_dev, ctx, args, mode, ts):
    ratio = ctx["ratio"]
    qpu = ctx["qpu"]
    implied = ("$%.4f" % (float(ratio) * 1000.0 / qpu)) if ratio else "-"
    audio = ctx["audio_ratio"]

    first = rows[0] if rows else {}
    live = not args.from_logs
    n = len(rows)

    def total(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) if vals else None

    chars_sent = total("sent")
    chars_back = total("header_chars") if live else None
    chars_billed = total("prompt_tokens") if live else total("sent")
    quota = total("quota")
    usd = (quota / qpu) if quota is not None else None
    exp_total = sum([r["expected"] for r in rows if r.get("expected") is not None]) or None
    usd_expected = (exp_total / qpu) if exp_total is not None else None
    tag = "（%d 笔合计）" % n if n > 1 else ""

    def cell(v, suffix=""):
        if v is None:
            return "—"
        if isinstance(v, float):
            return "%.6f%s" % (v, suffix) if suffix else "%.6f" % v
        return "%s%s" % (v, suffix)

    trs = []
    for i, r in enumerate(rows, 1):
        if r.get("reason"):
            trs.append("<tr><td>%d</td><td>%s</td><td colspan='7' class='bad'>%s</td></tr>"
                       % (i, fmt(r.get("log_id")), r["reason"]))
            continue
        cls = "ok" if r["ok"] else "bad"
        mark = "一致" if r["ok"] else ("%.2f%%" % (r["dev_pct"] or 0))
        trs.append(
            "<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td class='%s'>%s</td><td>%s</td></tr>" % (
                i, fmt(r.get("log_id")), fmt(r.get("sent")), fmt(r.get("header_chars")),
                fmt(r.get("prompt_tokens")), fmt(r.get("quota")),
                ("%.0f" % r["expected"]) if r.get("expected") else "-",
                cls, mark, ("%sms" % r["ms"]) if r.get("ms") else "-"))

    if passed:
        border, bg, fg = "#1f7a3d", "#f2f8f4", "#1f7a3d"
        if live:
            verdict = "✓ 四段一致，计费可复算"
            detail = "全部 %d 笔样本中，发送字符数、回传字符数、日志 quota 与折算金额完全对齐（最大偏差 %.4f%%）。" \
                     % (len(rows), max_dev)
        else:
            # 复盘模式只取历史日志，拿不到响应头里的回传字符数 —— 只能验三段，
            # 不能声称"四段一致"，否则报告在替它没验过的东西背书。
            verdict = "✓ 三段一致，计费可复算（未含回传字符数）"
            detail = "全部 %d 笔历史样本中，计费字符数、日志 quota 与折算金额完全对齐（最大偏差 %.4f%%）。" \
                     "本次为复盘模式，无响应头回传字符数，故未校验第②段；需完整四段请用实测模式重跑。" \
                     % (len(rows), max_dev)
    else:
        verdict = "✗ 存在偏差，账单不可直接复算"
        border, bg, fg = "#c62828", "#fdecec", "#c62828"
        detail = "最大偏差 %.2f%%。请按明细逐笔核对：若①≠②，看适配器是否回传字符数；若③×倍率≠④，看倍率或 AudioRatio。" \
                 % max_dev

    risk = ""
    if audio is None:
        risk = ("<div class='warn'><b>风险：AudioRatio 未配置。</b> new-api 对缺省值按 1 处理，"
                "音频伪 token 会被二次计价，实测曾出现多收 5 倍。建议立即在定价面板把该模型设为 0。</div>")
    elif float(audio) != 0:
        risk = ("<div class='warn'><b>风险：AudioRatio = %s（应为 0）。</b>"
                "每一笔音频输出的伪 token 都在按倍率二次计费，账单会显著高于官方应收。</div>" % audio)

    if passed:
        conclusion = ("倍率与计费口径自洽。建议：①把本报告作为接入验收附件留存；"
                      "②每月调价后用同一命令重跑一次；③新增模型上架前先跑一遍 12 项自检。"
                      + ("" if live else
                         " ⑤本报告为复盘模式，未校验适配器回传字符数；正式对外计费前请用实测模式重跑一次。"))
    else:
        conclusion = ("先修配置再对外计费：按明细定位断点 → 修完后重跑本报告，"
                      "直到所有样本显示「一致」。在此之前不要扩大客户量。")

    voice_arg = (" --voice %s" % args.voice) if args.voice else ""
    if args.from_logs:
        # 复现命令不写死本地 db 绝对路径：这份报告会被公开，路径泄露等于暴露目录结构。
        cmd = "python3 scripts/reconcile.py --db &lt;your-new-api-db&gt; --model %s%s --from-logs" % (
            ctx["model"], voice_arg)
    else:
        cmd = "python3 scripts/reconcile.py --db &lt;your-new-api-db&gt; --model %s%s --newapi &lt;newapi&gt; --token &lt;token&gt;" % (
            ctx["model"], voice_arg)

    chain = ("①→② 相等说明适配器如实回传；②→③ 相等说明中转层没有二次估算；"
             "③×ModelRatio = ④ 说明倍率配置正确；⑤≈⑥ 说明与官方计价一致。"
             if live else
             "本次为复盘模式：②（响应头回传字符数）无数据，故未校验 ①→② 与 ②→③ 两环；"
             "已校验的是 ③×ModelRatio = ④（倍率正确）与 ⑤≈⑥（与官方计价一致）。"
             "①→② 与 ②→③ 需用实测模式重跑才能确认。")

    return HTML % {
        "model": ctx["model"], "voice": args.voice or "（不限）", "ts": ts, "mode": mode,
        "verdict": verdict, "verdict_detail": detail,
        "verdict_border": border, "verdict_bg": bg, "verdict_fg": fg,
        "chars_sent": cell(chars_sent), "chars_back": cell(chars_back),
        "chars_billed": cell(chars_billed), "quota": cell(quota),
        "usd": cell(usd), "usd_expected": cell(usd_expected),
        "tag": tag,
        "segments": "四段数字" if live else "三段数字（复盘模式无第②段）",
        "datasource": "new-api 日志与响应头" if live else "new-api 历史日志（无响应头）",
        "chain": chain,
        "rows": "\n  ".join(trs),
        "ratio": fmt(ratio), "implied": implied, "audio": fmt(audio, "未配置（按 1 处理）"),
        "qpu": ("%d" % qpu), "channel": ("#%s %s" % (ctx["channel_id"], ctx["channel_name"]))
        if ctx["channel_id"] else "未找到",
        "risk": risk, "conclusion": conclusion, "cmd": cmd,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="ElevenLabs 计费对数报告")
    p.add_argument("--db", default=os.getenv("PS_DB", DEFAULT_DB))
    p.add_argument("--model", default="eleven_v3")
    p.add_argument("--voice", default="")
    p.add_argument("--newapi", default="", help="new-api 地址（实测模式必填）")
    p.add_argument("--token", default="", help="new-api 令牌（sk- 开头）")
    p.add_argument("--samples", default="10,30,100", help="实测字符数序列，默认 10,30,100")
    p.add_argument("--from-logs", action="store_true", help="复盘模式：只用历史日志，不调用、不花钱")
    p.add_argument("--limit", type=int, default=5, help="复盘模式取最近几条日志")
    p.add_argument("--out", default="report.html")
    p.add_argument("--price", type=float, default=None,
                   help="官方单价（USD/千字符）；默认由 ModelRatio 反推")
    a = p.parse_args()

    con = open_db(a.db)
    ctx = load_ctx(con, a.model)
    if ctx["ratio"] is None:
        raise SystemExit("ModelRatio 里没有 %s：先配倍率再对账" % a.model)
    if a.price:
        ctx["qpu"] = float(ctx["ratio"]) * 1000.0 / a.price   # 用官方价反算，仅用于展示一致性
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    if a.from_logs:
        mode = "复盘（仅历史日志）"
        samples = sample_from_logs(con, a.model, a.limit)
        if not samples:
            raise SystemExit("该模型没有历史日志；去掉 --from-logs 用实测模式")
    else:
        if not (a.newapi and a.token):
            raise SystemExit("实测模式需要 --newapi 与 --token；只想看历史日志请加 --from-logs")
        sizes = [int(x) for x in a.samples.split(",") if x.strip()]
        price = a.price if a.price else float(ctx["ratio"]) * 1000.0 / ctx["qpu"]
        cost = sum(sizes) / 1000.0 * price
        print("将发送 %d 次采样，合计 %d 字符（约 $%.4f，单价 $%.3f/千字符）。"
              % (len(sizes), sum(sizes), cost, price))
        mode = "实测（经 new-api）"
        samples = []
        for n in sizes:
            r = call_once(a.newapi, a.token, a.model, a.voice, n, con)
            print("  %4d 字符 → %s" % (n, "OK" if not r.get("error") else r["error"]))
            samples.append(r)

    rows, passed, max_dev = analyze(samples, ctx)
    html = render(rows, passed, max_dev, ctx, a, mode, ts)

    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    payload = {"ok": passed, "max_dev_pct": max_dev, "model": a.model,
               "mode": mode, "generated_at": ts, "ctx": ctx, "rows": rows,
               "report": os.path.abspath(a.out)}
    with io.open(os.path.splitext(a.out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("")
    print("结论：%s（最大偏差 %.4f%%）" % ("一致" if passed else "有偏差", max_dev))
    print("报告：%s" % os.path.abspath(a.out))
    print("数据：%s" % os.path.abspath(os.path.splitext(a.out)[0] + ".json"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

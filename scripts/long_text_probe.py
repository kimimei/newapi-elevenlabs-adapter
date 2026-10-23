# -*- coding: utf-8 -*-
"""真实上游长文本探针（带字符预算保护）。

在 <适配器服务器> 上执行：
    python3 long_text_probe.py 2000 --via-new-api
    python3 long_text_probe.py 3000 --direct

⚠️ 这是花钱的工具。每次成功请求都会消耗等长的 ElevenLabs 字符额度。
   脚本启动时会打印本次预计消耗，并要求 --yes 才真正发送；不加 --yes 只做干跑。

参数：
    <chars>          目标字符数
    --via-new-api    走 new-api(8300) → 适配器 → 上游，用 kamy 令牌鉴权
    --direct         直连上游（xi-api-key），绕过适配器，用渠道 Key
    （默认）          直连适配器(8123) 用渠道 Key
    --voice=NAME     音色，默认 zh-boyin
    --token-id=N     new-api 令牌 id，默认 4（kamy）
    --yes            确认发送（不加只干跑，不花钱）

实测基准（2026-10-20，香港服务器）：
    上游吞吐 ~33ms/字；1000字=35s，2000字=98s（120s 超时临界），3000字=105s
    适配器 ELEVENLABS_TIMEOUT=120s 时，>2000 字必 502
"""
import json
import os
import sqlite3
import sys
import time

import httpx

DB = "file:" + os.environ.get("NEWAPI_DB", "./one-api.db") + "?mode=ro"
ADAPTER_URL = os.environ.get("ADAPTER_URL", "http://127.0.0.1:8123") + "/v1/audio/speech"
NEWAPI_URL = os.environ.get("NEWAPI_URL", "http://127.0.0.1:8300") + "/v1/audio/speech"
VOICE_ID = "4AfodMgwXps9oZFhHzoj"  # zh-boyin -> yun
UPSTREAM_URL = "https://api.elevenlabs.io/v1/text-to-speech/%s" % VOICE_ID

# 实测吞吐（秒/字），用于预估耗时和判断是否会撞超时
SEC_PER_CHAR = 0.033
FIXED_OVERHEAD_S = 15.0

_PARA = (
    "数字经济的浪潮正深刻改变着内容生产的每一个环节。在传统媒体时代，一档音频节目"
    "的制作需要主播、录音棚、剪辑师、发行渠道等多个角色的配合，周期以天甚至以周计算。"
    "而今天，语音合成技术已经能够把成段的文字在几秒钟内转化为自然流畅的语音，让内容"
    "创作者把精力从怎么读解放到读什么上。这种转变的意义不仅在于效率，更在于它重新"
    "定义了创作的边界，一个人也可以拥有一整个广播台的产能。从新闻播报到有声读物，"
    "从教学课件到短视频旁白，语音合成的应用场景每天都在扩张，而支撑这一切的底层模型"
    "也在以月为单位的节奏快速迭代。可以预见，未来几年语音与文本之间的转换成本会趋近"
    "于零，真正值钱的将不再是朗读本身，而是内容的选择、组织与表达。"
)


def _key_from(table, ident):
    db = sqlite3.connect(DB, uri=True)
    try:
        row = db.execute("SELECT key FROM %s WHERE id=?" % table, (ident,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def _mask(key):
    if not key:
        return "<none>"
    return key[:6] + "****" + key[-4:]


def _gen_text(chars):
    para = _PARA.strip()
    parts, n = [], 0
    while n < chars:
        parts.append(para)
        n += len(para)
    return "".join(parts)[:chars]


def main():
    args = sys.argv[1:]
    chars = int(args[0]) if args and not args[0].startswith("--") else 2000
    via_new_api = "--via-new-api" in args
    direct = "--direct" in args
    confirmed = "--yes" in args
    voice = "zh-boyin"
    token_id = 4
    for a in args:
        if a.startswith("--voice="):
            voice = a.split("=", 1)[1]
        if a.startswith("--token-id="):
            token_id = int(a.split("=", 1)[1])

    text = _gen_text(chars)
    n = len(text)

    if direct:
        key, url, mode = _key_from("channels", 1), UPSTREAM_URL, "直连上游"
    elif via_new_api:
        key, url, mode = _key_from("tokens", token_id), NEWAPI_URL, "new-api→适配器"
    else:
        key, url, mode = _key_from("channels", 1), ADAPTER_URL, "适配器"

    est = FIXED_OVERHEAD_S + n * SEC_PER_CHAR
    print("=" * 56)
    print("模式      : %s" % mode)
    print("字符数    : %d" % n)
    print("预计耗时  : %.0fs" % est)
    print("计费消耗  : %d 字符（成功即扣）" % n)
    print("鉴权 Key  : %s" % _mask(key))
    if est > 120 and not direct:
        print("⚠️  超过适配器 ELEVENLABS_TIMEOUT=120s，预计 502 并重试（更贵）")
    print("=" * 56)

    if not confirmed:
        print("\n[干跑] 未加 --yes，未发送任何请求，未消耗字符。")
        print("确认后执行：python long_text_probe.py %d %s --yes" % (
            n, " ".join(a for a in args if a.startswith("--") and a != "--yes")))
        return

    payload = {"model": voice, "input": text,
               "response_format": "mp3", "speed": 1.0}
    req_headers = {"Authorization": "Bearer %s" % key}
    if direct:
        payload = {"text": text,
                   "voice_settings": {"stability": 0.8, "similarity_boost": 0.75, "style": 0.25}}
        req_headers = {"xi-api-key": key}

    t0 = time.time()
    try:
        r = httpx.post(url, headers=req_headers, json=payload, timeout=600.0)
        dt = round(time.time() - t0, 2)
        out = {"status": r.status_code, "elapsed_s": dt, "audio_bytes": len(r.content),
               "x_usage_input_chars": r.headers.get("x-usage-input-characters"),
               "chars_sent": n}
        if r.status_code >= 400:
            out["error"] = r.text[:300]
        print("RESULT " + json.dumps(out, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        print("RESULT " + json.dumps({
            "status": "exception", "error": "%s: %s" % (type(e).__name__, e),
            "elapsed_s": round(time.time() - t0, 2), "chars_sent": n,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()

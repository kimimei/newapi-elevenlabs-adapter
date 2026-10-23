#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用官方 openai SDK 调 —— 和写 OpenAI 语音代码一模一样，只换 base_url。

    pip install openai
    export OPENAI_BASE_URL=http://127.0.0.1:8123/v1   # 或直接改下面的 base_url
    export OPENAI_API_KEY=sk-你的令牌
    python examples/python/tts_openai_sdk.py "你好，世界" -o out.mp3

价值在于：你已经写好的 OpenAI 语音代码，改两个变量就能指向 ElevenLabs，
业务侧不用关心底层是哪家供应商 —— 这也是做这层适配器的初衷。
"""
import argparse
import sys

from _common import Client, add_common_args, die, load_env, read_text, save_audio

try:
    from openai import APIStatusError, OpenAI
except ImportError:                                             # pragma: no cover
    OpenAI = None


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(
        description="官方 openai SDK 调用示例（需要 pip install openai）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_args(p)
    args = p.parse_args()

    if OpenAI is None:
        die("没装 openai：pip install openai\n"
            "  不想装也行，用 tts.py —— 它只用标准库，功能一样")

    text = read_text(args)
    # 复用 _common 的地址规整逻辑，避免两处各写一遍 /v1 补全规则
    base = Client(args.base_url, args.token).base
    # SDK 要求 api_key 非空；服务端已配 ELEVENLABS_API_KEY 时随便填一个占位即可
    api_key = args.token or "self-hosted"

    client = OpenAI(base_url=base, api_key=api_key, timeout=args.timeout)

    body = dict(
        model=args.model,
        voice=args.voice,
        input=text,
        response_format=args.format,
    )
    if args.speed is not None:
        body["speed"] = args.speed
    if args.text_clean:
        # SDK 不认的字段统一走 extra_body —— 这正是「兼容 OpenAI 协议」的好处：
        # 多出来的能力不会破坏老代码，只是老客户端用不到
        body["extra_body"] = {"text_clean": args.text_clean}

    try:
        resp = client.audio.speech.create(**body)
    except APIStatusError as err:
        from _common import explain
        raw = err.response.content if err.response is not None else b""
        die("HTTP %d\n  %s" % (err.status_code, explain(err.status_code, raw)), code=1)

    audio = resp.content
    # 非流式响应里挂的是 httpx.Response，响应头在 .headers
    headers = dict(resp.response.headers) if getattr(resp, "response", None) is not None else {}
    if not save_audio(audio, headers, args.out):
        return 1

    if not args.quiet:
        print("✓ 200  %s（%d 字节）" % (args.out, len(audio)))
        for key in ("x-request-id", "x-usage-input-characters", "x-usage-upstream-credits",
                    "x-elevenlabs-model", "x-elevenlabs-voice"):
            if key in headers:
                print("  %-28s %s" % (key, headers[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

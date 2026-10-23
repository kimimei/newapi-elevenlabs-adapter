#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小可用的一次调用：合成一句话、存成文件、打印计费凭据。

    python examples/python/tts.py "你好，世界"
    python examples/python/tts.py --file 稿子.txt -o 成品.mp3 --speed 0.9
    python examples/python/tts.py "测试" --format wav --voice pangge

为什么留这个「最短路径」而不是只给 SDK 示例：
线上排障时 SDK 会套一层自己的错误处理，看不出到底是 401 还是 404；
这个脚本把状态码、响应头、响应体原样摊开，先确认链路通了再上 SDK。
"""
import argparse
import sys

from _common import (add_common_args, client_from_args, die, explain, load_env,
                     read_text, save_audio, show_trace)


def main() -> int:
    load_env()          # 必须在建 parser 之前：参数默认值是从环境变量读的
    p = argparse.ArgumentParser(
        description="调用适配器合成一段语音（零依赖，仅标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_args(p)
    args = p.parse_args()

    text = read_text(args)
    cli = client_from_args(args)

    if not args.quiet:
        print("→ POST %s/audio/speech" % cli.base)
        print("  model=%s voice=%s format=%s chars=%d"
              % (args.model, args.voice or "(模型自带)", args.format, len(text)))

    status, headers, body, elapsed = cli.speech(
        text, args.model, args.voice, args.format, args.speed, args.text_clean)

    if status != 200:
        die("HTTP %d\n  %s" % (status, explain(status, body)), code=1)

    if not save_audio(body, headers, args.out):
        return 1

    if not args.quiet:
        print("✓ 200")
        show_trace(headers, elapsed, len(body), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

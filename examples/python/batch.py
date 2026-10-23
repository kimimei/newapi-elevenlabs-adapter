#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长稿分段合成：切句 → 逐段调用 → 拼接 → 断点续跑。

    python examples/python/batch.py --file 口播稿.txt -o 成品.mp3
    python examples/python/batch.py --file 口播稿.txt --segment 500 --no-concat

为什么必须分段，而不是把长稿一次性丢进去：
上游是「整段合成完才返回」，2000 字以上大概率撞上网关的 60–120 秒超时，
而超时的请求**上游可能已经扣费**。分段后单次请求短、失败只损失一段。

顺带解决三件实操中的事：
  1. 切在句号上，不切断语义 —— 音频衔接自然
  2. 已生成的段直接复用 —— 第 7 段失败不用从第 1 段重来
  3. 每段单独落盘 —— 想重录某一句，删掉那一个文件再跑一次即可
"""
import argparse
import os
import shutil
import subprocess
import sys

from _common import (add_common_args, client_from_args, die, explain,
                     human_bytes, load_env, save_audio)

# 句末符号。逗号不算边界 —— 切在逗号上会让语调断在半句。
# ASCII 句点 "." 要额外看后一个字符：小数（3.14）和缩写（e.g.）不能切。
_END = "。！？!?；;…"


def _sentences(text: str):
    """逐字符切句。

    用正则的变宽 lookbehind 会直接抛 PatternError（`(?<=[。？]\\s*)` 宽度不固定），
    而且中文标点后面常常跟空格/换行，正则很难兼顾 —— 手写循环反而最短最稳。
    """
    out, buf = [], ""
    for i, ch in enumerate(text):
        buf += ch
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in _END or (ch == "." and nxt in ("", " ", "\t", "\n")):
            if buf.strip():
                out.append(buf)
            buf = ""
        elif ch == "\n":
            # 换行是段落停顿，不该单独成句（否则这条 1 字符的「句子」会被丢掉，
            # 段落之间的停顿也就没了）。接回上一句尾部即可。
            if buf.strip():
                out.append(buf)
            elif out:
                out[-1] += buf
            buf = ""
    if buf.strip():
        out.append(buf)
    return out


def split_text(text: str, target: int = 700, hard_max: int = 1500):
    """按句子边界装箱到 ~target 字符一段。

    hard_max 是兜底：没有标点的长段落（会议记录、代码注释）按硬上限切，
    否则一段 3000 字照样超时。
    """
    sentences = _sentences(text) or [text]

    chunks, buf = [], ""
    for s in sentences:
        if buf and len(buf) + len(s) > target:
            chunks.append(buf)
            buf = s
        else:
            buf += s
    if buf.strip():
        chunks.append(buf)

    # 兜底硬切：优先在逗号/顿号/空格处断，实在没有才按字数硬切
    out = []
    for c in chunks:
        while len(c) > hard_max:
            cut = max(c.rfind("，", 0, hard_max), c.rfind(",", 0, hard_max),
                      c.rfind("、", 0, hard_max), c.rfind(" ", 0, hard_max))
            cut = cut + 1 if cut > 0 else hard_max
            out.append(c[:cut])
            c = c[cut:]
        if c.strip():
            out.append(c)
    return out


def concat_with_ffmpeg(parts, out_path: str) -> bool:
    """用 ffmpeg 拼接。没有 ffmpeg 就如实说，不伪造一个成品。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("\n· 没找到 ffmpeg，跳过拼接。分段文件都在，可以：")
        print("    1) brew install ffmpeg  /  apt install ffmpeg  后用下面的命令合并")
        print("    2) 或在剪辑软件里按序号导入（文件名已按顺序编号）")
        print("    3) 只想听效果：直接播 part-01")
        return False

    list_file = os.path.join(os.path.dirname(os.path.abspath(out_path)), "_concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/"))

    base_cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                "-i", list_file]
    # 先试无损直拼（各段参数一致时有效且极快），失败再重编码
    for extra in (["-c", "copy"], []):
        r = subprocess.run(base_cmd + extra + [out_path], capture_output=True)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            os.remove(list_file)
            return True
    print("\n! ffmpeg 拼接失败：%s" % r.stderr.decode("utf-8", "replace")[:300])
    print("  各段文件仍然可用，可手动合并")
    return False


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(
        description="长稿分段合成（断点续跑 + 可选 ffmpeg 拼接）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_args(p, out_default="batch.mp3")
    p.add_argument("--segment", type=int, default=700,
                   help="每段目标字符数，默认 700（1000 以内最稳）")
    p.add_argument("--hard-max", type=int, default=1500,
                   help="单段绝对上限，超过就硬切，默认 1500")
    p.add_argument("--no-concat", action="store_true", help="只分段，不拼接")
    p.add_argument("--force", action="store_true", help="已存在的段也重新合成")
    args = p.parse_args()

    if not args.file:
        die("这个脚本按文件工作，请用 --file 指定稿子（- 表示 stdin）\n"
            "  想直接传一句话，用 tts.py")
    if args.file == "-":
        text = sys.stdin.read()
    else:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    if not text.strip():
        die("文件是空的：%s" % args.file)

    parts_text = split_text(text, args.segment, args.hard_max)
    stem = os.path.splitext(os.path.abspath(args.out))[0]
    parts_dir = stem + ".parts"
    os.makedirs(parts_dir, exist_ok=True)
    ext = args.format

    cli = client_from_args(args)
    print("→ 共 %d 字，切成 %d 段，输出目录 %s" % (len(text), len(parts_text), parts_dir))
    print("  %-4s %-6s %s" % ("段", "字符", "开头"))

    made, reused, total_chars = [], 0, 0
    for i, seg in enumerate(parts_text, 1):
        path = os.path.join(parts_dir, "part-%02d.%s" % (i, ext))
        preview = seg.strip().replace("\n", " ")[:24]
        if os.path.exists(path) and os.path.getsize(path) > 0 and not args.force:
            reused += 1
            print("  %-4d %-6d %s…（已存在，跳过）" % (i, len(seg), preview))
            made.append(path)
            continue

        status, headers, body, elapsed = cli.speech(
            seg, args.model, args.voice, args.format, args.speed, args.text_clean)
        if status != 200:
            print("  %-4d %-6d %s…" % (i, len(seg), preview))
            print("\n✗ 第 %d 段失败（HTTP %d）\n  %s" % (i, status, explain(status, body)))
            print("\n已完成的段都在 %s，修好问题后**重跑同一条命令**即可续上，"
                  "不会重复合成前 %d 段。" % (parts_dir, len(made)))
            return 1

        save_audio(body, headers, path)
        made.append(path)
        total_chars += len(seg)
        print("  %-4d %-6d %s…  → %s %.1fs"
              % (i, len(seg), preview, human_bytes(len(body)), elapsed))

    if reused:
        print("\n· 复用已有 %d 段，本次新合成 %d 段 / %d 字符"
              % (reused, len(made) - reused, total_chars))

    if args.no_concat:
        print("\n分段完成：%s" % parts_dir)
        return 0

    print("\n→ 拼接为 %s" % args.out)
    if concat_with_ffmpeg(made, args.out):
        print("✓ 完成：%s（%s）" % (args.out, human_bytes(os.path.getsize(args.out))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

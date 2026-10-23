#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""异步任务：提交 → 轮询 → 下载。适合几千字、必定超过网关超时的稿子。

    python examples/python/long_text_job.py --file 有声书一章.txt -o 成品.mp3
    python examples/python/long_text_job.py "短句也行" --interval 2

前提：服务端要开 `ADAPTER_JOBS_ENABLED=1`，否则提交会直接 404。
没开也没关系 —— batch.py 分段同样能跑完长稿，只是请求数多。

为什么不反复重提：
提交后返回的 `estimated_seconds` 就是预估等待，`queued` 是正常状态。
等不及就重提一次 = 队列里多一份同样的活，这才是打爆并发的主因。
"""
import argparse
import json
import sys
import time

from _common import (add_common_args, client_from_args, die, explain, hget,
                     human_bytes, load_env, save_audio)


def _poll(cli, job_id: str, interval: float, timeout: float):
    """轮询到终态。间隔逐步放宽到 --max-interval，少打点空转请求。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, headers, body, _ = cli.get("/audio/speech/jobs/" + job_id)
        if status == 404:
            die("job 查不到（HTTP 404）\n  %s\n  → 服务端是否重启过？任务是内存态，重启即丢"
                % explain(404, body))
        if status != 200:
            die("轮询失败 HTTP %d\n  %s" % (status, explain(status, body)))
        job = json.loads(body.decode("utf-8"))
        if job["status"] != last:
            print("  [%s] %s" % (time.strftime("%H:%M:%S"), job["status"]))
            last = job["status"]
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(interval)
    die("等待超时（%.0fs）。任务可能还在跑 —— 用 id=%s 手动查：\n"
        "  curl %s/audio/speech/jobs/%s" % (timeout, job_id, cli.base, job_id))


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(
        description="长文本异步任务：提交、轮询、下载",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    add_common_args(p, out_default="job.mp3")
    p.add_argument("--interval", type=float, default=3.0, help="轮询间隔秒数，默认 3")
    p.add_argument("--max-wait", type=float, default=900, help="最长等待秒数，默认 900")
    p.add_argument("--keep", action="store_true", help="失败时保留任务，便于排查")
    args = p.parse_args()

    from _common import read_text
    text = read_text(args)
    cli = client_from_args(args)

    print("→ 提交任务，%d 字符" % len(text))
    status, headers, body, _ = cli.post("/audio/speech/jobs", {
        "model": args.model,
        "voice": args.voice,
        "input": text,
        "response_format": args.format,
        **({"speed": args.speed} if args.speed is not None else {}),
        **({"text_clean": args.text_clean} if args.text_clean else {}),
    })

    if status == 202:
        job = json.loads(body.decode("utf-8"))
        job_id = job["id"]
        print("  job_id=%s  预估 %.1fs  队列位置 %s"
              % (job_id, job.get("estimated_seconds", 0), job.get("queue_position")))
    elif status == 404:
        die("异步接口没开（HTTP 404）\n  %s\n"
            "  → 服务端加 ADAPTER_JOBS_ENABLED=1 后重启；\n"
            "    不想改配置就用 batch.py 分段提交" % explain(404, body))
    else:
        die("提交失败 HTTP %d\n  %s" % (status, explain(status, body)))

    print("→ 轮询 %s/audio/speech/jobs/%s" % (cli.base, job_id))
    job = _poll(cli, job_id, args.interval, args.max_wait)

    if job["status"] != "succeeded":
        print("\n✗ 任务未成功：%s" % job.get("error", job["status"]))
        if job.get("fix"):
            print("  → %s" % job["fix"])
        if args.keep:
            print("  任务保留：%s/audio/speech/jobs/%s" % (cli.base, job_id))
        return 1

    print("  ✓ 合成完成：%s，%.1fs，计费 %s 字符"
          % (human_bytes(job.get("audio_bytes") or 0),
             job.get("duration_sec") or 0,
             job.get("usage_chars", job.get("chars"))))

    status, headers, body, _ = cli.get("/audio/speech/jobs/%s/audio" % job_id)
    if status != 200:
        die("下载失败 HTTP %d\n  %s" % (status, explain(status, body)))
    if not save_audio(body, headers, args.out):
        return 1

    print("→ 已保存 %s（%s）" % (args.out, human_bytes(len(body))))
    if hget(headers, "x-elevenlabs-voice"):
        print("  实际下发 %s / %s"
              % (hget(headers, "x-elevenlabs-model"), hget(headers, "x-elevenlabs-voice")))
    print("\n提示：结果只在服务端保留一段时间（本次 expires_in=%s 秒），"
          "过期后 job 与音频一起被清掉 —— 要留的音先下载到本地。"
          % job.get("expires_in", "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

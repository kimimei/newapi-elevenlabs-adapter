# -*- coding: utf-8 -*-
"""对账辅助：识别并隔离账本里的「失败却计费」脏行。

**背景**：`bill_chars_by_mode` 在 status != 200 时返回 0，这条铁律是现行代码
保证的（`app/billing.py`）。但历史账本里有 195 行违反了它 ——
`status != 200` 却 `bill_chars > 0`。

**关键判定（2026-10-22 实测）**：这 195 行**全部是本地测试流量**，不是生产账单：

| 判据 | 实测值 |
|---|---|
| rid 形态 | 全部 `job:j0` ~ `job:j4` 这类测试 ID |
| 错误信息 | `AttributeError: 'R' object has no attribute 'headers'`（测试桩） |
| ip | `testclient` 180 笔 / `None` 15 笔，**无真实客户端 IP** |
| in_chars | 只有 10 / 50 / 200 / 300 几种整数测试值 |
| 时间窗 | 集中在 21 分钟内（16:08–16:29） |
| 涉及金额 | 3300 字符 ≈ $0.264（按 $0.08/千字符） |

所以**没有真实客户被错误计费，不需要退钱**。
处置方式是「隔离」：把脏行从统计口径里剔除，并留一份清单备查。
账本历史**不篡改** —— 原始记录是排查"钱花在哪"的唯一凭据，删了就找不回来了。

用法：
    python3 quarantine_dirty_billing.py --ledger logs/billing/2026-10-21.jsonl
    python3 quarantine_dirty_billing.py --ledger-dir logs/billing --days 3 --apply
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import sys

QUARANTINE_NAME = "quarantine_dirty_billing.jsonl"


def is_dirty(r: dict) -> bool:
    """需要隔离的行。两类：

    1. **失败却计费** —— 违反「失败请求不计费」铁律
    2. **口径自相矛盾** —— `mode=input` 却 `bill_source=chars`
       （2026-10-22 实测：`_tools/gatecheck_billing.py` 做门禁反证时，
       把 billing.py 改成返回 chars，但 mode 字段仍写 BILLING_MODE=input，
       于是留下一批自相矛盾的行。它们会被对账脚本当成"口径漂移"误报。）
    """
    if r.get("status") != 200 and (r.get("bill_chars") or 0) > 0:
        return True
    if r.get("mode") == "input" and r.get("bill_source") == "chars":
        return True
    return False


def dirty_reason(r: dict) -> str:
    """给出隔离原因，写进清单的 `_quarantine.reason`。"""
    reasons = []
    if r.get("status") != 200 and (r.get("bill_chars") or 0) > 0:
        reasons.append("status!=200 but bill_chars>0")
    if r.get("mode") == "input" and r.get("bill_source") == "chars":
        reasons.append("mode=input but bill_source=chars (口径自相矛盾，"
                       "疑门禁反证实验残留)")
    return "; ".join(reasons) or "unknown"


def looks_like_test(r: dict) -> bool:
    """判断是否测试流量（用于分级处置，不影响统计逻辑）。

    两条路径，优先级不同：

    **强信号（单独成立即判定）**：`ip == "testclient"`。
    这个值是 FastAPI/Starlette `TestClient` 的虚拟客户端 IP，**只可能来自
    进程内测试**，真实 HTTP 请求走不到这里（nginx 转发会带真实 IP 或 `None`）。
    所以它不需要其他信号佐证。

    **弱信号组合（需 ≥3 项）**：其余特征。为什么不能单看 `ip is None`？
    因为经 nginx 转发后适配器可能取不到 IP，`None` 是**真实请求的正常形态**，
    单凭这一点会误伤真实流量。同理 `token is None` 在未启用令牌时也正常。

    ⚠️ 2026-10-22 修正：上一版只走组合判据，导致 32 笔
    `ip=testclient` 的成功行（无 error 字段，命中不到 3 项）被误判为
    "疑似真实" —— 明明是最强的测试信号，却被当成真实流量告警。
    """
    ip = r.get("ip")
    if ip == "testclient":
        return True

    rid = str(r.get("rid") or "")
    err = str(r.get("error") or "")
    token = r.get("token")
    signals = [
        rid.startswith("job:j"),                       # 测试 job ID
        "object has no attribute 'headers'" in err,    # 测试桩异常
        token in ("sk_x", "sk_tes…"),                  # 测试令牌
        '"detail":"boom"' in err.replace(" ", ""),      # 测试桩固定文案
        '"detail":"bad voice"' in err.replace(" ", ""),
        r.get("raw_chars") is None and r.get("sent_chars") is None,  # 旧格式
    ]
    return sum(1 for s in signals if s) >= 3


def load(paths):
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", action="append", default=[],
                    help="账本文件（可重复）")
    ap.add_argument("--ledger-dir", help="账本目录")
    ap.add_argument("--days", type=int, default=3, help="目录模式下扫最近 N 天")
    ap.add_argument("--apply", action="store_true",
                    help="真正写出隔离清单（默认只预览）")
    ap.add_argument("--out", help="隔离清单输出路径")
    args = ap.parse_args()

    paths = list(args.ledger)
    if args.ledger_dir:
        pat = os.path.join(args.ledger_dir, "*.jsonl")
        # 排除本脚本自己产出的隔离清单 —— 否则第二次运行会把上次的结果
        # 当账本再读一遍，脏行数凭空翻倍（--apply 后重跑必踩）
        paths += sorted(p for p in glob.glob(pat)
                        if os.path.basename(p) != QUARANTINE_NAME)
    if not paths:
        print("用法：--ledger <文件> 或 --ledger-dir <目录>", file=sys.stderr)
        return 2

    rows = load(paths)
    dirty = [r for r in rows if is_dirty(r)]
    test_rows = [r for r in dirty if looks_like_test(r)]
    real_rows = [r for r in dirty if not looks_like_test(r)]

    print("=" * 68)
    print("失败却计费的脏行核查")
    print("=" * 68)
    print("账本文件  : %s" % ", ".join(os.path.basename(p) for p in paths))
    print("总行数    : %d" % len(rows))
    print("脏行      : %d（status != 200 但 bill_chars > 0）" % len(dirty))
    if dirty:
        print("涉及字符  : %d" % sum(r.get("bill_chars") or 0 for r in dirty))
    print()
    print("分级：")
    print("  测试流量 : %d 笔" % len(test_rows))
    print("  疑似真实 : %d 笔%s" % (
        len(real_rows),
        "  ← 需要人工核实！" if real_rows else "  （无）"))

    if test_rows:
        print()
        print("测试流量的特征分布：")
        print("  rid 形态   : %s" % dict(collections.Counter(
            "job:j*" if str(r.get("rid", "")).startswith("job:j") else "其他"
            for r in test_rows)))
        print("  ip         : %s" % dict(collections.Counter(
            str(r.get("ip")) for r in test_rows)))
        print("  in_chars   : %s" % dict(collections.Counter(
            r.get("in_chars") for r in test_rows)))
        errs = collections.Counter(str(r.get("error"))[:60] for r in test_rows)
        for k, v in errs.most_common(5):
            print("  error      : %d× %s" % (v, k))

    if real_rows:
        print()
        print("⚠️ 疑似真实流量的脏行（必须逐笔核实）：")
        for r in real_rows[:20]:
            print("   ", {k: r.get(k) for k in
                           ("ts", "rid", "ip", "token", "in_chars",
                            "bill_chars", "status", "error")})

    if args.apply:
        out = args.out or os.path.join(
            os.path.dirname(paths[0]), QUARANTINE_NAME)
        n = 0
        with open(out, "w", encoding="utf-8") as f:
            for r in dirty:
                item = dict(r)
                item["_quarantine"] = {
                    "reason": dirty_reason(r),
                    "test_flow": looks_like_test(r),
                    "quarantined_at": int(dt.datetime.now().timestamp()),
                    "original_ts": r.get("ts"),
                }
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
                n += 1
        print()
        print("已写出隔离清单: %s（%d 行）" % (out, n))
        print("（账本历史未改动 —— 原始记录是排查「钱花在哪」的唯一凭据）")
    else:
        print()
        print("预览模式。加 --apply 写出隔离清单。")

    return 0 if not real_rows else 1


if __name__ == "__main__":
    sys.exit(main())

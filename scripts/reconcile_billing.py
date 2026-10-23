# -*- coding: utf-8 -*-
"""三层对账：适配器账本 ↔ new-api 记账 ↔ 上游积分。

    L1  适配器 bill_chars  vs  new-api logs.prompt_tokens   → 中转站漏记/重复记
    L2  适配器 up_credits  vs  上游 /v1/history            → 上游扣费异常 / Key 被挪用
    L3  bill_chars × 售价  vs  up_credits × 成本           → 实际毛利率

为什么不能只靠时间戳配对：并发请求时间重叠，失败请求压根没有 new-api 日志，
而失败恰恰是最烧钱的部分。所以适配器自己写账本（app/billing.py），
每行带 rid / up_rid，配对靠 ID 不靠猜。

用法（在服务器上）：
    python3 reconcile_billing.py --ledger logs/billing/2026-10-21.jsonl
    python3 reconcile_billing.py --ledger-dir logs/billing --days 3
    python3 reconcile_billing.py --help-ledger          # 只解释账本字段

退出码：0 = 对平；1 = 有差异；2 = 数据源不可用。
"""
import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict

import httpx

DB_PATH = os.getenv("NEWAPI_DB", "/opt/newapi-verify/data/one-api.db")
UPSTREAM_KEY = os.getenv("ELEVENLABS_API_KEY") or ""
UPSTREAM_BASE = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")

LEDGER_FIELDS = [
    ("ts", "unix 秒"),
    ("rid", "适配器本地的 request id，L1 配对用"),
    ("up_rid", "上游 request-id 头，L2 配对用"),
    ("in_chars", "客户提交的原始字符数（**不 strip**）= input 口径的计费基数"),
    ("sent_chars", "实际送上游合成的字符数（清理后）= **成本基数**"),
    ("bill_chars", "向客户收费的数量；input 口径下 == in_chars；**失败请求恒为 0**"),
    ("bill_source", "input / chars / upstream / failed / capped —— 标明本次用的是哪个口径；"
                    "capped 是历史上「毛利封顶 30%」的遗留值，现行代码已不再产生，"
                    "只在 2026-10-21 及更早的账本里出现"),
    ("bill_rule", "一句话计费依据，例：status=200 mode=input bill=len(input)=1000"),
    ("up_credits", "上游真实扣的积分，null = 没拿到"),
    ("status", "HTTP 状态；非 200 也要有记录"),
    ("ms", "耗时毫秒"),
    ("model / voice", "模型与音色"),
    ("ip / token", "来源 IP 与密钥前 6 位"),
]


def die(msg, code=2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


# --- 毛利换算 ---------------------------------------------------------------
# 上游积分（ElevenLabs character-cost）与我方字符数是**两个不同量纲**，
# 直接相除得不到毛利率。历史实现 `1 - up_credits / bill_chars` 就是这么错的：
# 上游约 0.48 积分/字符，碰巧接近 0.5，于是算出一个"看着挺合理"的百分比，
# 却因为量纲不成立而毫无意义 —— 这正是最危险的那类 bug：数字自洽、结论全错。
#
# 正确做法：两边都换成美元再比。
UP_CREDITS_PER_USD = float(os.getenv("ADAPTER_UP_CREDITS_PER_USD") or 0)
CHARS_PER_USD = float(os.getenv("ADAPTER_CHARS_PER_USD") or 0)


def _gross_margin(up_credits, bill_chars):
    """真毛利率 = 1 - 上游成本USD / 收入USD。缺单价则返回 None。

    **宁可不给，也不给错数** —— 这是本项目对财务口径的一贯要求。
    """
    if not up_credits or not bill_chars:
        return None
    if UP_CREDITS_PER_USD <= 0 or CHARS_PER_USD <= 0:
        return None
    cost_usd = up_credits / UP_CREDITS_PER_USD
    revenue_usd = bill_chars / CHARS_PER_USD
    if revenue_usd <= 0:
        return None
    return 1 - cost_usd / revenue_usd


def _margin_rounded(up_credits, bill_chars):
    m = _gross_margin(up_credits, bill_chars)
    return None if m is None else round(m, 4)


def fmt_ts(ts, tz):
    if not ts:
        return "?"
    tzinfo = dt.timezone(dt.timedelta(hours=tz))
    return dt.datetime.fromtimestamp(ts, tzinfo).strftime("%m-%d %H:%M:%S")


def load_ledger(path):
    rows = []
    if not os.path.exists(path):
        die("账本不存在：%s\n（确认 ADAPTER_BILLING_LOG_DIR 与运行目录一致）" % path)
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                print("WARN: %s 第 %d 行不是合法 JSON，已跳过" % (path, i), file=sys.stderr)
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def load_newapi(since, until):
    if not os.path.exists(DB_PATH):
        return None
    db = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        rows = db.execute(
            "SELECT id, created_at, model_name, prompt_tokens, quota, token_name "
            "FROM logs WHERE created_at >= ? AND created_at <= ? ORDER BY created_at",
            (since, until)).fetchall()
    except sqlite3.OperationalError as e:
        db.close()
        die("读 logs 表失败：%s" % e)
    db.close()
    return [{"id": r[0], "ts": r[1], "model": r[2], "chars": r[3] or 0,
             "quota": r[4] or 0, "token": r[5]} for r in rows]


def upstream_delta(key, days=2):
    """上游最近 N 天的总扣积分。只用于 L2 的总量校验（逐笔 ID 对不上）。"""
    if not key:
        return None, "未配置 ELEVENLABS_API_KEY"
    try:
        r = httpx.get(UPSTREAM_BASE + "/v1/history",
                      headers={"xi-api-key": key}, params={"page_size": 100}, timeout=30)
    except httpx.HTTPError as e:
        return None, "上游请求失败：%s" % e
    if r.status_code != 200:
        return None, "上游 /v1/history 返回 %d（可能缺 history 权限）" % r.status_code
    items = [i for i in r.json().get("history", [])
             if i.get("character_count_change_to") is not None]
    if not items:
        return 0, None
    cutoff = dt.datetime.now().timestamp() - days * 86400
    recent = [i for i in items if (i.get("date_unix") or 0) >= cutoff]
    total = sum(i["character_count_change_to"] - i["character_count_change_from"]
                for i in recent)
    return total, None


def print_ledger_fields():
    print("账本字段（每行一个 JSON）：\n")
    for k, v in LEDGER_FIELDS:
        print("  %-14s %s" % (k, v))
    print("\n三层对账：")
    print("  L1  bill_chars  vs  new-api logs.prompt_tokens  → 中转站漏记/重复记")
    print("  L2  up_credits  vs  上游 /v1/history            → 上游扣费异常")
    print("  L3  bill_chars × 售价  vs  up_credits × 成本    → 毛利率")


def main():
    global DB_PATH
    ap = argparse.ArgumentParser(description="三层对账：账本 ↔ new-api ↔ 上游")
    ap.add_argument("--ledger", help="单个账本文件（YYYY-MM-DD.jsonl）")
    ap.add_argument("--ledger-dir", help="账本目录，配合 --days 合并多天")
    ap.add_argument("--days", type=int, default=1, help="与 --ledger-dir 配合，回溯天数")
    ap.add_argument("--db", default=None, help="new-api sqlite 路径")
    ap.add_argument("--key", default=None, help="上游 ElevenLabs Key（查 L2 用）")
    ap.add_argument("--tz", type=int, default=8, help="时区（默认 8）")
    ap.add_argument("--json", action="store_true", help="额外输出 JSON 摘要")
    ap.add_argument("--help-ledger", action="store_true", help="解释账本字段")
    args = ap.parse_args()

    if args.help_ledger:
        print_ledger_fields()
        return

    if args.db:
        DB_PATH = args.db

    if args.ledger:
        paths = [args.ledger]
    elif args.ledger_dir:
        cutoff = dt.date.today() - dt.timedelta(days=args.days - 1)
        paths = []
        for p in sorted(glob.glob(os.path.join(args.ledger_dir, "*.jsonl"))):
            try:
                d = dt.datetime.strptime(os.path.basename(p)[:-6], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d >= cutoff:
                paths.append(p)
        if not paths:
            die("目录 %s 下没有匹配的账本文件" % args.ledger_dir)
    else:
        ap.print_help()
        return

    rows = []
    for p in paths:
        rows.extend(load_ledger(p))
    if not rows:
        die("账本为空：%s" % ", ".join(paths))
    rows.sort(key=lambda r: r.get("ts") or 0)

    since = rows[0].get("ts") or 0
    until = rows[-1].get("ts") or 0

    total_req = len(rows)
    ok = [r for r in rows if r.get("status") == 200]
    fail = [r for r in rows if r.get("status") != 200]
    sum_in = sum(r.get("in_chars") or 0 for r in rows)
    sum_bill = sum(r.get("bill_chars") or 0 for r in rows)
    sum_up = sum(r.get("up_credits") or 0 for r in rows)
    bill_ok = sum(r.get("bill_chars") or 0 for r in ok)
    up_ok = sum(r.get("up_credits") or 0 for r in ok)
    # 成本基数是 sent_chars（上游只按实际合成量收钱），不是 bill_chars。
    # input 口径下两者不等，用 bill_chars 算成本会高估。
    sum_sent_ok = sum(
        (r.get("sent_chars") if r.get("sent_chars") is not None
         else (r.get("in_chars") or 0)) for r in ok)
    bill_fail = sum(r.get("bill_chars") or 0 for r in fail)

    print("=" * 68)
    print("适配器账本：%s" % ", ".join(os.path.basename(p) for p in paths))
    print("时间窗：%s → %s（UTC%+d）" % (fmt_ts(since, args.tz), fmt_ts(until, args.tz), args.tz))
    print("=" * 68)

    print("\n【账本汇总】")
    print("  请求      : %d（成功 %d / 失败 %d）" % (total_req, len(ok), len(fail)))
    sum_sent = sum(r.get("sent_chars") or 0 for r in rows)
    # 计费口径：bill_source 决定 bill_chars 是 raw 还是 sent
    srcs = defaultdict(int)
    for r in rows:
        srcs[r.get("bill_source")] += 1
    src_desc = " ".join("%s×%d" % (k, v) for k, v in sorted(srcs.items(), key=lambda kv: -kv[1]))
    print("  计费口径  : %s" % (src_desc or "-"))
    print("  输入字符  : %d（客户提交的原始量）" % sum_in)
    print("  实际合成  : %d（清理后送上游 = 成本基数）" % sum_sent)
    print("  计费字符  : %d" % sum_bill)
    if sum_in and sum_sent:
        print("  清理节省  : %d 字符（%.1f%%）→ 我方上游成本节省"
              % (sum_in - sum_sent, (sum_in - sum_sent) * 100.0 / sum_in))
    missing_up = any(r.get("up_credits") is None for r in rows)
    print("  上游积分  : %d%s" % (sum_up, "（部分请求未取到）" if missing_up else ""))
    if bill_ok and up_ok:
        # ⚠️ 不打印"毛利率"：up_ok（积分）/ bill_ok（字符）量纲不同，
        # 相除出来的数没有意义。这里只能给同量纲的单位成本比，
        # 真毛利率要两边换美元，见下方 L3。
        print("  单位成本  : %.3f 积分/字符（同量纲；毛利率见 L3，需配单价）"
              % (up_ok / bill_ok))

    if fail:
        up_fail_known = sum(r.get("up_credits") or 0 for r in fail
                            if r.get("up_credits") is not None)
        print("\n【失败请求】%d 笔，计费字符 %d" % (len(fail), bill_fail))
        if up_fail_known:
            print("  ⚠️  其中已确认消耗上游积分 %d —— 钱花了，客户没拿到音频" % up_fail_known)
        else:
            print("  上游积分未回传（超时类请求通常拿不到 character-cost）")
        by_status = defaultdict(int)
        for r in fail:
            by_status[r.get("status")] += 1
        print("  按状态码：" + "  ".join("%s×%d" % (k, v) for k, v in sorted(by_status.items())))

    # ---- L1 ----
    logs = load_newapi(since, until)
    print("\n【L1】账本 ↔ new-api 记账")
    na_chars = None
    if logs is None:
        print("  跳过：找不到 new-api 数据库（%s）" % DB_PATH)
    else:
        na_chars = sum(g["chars"] for g in logs)
        print("  账本计费字符 : %d（失败请求已计 0）" % sum_bill)
        print("  new-api 记账 : %d（%d 条日志）" % (na_chars, len(logs)))
        diff = na_chars - sum_bill
        # 容差随口径变：
        #   input 口径 —— 与 new-api 同源，差额**应当为 0**。
        #     容忍 1% 反而会掩盖口径漂移（把系统性错误当成正常波动），
        #     所以收紧到 0.1%，并且只要有差额就明确提示「查口径」。
        #   chars 口径 —— 与 new-api 有结构性差额（raw−sent），容差按 1%。
        all_input = all(r.get("bill_source") in ("input", "failed")
                        for r in rows) and any(
            r.get("bill_source") == "input" for r in rows)
        if all_input:
            tol = max(sum_bill * 0.001, 1)
            print("  差额         : %+d（input 口径应恒为 0，容差 ±%d）" % (diff, int(tol)))
            if abs(diff) <= tol:
                print("  结论 ✅ 对平（口径与 new-api 一致）")
            elif diff > 0:
                print("  结论 ⚠️  new-api 记得更多 —— 有请求绕过了适配器，或重复记账")
            else:
                print("  结论 ⚠️  账本记得更多 —— 查计费口径是否被改动（input 应逐笔相等）")
        else:
            tol = max(sum_bill * 0.01, 1)
            print("  差额         : %+d（容差 ±%d；非 input 口径含结构性差额）"
                  % (diff, int(tol)))
            if abs(diff) <= tol:
                print("  结论 ✅ 基本对平")
            elif diff > 0:
                print("  结论 ⚠️  new-api 记得更多 —— 可能有请求绕过了适配器，或重复记账")
            else:
                print("  结论 ⚠️  new-api 记得更少 —— 有成功请求没被记账（查渠道配置）")

    # ---- L2 ----
    print("\n【L2】账本 ↔ 上游积分")
    up_total, err = upstream_delta(args.key or UPSTREAM_KEY, days=max(args.days, 2))
    if err:
        print("  跳过：%s" % err)
    else:
        print("  账本记录积分 : %d" % sum_up)
        print("  上游实际扣费 : %d（近 %d 天）" % (up_total, max(args.days, 2)))
        gap = up_total - sum_up
        print("  差额         : %+d" % gap)
        if up_total == 0:
            print("  结论：上游区间内无扣费")
        elif sum_up == 0:
            print("  结论 ⚠️  账本一笔都没记到积分 —— 检查 character-cost 头是否被代理剥离")
        elif abs(gap) <= max(sum_up * 0.05, 100):
            print("  结论 ✅ 基本对平")
        elif gap > 0:
            print("  结论 ⚠️  上游扣得更多 —— 可能有其他服务共用这个 Key，或超时请求上游仍扣费")
        else:
            print("  结论 ⚠️  账本记的比上游多 —— character-cost 可能被重复累加")

    # ---- L3 ----
    print("\n【L3】毛利")
    if bill_ok and up_ok:
        print("  成功请求计费字符 : %d" % bill_ok)
        print("  成功请求上游积分 : %d" % up_ok)
        # 单位成本比是同量纲的（积分 / 字符），可以放心给
        if sum_sent_ok:
            print("  单位成本比       : %.3f 积分/字符（按实际合成量）"
                  % (up_ok / sum_sent_ok))
            if bill_ok != sum_sent_ok:
                print("                      %.3f 积分/计费字符（按计费量，含清理未合成部分）"
                      % (up_ok / bill_ok))
        # 真毛利率：两边必须都换成美元
        margin = _gross_margin(up_ok, bill_ok)
        if margin is None:
            print("  毛利率           : 未计算 —— 需要配 ADAPTER_UP_CREDITS_PER_USD")
            print("                     和 ADAPTER_CHARS_PER_USD 才能算（否则是拿积分除字符）")
        else:
            print("  毛利率           : %.1f%%（上游成本 / 收入，均折美元）"
                  % (margin * 100))
    else:
        print("  数据不足（需要成功请求同时有 bill_chars 和 up_credits）")

    if args.json:
        print("\n" + json.dumps({
            "requests": total_req, "ok": len(ok), "fail": len(fail),
            "in_chars": sum_in, "bill_chars": sum_bill, "up_credits": sum_up,
            "credits_per_char": (round(up_ok / sum_sent_ok, 4)
                                 if up_ok and sum_sent_ok else None),
            # 未配单价时为 None —— 不给错的数
            "margin": _margin_rounded(up_ok, bill_ok),
        }, ensure_ascii=False, indent=2))

    bad = bool(fail) or (na_chars is not None
                         and abs(na_chars - sum_bill) > max(sum_bill * 0.01, 1))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

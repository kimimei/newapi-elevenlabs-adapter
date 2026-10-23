# -*- coding: utf-8 -*-
"""计费账本：每个请求一行 JSONL，用来做三层对账。

为什么适配器要自己记账 —— new-api 的 logs 表只有**一个** prompt_tokens 字段，
装不下「客户付了多少」和「上游扣了多少」两个数。靠时间戳去猜配对，并发时必然配错，
失败请求更是压根没有 new-api 日志（而这恰恰是最烧钱的部分）。

账本一行就带齐两份数：

    {"ts":1761030000,"rid":"...","up_rid":"...","model":"zh-boyin","voice":"yun",
     "in_chars":1000,"bill_chars":1000,"up_credits":480,
     "status":200,"ms":7021,"mode":"chars","ip":"203.0.113.9","token":"kamy"}

三层对账各解决一个问题：
    L1  适配器 bill_chars  vs  new-api logs.prompt_tokens   → 中转站漏记/重复记
    L2  适配器 up_credits  vs  上游 /v1/history            → 上游扣费异常 / Key 被挪用
    L3  bill_chars × 售价  vs  up_credits × 成本           → 实际毛利率

设计取舍：
  * **失败请求也记**。status != 200 的行正是查「上游扣了但没记账」的唯一凭据。
  * **追加写，不阻塞响应**。写失败只记 warning，绝不能让记账把业务请求拖挂。
  * **按天切分**。单文件过大时不便于 grep，也不便于按天核对；清理也简单。
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from .config import (
    BILLING_BASE_INPUT,
    BILLING_BASE_SENT,
    BILLING_LOG_DAYS,
    BILLING_LOG_DIR,
    BILLING_LOG_OFF,
    BILLING_MODE,
    CHARS_PER_USD,
    UP_CREDITS_PER_USD,
)

logger = logging.getLogger("elevenlabs-adapter")

_lock = threading.Lock()
_last_cleanup_day = None


def _today_path(ts: float | None = None) -> str:
    d = datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")
    return os.path.join(BILLING_LOG_DIR, "%s.jsonl" % d)


def _cleanup_old(days: int) -> None:
    """删除超过保留期的账本文件。每天最多跑一次，失败不影响主流程。"""
    global _last_cleanup_day
    today = datetime.now().date()
    if _last_cleanup_day == today:
        return
    _last_cleanup_day = today
    cutoff = today - timedelta(days=days)
    try:
        for name in os.listdir(BILLING_LOG_DIR):
            if not name.endswith(".jsonl"):
                continue
            try:
                d = datetime.strptime(name[:-6], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                os.remove(os.path.join(BILLING_LOG_DIR, name))
                logger.info("billing log purged: %s", name)
    except OSError as e:
        logger.warning("billing log cleanup skipped: %s", e)


def bill_chars(in_chars: int, up_credits: int | None, status: int = 200) -> tuple[int, str]:
    """兼容旧调用：只有输入长度、没有清理信息时的简化版。"""
    return bill_chars_by_mode(in_chars, in_chars, up_credits, status)


def bill_chars_by_mode(in_chars: int, sent_chars: int, up_credits: int | None,
                       status: int = 200) -> tuple[int, str]:
    """算出「向客户收费的字符数」。

    **口径必须与 new-api 同源，否则账对不上。**

    new-api 的 TTS 计费取 `prompt_tokens`，值是**原始请求体 `input` 字段的
    Unicode 字符数**（Go 侧 `utf8.RuneCountInString`）。收钱的是 new-api，
    我们的账本只是记录 —— 口径不一致时，先怀疑自己。

        input  （默认） 客户提交多少就记多少 —— 与 new-api 逐笔相等
        chars            按实际送上游合成的字符数（清理后）
        upstream         按上游真实积分

    两种口径的取舍：

    | 口径 | 优点 | 代价 |
    |---|---|---|
    | input | 与 new-api **逐笔**相等，客户拿后台日志一对就上 | 客户看不见的字符也计费 |
    | chars | 账单自解释，客户肉眼数得出来 | 与 new-api 有 raw−sent 差额 |

    选 input 的真正理由不是"对客户好"，而是**它与真实扣费一致**：
    清理掉的字符不再计费，但那部分**我方上游也不付钱**（上游只收 sent），
    所以它是我方成本节省，**不是给客户的让利**。两者混为一谈就会算错毛利。

    **失败请求一律计 0**：new-api 对非 200 响应不扣费，账本若仍计费，
    两边永远对不上，只能靠"容差"自欺欺人。
    """
    if status != 200:
        return 0, "failed"
    if BILLING_MODE == "upstream" and up_credits is not None:
        return up_credits, "upstream"
    if BILLING_MODE == BILLING_BASE_SENT:
        # 按实际合成量：这个数字客户能自己数出来（去掉不可见字符后的长度）
        if sent_chars <= 0:
            return 0, "chars"
        return sent_chars, "chars"
    # 默认 input：与 new-api 同源。清理掉的字符同样计费，
    # 但那是我方上游成本的节省，不是给客户的折扣。
    if in_chars <= 0:
        return 0, "input"
    return in_chars, "input"


def count_chars(text: str) -> int:
    """计费基数字符数：去掉首尾空白后再数。

    ⚠️ **仅用于 chars 口径（按实际合成量计费）**。

    input 口径（默认）不能用这个：new-api 计的是 `RuneCount(input)`，
    **不 strip**。客户提交 "  你好  " 共 6 个字符，new-api 就收 6，
    我们若 strip 成 4，逐笔对账就差了 2 —— 差额会稳定出现且无法解释。

    中间的空格照常计（它会真的产生停顿）。
    """
    return len((text or "").strip())


def rule_text(n: int, raw_chars: int, up_credits: int | None, status: int,
              source: str = "input") -> str:
    """生成一句话的计费依据，写进账本（可用中文，账本是 UTF-8 文件）。

    注意：**这个函数不能直接用于 HTTP 响应头** —— HTTP 头只允许 latin-1，
    中文会直接抛 UnicodeEncodeError 把请求打挂。给客户看的头用 rule_header()。
    """
    if status != 200:
        return "status=%d 未计费（失败请求不收费）" % status
    if BILLING_MODE == "upstream" and up_credits is not None:
        return "status=200 mode=upstream bill=character-cost=%d" % n
    if BILLING_MODE == "upstream" and up_credits is None:
        return "status=200 mode=upstream 但上游未回传积分，退回 len(input)=%d" % n
    if source == "input":
        # input 口径：与 new-api 同源，账单一字不差
        s = "status=200 mode=input bill=len(input)=%d" % n
        if raw_chars != n:
            s += "（合成 %d 字符，差额为我方成本节省）" % raw_chars
    else:
        # chars 口径：按实际合成量收费
        s = "status=200 mode=chars bill=sent=%d" % n
        if raw_chars != n:
            s += "（原始输入 %d，已剔除不可朗读字符）" % raw_chars
    if up_credits is not None:
        s += " (upstream=%d credits)" % up_credits
    return s


def rule_header(n: int, raw_chars: int, up_credits: int | None, status: int,
                source: str = "input") -> str:
    """给 HTTP 响应头用的纯 ASCII 版本。

    HTTP 头只能是 latin-1，任何非 ASCII 字符写入都会抛
    UnicodeEncodeError，把整个响应打挂（不是只丢那个头，是整个请求 500）。
    """
    if status != 200:
        return "status=%d not-billed" % status
    if BILLING_MODE == "upstream" and up_credits is not None:
        return "status=200 mode=upstream bill=%d" % n
    if BILLING_MODE == "upstream" and up_credits is None:
        return "status=200 mode=upstream no-credits fallback=%d" % n
    if source == "input":
        s = "status=200 mode=input bill=input=%d" % n
        if raw_chars != n:
            s += " sent=%d" % raw_chars
    else:
        s = "status=200 mode=chars bill=sent=%d" % n
        if raw_chars != n:
            s += " raw=%d" % raw_chars
    if up_credits is not None:
        s += " upstream=%d" % up_credits
    return s


def record(
    *,
    request_id: str,
    in_chars: int,
    up_credits: int | None,
    status: int,
    elapsed_ms: int,
    model: str | None = None,
    voice: str | None = None,
    upstream_request_id: str | None = None,
    client_ip: str | None = None,
    token_name: str | None = None,
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    """写一行账本。任何异常都吞掉 —— 记账失败不能影响业务响应。

    参数 in_chars 是**客户原始输入**的长度（input 口径下 = 未 strip 的
    原文字符数，与 new-api 的 `RuneCount(input)` 逐字相等）。

    账本同时留两个数，缺一不可：
        in_chars   客户提交的量 —— 计费依据（input 口径），也是与 new-api 对账的锚
        sent_chars 实际送上游合成的量 —— **成本依据**（上游只收这个）

    两者之差 = 清理掉的字符 = 我方上游成本的节省，**不是给客户的让利**。
    账本若只留一个数，毛利就算不准了。
    """
    if BILLING_LOG_OFF:
        return
    try:
        sent = (extra or {}).get("cleaned_chars")
        sent = in_chars if sent is None else sent
        n, src = bill_chars_by_mode(in_chars, sent, up_credits, status)
        row = {
            "ts": int(time.time()),
            "rid": request_id,
            "up_rid": upstream_request_id,
            "model": model,
            "voice": voice,
            "in_chars": in_chars,      # 客户提交的量（input 口径的计费依据）
            "sent_chars": sent,        # 实际合成 = 成本依据
            "bill_chars": n,
            "bill_source": src,        # input / chars / upstream / failed
            # 计费依据：把算式写进账本，客户质疑时不用翻代码
            "bill_rule": rule_text(n, in_chars, up_credits, status, src),
            "up_credits": up_credits,
            "status": status,
            "ms": elapsed_ms,
            "mode": BILLING_MODE,
            "ip": client_ip,
            "token": token_name,
        }
        if error:
            row["error"] = error[:200]
        if extra:
            row.update(extra)

        path = _today_path()
        with _lock:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _cleanup_old(BILLING_LOG_DAYS)
    except Exception as e:  # noqa: BLE001
        logger.warning("billing log write failed (request %s): %s", request_id, e)


def stats(day: str | None = None) -> dict:
    """汇总某天账本，给 /v1/usage 与对账脚本用。

    返回 {requests, succeeded, failed, in_chars, bill_chars, up_credits,
          ok_bill_chars, ok_up_credits, ok_sent_chars,
          saved_chars, saved_ratio, credits_per_char, gross_margin_ratio}

    `saved_chars` = 原始输入 − 实际合成 = **清理掉的字符数**。

    ⚠️ 语义随口径变，别读错：
      input 口径（默认）下，客户按 in_chars 付费，这部分**仍然收客户的钱**，
      但上游只收 sent —— 所以 saved_chars 是**我方成本节省**，不是客户折扣。
      chars 口径下，它才是真正的"没向客户收的部分"。

    ## gross_margin_ratio 的口径说明（2026-10-22 更正）

    ⚠️ **这个字段不是真毛利率**，历史实现把它当毛利率用是错的：

        旧写法: 1 - up_credits / bill_chars

    分子是**上游积分**（ElevenLabs character-cost），分母是**字符数** ——
    **两个不同量纲的比值，数学上没有意义**。上游约 0.48 积分/字符
    （`up_credits / sent_chars`），所以这个数会稳定落在负数附近，
    切换到 input 口径后还会再变一次，看着像"毛利率恶化了"，其实只是量纲变了。

    现在改为：

        credits_per_char = ok_up_credits / ok_sent_chars      上游真实单价（积分/字符）
        gross_margin_ratio = 1 - up_cost_usd / revenue_usd    真毛利率

    其中 `up_cost_usd = ok_up_credits / UP_CREDITS_PER_USD`、
    `revenue_usd = ok_bill_chars / CHARS_PER_USD`。
    缺少这两项配置时 `gross_margin_ratio` 返回 None —— **宁可不给，也不给错数**。
    """
    path = _today_path() if not day else os.path.join(BILLING_LOG_DIR, "%s.jsonl" % day)
    out = {"requests": 0, "succeeded": 0, "failed": 0,
           "in_chars": 0, "bill_chars": 0, "up_credits": 0,
           "ok_bill_chars": 0, "ok_up_credits": 0, "ok_sent_chars": 0,
           "sent_chars": 0, "saved_chars": 0, "saved_ratio": None,
           "credits_per_char": None,
           "gross_margin_ratio": None, "path": path}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out["requests"] += 1
                ok = r.get("status") == 200
                if ok:
                    out["succeeded"] += 1
                else:
                    out["failed"] += 1
                out["in_chars"] += r.get("in_chars") or 0
                out["bill_chars"] += r.get("bill_chars") or 0
                out["up_credits"] += r.get("up_credits") or 0
                if ok:
                    out["ok_bill_chars"] += r.get("bill_chars") or 0
                    out["ok_up_credits"] += r.get("up_credits") or 0
                    # 清理掉的字符：原始输入 - 实际合成
                    raw = r.get("in_chars") or 0
                    sent = r.get("sent_chars")
                    sent = raw if sent is None else sent
                    out["sent_chars"] += sent
                    out["ok_sent_chars"] += sent
                    if raw > sent:
                        out["saved_chars"] += raw - sent
    except OSError as e:
        logger.warning("billing stats read failed: %s", e)
    if out["in_chars"] and out["saved_chars"]:
        out["saved_ratio"] = round(out["saved_chars"] / out["in_chars"], 4)

    # --- 上游真实单价：积分 / 字符。同量纲，有意义 ---
    if out["ok_sent_chars"] and out["ok_up_credits"]:
        out["credits_per_char"] = round(
            out["ok_up_credits"] / out["ok_sent_chars"], 4)

    # --- 毛利率：必须先把两边都换成美元，否则是拿积分除字符 ---
    # 只算成功请求：失败请求不收费但上游可能扣了钱，
    # 混进来会把毛利率算歪（那是"白扣"损失，不是毛利）
    if (out["ok_bill_chars"] and out["ok_up_credits"]
            and UP_CREDITS_PER_USD > 0 and CHARS_PER_USD > 0):
        up_cost_usd = out["ok_up_credits"] / UP_CREDITS_PER_USD
        revenue_usd = out["ok_bill_chars"] / CHARS_PER_USD
        if revenue_usd > 0:
            out["gross_margin_ratio"] = round(1 - up_cost_usd / revenue_usd, 4)
            out["up_cost_usd"] = round(up_cost_usd, 6)
            out["revenue_usd"] = round(revenue_usd, 6)
    return out


def window_stats(days: int = 30) -> dict:
    """聚合最近 N 天账本，返回与 stats() 同构的汇总（含 gross_margin_ratio）。

    /v1/usage 的「窗口」视图需要跨天累计 up_credits（成本依据）与
    bill_chars（计费依据）才能算真毛利率——usage_by_token() 只按 token 聚合、
    不含 bill_chars，故这里单列一个窗口聚合，逻辑与 stats() 完全对齐。
    """
    from datetime import date, timedelta

    out = {"requests": 0, "succeeded": 0, "failed": 0,
           "in_chars": 0, "bill_chars": 0, "up_credits": 0,
           "ok_bill_chars": 0, "ok_up_credits": 0, "ok_sent_chars": 0,
           "sent_chars": 0, "saved_chars": 0, "saved_ratio": None,
           "credits_per_char": None,
           "gross_margin_ratio": None}
    today = date.today()
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(BILLING_LOG_DIR, "%s.jsonl" % d)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    out["requests"] += 1
                    ok = r.get("status") == 200
                    if ok:
                        out["succeeded"] += 1
                    else:
                        out["failed"] += 1
                    out["in_chars"] += r.get("in_chars") or 0
                    out["bill_chars"] += r.get("bill_chars") or 0
                    out["up_credits"] += r.get("up_credits") or 0
                    if ok:
                        out["ok_bill_chars"] += r.get("bill_chars") or 0
                        out["ok_up_credits"] += r.get("up_credits") or 0
                        raw = r.get("in_chars") or 0
                        sent = r.get("sent_chars")
                        sent = raw if sent is None else sent
                        out["sent_chars"] += sent
                        out["ok_sent_chars"] += sent
                        if raw > sent:
                            out["saved_chars"] += raw - sent
        except OSError as e:
            logger.warning("window stats read failed for %s: %s", path, e)
    if out["in_chars"] and out["saved_chars"]:
        out["saved_ratio"] = round(out["saved_chars"] / out["in_chars"], 4)
    if out["ok_sent_chars"] and out["ok_up_credits"]:
        out["credits_per_char"] = round(out["ok_up_credits"] / out["ok_sent_chars"], 4)
    if (out["ok_bill_chars"] and out["ok_up_credits"]
            and UP_CREDITS_PER_USD > 0 and CHARS_PER_USD > 0):
        up_cost_usd = out["ok_up_credits"] / UP_CREDITS_PER_USD
        revenue_usd = out["ok_bill_chars"] / CHARS_PER_USD
        if revenue_usd > 0:
            out["gross_margin_ratio"] = round(1 - up_cost_usd / revenue_usd, 4)
            out["up_cost_usd"] = round(up_cost_usd, 6)
            out["revenue_usd"] = round(revenue_usd, 6)
    return out

def usage_by_token(days: int = 30) -> list[dict]:
    """按客户令牌聚合最近 N 天的用量 —— 给用量看板用。

    按 `token` 分组（只存密钥前 6 位，不落完整密钥）。
    返回按节省量降序排列的列表。

    `saved_chars` 的语义随口径变，别读错：
      input 口径（默认）下客户按 in_chars 付费，这段是**我方成本节省**；
      chars 口径下它才是真正的「没向客户收的部分」。
    """
    from datetime import date, timedelta

    agg: dict[str, dict] = {}
    today = date.today()
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(BILLING_LOG_DIR, "%s.jsonl" % d)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("status") != 200:
                        continue
                    tok = r.get("token") or "<anonymous>"
                    a = agg.setdefault(tok, {
                        "token": tok, "requests": 0,
                        "in_chars": 0, "sent_chars": 0, "up_credits": 0,
                        "saved_chars": 0, "last_seen": 0,
                    })
                    raw = r.get("in_chars") or 0
                    sent = r.get("sent_chars")
                    sent = raw if sent is None else sent
                    a["requests"] += 1
                    a["in_chars"] += raw
                    a["sent_chars"] += sent
                    a["up_credits"] += r.get("up_credits") or 0
                    if raw > sent:
                        a["saved_chars"] += raw - sent
                    a["last_seen"] = max(a["last_seen"], r.get("ts") or 0)
        except OSError as e:
            logger.warning("usage scan failed for %s: %s", path, e)

    rows = list(agg.values())
    for r in rows:
        r["saved_ratio"] = round(r["saved_chars"] / r["in_chars"], 4) if r["in_chars"] else 0.0
    rows.sort(key=lambda x: x["saved_chars"], reverse=True)
    return rows

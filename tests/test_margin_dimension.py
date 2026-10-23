# -*- coding: utf-8 -*-
"""毛利率量纲正确性 —— `stats()` 的 `gross_margin_ratio`。

## 为什么单独一个文件

2026-10-22 发现历史实现是：

    gross_margin_ratio = 1 - up_credits / bill_chars

分子是**上游积分**，分母是**字符数** —— 两个不同量纲的比值，**数学上无意义**。
上游约 0.48 积分/字符，所以这个数稳定落在 1 附近甚至负数，看着像"毛利率 90%+"
或"毛利率 -50%"，实际上什么都不是。更糟的是切换计费口径后它会再变一次，
让人误以为毛利率恶化了。

这类 bug 的特点是**不报错、不崩溃、数字还挺像回事**，只能靠断言固定住。
"""
import json
import os

import pytest

from app import billing


def _write_ledger(tmp_path, rows):
    p = os.path.join(str(tmp_path), "2026-10-22.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    return tmp_path


def test_margin_is_none_without_price_config(ledger, monkeypatch):
    """没配单价 → 必须返回 None，不能返回一个假的毛利率。

    "宁可不给，也不给错数" —— 这是本次修复的核心约束。
    """
    monkeypatch.setattr(billing, "UP_CREDITS_PER_USD", 0.0)
    monkeypatch.setattr(billing, "CHARS_PER_USD", 0.0)
    _write_ledger(ledger, [{
        "status": 200, "in_chars": 100, "sent_chars": 100, "bill_chars": 100,
        "up_credits": 48, "bill_source": "input"}])

    s = billing.stats("2026-10-22")
    assert s["gross_margin_ratio"] is None, (
        "未配单价时必须返回 None；返回数字说明又在拿积分除字符")
    assert "up_cost_usd" not in s
    assert "revenue_usd" not in s


def test_margin_uses_usd_on_both_sides(ledger, monkeypatch):
    """配了单价 → 两边都换美元后算，结果必须是真毛利率。

    构造：上游 48 积分，我方收 100 字符。
      UP_CREDITS_PER_USD = 4545.4545   → 成本 = 48 / 4545.4545 = $0.01056
      CHARS_PER_USD      = 12500       → 收入 = 100 / 12500   = $0.008
    毛利率 = 1 - 0.01056/0.008 = 1 - 1.32 = -0.32（负毛利，卖亏了）

    这个样本故意选成负毛利：它能证明公式真的在算美元，
    而不是碰巧凑出一个好看的数。
    """
    monkeypatch.setattr(billing, "UP_CREDITS_PER_USD", 4545.4545)
    monkeypatch.setattr(billing, "CHARS_PER_USD", 12500.0)
    _write_ledger(ledger, [{
        "status": 200, "in_chars": 100, "sent_chars": 100, "bill_chars": 100,
        "up_credits": 48, "bill_source": "input"}])

    s = billing.stats("2026-10-22")
    assert s["gross_margin_ratio"] == pytest.approx(-0.32, abs=0.01), (
        "毛利率应是 -32%%（卖亏），实际 %r" % s["gross_margin_ratio"])
    assert s["up_cost_usd"] == pytest.approx(0.01056, abs=1e-5)
    assert s["revenue_usd"] == pytest.approx(0.008, abs=1e-6)


def test_margin_positive_when_price_above_cost(ledger, monkeypatch):
    """售价高于成本 → 毛利率为正。"""
    # 成本：48 / 4545.4545 = $0.01056 / 100 字符
    # 售价：100 字符 / 12500 = $0.008  ← 太便宜
    # 换成 100 字符卖 $0.02 → CHARS_PER_USD = 5000
    monkeypatch.setattr(billing, "UP_CREDITS_PER_USD", 4545.4545)
    monkeypatch.setattr(billing, "CHARS_PER_USD", 5000.0)
    _write_ledger(ledger, [{
        "status": 200, "in_chars": 100, "sent_chars": 100, "bill_chars": 100,
        "up_credits": 48, "bill_source": "input"}])

    s = billing.stats("2026-10-22")
    # 1 - 0.01056/0.02 = 1 - 0.528 = 0.472
    assert s["gross_margin_ratio"] == pytest.approx(0.472, abs=0.01)


def test_margin_only_counts_successful_requests(ledger, monkeypatch):
    """失败请求不进毛利率 —— 它上游可能扣了钱，但那是"白扣"损失不是毛利。"""
    monkeypatch.setattr(billing, "UP_CREDITS_PER_USD", 4545.4545)
    monkeypatch.setattr(billing, "CHARS_PER_USD", 5000.0)
    _write_ledger(ledger, [
        {"status": 200, "in_chars": 100, "sent_chars": 100,
         "bill_chars": 100, "up_credits": 48, "bill_source": "input"},
        # 失败的：bill=0 但 up_credits=10（上游已扣），不能拉低毛利率
        {"status": 500, "in_chars": 100, "sent_chars": 100,
         "bill_chars": 0, "up_credits": 10, "bill_source": "failed"},
    ])
    s = billing.stats("2026-10-22")
    assert s["gross_margin_ratio"] == pytest.approx(0.472, abs=0.01)


def test_credits_per_char_is_same_dimension(ledger):
    """credits_per_char = 积分 / 字符 —— 同量纲，可以有两个同时存在的口径。"""
    _write_ledger(ledger, [
        {"status": 200, "in_chars": 100, "sent_chars": 80,
         "bill_chars": 100, "up_credits": 40, "bill_source": "input"},
        {"status": 200, "in_chars": 100, "sent_chars": 20,
         "bill_chars": 100, "up_credits": 8, "bill_source": "input"},
    ])
    s = billing.stats("2026-10-22")
    # 48 积分 / 100 sent 字符 = 0.48
    assert s["ok_sent_chars"] == 100
    assert s["ok_up_credits"] == 48
    assert s["credits_per_char"] == pytest.approx(0.48, abs=1e-6)


def test_sent_chars_used_as_cost_base_not_bill_chars(ledger):
    """成本基数是 sent_chars，不是 bill_chars —— input 口径下两者不等。

    这条是口径归一最容易踩的坑：账单按 input 记（100），
    但上游只收 sent（80）。若用 bill_chars 算成本，会高估 25%。
    """
    _write_ledger(ledger, [
        {"status": 200, "in_chars": 100, "sent_chars": 80,
         "bill_chars": 100, "up_credits": 40, "bill_source": "input"},
    ])
    s = billing.stats("2026-10-22")
    assert s["ok_bill_chars"] == 100      # 计费依据
    assert s["ok_sent_chars"] == 80       # 成本依据
    # 40 积分 / 80 字符 = 0.5，不是 40/100=0.4
    assert s["credits_per_char"] == pytest.approx(0.5, abs=1e-6)

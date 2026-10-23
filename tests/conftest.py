# -*- coding: utf-8 -*-
"""pytest 全局夹具：**把账本目录钉死到临时目录**。

## 为什么必须有这个文件

2026-10-22 实测发现：`logs/billing/` 里 3072 行中 **2827 行 ip 是 `testclient`**，
全部是测试流量。原因是 18 个测试文件里有 10 个会发 HTTP 请求，却只有 8 个
自己 `monkeypatch.setattr(billing, "BILLING_LOG_DIR", tmp_path)` —— 剩下那些
每跑一次 `pytest` 就往**真实账本目录**追加一批行。

后果很实在：对账脚本读到的"生产账本"里混着一堆 `in_chars=2`、`error={"detail":"boom"}`
的测试桩数据，算出来的差额、省幅、毛利率全是假的。本机 980KB 的
`logs/billing/2026-10-21.jsonl` 就是这么攒出来的，而服务器上真实账本只有 6 行。

## 做法

在 `pytest` 启动时（`pytest_configure`）把 `app.billing` 的模块级常量改指向
一个本次会话专用的临时目录，并默认关闭写盘。测试若要验证"真的写了什么"，
用 `tmp_path` + `monkeypatch` 显式覆盖回自己的目录即可（覆盖会生效，
因为 `record()` 每次都读模块属性，不走闭包缓存）。

## 代价（必须诚实说明）

这是**兜底**，不是替代。测试自己声明期望的账本目录仍然更好读。
本文件保证的是：**忘了声明的测试也污染不到真实账本**。
"""
import os
import shutil
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_billing_ledger():
    """整个测试会话期间，账本目录一律指向临时目录。

    autouse + session scope：不用任何测试显式引用，也不给"漏网的"测试留缝。
    """
    from app import billing

    old_dir = billing.BILLING_LOG_DIR
    old_off = billing.BILLING_LOG_OFF

    tmp = tempfile.mkdtemp(prefix="adapter-test-ledger-")
    billing.BILLING_LOG_DIR = tmp
    # 默认不写盘：绝大多数测试不关心账本，写了只是噪音。
    # 想验证写盘行为的测试会自己 setattr 回 False 并把目录换成 tmp_path。
    billing.BILLING_LOG_OFF = True

    try:
        yield tmp
    finally:
        billing.BILLING_LOG_DIR = old_dir
        billing.BILLING_LOG_OFF = old_off
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def isolated_ledger_dir():
    """给需要真读真写的测试用：一个干净的、测完自动删的账本目录。"""
    from app import billing

    tmp = tempfile.mkdtemp(prefix="adapter-ledger-case-")
    old_dir = billing.BILLING_LOG_DIR
    old_off = billing.BILLING_LOG_OFF
    billing.BILLING_LOG_DIR = tmp
    billing.BILLING_LOG_OFF = False
    try:
        yield tmp
    finally:
        billing.BILLING_LOG_DIR = old_dir
        billing.BILLING_LOG_OFF = old_off
        shutil.rmtree(tmp, ignore_errors=True)

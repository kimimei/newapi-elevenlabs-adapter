# -*- coding: utf-8 -*-
"""A 方案验收（异步路径）：jobs 的口径必须与同步一致。

**历史 bug**：`jobs.py:257` 原来把 `bill_chars_by_mode` 的两个参数都传成 raw，
导致同步按 sent、异步按 raw —— 同一客户两条路径两个价。
A 口径下两者都应是 raw（= 客户提交的原始字符数），本文件钉死这个不变量。

用 `httpx.ASGITransport` 走真异步：TestClient 的同步模式会阻塞事件循环，
后台 worker 起不来，任务永远停在 queued（这个坑踩过一次）。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing, jobs  # noqa: E402

PCM = b"\x01\x00\x02\x00" * 200


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "_last_cleanup_day", None)
    return tmp_path


def _rows(tmp_path):
    out = []
    for f in sorted(tmp_path.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _patch_upstream(monkeypatch):
    from app import main as m

    async def _fake(payload_text, *a, **k):
        return httpx.Response(200, content=PCM, headers={"character-cost": "1"})

    for mod in (m, jobs):
        monkeypatch.setattr(mod, "resolve_api_key", lambda *a, **kw: "sk_dummy")
        monkeypatch.setattr(mod, "call_elevenlabs_tts", _fake)
    monkeypatch.setattr(m, "MOCK", False)


def _run_job(monkeypatch, text):
    """提交一个异步任务并等它跑完，返回 (job_view, ledger_row)。"""
    from app import main as m

    async def _go():
        transport = httpx.ASGITransport(app=m.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as c:
            r = await c.post("/v1/audio/speech/jobs",
                             json={"model": "tts-1", "voice": "rachel", "input": text})
            assert r.status_code in (200, 202), r.text
            job_id = r.json()["id"]
            for _ in range(200):
                g = await c.get("/v1/audio/speech/jobs/" + job_id)
                if g.status_code == 200:
                    view = g.json()
                    if view.get("status") in ("succeeded", "failed"):
                        return view
                await asyncio.sleep(0.05)
            return None

    return asyncio.run(_go())


def test_async_billing_uses_input_length(monkeypatch, ledger):
    """异步任务：计费 = 客户提交的原始字符数，与同步、与 new-api 全部一致。"""
    monkeypatch.setattr(jobs, "JOBS_ENABLED", True)
    monkeypatch.setattr(jobs, "JOBS_MAX_PENDING", 10)
    monkeypatch.setattr(jobs, "JOBS_WORKERS", 1)
    _patch_upstream(monkeypatch)

    text = "你\u200b好\u200b世界"          # 提交 6，清理后 4
    view = _run_job(monkeypatch, text)
    assert view is not None, "任务未在预期时间内完成"
    assert view["status"] == "succeeded", view

    submit_len = len(text)
    assert view["usage_chars"] == submit_len
    assert view["usage_source"] == "input"

    row = _rows(ledger)[-1]
    assert row["bill_chars"] == submit_len, "账本口径与同步路径不一致"
    assert row["in_chars"] == submit_len
    assert row["sent_chars"] == 4, "sent 是成本依据"
    assert row["bill_source"] == "input"


def test_async_and_sync_same_billing(monkeypatch, ledger):
    """同一段文本，同步与异步必须算出同一个计费数。

    这是历史 bug 的直接回归测试：原来同步按 sent、异步按 raw。
    """
    monkeypatch.setattr(jobs, "JOBS_ENABLED", True)
    monkeypatch.setattr(jobs, "JOBS_MAX_PENDING", 10)
    monkeypatch.setattr(jobs, "JOBS_WORKERS", 1)
    _patch_upstream(monkeypatch)

    text = "第一段\r\n第二段  你好"
    view = _run_job(monkeypatch, text)
    assert view is not None and view["status"] == "succeeded"

    # 同步路径
    from fastapi.testclient import TestClient
    from app import main as m

    r = TestClient(m.app).post("/v1/audio/speech",
                                json={"model": "tts-1", "voice": "rachel", "input": text})
    assert r.status_code == 200
    sync_bill = int(r.headers["X-Usage-Input-Characters"])

    assert view["usage_chars"] == sync_bill, (
        "同步 %d != 异步 %d —— 同一客户两条路径两个价" % (sync_bill, view["usage_chars"]))

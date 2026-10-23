# -*- coding: utf-8 -*-
"""T5 长文本异步任务的回归。

这些用例必须能抓住回退，所以每条都端到端打真实请求，
不测「函数存在」—— 那种测法在实现被改回旧写法时照样全绿。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.jobs as jobs          # noqa: E402
import app.main as m             # noqa: E402
from app.jobs import JobRequest      # noqa: E402
from app.voices import load_voices   # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    """开启任务接口 + 拦掉真实上游（否则一次跑测就烧一次额度）。"""
    monkeypatch.setattr(jobs, "JOBS_ENABLED", True)
    monkeypatch.setattr(jobs, "_voices", lambda: load_voices())
    monkeypatch.setattr(m, "MOCK", False)

    async def fake_tts(text, model, voice_id, key, fmt, speed, extra=None):
        class R:
            status_code = 200
            content = b"FAKEAUDIO" * 64
            text = ""
        R.req = (text, model, voice_id, key, fmt, speed)
        return R()

    seen = {}

    async def spy(text, *a, **kw):
        seen["last"] = (text, a, kw)
        return await fake_tts(text, *a, **kw)

    monkeypatch.setattr(jobs, "call_elevenlabs_tts", spy)
    monkeypatch.setattr(m, "MAX_CHARS", 0)
    jobs._jobs.clear()
    jobs._pending.clear()
    yield seen
    jobs._jobs.clear()
    jobs._pending.clear()


def _wait(client, jid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get("/v1/audio/speech/jobs/%s" % jid).json()
        if s["status"] in ("succeeded", "failed", "cancelled"):
            return s
        time.sleep(0.02)
    return client.get("/v1/audio/speech/jobs/%s" % jid).json()


# ---------- 提交 → 轮询 → 下载 ----------

def test_jobs_disabled_by_default():
    """默认关闭：不需要长文本的人不该被多出来的端点影响。"""
    c = TestClient(m.app)
    assert jobs.JOBS_ENABLED is False
    r = c.post("/v1/audio/speech/jobs", json={"input": "你好", "voice": "rachel"})
    assert r.status_code == 404
    assert "JOBS_ENABLED" in r.text


def test_submit_returns_202_immediately(env):
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_test"},
               json={"model": "eleven_v3", "voice": "rachel", "input": "长文本" * 100})
    assert r.status_code == 202, r.text
    j = r.json()
    assert j["status"] == "queued"
    assert j["chars"] == 300
    assert j["poll_url"] == "/v1/audio/speech/jobs/%s" % j["id"]
    # 必须给出等待时间预估：客户看不到就会以为卡死，然后反复提交
    assert j["estimated_seconds"] > 0
    assert r.headers.get("Location") == j["poll_url"]


def test_full_cycle_produces_downloadable_audio(env):
    c = TestClient(m.app)
    jid = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_test"},
                 json={"model": "eleven_v3", "voice": "rachel",
                       "input": "配音测试" * 50, "response_format": "mp3"}).json()["id"]
    s = _wait(c, jid)
    assert s["status"] == "succeeded", s
    assert s["audio_bytes"] > 0
    assert s["download_url"].endswith("/audio")
    assert s["expires_in"] > 0

    a = c.get(s["download_url"])
    assert a.status_code == 200
    assert a.headers["content-type"] == "audio/mpeg"
    # 计费回传头不能丢：中转站靠它对账。"配音测试" 4 字 × 50 = 200
    assert a.headers["X-Usage-Input-Characters"] == str(s["chars"]) == "200"
    assert a.headers["X-ElevenLabs-Voice"] == "21m00Tcm4TlvDq8ikWAM"


def test_audio_before_done_returns_409(env):
    """没做完就下载要说清楚，而不是404 或500。"""
    c = TestClient(m.app)
    jobs._jobs["pending1"] = {"id": "pending1", "status": "queued", "chars": 10,
                              "created_at": time.time(), "model": "eleven_v3",
                              "voice": "rachel", "response_format": "mp3"}
    r = c.get("/v1/audio/speech/jobs/pending1/audio")
    assert r.status_code == 409
    assert "queued" in r.text


# ---------- 参数校验 ----------

def test_missing_key_returns_401(env):
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech/jobs", json={"input": "你好", "voice": "rachel"})
    assert r.status_code == 401
    assert "API Key" in r.text


def test_empty_input_rejected(env):
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_x"},
               json={"input": "   "})
    assert r.status_code == 400


def test_chars_over_limit_rejected(env, monkeypatch):
    """单任务超长要拒：ElevenLabs 本身有上限，超了就是白等一趟。"""
    monkeypatch.setattr(jobs, "JOBS_MAX_CHARS", 100)
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_x"},
               json={"input": "字" * 200, "voice": "rachel"})
    assert r.status_code == 400
    assert "JOBS_MAX_CHARS" in r.text


def test_bad_voice_becomes_failed_job_not_crash(env, monkeypatch):
    """voice 非法应该在任务里 failed + 带 fix，而不是把协程炸掉。"""
    async def boom(text, model, voice_id, key, fmt, speed, extra=None):
        raise ValueError("voice_id 不合法：收到 'x'")

    monkeypatch.setattr(jobs, "call_elevenlabs_tts", boom)
    c = TestClient(m.app)
    jid = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_x"},
                 json={"model": "eleven_v3", "voice": "rachel", "input": "长" * 50}).json()["id"]
    s = _wait(c, jid)
    assert s["status"] == "failed"
    assert s["fix"]


def test_upstream_error_carries_actionable_fix(env, monkeypatch):
    """上游 401 时 fix 必须指向具体动作（key 失效是最常见接入问题）。"""
    async def unauthorized(text, model, voice_id, key, fmt, speed, extra=None):
        class R:
            status_code = 401
            content = b""
            text = '{"detail":{"status":"invalid_api_key"}}'

            def json(self):
                return {"detail": {"status": "invalid_api_key"}}
        return R()

    monkeypatch.setattr(jobs, "call_elevenlabs_tts", unauthorized)
    c = TestClient(m.app)
    jid = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_x"},
                 json={"model": "eleven_v3", "voice": "rachel", "input": "长" * 50}).json()["id"]
    s = _wait(c, jid)
    assert s["status"] == "failed"
    assert "sk_" in (s["fix"] or ""), "fix 没提到 key 格式：%r" % s.get("fix")


# ---------- 队列与取消 ----------

def test_queue_full_returns_429(env, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_MAX_PENDING", 1)
    for i in range(1):
        jobs._jobs["q%d" % i] = {"id": "q%d" % i, "status": "queued", "chars": 1,
                                  "created_at": time.time(), "model": "eleven_v3",
                                  "voice": "rachel", "response_format": "mp3"}
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_x"},
               json={"input": "长" * 50, "voice": "rachel"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "10"


def test_cancel_queued_job(env):
    """排队中的任务必须能取消，否则队列永远堵着。

    必须走真事件循环：TestClient 每次请求结束就关闭loop，
    排队里的后台协程会被连带取消 —— 用 TestClient 根本观察不到 queued 态。
    """
    import app.config as cfg
    import app.jobs as J

    async def main():
        old_tts, old_sem, old_cache = J.call_elevenlabs_tts, J._worker_lock, J._voices_cache
        J.call_elevenlabs_tts = None      # 不会被调到（信号量 0 挡住）
        J._worker_lock = asyncio.Semaphore(0)
        J._voices_cache = load_voices()
        try:
            req = JobRequest(model="eleven_v3", voice="rachel", input="长" * 50)
            scope = Request({"type": "http", "headers": [
                (b"authorization", b"Bearer sk_x")]})
            r = await J.submit_job(req, scope)
            jid = json.loads(r.body)["id"]
            assert J._jobs[jid]["status"] == "queued", "没进排队态"
            resp = await J.cancel_job(jid)
            assert (resp["status"] if isinstance(resp, dict)
                    else json.loads(resp.body)["status"]) == "cancelled"
            assert J._jobs[jid]["status"] == "cancelled"
        finally:
            J.call_elevenlabs_tts = old_tts
            J._worker_lock = old_sem
            J._voices_cache = old_cache
            J._jobs.clear()
            J._pending.clear()

    asyncio.run(main())


def test_cancel_finished_returns_409(env):
    """已完成的不能"取消" —— 那是在骗客户端，实际不会退钱也不返音频。"""
    c = TestClient(m.app)
    jid = c.post("/v1/audio/speech/jobs", headers={"Authorization": "Bearer sk_test"},
                 json={"model": "eleven_v3", "voice": "rachel", "input": "长" * 50}).json()["id"]
    _wait(c, jid)
    r = c.delete("/v1/audio/speech/jobs/%s" % jid)
    assert r.status_code == 409


# ---------- 内存与可观测 ----------

def test_expired_jobs_purged(env, monkeypatch):
    """不清的话，一个 5000 字任务几 MB，长跑实例会被音频字节吃光。"""
    monkeypatch.setattr(jobs, "JOBS_TTL_SECONDS", 1)
    jobs._jobs["old"] = {"id": "old", "status": "succeeded", "chars": 10,
                         "created_at": time.time() - 9999,
                         "finished_at": time.time() - 9999,
                         "model": "eleven_v3", "voice": "rachel",
                         "response_format": "mp3", "audio": b"x" * 999999}
    jobs._jobs["new"] = {"id": "new", "status": "succeeded", "chars": 10,
                         "created_at": time.time(),
                         "finished_at": time.time(),
                         "model": "eleven_v3", "voice": "rachel",
                         "response_format": "mp3", "audio": b"x"}
    jobs._purge_expired()
    assert "old" not in jobs._jobs, "过期任务未清理"
    assert "new" in jobs._jobs, "未过期任务被误删"


def test_list_shows_queue_depth(env):
    """运维视角：接了监控就能在告警里看见"任务堆积"。"""
    jobs._jobs["a"] = {"id": "a", "status": "queued", "chars": 1,
                       "created_at": time.time(), "model": "eleven_v3",
                       "voice": "rachel", "response_format": "mp3"}
    c = TestClient(m.app)
    j = c.get("/v1/audio/speech/jobs").json()
    assert j["queue_depth"] == 1
    assert j["by_status"]["queued"] == 1
    assert j["workers"] >= 1


def test_healthz_exposes_queue_depth(env, monkeypatch):
    """healthz 从 app.config 读开关，必须一起 patch 两处。"""
    import app.config as cfg
    monkeypatch.setattr(cfg, "JOBS_ENABLED", True)
    c = TestClient(m.app)
    h = c.get("/healthz").json()
    assert h["jobs"]["enabled"] is True
    assert "queue_depth" in h["jobs"]


def test_worker_semaphore_bounds_concurrency():
    """并发闸门必须真的把并发卡在 workers 上。

    这条走真事件循环：同步 TestClient 每次请求都新建loop，
    后台协程等不到东西，也测不出"同时只有 N 个在跑"。
    """
    import app.config as cfg
    import app.jobs as J

    async def main():
        started = []
        gate = asyncio.Event()
        peak = 0

        async def slow(text, model, voice_id, key, fmt, speed, extra=None):
            nonlocal peak
            started.append(1)
            peak = max(peak, len(started))
            await gate.wait()

            class R:
                status_code = 200
                content = b"ID3"
                text = ""
            return R()

        old_tts, old_sem = J.call_elevenlabs_tts, J._worker_lock
        old_cache = J._voices_cache
        old_workers = J.JOBS_WORKERS
        J.JOBS_WORKERS = 2
        J.call_elevenlabs_tts = slow
        J._worker_lock = None          # 让 _sem() 按 JOBS_WORKERS 真实创建
        J._voices_cache = load_voices()
        J._jobs.clear()
        try:
            # _run_job 要求任务记录已存在（正常路径由 submit_job 建好）
            sem = J._sem()
            assert sem._value == 2, "信号量初值=%d，应等于 JOBS_WORKERS=2" % sem._value
            for i in range(5):
                J._jobs["j%d" % i] = {
                    "id": "j%d" % i, "status": "queued", "chars": 20,
                    "created_at": time.time(), "model": "eleven_v3",
                    "voice": "rachel", "response_format": "mp3"}
            tasks = [
                asyncio.create_task(J._run_job(
                    "j%d" % i,
                    {"model": "eleven_v3", "voice": "rachel", "input": "长" * 10,
                     "speed": None, "response_format": "mp3", "api_key": "sk_x"},
                    J._voices(), J._sem()))
                for i in range(5)
            ]
            await asyncio.sleep(0.15)
            # 门没开：只有 2 个能拿到信号量，其余 3 个必须还堵着
            assert peak == 2, "并发闸门失效：同时跑了 %d 个（应=2）" % peak
            gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            done = [v for v in J._jobs.values() if v["status"] == "succeeded"]
            assert len(done) == 5, "有任务没跑完：%s" % [v["status"] for v in J._jobs.values()]
        finally:
            J.call_elevenlabs_tts = old_tts
            J._worker_lock = old_sem
            J._voices_cache = old_cache
            J.JOBS_WORKERS = old_workers
            J._jobs.clear()

    asyncio.run(main())
# -*- coding: utf-8 -*-
"""并发闸门 + 请求体上限（含 chunked 绕过）的回归。

这两条是安全边界，测试必须端到端 —— 断言中间变量的话，
把实现换成`pass` 照样全绿（第一版反证就是这么被骗的）。
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as cfg        # noqa: E402
import app.main as m            # noqa: E402


@pytest.fixture(autouse=True)
def _mock():
    m.MOCK = True
    yield
    m.MOCK = True
    m._CONCURRENCY[0] = 0


# ---------- 请求体上限 ----------

def test_content_length_over_limit_413(monkeypatch):
    """带 Content-Length 的超大请求：老逻辑就该拦住。"""
    monkeypatch.setattr(m, "MAX_BODY_BYTES", 1024)
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech", json={"model": "eleven_v3", "input": "字" * 2000})
    assert r.status_code == 413


def test_chunked_body_also_blocked(monkeypatch):
    """chunked 传输必须同样被拦。

    只看 Content-Length 时，chunked 请求压根不带这个头 → 检查直接跳过
    → 超大请求照样进到解析里把内存打满。这条是S3 的真实绕过路径。
    """
    monkeypatch.setattr(m, "MAX_BODY_BYTES", 2048)
    c = TestClient(m.app)

    def gen():
        # 分块吐 4KB，超过 2048 上限
        for _ in range(4):
            yield b'{"model":"eleven_v3","voice":"rachel","input":"' + "字".encode() * 1000 + b'"}'

    r = c.post("/v1/audio/speech", content=gen(),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 413, "chunked 绕过：返回了%s" % r.status_code
    assert "上限" in r.text


def test_chunked_under_limit_still_works(monkeypatch):
    """反向验证：不能把正常 chunked 请求也拦掉（回填 body 出错就会在这里暴露）。"""
    monkeypatch.setattr(m, "MAX_BODY_BYTES", 100 * 1024)
    c = TestClient(m.app)

    def gen():
        yield b'{"model":"eleven_v3",'
        yield b'"voice":"rachel","input":"\xe4\xbd\xa0\xe5\xa5\xbd"}'

    r = c.post("/v1/audio/speech", content=gen(),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    # body 丢了会解析不出 input / chars 不对
    assert r.headers.get("X-Usage-Input-Characters") == "2"


def test_error_points_to_async_api(monkeypatch):
    """超限提示必须指向异步接口，否则客户只会一直分段。"""
    monkeypatch.setattr(m, "MAX_BODY_BYTES", 512)
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech", json={"model": "eleven_v3", "input": "字" * 2000})
    assert r.status_code == 413
    assert "speech/jobs" in r.text, "413 没提示异步接口：%r" % r.text[:200]


# ---------- 并发闸门 ----------

def test_concurrency_limit_off_by_default():
    """默认不限并发：硬塞一个默认值进去会误伤正常客户。"""
    assert cfg.MAX_CONCURRENCY == 0


def test_concurrency_over_limit_returns_429(monkeypatch):
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 2)
    m._CONCURRENCY[0] = 2          # 假装已满
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech", json={"model": "eleven_v3", "input": "你好",
                                          "voice": "rachel"})
    assert r.status_code == 429, "并发超限未拦截：%s" % r.status_code
    assert r.headers.get("Retry-After") == "3"
    assert r.headers.get("X-Adapter-Concurrency") == "2/2"
    assert "并发" in r.text


def test_concurrency_counter_increments_and_releases(monkeypatch):
    """计数必须加上去、且请求结束后还回来 —— 漏还就是永久性自锁。"""
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 4)
    c = TestClient(m.app)
    assert m._inflight() == 0
    for _ in range(3):
        assert c.post("/v1/audio/speech", json={
            "model": "eleven_v3", "input": "你好", "voice": "rachel"}).status_code == 200
    assert m._inflight() == 0, "计数没归还，并发槽会被慢慢吃光"


def test_concurrency_slot_actually_taken_during_request(monkeypatch):
    """处理中的请求必须真的占住一个槽。

    反证发现的缺口：只断言"结束后归零"的测试，在把 `_CONCURRENCY[0] += 1`
    删掉之后照样全绿 —— 因为它只看结束状态。必须抓处理中的瞬时值。
    """
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 4)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_key")   # 不设则 401，上游根本不会被调
    seen = []

    async def probe(text, model, vid, key, fmt, speed, extra=None):
        seen.append(m._inflight())

        class R:
            status_code = 200
            content = b"ID3"
            text = ""
        return R()

    monkeypatch.setattr(m, "MOCK", False)
    monkeypatch.setattr(m, "call_elevenlabs_tts", probe)
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech", json={"model": "eleven_v3", "input": "你好",
                                          "voice": "rachel"})
    assert r.status_code == 200, r.text
    assert seen == [1], "处理期间并发计数=%r，应为 1" % (seen,)
    assert m._inflight() == 0


def test_second_slot_blocks_third_request(monkeypatch):
    """两个请求在飞时，第三个必须吃 429 —— 这是闸门的实际目的。"""
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 2)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_key")
    gate = threading.Event()
    inflight = 0
    lock = threading.Lock()

    async def slow(text, model, vid, key, fmt, speed, extra=None):
        nonlocal inflight
        with lock:
            inflight += 1
        gate.wait(5)
        with lock:
            inflight -= 1

        class R:
            status_code = 200
            content = b"ID3"
            text = ""
        return R()

    monkeypatch.setattr(m, "MOCK", False)
    monkeypatch.setattr(m, "call_elevenlabs_tts", slow)

    codes = []

    def hit():
        c = TestClient(m.app)
        codes.append(c.post("/v1/audio/speech", json={
            "model": "eleven_v3", "input": "你好", "voice": "rachel"}).status_code)

    ts = [threading.Thread(target=hit) for _ in range(2)]
    for t in ts:
        t.start()
    #等前两个占住槽
    end = time.time() + 5
    while time.time() < end:
        if m._inflight() >= 2:
            break
        time.sleep(0.02)
    assert m._inflight() == 2, "前两个请求没占住槽（当前 %d）" % m._inflight()

    c = TestClient(m.app)
    third = c.post("/v1/audio/speech", json={"model": "eleven_v3", "input": "你好",
                                               "voice": "rachel"})
    assert third.status_code == 429, "满槽时第三个请求未被拦：%s" % third.status_code
    gate.set()
    for t in ts:
        t.join(timeout=10)


def test_concurrency_released_on_502(monkeypatch):
    """上游异常时也必须归还，否则一次故障就打满全部槽位。"""
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 4)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_key")

    async def boom(*a, **kw):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(m, "call_elevenlabs_tts", boom)
    monkeypatch.setattr(m, "MOCK", False)
    c = TestClient(m.app, raise_server_exceptions=False)
    r = c.post("/v1/audio/speech", json={"model": "eleven_v3", "input": "你好",
                                          "voice": "rachel"})
    assert r.status_code == 502, "上游异常应转 502，实际 %s" % r.status_code
    assert m._inflight() == 0, "异常路径没归还并发槽"


def test_real_concurrent_requests_bounded(monkeypatch):
    """真并发下峰值不能超过上限 —— 用真实线程打，验证信号量真的在拦。"""
    monkeypatch.setattr(m, "MAX_CONCURRENCY", 3)
    peak = 0
    cur = 0
    lock = threading.Lock()

    async def slow(text, model, vid, key, fmt, speed, extra=None):
        nonlocal peak, cur
        with lock:
            cur += 1
            peak = max(peak, cur)
        await asyncio.sleep(0.08)

        class R:
            status_code = 200
            content = b"ID3"
            text = ""
        with lock:
            cur -= 1
        return R()

    monkeypatch.setattr(m, "MOCK", False)
    monkeypatch.setattr(m, "call_elevenlabs_tts", slow)

    results = []

    def hit():
        c = TestClient(m.app)
        results.append(c.post("/v1/audio/speech", json={
            "model": "eleven_v3", "input": "你好", "voice": "rachel"}).status_code)

    ts = [threading.Thread(target=hit) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    assert len(results) == 10, "有请求没返回，疑似死锁"
    assert peak <= 3, "并发峰值 %d 超过上限 3" % peak
    assert m._inflight() == 0

# ---------- X-Forwarded-For 伪造（真实漏洞的回归） ----------
#
# 背景：uvicorn[standard] 的 proxy_headers 默认 True，会用 XFF 覆盖
# request.client.host。实测（scripts/check_proxy_ip.py 第④例）：
# 白名单填 203.0.113.9 时，本机直连 127.0.0.1 本该 403，
# 带上 -H"X-Forwarded-For: 203.0.113.9" 就 200 了 —— 白名单形同虚设。

def test_run_py_disables_proxy_headers():
    """run.py 传给 uvicorn.run 的那个参数必须显式是 False。

    两个坑：
    1. 只在文件里搜 "proxy_headers=False" 会被文件顶部的说明文字骗过 ——
       那段文档里也写着这句话，把实参改成 True 之后依然搜得到。
       所以必须排除注释与字符串，只认实参。
    2. 只测「适配器代码里没读 XFF」不够：uvicorn 在框架层就替换了
       client.host，适配器代码里根本看不到。必须锁启动参数。
    """
    import ast
    src = (Path(__file__).resolve().parent.parent / "run.py").read_text("utf-8")
    tree = ast.parse(src)

    found = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "run"):
            for kw in node.keywords:
                if kw.arg == "proxy_headers":
                    found = kw.value
    assert found is not None, "run.py 里没找到 proxy_headers 实参 —— uvicorn 默认是 True，必须显式关"
    assert isinstance(found, ast.Constant) and found.value is False, (
        "proxy_headers 实参=%r，必须是 False：uvicorn 默认会用 X-Forwarded-For "
        "覆盖 client.host，导致任何人都能伪造一个头绕过 ADAPTER_ALLOW_IPS"
        % ast.dump(found))


def test_uvicorn_default_is_dangerous():
    """锁住前提：一旦 uvicorn 改了默认值，这条会提醒我们重新评估。"""
    from uvicorn.config import Config
    assert Config.__init__.__defaults__ is not None
    import inspect
    assert inspect.signature(Config.__init__).parameters["proxy_headers"].default is True, (
        "uvicorn 的 proxy_headers 默认值变了 —— run.py 里的显式 False 仍有效，"
        "但值得重新评估这个开关的语义")


def test_forwarded_header_cannot_bypass(monkeypatch):
    """带伪造 XFF 的请求仍按 TCP 对端判定。"""
    monkeypatch.setattr(m, "ALLOW_IPS", ["203.0.113.9"])
    c = TestClient(m.app)
    r = c.post("/v1/audio/speech", json={"model": "eleven_v3", "input": "你好",
                                          "voice": "rachel"},
               headers={"X-Forwarded-For": "203.0.113.9"})
    # TestClient 的 client 恒为 testclient，也不在白名单 —— 无论哪种都该拒
    assert r.status_code == 403, "伪造 XFF 绕过了白名单：%s" % r.status_code


def test_healthz_exempt_from_allowlist(monkeypatch):
    """白名单配错时/healthz 必须还能用 —— 它是唯一的自查手段。

    实测踩过：白名单生效时 /healthz 也403，导致无法判断服务是死是活。
    """
    monkeypatch.setattr(m, "ALLOW_IPS", ["203.0.113.9"])
    c = TestClient(m.app)
    r = c.get("/healthz")
    assert r.status_code == 200, "/healthz 被白名单拦了，配错时无法自查"

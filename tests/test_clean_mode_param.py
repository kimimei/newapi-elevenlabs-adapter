# -*- coding: utf-8 -*-
"""请求级清理档位：客户可主动加强，不可削弱服务端下限。

为什么"只能加强不能削弱"是硬规则：
运维把 ADAPTER_TEXT_CLEAN 设成 safe，是为了让那些零宽字符不流进上游
（它们会让发音模型产生莫名停顿，还白占字符额度）。
如果客户传一个 off 就能绕过，这个兜底形同虚设 ——
最终受害的是客户自己听到的音频质量，以及我们对上游的承诺。
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing, text_clean  # noqa: E402

PCM = b"\x01\x00\x02\x00" * 200
MD = "**粗体**和[链接](http://x.com)"   # normalize 会砍掉标记，safe 不动


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "_last_cleanup_day", None)
    return tmp_path


def _rows(p):
    out = []
    for f in sorted(p.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _post(monkeypatch, text, clean=None, capture=None):
    from fastapi.testclient import TestClient
    from app import main as m

    async def _fake(payload_text, *a, **k):
        if capture is not None:
            capture.append(payload_text)
        return httpx.Response(200, content=PCM, headers={"character-cost": "1"})

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _fake)
    monkeypatch.setattr(m, "MOCK", False)
    body = {"model": "tts-1", "voice": "rachel", "input": text}
    if clean is not None:
        body["text_clean"] = clean
    try:
        c = TestClient(m.app)
        return c.post("/v1/audio/speech", json=body)
    finally:
        monkeypatch.setattr(m, "MOCK", True)


# ---------- resolve_mode 单元 ----------

def test_resolve_default_when_absent():
    assert text_clean.resolve_mode(None, "safe") == "safe"
    assert text_clean.resolve_mode("", "safe") == "safe"


def test_resolve_allows_escalation():
    """客户可以把 safe 提到 normalize。"""
    assert text_clean.resolve_mode("normalize", "safe") == "normalize"


def test_resolve_blocks_downgrade():
    """客户不能把 safe 降到 off —— 否则服务端兜底被绕过。"""
    assert text_clean.resolve_mode("off", "safe") == "safe"
    assert text_clean.resolve_mode("safe", "normalize") == "normalize"


def test_resolve_unknown_falls_back():
    """无法识别的值退回默认，不报错（清理不是核心业务，不值得打挂请求）。"""
    assert text_clean.resolve_mode("turbo", "safe") == "safe"
    assert text_clean.resolve_mode("  ", "safe") == "safe"


def test_resolve_case_insensitive():
    assert text_clean.resolve_mode("NORMALIZE", "safe") == "normalize"
    assert text_clean.resolve_mode("Off", "off") == "off"


# ---------- 端到端 ----------

def test_normalize_removes_markdown(monkeypatch, ledger):
    """传 text_clean=normalize 时，Markdown 标记被去掉，省下字符。"""
    got = []
    r = _post(monkeypatch, MD, clean="normalize", capture=got)
    assert r.status_code == 200
    assert got == ["粗体和链接"]
    assert r.headers["X-Text-Clean-Mode"] == "normalize"
    assert int(r.headers["X-Text-Saved-Chars"]) > 0


def test_default_keeps_markdown(monkeypatch, ledger):
    """不传参数时保持 safe，Markdown 原样保留 —— 不默认替客户做判断。"""
    got = []
    r = _post(monkeypatch, MD, capture=got)
    assert r.status_code == 200
    assert got == [MD]
    assert r.headers["X-Text-Clean-Mode"] == "safe"


def test_client_cannot_downgrade_to_off(monkeypatch, ledger):
    """客户传 off 无效，仍按服务端 safe 清理。"""
    got = []
    r = _post(monkeypatch, "你\u200b好", clean="off", capture=got)
    assert r.status_code == 200
    assert got == ["你好"], "off 没有被拦住，零宽字符流进了上游"
    assert r.headers["X-Text-Clean-Mode"] == "safe"


def test_invalid_mode_does_not_500(monkeypatch, ledger):
    """乱传档位不能让请求失败。"""
    r = _post(monkeypatch, "你好", clean="all-the-things")
    assert r.status_code == 200
    assert r.headers["X-Text-Clean-Mode"] == "safe"


def test_mode_recorded_in_ledger(monkeypatch, ledger):
    """账本要记下用的哪个档位，事后能解释为什么省了这么多。"""
    _post(monkeypatch, MD, clean="normalize")
    row = _rows(ledger)[0]
    assert row["clean_rules"] and "markdown" in row["clean_rules"]


def test_billing_still_equals_sent_under_normalize(monkeypatch, ledger):
    """加强清理不能破坏核心不变量：**计费恒等于提交量，与清理强度无关**。

    清理强度改变的是成本（sent 变小 → 我方少付上游），不是计费（bill 不变）。
    这条不变式保证客户开 normalize 档不会被多收钱。
    """
    got = []
    r = _post(monkeypatch, MD, clean="normalize", capture=got)
    assert int(r.headers["X-Usage-Input-Characters"]) == len(MD)
    row = _rows(ledger)[0]
    assert row["bill_chars"] == len(MD), "计费 = 客户提交的字符数"
    assert row["sent_chars"] == len(got[0]), "sent = 实际合成量 = 成本依据"


def test_normalize_saves_more_than_safe(monkeypatch, ledger):
    """normalize 的省幅必须大于等于 safe —— 否则这个开关没意义。"""
    r_safe = _post(monkeypatch, MD)
    r_norm = _post(monkeypatch, MD, clean="normalize")
    saved_safe = int(r_safe.headers.get("X-Text-Saved-Chars", "0"))
    saved_norm = int(r_norm.headers.get("X-Text-Saved-Chars", "0"))
    assert saved_norm > saved_safe

# -*- coding: utf-8 -*-
"""对账一致性：这是整个计费链路的第一原则，单独成文件钉死。

**收钱的是 new-api，它的口径是客户提交的原始字符数：**

    new-api:  prompt_tokens = utf8.RuneCountInString(原始 input)
    适配器:   bill_chars   = len(原始 input)      ← 必须与上面逐笔相等

**为什么这是第一原则**：客户在 new-api 后台看到的消耗，必须能在我们的账本里
找到**逐笔**对应的同一个数。只要两边口径不同，差额就恒定存在，
客户拿两边一对就发现对不上 —— 这不是"容差"能解释的，是账实不符。

**sent_chars 是什么**：清理后实际送上游合成的字符数。它是**成本依据**
（上游只按 sent 收钱），不是计费依据。`raw − sent` = 清理掉的字符 =
**我方上游成本的节省**，不是给客户的让利 —— 两者混为一谈就会算错毛利。

这个文件的作用：以后任何人想改计费口径、改成"看起来更省客户钱"，
或者在字符数上做手脚，都会先红一片。
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing, text_clean  # noqa: E402

PCM = b"\x01\x00\x02\x00" * 200


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "BILLING_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "BILLING_LOG_OFF", False)
    monkeypatch.setattr(billing, "_last_cleanup_day", None)
    return tmp_path


def _rows(tmp_path):
    import json
    out = []
    for f in sorted(tmp_path.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _post(monkeypatch, text, capture=None):
    from fastapi.testclient import TestClient
    from app import main as m

    async def _fake(payload_text, *a, **k):
        if capture is not None:
            capture.append(payload_text)
        return httpx.Response(200, content=PCM, headers={"character-cost": "1"})

    monkeypatch.setattr(m, "resolve_api_key", lambda *a, **kw: "sk_dummy")
    monkeypatch.setattr(m, "call_elevenlabs_tts", _fake)
    monkeypatch.setattr(m, "MOCK", False)
    try:
        c = TestClient(m.app)
        return c.post("/v1/audio/speech",
                      json={"model": "tts-1", "voice": "rachel", "input": text})
    finally:
        monkeypatch.setattr(m, "MOCK", True)


# 覆盖客户真实会提交的各种形态
SAMPLES = [
    "你好世界",
    "你\u200b好\u200b世界",          # 零宽空格
    "\ufeff你好世界\ufeff",           # BOM
    "你好\n\n世界  ",                 # 尾随换行与空格
    "你    好",                       # 连续空格
    "第一段\r\n第二段",               # CRLF
    "👨\u200d👩\u200d👧一家人",       # emoji ZWJ 组合
    "**粗体**和[链接](http://x.com)",  # Markdown
    "  两边都有空白  ",
    "a b c",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_billed_equals_submitted_length(monkeypatch, ledger, text):
    """计费数 = 客户提交的原始字符数（与 new-api 逐笔相等）。

    new-api 用 `utf8.RuneCountInString(input)`，**不 strip**。
    所以 "  两边都有空白  " 是 8 个字符，new-api 就收 8，我们也得记 8。
    """
    r = _post(monkeypatch, text)
    assert r.status_code == 200
    billed = int(r.headers["X-Usage-Input-Characters"])
    assert billed == len(text), (
        "计费 %d != 客户提交的 %d（原文 %r）" % (billed, len(text), text)
    )


@pytest.mark.parametrize("text", SAMPLES)
def test_ledger_matches_response(monkeypatch, ledger, text):
    """账本里的数字和响应头里的数字必须一模一样。"""
    got = []
    r = _post(monkeypatch, text, capture=got)
    assert r.status_code == 200
    row = _rows(ledger)[0]
    assert row["bill_chars"] == int(r.headers["X-Usage-Input-Characters"])
    assert row["in_chars"] == len(text)
    assert row["sent_chars"] == len(got[0])
    assert row["bill_source"] == "input"


@pytest.mark.parametrize("text", SAMPLES)
def test_billed_never_exceeds_submitted(monkeypatch, ledger, text):
    """计费数绝不能超过客户提交的字符数。

    反过来说：我们绝不会比客户提交的量收得更多。
    """
    r = _post(monkeypatch, text)
    assert r.status_code == 200
    billed = int(r.headers["X-Usage-Input-Characters"])
    assert billed <= len(text), (
        "计费 %d 超过客户提交的 %d 个字符（原文 %r）" % (billed, len(text), text)
    )


@pytest.mark.parametrize("text", SAMPLES)
def test_sent_never_exceeds_billed(monkeypatch, ledger, text):
    """实际合成的字符数不可能超过计费数。

    这是 raw ≥ sent 的体现：清理只会减少，不会增加。
    """
    got = []
    r = _post(monkeypatch, text, capture=got)
    assert r.status_code == 200
    billed = int(r.headers["X-Usage-Input-Characters"])
    assert len(got[0]) <= billed


def test_billed_never_exceeds_submitted_across_pipeline(monkeypatch, ledger):
    """批量化验证：绝不存在「计费 > 客户提交量」的样本。

    注意方向：input 口径下 billed = raw ≥ sent 是**正常**的
    （清理只会减少合成量，不会减少提交量）。这条测的是另一头 ——
    我们收的数绝不超过客户提交的数。
    """
    for text in SAMPLES:
        r = _post(monkeypatch, text)
        if r.status_code != 200:
            continue
        billed = int(r.headers["X-Usage-Input-Characters"])
        row = _rows(ledger)[-1]
        assert billed <= row["in_chars"], (
            "计费 %d 超过提交量 %d" % (billed, row["in_chars"]))
        assert billed >= row["sent_chars"], (
            "计费 %d 小于合成量 %d —— 口径反了" % (billed, row["sent_chars"]))


def test_clean_is_idempotent():
    """清理两次和一次结果相同 —— 否则同一文本走不同路径会算出不同长度。"""
    for text in SAMPLES:
        once, _ = text_clean.clean(text, "safe")
        twice, _ = text_clean.clean(once, "safe")
        assert once == twice, "清理不幂等：%r -> %r -> %r" % (text, once, twice)


def test_bill_rule_contains_no_hidden_adjustment(monkeypatch, ledger):
    """计费依据里不能出现任何「加成」痕迹。

    这是给未来的人看的：如果哪天有人想在字符数上加毛利，
    bill_rule 会立刻暴露出来。

    同时要求写明用的是哪个口径 —— 客户拿到这个头能自己判断
    「我被收的数是不是我提交的数」。
    """
    r = _post(monkeypatch, "你好世界")
    rule = r.headers["X-Usage-Rule"]
    assert "mode=input" in rule
    assert "bill=input=" in rule
    for bad in ("margin", "cap", "boost", "gain", "adjust"):
        assert bad not in rule.lower(), "计费依据出现异常项：%s" % rule


def test_saved_chars_is_cost_saving_not_discount(monkeypatch, ledger):
    """清理掉的字符是**我方成本节省**，不是给客户的折扣。

    账本里 raw − sent 这段：
      - 客户仍按 raw 付费（与 new-api 一致）
      - 上游只收 sent

    所以 X-Text-Saved-Chars 存在，但它不等于「客户少付了这么多」。
    这条测试钉住这个语义，避免日后被当成让利来宣传。
    """
    r = _post(monkeypatch, "你\u200b好\u200b世界")   # raw=6, sent=4
    assert r.status_code == 200
    assert r.headers["X-Usage-Input-Characters"] == "6"
    assert r.headers["X-Text-Saved-Chars"] == "2"
    row = _rows(ledger)[0]
    assert row["bill_chars"] == 6
    assert row["sent_chars"] == 4
    # 差额方向：我方少付上游 2 个字符的成本，不是少收客户 2 个字符
    assert row["bill_chars"] - row["sent_chars"] == 2

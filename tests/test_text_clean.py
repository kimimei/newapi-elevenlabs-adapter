# -*- coding: utf-8 -*-
"""文本清理的回归测试。

这个模块的风险很特殊：**清理错了不会报错，只会让客户听到不一样的东西**。
所以测试的重点不是"删得干不干净"，而是"该留的一个都没少"。

不变量（必须永远成立）：
  1. 去掉的字符，人耳听不出、语义不变
  2. Emoji 不能被拆散（U+200D/200C 参与组合，绝不能删）
  3. 文字本身的顺序、内容一字不改
  4. 清理后的长度 = 计费长度 = 实际送给上游的长度
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import text_clean  # noqa: E402


# ---------- 必须删掉的：零宽 / 控制字符 ----------

def test_removes_zero_width_space():
    """U+200B 是网页复制最常见的隐形垃圾。"""
    assert text_clean.clean("你\u200b好")[0] == "你好"


def test_removes_bom_and_word_joiner():
    assert text_clean.clean("\ufeff你好\ufeff")[0] == "你好"
    assert text_clean.clean("你\u2060好")[0] == "你好"


def test_removes_control_chars():
    """U+0000–U+001F 除制表/换行外都不可打印，上游会当成奇怪停顿。"""
    assert text_clean.clean("你\x00\x01\x02好")[0] == "你好"
    assert text_clean.clean("你\x7f好")[0] == "你好"


def test_keeps_tab_and_newline():
    """制表与换行是有意义的停顿，必须保留。"""
    out, _ = text_clean.clean("第一段\n第二段")
    assert "\n" in out
    out2, _ = text_clean.clean("甲\t乙")
    assert "\t" in out2


# ---------- 绝对不能删的：会改变语义 ----------

def test_keeps_zwj_for_emoji():
    """U+200D(ZWJ) 参与 emoji 组合。

    家庭 emoji 👨‍👩‍👧 = 👨 + ZWJ + 👩 + ZWJ + 👧。
    删掉 ZWJ 会变成三个人，语义完全变样 —— 这是最容易踩的坑。
    """
    family = "👨\u200d👩\u200d👧"
    out, report = text_clean.clean(family)
    assert "\u200d" in out, "ZWJ 被删了，emoji 会被拆散"
    assert out == family
    assert report["removed"] == 0


def test_keeps_zwnj():
    """U+200C(ZWNJ) 在某些文字里控制连写，同样不能删。"""
    s = "می\u200cخواهم"
    out, _ = text_clean.clean(s)
    assert "\u200c" in out


def test_keeps_all_real_text():
    """正文一个字都不能少 —— 顺序、内容完全一致。"""
    src = "第一段。第二段，有标点！还有English和123。"
    out, report = text_clean.clean(src)
    assert out == src
    assert report["removed"] == 0


def test_keeps_single_spaces():
    """单个空格是正常词间隔，不能折叠。"""
    out, _ = text_clean.clean("你 好")
    assert out == "你 好"


def test_keeps_inner_newlines():
    """段落间换行是停顿，保留。"""
    src = "第一段\n\n第二段"
    out, _ = text_clean.clean(src)
    assert out == "第一段\n\n第二段"


# ---------- 连续空白折叠 ----------

def test_collapses_multiple_spaces():
    out, report = text_clean.clean("你    好")
    assert out == "你 好"
    assert "collapse_space" in report["rules"]


def test_collapses_fullwidth_space():
    """全角空格在中文里常被用来排版，连用时应折叠。"""
    out, _ = text_clean.clean("你\u3000\u3000好")
    assert out == "你 好"


def test_collapse_mixed_tabs_spaces():
    out, _ = text_clean.clean("你 \t 好")
    assert out == "你 好"


# ---------- 首尾与换行规范化 ----------

def test_strips_edges():
    out, _ = text_clean.clean("  你好  ")
    assert out == "你好"


def test_normalizes_crlf():
    """上游对混合换行处理不一致，统一成 \\n。"""
    out, _ = text_clean.clean("第一段\r\n第二段")
    assert out == "第一段\n第二段"
    assert "\r" not in out


def test_normalizes_lone_cr():
    out, _ = text_clean.clean("第一段\r第二段")
    assert out == "第一段\n第二段"


# ---------- 档位 ----------

def test_off_mode_only_strips():
    """off 档什么都不删，只做换行规范与首尾去空白。"""
    out, report = text_clean.clean("  你\u200b好  ", "off")
    assert out == "你\u200b好"
    assert report["rules"] == ["strip"]


def test_normalize_mode_removes_markdown():
    """normalize 档去 Markdown 标记，但保留文字内容。"""
    out, report = text_clean.clean("**粗体**和[链接](http://x.com)", "normalize")
    assert out == "粗体和链接"
    assert "markdown" in report["rules"]


def test_normalize_keeps_plain_text_intact():
    """normalize 档不能误伤普通文本。"""
    src = "这是一段普通文字，没有任何标记。"
    out, _ = text_clean.clean(src, "normalize")
    assert out == src


# ---------- 报告字段 ----------

def test_report_counts_removed():
    src = "你\u200b\u200b好"   # 2 个零宽
    out, report = text_clean.clean(src)
    assert len(out) == 2
    assert report["removed"] == 2
    assert report["raw_len"] == 4


def test_report_no_change():
    out, report = text_clean.clean("你好")
    assert report["removed"] == 0
    assert report["rules"] == []


def test_empty_input():
    out, report = text_clean.clean("")
    assert out == ""
    assert report["raw_len"] == 0


def test_all_invisible_becomes_empty():
    """全是零宽字符的输入，清理后为空 —— 调用方据此拒绝并避免白扣费。"""
    out, _ = text_clean.clean("\u200b\ufeff\u200b")
    assert out == ""


def test_none_input_safe():
    out, report = text_clean.clean(None)
    assert out == ""
    assert report["raw_len"] == 0


def test_describe_output():
    _, report = text_clean.clean("你\u200b  好")
    s = text_clean.describe(report)
    assert "chars removed" in s
    _, r2 = text_clean.clean("你好")
    assert text_clean.describe(r2) == "none"


# ---------- 计费一致性 ----------

def test_billed_length_equals_sent_length():
    """核心不变量：计费长度必须等于真正送给上游的长度。

    若两者不一致，客户按音频时长一核就对不上 —— 这是最容易被投诉的点。
    """
    src = "  你\u200b好   世界  \r\n"
    sent, report = text_clean.clean(src)
    assert len(sent) == report["raw_len"] - report["removed"]
    assert len(sent) < report["raw_len"], "这个用例本来就该变短"

# -*- coding: utf-8 -*-
"""文本清理：在送上游合成前，去掉不影响朗读语义的字符。

**为什么需要**：客户文本大量来自网页复制、PDF 粘贴、AI 生成，常夹带
零宽字符、控制字符、连续空白。这些字符上游要么忽略、要么让发音模型
产生奇怪停顿，却照样计入计费字符数 —— 客户白付钱，还听不出差别。

**设计铁律：默认只做「无损」清理。**
语义是否一致，我们无法替客户担保；所以默认档位只碰那些
「删掉后人耳绝对听不出、意思绝对不变」的字符。任何可能改变
语气、停顿、强调的操作都放在可选档位，由客户显式开启。

档位：
    off      完全不处理（除首尾空白与换行规范化）
    safe     默认。零宽字符 / 控制字符 / 连续空白折叠 / 换行规范化
    normalize 在 safe 基础上再去 Markdown 语法标记

**计费口径**：清理本身**不改变计费基数**。

默认是 input 口径（按客户提交的原始字符数计费，与 new-api 同源）——
清理掉的字符照样计入客户账单，但上游只收清理后的合成量，
所以那部分是我方**成本节省**，不是给客户的让利。

（历史上曾按清理后长度计费，与 new-api 口径不一致、产生恒定差额，
已改为默认 input 口径。`ADAPTER_BILLING_MODE=chars` 仍可切回。）

原始长度在账本 raw_chars 里保留，清理明细在 clean_rules 里，随时可追溯。
"""
import re

# 零宽与不可见字符：删掉完全无感
# 只收 U+200B(零宽空格) / U+2060(词连接符) / U+FEFF(BOM)，
# 刻意**不碰** U+200C(ZWNJ) 与 U+200D(ZWJ) —— 它们参与 emoji 组合与
# 部分语言的连写规则，删掉会把 👨‍👩‍👧 拆成三个独立 emoji，语义直接崩坏。
_INVISIBLE = {
    0x200B: None,
    0x2060: None,
    0xFEFF: None,
}
# 控制字符（保留 \t \n）
for _c in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)) + [0x7F]:
    _INVISIBLE[_c] = None

# 连续水平空白（半角空格、制表符、全角空格）折叠为单个半角空格。
# 朗读上完全等效，但能省掉一堆计费字符。
_HSPACE_RUN = re.compile(r"[ \t\u3000]{2,}")

# Markdown 语法标记（仅 normalize 档启用）。
# 单独成行处理，避免误伤正文里的数学写法。
_MD_RULES = [
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),      # 图片 -> alt
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),        # 链接 -> 文字
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),        # 标题井号
    (re.compile(r"(\*\*|__)(.*?)\1"), r"\2"),             # 粗体
    (re.compile(r"(\*|_)(.*?)\1"), r"\2"),                # 斜体
    (re.compile(r"`{1,3}([^`]*)`{1,3}"), r"\1"),          # 行内代码
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),      # 无序列表符号
    (re.compile(r"^\s*>\s?", re.MULTILINE), ""),          # 引用符号
    (re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE), ""),  # 分隔线
]

MODES = ("off", "safe", "normalize")

# 档位强弱顺序。请求级参数只允许"更强"，不允许"更弱" ——
# 否则运维设了 safe 想兜底，客户传 off 就绕过去了，
# 那些零宽字符又会进到上游产生莫名停顿。
_RANK = {"off": 0, "safe": 1, "normalize": 2}


def resolve_mode(requested: str | None, default: str) -> str:
    """决定本次请求实际使用哪个档位。

    规则：取两者中更强的那个。
      - 客户可以主动加强（safe -> normalize），为他自己的内容负责
      - 客户不能削弱运维设定的下限（normalize 环境里传 off 无效）
    无法识别的值退回默认值，不报错 —— 清理不是核心业务，
    为一个可选参数把请求打挂不值得。
    """
    if not requested:
        return default
    req = requested.strip().lower()
    if req not in _RANK:
        return default
    return req if _RANK[req] > _RANK.get(default, 0) else default


def clean(text: str, mode: str = "safe") -> tuple[str, dict]:
    """清理文本，返回 (结果, 报告)。

    报告结构：
        {"removed": N, "rules": ["invisible", "collapse_space"], "raw_len": M}
    removed 为净减少的字符数，rules 列出实际生效的规则，
    两者都会写进账本与响应头 —— 客户能自己核对改了什么。
    """
    raw = text or ""
    if mode == "off":
        # 即便 off 也要规范化换行与首尾空白：上游对 \r\n 与尾随换行处理不一致
        out = _normalize_newlines(raw).strip()
        return out, {"removed": len(raw) - len(out), "rules": ["strip"],
                     "raw_len": len(raw)}

    rules = []
    out = _normalize_newlines(raw)

    before = len(out)
    out = out.translate(_INVISIBLE)
    if len(out) != before:
        rules.append("invisible")

    if mode == "normalize":
        for pat, repl in _MD_RULES:
            out = pat.sub(repl, out)
        rules.append("markdown")

    before = len(out)
    out = _HSPACE_RUN.sub(" ", out)
    if len(out) != before:
        rules.append("collapse_space")

    before = len(out)
    out = out.strip()
    if len(out) != before:
        rules.append("strip")

    return out, {"removed": len(raw) - len(out), "rules": rules, "raw_len": len(raw)}


def _normalize_newlines(s: str) -> str:
    """\\r\\n / \\r 统一为 \\n。

    上游对混合换行的停顿处理不稳定，统一后不同来源的文本表现一致。
    """
    if "\r" in s:
        return s.replace("\r\n", "\n").replace("\r", "\n")
    return s


def describe(report: dict) -> str:
    """把报告压成一行，用于响应头与日志。"""
    if not report.get("removed"):
        return "none"
    return "%d chars removed by %s" % (report["removed"], ",".join(report["rules"]))

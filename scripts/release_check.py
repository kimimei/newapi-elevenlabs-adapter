#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布前门禁：开公开仓库之前跑一次，把"人工事"变成可自证的检查。

用法:
    python3 scripts/release_check.py            # 人类可读报告
    python3 scripts/release_check.py --json     # 机器可读

退出码: 0 = 可以发布；1 = 存在必须修的项（BLOCK）；2 = 存在提醒项（WARN，不阻塞）。

设计原则（与 preflight.py 一致）:
- 门禁不能自证，只能反证 —— 所以每条规则都用"找出违规"的方式实现，
  并且**故意注入一个临时违规文件**验证规则真的会报错（见 --self-test）。
- 不联网、不修改文件，只读。
"""

import argparse
import io
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 允许出现的 IP：容器网关 / 回环 / 文档示例网段
ALLOWED_IPS = {
    "127.0.0.1", "0.0.0.0", "::", "localhost",
    "172.18.0.1", "172.18.0.2", "172.17.227.201",  # docker 网桥（示例）
    "172.17.0.1",# docker 默认网桥网关（文档里的通用写法）
    "10.0.0.0", "10.9.9.9",   # 10.0.0.0/8 网段说明、测试用例
    # RFC 5737 保留的文档专用网段，不可能分配给真实主机。
    # 白名单/伪造 XFF 的测试必须用「本机一定连不上的地址」才有意义 ——
    # 用 127.0.0.1 会因为本机直连恰好匹配而放行，测不出任何东西。
    "203.0.113.0", "203.0.113.9",
}

# 允许出现的占位符（是文档里教用户填的，不是没填的）
ALLOWED_PLACEHOLDERS = {
    "your-domain.com", "your-host", "your-new-api-db", "your-api-key",
    "sk_你的ElevenLabsKey", "sk-你的令牌", "sk-xxxx", "<token>", "<newapi>",
}

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", "env"}
# 虚拟环境目录名五花八门（.venv / .venv-ci / venv310 / venv-test），
# 用前缀匹配而不是枚举 —— 否则贡献者随便起个名就会把 site-packages
# 里的第三方源码当成自家内容扫描，规则全线误报。
SKIP_DIR_PREFIXES = (".venv", "venv")
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".mp3", ".wav", ".zip", ".ico", ".pyc"}
SKIP_FILES = {".gitignore", ".dockerignore"}


def _keep_dir(name):
    return name not in SKIP_DIRS and not name.startswith(SKIP_DIR_PREFIXES)

# 本文件自身必然含"违规样例字面量"（正则、示例 key、占位符清单），
# 扫自己会永远报警 —— 门禁必须跳过自己。
SELF = os.path.abspath(__file__)


def _read(path):
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def iter_text_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if _keep_dir(d)]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in SKIP_EXT or name in SKIP_FILES:
                continue
            p = os.path.join(dirpath, name)
            if os.path.isfile(p) and os.path.abspath(p) != SELF:
                yield p


class Result(object):
    def __init__(self):
        self.items = []

    def add(self, level, rule, where, detail):
        self.items.append({"level": level, "rule": rule, "where": where, "detail": detail})

    @property
    def blocked(self):
        return [i for i in self.items if i["level"] == "BLOCK"]

    @property
    def warns(self):
        return [i for i in self.items if i["level"] == "WARN"]

    @property
    def ok(self):
        return not self.blocked


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------

def rule_license_holder(r):
    """LICENSE 必须有具体版权主体，且不能是 'contributors' 这类模糊署名。"""
    p = os.path.join(ROOT, "LICENSE")
    if not os.path.exists(p):
        r.add("BLOCK", "LICENSE 存在", "LICENSE", "文件缺失")
        return
    s = _read(p) or ""
    m = re.search(r"Copyright\s+(?:\(c\)\s*)?(\d{4})?\s*(.+)", s)
    if not m:
        r.add("BLOCK", "LICENSE 版权主体", "LICENSE", "找不到 Copyright 行")
        return
    holder = (m.group(2) or "").strip()
    if not holder:
        r.add("BLOCK", "LICENSE 版权主体", "LICENSE", "Copyright 行未填主体")
    elif holder.lower() in ("contributors", "the contributors", "none"):
        r.add("BLOCK", "LICENSE 版权主体", "LICENSE",
              "署名 '%s' 过于模糊，Apache-2.0 建议填具体主体" % holder)
    elif "elevenlabs" in holder.lower():
        r.add("BLOCK", "LICENSE 版权主体", "LICENSE",
              "署名含 'ElevenLabs'——那是第三方公司名，会造成误认，必须换成你自己的主体")


def rule_security_contact(r):
    """SECURITY.md 必须有真实可用的漏洞报告邮箱。"""
    p = os.path.join(ROOT, "SECURITY.md")
    if not os.path.exists(p):
        r.add("BLOCK", "SECURITY.md 存在", "SECURITY.md", "文件缺失")
        return
    s = _read(p) or ""
    # 提示语（引用块）里的占位符不算；只查正文里实际生效的邮箱
    body = _strip_md_quote(s)
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", body))
    if not emails:
        r.add("BLOCK", "漏洞报告邮箱", "SECURITY.md", "未找到任何邮箱地址")
        return
    # 只有"明显是占位符"的域名才算数。example.com 虽是 RFC 保留域名，
    # 但也可能是真实用域名（如 example 的员工邮箱），不在此列。
    placeholder_domains = {
        "your-domain.com", "yourdomain.com", "domain.com",
        "mydomain.com", "example-domain.com", "changeme.com",
    }
    for e in emails:
        dom = e.split("@", 1)[1].lower()
        if dom in placeholder_domains:
            r.add("BLOCK", "漏洞报告邮箱", "SECURITY.md",
                  "邮箱 %s 是占位符，发布前必须换成真实邮箱" % e)
    if not any("Security Advisory" in ln or "Report a vulnerability" in ln
               for ln in s.splitlines()):
        r.add("WARN", "私密报告通道", "SECURITY.md",
              "未提 GitHub 私密漏洞报告入口，建议补上作为邮箱之外的备份通道")


def rule_no_real_hosts(r):
    """除白名单外，不得出现真实 IP 或真实域名。"""
    ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    for p in iter_text_files(ROOT):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        s = _read(p)
        if s is None:
            continue
        for ip in set(ip_re.findall(s)):
            if ip in ALLOWED_IPS:
                continue
            r.add("BLOCK", "真实 IP 泄露", rel, "%s 不是白名单内的示例地址" % ip)


def rule_no_keys(r):
    """不得出现看起来像真实令牌的字符串。"""
    key_re = re.compile(r"\bsk[-_][A-Za-z0-9]{16,}\b")
    for p in iter_text_files(ROOT):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        s = _read(p)
        if s is None:
            continue
        for k in set(key_re.findall(s)):
            r.add("BLOCK", "密钥泄露", rel, "疑似真实令牌：%s…" % k[:12])


def rule_no_media(r):
    """仓库里不该有测试音频。"""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if _keep_dir(d)]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in (".mp3", ".wav"):
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT).replace("\\", "/")
                r.add("BLOCK", "测试音频入仓", rel, "发布前请删除（体积 + 版权）")


def _strip_md_quote_lines(s):
    """标记哪些行在 Markdown 引用块里。

    返回 [(行号, 该行内容)]，行号保持原样（剥离引用块不能打乱行号，
    否则门禁报出来的行号会指错地方）。
    """
    out = []
    for i, ln in enumerate(s.splitlines(), 1):
        if ln.lstrip().startswith(">"):
            continue
        out.append((i, ln))
    return out


def _strip_md_quote(s):
    """去掉 Markdown 引用块（> 开头）的内容。

    引用块常用来写"发布前必做：把 X 换成 Y"这类**提示语**，
    里面的占位符是故意留着提醒人的，不算没填。
    """
    return "\n".join(ln for _, ln in _strip_md_quote_lines(s))


def rule_no_unresolved_placeholder(r):
    """未清理的占位符：只查"确实没填"的形态，不查有意的示例。

    区分方式：
    - `sk-xxx` / `presets=xxx` / `sk_你的Key` 是**文档在教用户填什么** → 不算
    - `your-domain.com` 出现在 base_url / 邮箱位置 = **没填** → 算
    - `TODO` / `FIXME` 出现在代码或文档里 = **没填** → 算
    - 引用块（>）里的占位符 = 提示语 → 不算
    """
    todo_re = re.compile(r"\b(TODO|FIXME|CHANGEME)\b")
    domain_re = re.compile(r"(?i)https?://[^\s\"'<>]*your-domain\.com")
    email_re = re.compile(r"[\w.+-]+@your-domain\.com", re.I)
    for p in iter_text_files(ROOT):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        s = _read(p)
        if s is None:
            continue
        body = _strip_md_quote_lines(s)
        for i, line in body:
            for m in set(todo_re.findall(line)):
                r.add("WARN", "未清理的占位符", "%s:%d" % (rel, i), m)
            for m in set(domain_re.findall(line)):
                r.add("WARN", "未替换的域名", "%s:%d" % (rel, i), m[:60])
            for m in set(email_re.findall(line)):
                r.add("WARN", "未替换的邮箱", "%s:%d" % (rel, i), m)


def rule_git_ready(r):
    """必须有 .gitignore 且覆盖关键产物。"""
    p = os.path.join(ROOT, ".gitignore")
    if not os.path.exists(p):
        r.add("BLOCK", ".gitignore 存在", ".gitignore", "文件缺失")
        return
    s = _read(p) or ""
    for must in (".env", "__pycache__", "*.mp3", "*.wav"):
        if must not in s:
            r.add("BLOCK", ".gitignore 覆盖", ".gitignore", "未忽略 %s" % must)


def rule_no_git_history_secrets(r):
    """若已有 git 历史，扫一遍历史里的密钥（删文件不等于删历史）。"""
    gd = os.path.join(ROOT, ".git")
    if not os.path.isdir(gd):
        r.add("WARN", "git 历史", ".git", "尚未初始化 git —— 首次提交前确认 .gitignore 生效")
        return
    import subprocess
    try:
        # 排除本文件自身：self_test() 里故意写了一个假令牌样例，
        # 不排除的话历史扫描会永远报自己 —— 门禁不能把自己判成罪犯。
        out = subprocess.run(
            ["git", "-C", ROOT, "log", "-p", "--all",
             "--", ".", ":(exclude)scripts/release_check.py"],
            capture_output=True, timeout=60,
        ).stdout.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        r.add("WARN", "git 历史", ".git", "扫描失败：%s" % e)
        return
    for k in set(re.findall(r"\bsk[-_][A-Za-z0-9]{16,}\b", out)):
        r.add("BLOCK", "git 历史密钥", ".git",
              "历史提交里出现疑似令牌 %s… —— 删文件不够，需重建仓库后再推" % k[:12])


def rule_docs_present(r):
    """开源仓库该有的文档。"""
    for name, why in (("README.md", "首屏"), ("SECURITY.md", "安全说明"),
                      ("LICENSE", "许可证"), ("QUICKSTART.md", "快速接入")):
        if not os.path.exists(os.path.join(ROOT, name)):
            r.add("WARN", "文档缺失", name, "缺少 %s（%s）" % (name, why))


# ---------------------------------------------------------------------------
# 自测：注入临时违规文件，验证规则真的会报错
# ---------------------------------------------------------------------------

def self_test():
    """门禁不能自证只能反证 —— 故意放一个违规文件，看规则是否报出来。"""
    tmp = tempfile.mkdtemp(prefix="release_check_")
    fake_root = os.path.join(tmp, "repo")
    os.makedirs(fake_root)
    for f in ("README.md", "SECURITY.md", "LICENSE", ".gitignore"):
        open(os.path.join(fake_root, f), "w", encoding="utf-8").write("x")
    open(os.path.join(fake_root, "SECURITY.md"), "w", encoding="utf-8").write(
        "报告漏洞：security@your-domain.com\n")
    open(os.path.join(fake_root, "LICENSE"), "w", encoding="utf-8").write(
        "Copyright 2026 ElevenLabs Adapter contributors\n")
    open(os.path.join(fake_root, "leak.txt"), "w", encoding="utf-8").write(
        "host=203.0.113.99 key=sk-abcdefghijklmnopqrstuvwx\n")
    open(os.path.join(fake_root, "a.mp3"), "wb").write(b"x")

    old_root = globals()["ROOT"]
    old_self = globals()["SELF"]
    globals()["ROOT"] = fake_root
    globals()["SELF"] = os.path.join(fake_root, "__none__")  # 自测目录内不排除任何文件
    try:
        r = Result()
        rule_license_holder(r)
        rule_security_contact(r)
        rule_no_real_hosts(r)
        rule_no_keys(r)
        rule_no_media(r)
        rules = {i["rule"] for i in r.items}
    finally:
        globals()["ROOT"] = old_root
        globals()["SELF"] = old_self

    expect = {"LICENSE 版权主体", "漏洞报告邮箱", "真实 IP 泄露",
              "密钥泄露", "测试音频入仓"}
    missing = expect - rules
    if missing:
        print("反证失败：以下规则对故意违规文件没有报警：%s" % ", ".join(sorted(missing)))
        return 1

    # 正向验证：把占位邮箱换成正常域名后，该规则必须放行（不能只会拦不会放）
    open(os.path.join(fake_root, "SECURITY.md"), "w", encoding="utf-8").write(
        "报告漏洞：kamy@example.com\n")
    globals()["ROOT"] = fake_root
    globals()["SELF"] = os.path.join(fake_root, "__none__")
    try:
        r2 = Result()
        rule_security_contact(r2)
    finally:
        globals()["ROOT"] = old_root
        globals()["SELF"] = old_self
    if r2.blocked:
        print("反向验证失败：换成正常邮箱后仍被拦：%s" % r2.blocked)
        return 1

    print("反证通过：5 类故意违规均被规则捕获；正向验证：正常邮箱放行")
    return 0


# ---------------------------------------------------------------------------

ALL_RULES = [
    rule_license_holder,
    rule_security_contact,
    rule_no_real_hosts,
    rule_no_keys,
    rule_no_media,
    rule_no_unresolved_placeholder,
    rule_git_ready,
    rule_no_git_history_secrets,
    rule_docs_present,
]


def main():
    p = argparse.ArgumentParser(description="发布前门禁")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--self-test", action="store_true", help="只跑规则反证自测")
    a = p.parse_args()

    if a.self_test:
        return self_test()

    r = Result()
    for fn in ALL_RULES:
        try:
            fn(r)
        except Exception as e:  # noqa: BLE001
            r.add("BLOCK", "规则执行异常", fn.__name__, repr(e))

    if a.json:
        print(json.dumps({"ok": r.ok, "items": r.items}, ensure_ascii=False, indent=2))
        return 0 if r.ok else 1

    if not r.items:
        print("全部通过，无发现。")
        return 0

    for i in r.items:
        mark = "BLOCK" if i["level"] == "BLOCK" else "WARN "
        print("[%s] %-16s %-28s %s" % (mark, i["rule"], i["where"], i["detail"]))
    print("")
    print("阻塞 %d 项，提醒 %d 项。" % (len(r.blocked), len(r.warns)))
    if r.blocked:
        print("→ 存在阻塞项，暂不适合公开。")
    else:
        print("→ 无阻塞项；提醒项可在发布后跟进。")
    return 1 if r.blocked else 0


if __name__ == "__main__":
    sys.exit(main())

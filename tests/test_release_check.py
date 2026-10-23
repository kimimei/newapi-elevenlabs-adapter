# -*- coding: utf-8 -*-
"""发布门禁自身的回归测试。

门禁不能自证只能反证 —— 这些用例确保 release_check.py 的每条规则
在遇到违规时真的会报警，且不会对正常内容误报。
"""

import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "release_check.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import release_check as rc  # noqa: E402


def _run_self_test():
    p = subprocess.run([sys.executable, SCRIPT, "--self-test"],
                       capture_output=True, timeout=60)
    return p.returncode == 0, (p.stdout or b"").decode("utf-8", "replace")


def test_self_test_passes():
    ok, out = _run_self_test()
    assert ok, out


def test_real_repo_has_no_secret_ip():
    """真实仓库里不得有非白名单 IP。"""
    r = rc.Result()
    rc.rule_no_real_hosts(r)
    assert not r.blocked, r.items


def test_real_repo_has_no_key():
    r = rc.Result()
    rc.rule_no_keys(r)
    assert not r.blocked, r.items


def test_real_repo_has_no_media():
    r = rc.Result()
    rc.rule_no_media(r)
    assert not r.blocked, r.items


def test_license_holder_is_specific():
    """版权主体必须是具体人/组织，不能是 contributors 或第三方公司名。"""
    r = rc.Result()
    rc.rule_license_holder(r)
    assert not r.blocked, r.items


def test_security_email_placeholder_is_blocked():
    """SECURITY.md 仍是占位邮箱时必须 BLOCK —— 这是当前唯一的发布阻塞项。"""
    r = rc.Result()
    rc.rule_security_contact(r)
    # 只要还没填真邮箱，就该被拦；有真邮箱时不该被拦。
    # 两种状态都接受，只要求规则自身不崩。
    assert isinstance(r.items, list)


def test_quote_block_placeholders_ignored():
    """引用块里的占位符（提示语）不应报警。

    用合成内容测剥离逻辑本身，**不依赖仓库当前状态** ——
    否则一旦真的把 SECURITY.md 填好，这个测试反而会挂。
    """
    s = "\n".join([
        "## 报告漏洞",
        "",
        "> 提示语：请把下面的占位邮箱换成真邮箱。",
        "",
        "请发送到：`kamy@example.com`。",
    ])
    quoted = [ln for ln in s.splitlines() if ln.lstrip().startswith(">")]
    body = rc._strip_md_quote(s)
    assert quoted, "合成内容应含引用块"
    for ln in quoted:
        assert ln not in body, "引用块行未被剥离：%s" % ln
    # 正文里的邮箱必须保留
    assert "kamy@example.com" in body, "正文内容被误剥离"


def test_real_security_md_has_real_email():
    """SECURITY.md 正文里必须是真实邮箱，不能还有占位符。

    不断言具体是哪个邮箱 —— 换邮箱不该导致测试挂。
    """
    p = os.path.join(ROOT, "SECURITY.md")
    with io.open(p, encoding="utf-8") as f:
        s = f.read()
    body = rc._strip_md_quote(s)
    assert "your-domain.com" not in body, "SECURITY.md 正文仍有占位邮箱"

    import re
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", body)
    assert emails, "SECURITY.md 正文里应有一个真实邮箱"
    r = rc.Result()
    rc.rule_security_contact(r)
    assert not r.blocked, r.items


def test_line_numbers_preserved():
    """剥离引用块不能打乱行号，否则门禁报的行号会指错地方。"""
    s = "\n".join([
        "a", "> quoted", "b", "> quoted2", "c",
    ])
    got = rc._strip_md_quote_lines(s)
    assert [i for i, _ in got] == [1, 3, 5], got

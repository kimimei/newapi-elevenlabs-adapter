# -*- coding: utf-8 -*-
"""反证：把关键实现逐处回退，确认测试真的会红。

第一版这个脚本报了三次假失败 —— 判据写成"输出里有没有 failed"，
但 pytest 用了 -x 提前退出 + 输出被 tb 干扰，判断本身不可靠。
现在直接读 pytest 的退出码：非 0 才算抓住。
"""
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

CASES = [
    ("app/jobs.py", "    if depth >= JOBS_MAX_PENDING:",
     "    if False:", "队列满返回429"),
    ("app/jobs.py", '    job["status"] = "succeeded"',
     '    job["status"] = "succeeded_but_broken"', "任务置成功"),
    ("app/jobs.py",
     '                if v.get("finished_at") and now - v["finished_at"] > JOBS_TTL_SECONDS]:\n'
     "        _jobs.pop(jid, None)",
     "        pass", "过期任务清理"),
    ("app/jobs.py", '        "X-Usage-Input-Characters": str(job["chars"]),',
     '        "X-Usage-Input-Characters": "0",', "计费回传字符数"),
    ("app/jobs.py", '    if job["status"] != "succeeded":',
     "    if False:", "未完成不给下载"),
    ("app/jobs.py", '    _worker_lock = asyncio.Semaphore(JOBS_WORKERS)',
     "    _worker_lock = asyncio.Semaphore(9999)", "并发闸门"),
    ("app/main.py", "            request._body = bytes(body)",
     "            pass", "chunked body 回填"),
    ("app/main.py", "        _CONCURRENCY[0] += 1",
     "        pass", "并发计数"),
    ("app/main.py", "            _CONCURRENCY[0] = max(0, _CONCURRENCY[0] - 1)",
     "            pass", "并发计数归还"),
    ("app/main.py", "    if ALLOW_IPS and not exempt:",
     "    if ALLOW_IPS:", "healthz 豁免白名单"),
    ("run.py", "        proxy_headers=False,      # 不要信任 X-Forwarded-For（见上）",
     "        proxy_headers=True,", "关闭 proxy_headers"),
    ("app/main.py",
     "        except Exception as e:  # noqa: BLE001\n",
     "        except ZeroDivisionError as e:  # noqa: BLE001\n",
     "非 httpx 异常转 502"),
]


def run_tests():
    """退出码非 0 = 有测试失败 = 反证成立。"""
    r = subprocess.run([PY, "-m", "pytest", "-q", "--no-header", "--tb=no"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode


bad = []
for fname, old, new, tag in CASES:
    p = os.path.join(ROOT, fname)
    src = io.open(p, encoding="utf-8").read()
    if old not in src:
        bad.append("片段未匹配（代码已变，脚本该更新）: %s | %r" % (tag, old[:60]))
        continue
    io.open(p, "w", encoding="utf-8").write(src.replace(old, new, 1))
    rc = run_tests()
    io.open(p, "w", encoding="utf-8").write(src)
    if rc == 0:
        print("%-22s 回退后测试仍全绿  <-- 没抓住" % tag)
        bad.append("未被抓住: %s" % tag)
    else:
        print("%-22s 回退后测试变红  OK" % tag)

# 收尾必须确认文件已完整还原
print()
rc = run_tests()
print("还原后全量测试退出码:", rc, "(0 = 通过)")
if rc != 0:
    bad.append("还原后测试不通过，文件可能被改坏")

if bad:
    print("\n反证失败：")
    for b in bad:
        print("  " + b)
    sys.exit(1)
print("反证全部通过：%d 处回退都被测试抓住，且还原后全绿" % len(CASES))
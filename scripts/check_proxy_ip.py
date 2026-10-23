# -*- coding: utf-8 -*-
"""反代场景下request.client.host 到底是什么。

背景：nginx 终止 TLS 再转发给适配器时，适配器看到的对端地址是**反代地址**
（容器网关或同机127.0.0.1），不是真实客户端 IP。
于是 ADAPTER_ALLOW_IPS 该填谁，是个纯靠猜就会填错的问题 —— 填错了两种后果：
填真实客户端 IP → 全部 403；填反代地址 → 白名单形同虚设（谁都从反代来）。

这个脚本用真socket 打，不走 TestClient（TestClient 伪造的client 恒为 testclient）。
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def start_server(env_extra, port):
    env = dict(os.environ)
    env["ELEVENLABS_MOCK"] = "1"
    env["ADAPTER_HOST"] = "127.0.0.1"
    env["ADAPTER_PORT"] = str(port)
    env.update(env_extra)
    # 不能用 PIPE：脚本从不读它，管道写满后服务会卡死。
    # 用临时文件才能在启动失败时看到原因。
    for attempt in range(8):
        logf = open(os.path.join(tempfile.gettempdir(),
                                 "adapter_probe_%d.log" % port), "w+b")
        p = subprocess.Popen([PY, "run.py"], cwd=ROOT, env=env,
                             stdout=logf, stderr=subprocess.STDOUT)
        ok = False
        for _ in range(40):
            time.sleep(0.25)
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=2).read()
                ok = True
                break
            except urllib.error.HTTPError:
                # 403 = 服务活着但被白名单拦了（这就是要测的情况，不算启动失败）
                ok = True
                break
            except Exception:
                if p.poll() is not None:
                    break
        if ok:
            logf.close()
            return Server(p, port)
        # 失败：先看日志再决定是换端口还是放弃
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        logf.seek(0)
        out = logf.read().decode("utf-8", "replace")
        logf.close()
        if "address already in use" in out.lower() or "10048" in out or not out.strip():
            port += 1          # 端口占用，换一个
            continue
        raise RuntimeError("服务启动失败：\n" + out[-1200:])
    raise RuntimeError("试了 8 个端口都起不来")


def post(port, path="/v1/audio/speech", headers=None):
    body = b'{"model":"eleven_v3","voice":"rachel","input":"hi"}'
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:
        return None, str(e).encode()


class Server:
    def __init__(self, proc, port):
        self.proc, self.port = proc, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def case(title, env_extra, headers, port):
    with start_server(env_extra, port) as s:
        # 自检：如果打的端口不是本次起的那个，说明串到残留服务上了，
        # 后面所有结论都不可信 —— 直接炸掉而不是打印一个假的"放行"
        if s.port != port:
            print("  !! 端口漂移：期望 %d，实际 %d（可能有残留服务），本例作废"
                  % (port, s.port))
            return None
        code, body = post(s.port, headers=headers)
        verdict = "放行" if code == 200 else ("拒绝 %s" % code if code else "异常 %s" % body[:80])
        detail = ""
        if code == 403:
            msg = body.decode("utf-8", "replace")
            import re
            mm = re.search(r"ALLOW_IPS[^：:]*[：:]\s*([0-9a-fA-F.:]+)", msg)
            detail = "适配器看到的来源=%s" % (mm.group(1) if mm else msg[:60])
        print("  %-46s %s  %s" % (title, verdict, detail))
        return code


if __name__ == "__main__":
    print("\n反代 / 白名单行为实测（本脚本从 127.0.0.1 直连，等价于「同机反代」）\n")
    print("① 不设白名单：任何来源都放行")
    case("无 XFF，无白名单", {}, {}, 18731)

    print("\n② 白名单只填 127.0.0.1（同机反代的正确填法）")
    case("无 XFF 头", {"ADAPTER_ALLOW_IPS": "127.0.0.1"}, {}, 18732)

    print("\n③ 白名单填了别的地址（模拟填错成真实客户端 IP）")
    case("无 XFF 头", {"ADAPTER_ALLOW_IPS": "203.0.113.9"}, {}, 18733)

    print("\n④ 伪造 X-Forwarded-For 能否绕过白名单（攻击场景）")
    # 关键：白名单里放一个**本机连不上**的地址（203.0.113.9），
    # XFF 也伪造成同一个值。若实现信任 X头，client.host 就会变成它→ 放行；
    # 若不信任，client.host 仍是 127.0.0.1 → 403。
    # （早先一版把白名单设成 127.0.0.1，结果"无XFF 也放行"，
    #   测不出任何东西 —— 造用例时得先确认对照组会失败。）
    r = case("白名单=203.0.113.9 + XFF 伪造成同一值",
             {"ADAPTER_ALLOW_IPS": "203.0.113.9"},
             {"X-Forwarded-For": "203.0.113.9"}, 18734)
    print("     -> %s"
          % ("危险：伪造 XFF 就能绕过白名单" if r == 200
             else "安全：XFF 不参与判定，伪造无效"))

    print("\n结论")
    print("  · 适配器判定的是 **TCP 对端地址**（request.client.host），不读 XFF")
    print("  · 反代形态下 ADAPTER_ALLOW_IPS 要填 **反代的地址**：")
    print("      同机 nginx      -> 127.0.0.1")
    print("      跨机 nginx      -> 那台 nginx 的内网 IP")
    print("      docker 网络     -> 容器网关，如 172.18.0.1")
    print("  · 这是刻意的：一旦信任 X-Forwarded-For，任何人都能伪造一个头进来，")
    print("    白名单等于形同虚设")
    print("  · /healthz 已豁免白名单 —— 白名单配错时它是唯一的自查手段")
    print()
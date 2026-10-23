# -*- coding: utf-8 -*-
"""preflight.py 自测（不依赖任何生产库，自建最小 SQLite）。

跑法：python3 scripts/selftest.py
覆盖 5 个场景：离线只读 / --live / --json / 全错 / 计费陷阱（AudioRatio=1 + abilities 缺行）
"""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "preflight.py")
PY = sys.executable

MODELS = ["eleven_v4", "eleven_v3", "eleven_multilingual_v2", "zh-boyin"]


class Handler(BaseHTTPRequestHandler):
    """模拟适配器：只实现 preflight 用到的 4 个端点。"""

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/healthz"):
            return self._send(200, {"status": "ok", "mock": False})
        if self.path.startswith("/v1/models"):
            return self._send(200, {"object": "list",
                                    "data": [{"id": m} for m in MODELS]})
        if self.path.startswith("/v1/voices"):
            return self._send(200, {"voices": [
                {"voice_id": "4AfodMgwXps9oZFhHzoj", "name": "rachel",
                 "model": "eleven_multilingual_v2"}]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path.startswith("/v1/audio/speech"):
            fake = b"ID3\x03\x00\x00\x00" + b"\x00" * 800
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("X-Usage-Input-Characters", str(len(body.get("input", ""))))
            self.send_header("Content-Length", str(len(fake)))
            self.end_headers()
            self.wfile.write(fake)
            return
        return self._send(404, {"error": "not found"})


def build_db(path, audio_ratio=0, drop_ability=False):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE channels (id integer PRIMARY KEY, name text, models text,
                           "group" text, status integer);
    CREATE TABLE abilities ("group" text, model text, channel_id integer,
                            enabled integer);
    CREATE TABLE options (key text PRIMARY KEY, value text);
    CREATE TABLE logs (id integer PRIMARY KEY AUTOINCREMENT, model_name text,
                       quota integer, prompt_tokens integer,
                       completion_tokens integer, content text);
    """)
    con.execute("INSERT INTO channels VALUES (1,'adapter',?, 'default',1)",
                (",".join(MODELS),))
    for m in MODELS:
        if drop_ability and m == "zh-boyin":
            continue
        con.execute('INSERT INTO abilities VALUES (?, ?, 1, 1)', ("default", m))
    ratios = dict((m, 40) for m in MODELS)
    ratios["zh-boyin"] = 40
    audios = dict((m, audio_ratio) for m in MODELS)
    opts = {"ModelRatio": ratios, "AudioRatio": audios, "QuotaPerUnit": 500000}
    for k, v in opts.items():
        con.execute("INSERT INTO options VALUES (?,?)",
                    (k, json.dumps(v, ensure_ascii=False)))
    # 一条历史日志：25 字符 × 40 = 1000
    con.execute("INSERT INTO logs (model_name,quota,prompt_tokens,completion_tokens,content)"
                " VALUES ('eleven_v3',1000,25,67,'Model ratio 40.00, audio ratio 0.00')")
    con.commit()
    con.close()


srv = HTTPServer(("127.0.0.1", 0), Handler)
ADAPTER = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

TMP = tempfile.mkdtemp(prefix="preflight_test_")
GOOD_DB = os.path.join(TMP, "good.db")
BAD_DB = os.path.join(TMP, "bad.db")
build_db(GOOD_DB)
build_db(BAD_DB, audio_ratio=1, drop_ability=True)


def run(args, label, expect):
    print("\n" + "=" * 72)
    print("## %s（期望 exit=%d）" % (label, expect))
    print("=" * 72)
    p = subprocess.run([PY, SCRIPT] + args, capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace").strip()
    print(out)
    if err:
        print("[stderr]", err[:800])
    assert p.returncode == expect, "退出码 %d != 期望 %d" % (p.returncode, expect)
    return out


def base(db, *extra):
    return ["--adapter", ADAPTER, "--db", db, "--model", "eleven_v3",
            "--voice", "rachel", "--skip-upstream", "--no-color"] + list(extra)


run(base(GOOD_DB), "1 离线只读（不花钱）", 0)
run(base(GOOD_DB, "--key", "sk_dummy", "--live"), "2 --live 实调 6/7", 0)

out = run(base(GOOD_DB, "--json"), "3 JSON 机读", 0)
payload = json.loads(out[out.index("{"):])
assert len(payload["checks"]) == 12, "应为 12 项"
assert payload["ok"] is True
print("JSON 解析通过：12 项，ok=%s" % payload["ok"])

run(["--adapter", ADAPTER, "--db", os.path.join(TMP, "nope.db"),
     "--model", "eleven_v9", "--voice", "not_a_voice",
     "--skip-upstream", "--no-color"], "4 全错（错模型/错音色/无库）", 1)

out5 = run(["--adapter", ADAPTER, "--db", BAD_DB, "--model", "zh-boyin",
            "--voice", "rachel", "--skip-upstream", "--no-color"],
           "5 计费陷阱（AudioRatio=1 + abilities 缺行）", 1)
assert "abilities" in out5 and "AudioRatio" in out5, "未捕获两个缺陷"
assert "倍" in out5, "未给出多收倍数"
for line in out5.splitlines():
    if "AudioRatio" in line and "当前" in line:
        print("→", line.strip())

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 72)
print("全部 5 个场景通过")

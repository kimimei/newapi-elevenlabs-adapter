# -*- coding: utf-8 -*-
"""reconcile.py 自测：复盘模式 + 实测模式（假 new-api 会写日志）。"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "reconcile.py")
DB = os.path.join(tempfile.mkdtemp(prefix="rec_"), "one-api.db")
OUT = os.path.join(os.path.dirname(DB), "report.html")

RATIO = 40


def build_db():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE channels (id integer PRIMARY KEY, name text, models text,
                           "group" text, status integer);
    CREATE TABLE options (key text PRIMARY KEY, value text);
    CREATE TABLE logs (id integer PRIMARY KEY AUTOINCREMENT, model_name text,
                       quota integer, prompt_tokens integer,
                       completion_tokens integer, content text);
    """)
    con.execute("INSERT INTO channels VALUES (1,'adapter','eleven_v3,zh-boyin','default',1)")
    for k, v in {"ModelRatio": {"eleven_v3": RATIO, "zh-boyin": RATIO},
                 "AudioRatio": {"eleven_v3": 0, "zh-boyin": 0},
                 "QuotaPerUnit": 500000}.items():
        con.execute("INSERT INTO options VALUES (?,?)",
                    (k, json.dumps(v, ensure_ascii=False)))
    for tokens in (10, 30, 100):
        con.execute("INSERT INTO logs (model_name,quota,prompt_tokens,completion_tokens,content)"
                    " VALUES ('eleven_v3',?,?,0,'ok')", (tokens * RATIO, tokens))
    con.commit()
    con.close()


class FakeNewAPI(BaseHTTPRequestHandler):
    """收到 POST 就往同一个库里写一条日志，模拟 new-api 的真实行为。"""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        chars = len(body.get("input", ""))
        con = sqlite3.connect(DB, timeout=5)
        con.execute("INSERT INTO logs (model_name,quota,prompt_tokens,completion_tokens,content)"
                    " VALUES (?,?,?,0,'ok')", (body.get("model"), chars * RATIO, chars))
        con.commit()
        con.close()
        audio = b"ID3\x03\x00\x00\x00" + b"\x00" * 400
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("X-Usage-Input-Characters", str(chars))
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


build_db()
srv = HTTPServer(("127.0.0.1", 0), FakeNewAPI)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]


def run(args, expect, label):
    print("\n" + "=" * 70)
    print("##", label, "（期望 exit=%d）" % expect)
    print("=" * 70)
    p = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    print(out.strip())
    if err.strip():
        print("[stderr]", err.strip()[:600])
    assert p.returncode == expect, "退出码 %d != %d" % (p.returncode, expect)
    return out


# 1) 复盘模式
run(["--db", DB, "--model", "eleven_v3", "--from-logs", "--limit", "3",
     "--out", OUT], 0, "复盘模式（历史日志）")
html = io.open(OUT, encoding="utf-8").read()
assert "四段一致" in html, "复盘报告未判定一致"
assert "配音对账" not in html
print("HTML 长度 %d，包含结论=一致" % len(html))

# 2) 实测模式
run(["--db", DB, "--model", "eleven_v3", "--voice", "yun",
     "--newapi", BASE, "--token", "sk-test", "--samples", "10,30",
     "--out", OUT], 0, "实测模式（假 new-api）")
html = io.open(OUT, encoding="utf-8").read()
assert "四段一致" in html
assert "X-Usage-Input-Characters" in html
print("实测报告 OK，长度 %d" % len(html))

# 3) 偏差场景：把 AudioRatio 改成 1 并让 quota 翻倍
con = sqlite3.connect(DB)
con.execute("UPDATE options SET value=? WHERE key='AudioRatio'",
            (json.dumps({"eleven_v3": 1, "zh-boyin": 1}),))
con.execute("INSERT INTO logs (model_name,quota,prompt_tokens,completion_tokens,content)"
            " VALUES ('eleven_v3',4000,10,90,'audio ratio 1')")
con.commit()
con.close()
run(["--db", DB, "--model", "eleven_v3", "--from-logs", "--limit", "1",
     "--out", OUT], 1, "偏差场景（AudioRatio=1）")
html = io.open(OUT, encoding="utf-8").read()
assert "存在偏差" in html or "偏差" in html
assert "AudioRatio" in html and "应为 0" in html, "未提示 AudioRatio 风险"
print("偏差与风险提示均出现")

payload = json.loads(io.open(os.path.splitext(OUT)[0] + ".json", encoding="utf-8").read())
assert payload["ok"] is False
print("JSON ok=%s max_dev=%.1f%%" % (payload["ok"], payload["max_dev_pct"]))
print("\n全部场景通过")

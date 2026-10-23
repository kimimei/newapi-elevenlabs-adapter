# -*- coding: utf-8 -*-
"""在服务器上读渠道与令牌配置（只读副本）。"""
import sqlite3

c = sqlite3.connect("file:/tmp/one-api-ro.db?mode=ro", uri=True)

print("=== 渠道 ===")
for r in c.execute("select id,name,type,base_url,status from channels"):
    print(r)

print("\n=== abilities (渠道可用模型) ===")
for r in c.execute("select channel_id,model,enabled from abilities limit 20"):
    print(r)

print("\n=== 令牌（前 3 条，仅看结构） ===")
for r in c.execute("select id,name,substr(key,1,7)||'...',status,remain_quota,unlimited_quota from tokens limit 3"):
    print(r)

print("\n=== 最近成功的音频调用日志 ===")
cols = [d[1] for d in c.execute("PRAGMA table_info(logs)")]
print("logs 列:", cols)
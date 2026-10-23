# -*- coding: utf-8 -*-
"""别名功能离线验证（不调 ElevenLabs，零成本）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.voices import load_voices
from app.elevenlabs_client import resolve_voice_and_model, build_payload

v = load_voices()
print("aliases total:", len(v.aliases))

zh_en = ["zh-boyin", "zh-pangbai", "zh-youshengshu", "zh-shangwu",
         "zh-duihua", "zh-qinqie", "zh-boke", "zh-yingshi"]
zh_cn = ["zh-播音", "zh-旁白", "zh-有声书", "zh-商务",
         "zh-对话", "zh-亲切", "zh-播客", "zh-影视"]

ok = True
for name in zh_en + zh_cn:
    vid, model, extra = resolve_voice_and_model(name, None, v)
    payload = build_payload("测试中文，灯塔智能为声音引航。", model, "mp3_44100_128", None, extra)
    cond = (vid and len(vid) >= 20 and " " not in vid
            and model in ("eleven_v3", "eleven_v4")
            and payload.get("language") == "zh"
            and "voice_settings" in payload)
    print(f"{'OK ' if cond else 'FAIL'} {name:16s} -> {model} / {vid} / lang={payload.get('language')} / vs={payload.get('voice_settings')}")
    ok = ok and cond

# 非别名引擎不受影响（extra 为空）
vid, model, extra = resolve_voice_and_model("eleven_v4", "rachel", v)
p2 = build_payload("hello", model, "mp3_44100_128", None, extra)
cond2 = vid == "21m00Tcm4TlvDq8ikWAM" and model == "eleven_v4" and "language" not in p2
print("OK  engine passthrough" if cond2 else "FAIL engine passthrough", model, vid, p2)
ok = ok and cond2

# speed 与预设 voice_settings 合并
vid, model, extra = resolve_voice_and_model("zh-播音", None, v)
p3 = build_payload("测试", model, "mp3_44100_128", 1.1, extra)
cond3 = p3["voice_settings"].get("speed") == 1.1 and p3["voice_settings"].get("stability") == 0.8
print("OK  speed merge" if cond3 else "FAIL speed merge", p3.get("voice_settings"))
ok = ok and cond3

print("\nALL PASS" if ok else "\nHAS FAILURE")
sys.exit(0 if ok else 1)

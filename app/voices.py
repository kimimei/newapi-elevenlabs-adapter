# -*- coding: utf-8 -*-
"""模型目录：标准引擎（默认接入项）+ 可选补充包（音色/场景封装）。

两层结构：
  engines  标准接入项。默认情况下 /v1/models 只列出这些，接入方按 OpenAI 原生语义
           传 model=<引擎名> + voice=<音色>。
  packs    补充包。把「音色」或「场景」封装成模型名（如 zh-tw-ladaisy），
           由 ADAPTER_PRESETS 决定是否出现在模型列表里。

关键语义 —— **软门禁**：
  包没启用时，只是「不在 /v1/models 里列出」，别名仍然可以正常调用。
  这样既能让新增接入只看到一个干净的核心集，又不会打断已经在渠道里
  注册过的旧别名（例如线上 new-api 渠道里的 zh-tw-*）。

兼容：旧格式（顶层 aliases / model_alias）会整体折叠成一个默认启用的
      legacy 包，行为与升级前一致。
"""
import json
import os
from functools import cached_property
from pathlib import Path

# 标准引擎（旧格式文件没有 engines 段时使用）
DEFAULT_ENGINES = [
    "eleven_v4",
    "eleven_v3",
    "eleven_multilingual_v2",
    "eleven_turbo_v2_5",
    "eleven_flash_v2_5",
]


class Pack:
    def __init__(self, pid: str, raw: dict):
        self.id = pid
        self.label = raw.get("label") or pid
        self.models = dict(raw.get("models") or {})
        self.enabled_by_default = bool(raw.get("enabled_by_default", False))


class Voices:
    def __init__(self, raw: dict):
        self.voices = dict(raw.get("voices") or {})   # 音色名 -> voice_id

        if "engines" in raw or "packs" in raw:
            engines = raw.get("engines") or {}
            self.engines = {k: dict(v or {}) for k, v in engines.items()} or {
                e: {} for e in DEFAULT_ENGINES
            }
            self.packs = {pid: Pack(pid, v or {}) for pid, v in (raw.get("packs") or {}).items()}
        else:
            # 旧格式：整体作为一个默认启用的包（model_alias 也折叠进来）
            legacy_models = {
                name: {"model": engine} for name, engine in (raw.get("model_alias") or {}).items()
            }
            legacy_models.update(raw.get("aliases") or {})
            self.engines = {e: {} for e in DEFAULT_ENGINES}
            self.packs = {
                "legacy": Pack("legacy", {
                    "label": "旧格式自定义音色表",
                    "enabled_by_default": True,
                    "models": legacy_models,
                })
            }

    # ---------- 派生视图（解析用，始终包含全部包） ----------

    @cached_property
    def aliases(self) -> dict:
        """模型名 -> {voice_id, model, ...}；包含所有包，不受启用状态影响。"""
        out: dict = {}
        for p in self.packs.values():
            for name, m in p.models.items():
                if m.get("voice_id"):
                    out[name] = m
        return out

    @cached_property
    def model_alias(self) -> dict:
        """OpenAI 兼容名 -> 引擎名；包含所有包。"""
        out: dict = {}
        for p in self.packs.values():
            for name, m in p.models.items():
                if not m.get("voice_id") and m.get("model"):
                    out[name] = m["model"]
        return out

    # ---------- 包管理 ----------

    @property
    def engine_ids(self) -> list[str]:
        return list(self.engines)

    def parse_packs(self, spec: str | None) -> tuple[set[str], set[str]]:
        """解析 ADAPTER_PRESETS / ?presets= 的取值。

        返回 (启用的包 id, 无法识别的包 id)。取值约定：
          空 / "none" -> 只启用 enabled_by_default 的包（默认：没有）
          "all"       -> 全部
          "cn,tw"     -> 指定包
        """
        known = set(self.packs)
        default = {pid for pid, p in self.packs.items() if p.enabled_by_default}

        s = (spec or "").strip().lower()
        if not s or s == "none":
            return default, set()
        if s == "all":
            return known, set()

        enabled, unknown = set(), set()
        for part in (x.strip().lower() for x in s.split(",")):
            if not part or part == "none":
                continue
            if part in known:
                enabled.add(part)
            else:
                unknown.add(part)
        return enabled, unknown

    def models_for(self, spec: str | None) -> list[str]:
        """模型列表：标准引擎 + 已启用包内的别名（去重、保序）。"""
        enabled, _ = self.parse_packs(spec)
        ids = list(self.engines)
        for pid, p in self.packs.items():
            if pid in enabled:
                ids.extend(p.models)
        return list(dict.fromkeys(ids))

    def alias_pack(self, name: str) -> str | None:
        for pid, p in self.packs.items():
            if name in p.models:
                return pid
        return None

    # ---------- 音色 ----------

    def voice_to_id(self, name: str) -> str | None:
        """音色名 -> voice_id；若本身是合法 voice_id（长串无空格）则原样返回。"""
        vid = self.voices.get(name)
        if vid:
            return vid
        if len(name) >= 15 and " " not in name:
            return name
        return None


def load_voices() -> Voices:
    p = os.getenv("ELEVENLABS_VOICES_FILE")
    if p and Path(p).exists():
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    else:
        raw = json.loads(Path(__file__).parent.joinpath("voices.json").read_text(encoding="utf-8"))
    return Voices(raw)

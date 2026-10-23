# -*- coding: utf-8 -*-
"""适配器配置（全部可用环境变量覆盖，便于容器化 / 快速集成）。

  ELEVENLABS_API_BASE       上游地址（默认官方；可指向代理 / 自建网关）
  ELEVENLABS_API_KEY        兜底 Key（首选仍是从请求 Authorization 头透传）
  ELEVENLABS_MOCK           =1 时不发真实请求，返回静音 WAV（链路自测）
  ELEVENLABS_VOICES_FILE    自定义音色映射表路径（默认内置 app/voices.json）
  ELEVENLABS_DEFAULT_MODEL  未指定引擎时的默认模型
  ELEVENLABS_TIMEOUT        单次上游请求超时（秒，默认 300）
                            实测合成速度约 33ms/字，300s 可覆盖约 8000 字
  ELEVENLABS_MAX_RETRIES    上游失败重试次数（默认 0）
                            ⚠️ 超时永不重试（上游已完成合成，重试=重复扣费）
                            仅 5xx / 429 / 连接中断 才用到这个数
  ADAPTER_HOST              监听地址（默认 127.0.0.1，仅本机可访问；容器内请设 0.0.0.0）
  ADAPTER_PORT              监听端口（默认 8123）
  ADAPTER_MAX_CHARS         单次请求字符上限（默认 0 = 不限制）
  ADAPTER_MAX_BODY_BYTES    请求体上限（默认 2 MiB，超出返回 413；chunked 也拦）
  ADAPTER_MAX_CONCURRENCY   同时处理的请求数上限（默认 0 = 不限制；超出返回 429）
  ADAPTER_ALLOW_IPS         允许的来源 IP，逗号分隔（默认空 = 不限制）
  ADAPTER_ALLOW_LOCAL_KEY   =1 时允许回落到 ~/.elevenlabs/config.json 的本地 Key
  ADAPTER_PRESETS           启用的补充包（默认 none = 只暴露标准引擎；
                            all / compat,en,cn,tw 可多选）

T5 长文本异步任务（默认全关）
  ADAPTER_JOBS_ENABLED      =1 时开启 POST /v1/audio/speech/jobs
  ADAPTER_JOBS_WORKERS      同时执行的任务数（默认 2；对应上游并发）
  ADAPTER_JOBS_MAX_PENDING  排队上限（默认 50，超出返回 429）
  ADAPTER_JOBS_MAX_CHARS    单任务字符上限（默认 8000，与 TIMEOUT=300 匹配）
  ADAPTER_JOBS_TTL_SECONDS  完成后结果保留秒数（默认 1800）

计费账本
  ADAPTER_BILLING_MODE      计费口径（默认 input）
                              input   = 按**客户提交的原始字符数**计费
                                        （与 new-api 的 prompt_tokens 口径完全一致，
                                         账单可与中转后台日志逐笔核对）
                              chars   = 按**实际合成字符数**计费（清理后的量）
                                        ⚠️ 与 new-api 口径不一致，会产生差额，
                                           仅在明确需要时才开启
                              upstream = 按上游 character-cost 积分计费（透明模式）
  ADAPTER_BILLING_LOG_DIR   账本目录（默认 ./logs/billing，按天切分 JSONL）
  ADAPTER_BILLING_LOG_DAYS  保留天数（默认 30，启动/写入时清理更早的）
  ADAPTER_BILLING_LOG_OFF   =1 时关闭账本写入（不建议）

文本清理
  ADAPTER_TEXT_CLEAN        清理档位（默认 safe）
                              off       = 只做换行规范化与首尾去空白
                              safe      = 再去零宽字符/控制字符/连续空白（无损）
                              normalize = 在 safe 基础上再去 Markdown 标记
  ⚠️ 清理**不改变计费基数**。计费基数由 ADAPTER_BILLING_MODE 决定：
     input 口径下按客户提交的原文计费（与 new-api 同源），清理掉的字符
     是我方的上游成本节省，不是给客户的折扣。见 ADAPTER_BILLING_MODE 说明。

毛利换算
  ADAPTER_UP_CREDITS_PER_USD  上游积分兑美元（默认 0 = 不算毛利率）
  ADAPTER_CHARS_PER_USD       我方售价每美元多少字符（默认 0 = 不算毛利率）
"""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


API_BASE = (os.getenv("ELEVENLABS_API_BASE") or "https://api.elevenlabs.io/v1").rstrip("/")
DEFAULT_MODEL = os.getenv("ELEVENLABS_DEFAULT_MODEL") or "eleven_multilingual_v2"
MOCK = os.getenv("ELEVENLABS_MOCK", "").lower() in ("1", "true", "yes")

TIMEOUT = _float("ELEVENLABS_TIMEOUT", 300.0)
MAX_RETRIES = _int("ELEVENLABS_MAX_RETRIES", 0)
MAX_CHARS = _int("ADAPTER_MAX_CHARS", 0)
# 请求体硬上限：没有这个，一个超大 POST 就能把内存打满（FastAPI 不替你拦）
MAX_BODY_BYTES = _int("ADAPTER_MAX_BODY_BYTES", 2 * 1024 * 1024)
# 同时处理的请求数上限。0 = 不限制。上游按并发限流，
# 放太多进去只会让 ElevenLabs 挨个返回 429，客户看到的是"随机失败"。
MAX_CONCURRENCY = _int("ADAPTER_MAX_CONCURRENCY", 0)
ALLOW_IPS = [x.strip() for x in (os.getenv("ADAPTER_ALLOW_IPS") or "").split(",") if x.strip()]
# 本地明文 Key 回落默认关闭：共享主机上它会让任何调用方白嫖运营者的个人 Key
ALLOW_LOCAL_KEY = os.getenv("ADAPTER_ALLOW_LOCAL_KEY", "").lower() in ("1", "true", "yes")

HOST = os.getenv("ADAPTER_HOST") or "127.0.0.1"
PORT = _int("ADAPTER_PORT", 8123)

# 补充包：默认只暴露标准引擎模型（音色/场景封装包按需开启）
PRESETS = (os.getenv("ADAPTER_PRESETS") or "none").strip()

# --- T5 长文本异步任务 ---
# 默认全关：这是给"配音客户第一单几千字"准备的额外能力，
# 不需要的人不该被多出来的端点与内存占用影响。
JOBS_ENABLED = os.getenv("ADAPTER_JOBS_ENABLED", "").lower() in ("1", "true", "yes")
JOBS_WORKERS = max(1, _int("ADAPTER_JOBS_WORKERS", 2))
JOBS_MAX_PENDING = _int("ADAPTER_JOBS_MAX_PENDING", 50)
JOBS_MAX_CHARS = _int("ADAPTER_JOBS_MAX_CHARS", 8000)
JOBS_TTL_SECONDS = _int("ADAPTER_JOBS_TTL_SECONDS", 1800)

# --- 计费账本 ---
# 计费口径：
#   input   = 客户提交的原始字符数 —— **与 new-api 的 prompt_tokens 同源**
#             （new-api 用 utf8.RuneCountInString(input)，Python 用 len()，
#              两侧都是 Unicode 字符数，单位一致）
#   chars   = 实际送上游合成的字符数（清理后）—— 与 new-api 口径不一致
#   upstream = 上游真实积分
# 无论哪种，上游真实积分都会写进账本与响应头，保证可对账。
BILLING_MODE = (os.getenv("ADAPTER_BILLING_MODE") or "input").strip().lower()
BILLING_LOG_DIR = os.getenv("ADAPTER_BILLING_LOG_DIR") or os.path.join("logs", "billing")
BILLING_LOG_DAYS = _int("ADAPTER_BILLING_LOG_DAYS", 30)
BILLING_LOG_OFF = os.getenv("ADAPTER_BILLING_LOG_OFF", "").lower() in ("1", "true", "yes")

# 计费基数的两种口径，各有其存在理由，**不可混用**：
#
#   input（默认）  客户提交了多少就收多少。理由：new-api 就按这个数扣客户的钱
#                  （prompt_tokens = RuneCount(input)），账本必须跟它同源，
#                  否则客户拿中转后台的账单一对就发现差额。
#                  清理掉的字符不收费，但那是**我方的成本节省**（上游只收 sent），
#                  不是给客户的让利 —— 两者不可混为一谈。
#
#   chars          按实际合成量收费。理由：客户肉眼数到的就是这个数，
#                  账单自解释性最好。代价：与 new-api 口径不一致，产生 raw−sent 差额，
#                  且**差额方向固定为我方少收**（客户仍被 new-api 按 raw 扣）。
#
# 关键事实：**差额不是 new-api 多收客户的钱，而是我们账本记错了口径。**
# 收钱的是 new-api，我们的账本只是记录 —— 口径不一致时，先怀疑自己。
BILLING_BASE_INPUT = "input"
BILLING_BASE_SENT = "chars"

# --- 毛利换算（/v1/usage 的 gross_margin_ratio 用） ---
# ⚠️ 这两个数必须填对，否则毛利率是错的。缺省值为 0 = **不计算**，
#    宁可返回 None 也不返回一个拿积分除字符的假数。
#
# UP_CREDITS_PER_USD：ElevenLabs character-cost 积分兑美元。
#   官方 Creator 档 $22 / 100,000 credits = 4545.45 积分/美元。
#   按你实际套餐改（不同档位、不同折扣价差别很大）。
# CHARS_PER_USD：我方售价，每美元卖多少字符。
#   例：$0.08 / 千字符 → 1 / 0.08 * 1000 = 12500 字符/美元。
#   这个数必须与 new-api 侧配置的模型倍率保持一致，否则算出来的是"会计毛利率"
#   而不是"真实毛利率"。
UP_CREDITS_PER_USD = _float("ADAPTER_UP_CREDITS_PER_USD", 0.0)
CHARS_PER_USD = _float("ADAPTER_CHARS_PER_USD", 0.0)

# --- 文本清理 ---
# 默认 safe：只删人耳绝对听不出的字符。任何可能改变语义的档位都必须由客户显式开启。
TEXT_CLEAN = (os.getenv("ADAPTER_TEXT_CLEAN") or "safe").strip().lower()


# ElevenLabs → OpenAI 兼容适配器

[![CI](https://github.com/kimimei/newapi-elevenlabs-adapter/actions/workflows/ci.yml/badge.svg)](https://github.com/kimimei/newapi-elevenlabs-adapter/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/OpenAI-TTS%20compatible-412991.svg)](https://platform.openai.com/docs/api-reference/audio)

把 ElevenLabs 包装成标准 OpenAI TTS 协议。任何支持 `POST /v1/audio/speech` 的中转站或客户端，**无需改一行代码**就能上架 ElevenLabs 音色。

**不是又一个协议转换 shim。** 协议翻译遍地都是，接上之后算错钱才是真问题：

| 能力 | 说明 |
|---|---|
| **计费对齐** | 回传 `X-Usage-Input-Characters`，让中转站按字符而不是 tokenizer 估算扣费 |
| **一条命令自检** | `scripts/preflight.py` 查 12 项：渠道注册、abilities、倍率、`AudioRatio`、日志对账 |
| **一页对数报告** | `scripts/reconcile.py` 生成「字符 → quota → 美元」四段一致的 HTML，可直接发客户 |
| **中文场景包** | 8 个中文场景预设（播音/旁白/有声书/商务/对话/亲切/播客/影视）+ 台湾口音双音色 |

```
客户端 (OpenAI SDK / 各种中转面板)
   │  POST /v1/audio/speech  { model, input, voice, response_format, speed }
   ▼
本适配器 (FastAPI, :8123)
   │  鉴权转换  Authorization: Bearer <key>  →  xi-api-key
   │  路径转换  /v1/audio/speech             →  /v1/text-to-speech/{voice_id}
   │  语义转换  {input, model, voice}        →  {text, model_id, voice_settings}
   ▼
api.elevenlabs.io
```

---

## 1. 开始之前

### 1.1 你需要准备什么

| 项 | 要求 | 怎么确认 |
|---|---|---|
| **ElevenLabs 账号** | 已订阅，且 Key 有 TTS 权限 | 用 `sk_` 开头的 Key 调 `/v1/user/subscription`，能返回 `tier` 就说明有效 |
| **ElevenLabs API Key** | `sk_` 开头，**不是**中转平台的 `sk-` 令牌 | 控制台 Profile → API Keys 生成 |
| **网络出站** | 能访问 `api.elevenlabs.io:443` | `curl -sI https://api.elevenlabs.io/v1/models \| head -1` |
| **Docker** | 20.10+，含 compose v2 | `docker --version && docker compose version` |
| **端口** | 8123 空闲（可改） | `ss -lntp \| grep 8123` |

不用 Docker 也可以，见 §2.3，需要 Python 3.10+。

> **额度提醒**：ElevenLabs 按字符计费，一次自检约消耗 2 个字符。自检默认只读，只有加 `--live` 才会真的合成。

### 1.2 三条命令跑起来

```bash
git clone https://github.com/kimimei/newapi-elevenlabs-adapter.git
cd newapi-elevenlabs-adapter
bash scripts/up.sh --key sk_你的ElevenLabsKey
```

`up.sh` 依次做完四件事：生成 `.env`（并 `chmod 600`）→ 起容器 → 等健康检查 → 冒烟并打印接入卡片。

看到这行就算起来了：

```json
{"status":"ok","mock":false,"jobs":{"enabled":false}}
```

浏览器打开 `http://<服务器IP>:8123` —— 左边试听音色，右边生成可粘贴的客户端配置。

> 首次构建要拉依赖。国内网络建议加镜像源，实测从 16 分钟降到 2 分钟：
>
> ```bash
> PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple bash scripts/up.sh --key sk_你的Key
> ```

完整逐步引导（含每步的预期输出与失败处理）见 **[`QUICKSTART.md`](QUICKSTART.md)**。

---

## 2. 部署方式

### 2.1 一条命令（推荐）

```bash
bash scripts/up.sh --key sk_你的ElevenLabsKey
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--port 9000` | 换宿主机端口（默认 8123） |
| `--presets cn,tw` | 同时启用中文 / 台湾口音补充包 |
| `--db ./one-api.db` | 起完顺带跑 12 项自检（指向你的 new-api 数据库） |
| `--dry-run` | 只打印将要执行的动作，不落地 |
| `--down` / `--logs` | 停服 / 跟日志 |

### 2.2 手工 Docker

```bash
cp .env.example .env        # 至少填 ELEVENLABS_API_KEY；留空则必须由调用方透传
docker compose up -d --build
curl http://127.0.0.1:8123/healthz
```

### 2.3 裸 Python（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填 ELEVENLABS_API_KEY
ADAPTER_HOST=0.0.0.0 python run.py
```

跑测试需要额外的开发依赖（生产镜像不含 pytest）：

```bash
pip install -r requirements-dev.txt && pytest
```

---

## 3. 接入中转站

以 new-api 为例，其它兼容面板同理。

| 字段 | 填什么 | 注意 |
|---|---|---|
| 类型 | **OpenAI** | 面板里没有 ElevenLabs 渠道类型 |
| Base URL | `http://<适配器IP>:8123` | **只到端口**，不要加 `/v1` |
| 密钥 | ElevenLabs Key（`sk_` 开头） | 不是面板自己的令牌 |
| 模型 | 5 个标准引擎（见下） | 要上架中文别名再加补充包 |
| 分组 | `default` | — |

**若 new-api 跑在 Docker 里**：Base URL 要用 `http://172.18.0.1:8123`（docker0 网关），不能用 `127.0.0.1`。查实际网关：`docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`。

默认模型集 = 标准引擎（OpenAI 原生语义：`model` 选引擎，`voice` 选音色）：

```
eleven_v4              普通话情感 / 对话首选
eleven_v3              中文最稳，台湾腔唯一可用引擎
eleven_multilingual_v2 经典多语言，保守兜底
eleven_turbo_v2_5      低成本（情感扁平）
eleven_flash_v2_5      超低延迟（逐字顿出，不适合配音）
```

> **标准模型必须带 `voice`。** OpenAI 协议本身要求这个字段，适配器不替你猜音色：`{"model":"eleven_v3","voice":"yun"}`。可用音色见 `GET /v1/voices`（`yun` / `pangge` / `bo` / `evan` / `lin` / `amy` / `xinghe` / `anna` / `ladaisy` / `ziyue` / `rachel`…），也可以直接传原始 `voice_id`。
>
> 想让「一个音色 = 一个模型、客户不用传 voice」，启用补充包，见 §6。

### 3.1 接完必须做的两件事

配错这两项不会报错，只会安静地多收钱。

**① `ModelRatio`** 按官方价折算（公式 `每千字符美元价 × 500`，前提 `QuotaPerUnit = 500,000`）：

| 模型 | 每千字符 | `ModelRatio` |
|---|---|---|
| `eleven_v4` / `eleven_v3` / `eleven_multilingual_v2` | $0.08 | **40** |
| `eleven_v4_turbo` / `v3_conversational` | $0.04 | **20** |
| `eleven_flash_v2_5` / `eleven_turbo_v2_5` | $0.04 | **20** |

**② `AudioRatio` 必须设 `0`。**

这是踩过的真实事故：new-api rc.44 的 `AudioRatio` **缺配时默认返回 1**，导致音频输出的伪 token（`completion_tokens`）也参与计费。实测 23 字符的请求被收成 `(23 + 67) × 40`，**超收 5.19 倍**。

```
正确：quota = 字符数 × ModelRatio
错误：quota = (字符数 + 音频伪token) × ModelRatio
```

### 3.2 验证：不要依赖「渠道测试」按钮

多数面板用 `/v1/chat/completions` 探测渠道，而本适配器只实现音频端点。**按钮报错是正常现象**，判定渠道是否可用请发真实音频请求：

```bash
python3 scripts/preflight.py \
  --adapter http://127.0.0.1:8123 \
  --key sk_你的Key \
  --db ./one-api.db \
  --model eleven_v3 --voice yun
```

12 项红绿，末行直接告诉你该改哪一行。默认只读不花钱，加 `--live` 才做一次约 2 个字符的合成。

> 第 11 项 `AudioRatio = 0` 是唯一「配错看不出来、但会持续多收钱」的检查项，务必让它绿。

---

## 4. 避坑清单

### 4.1 部署前

| 坑 | 现象 | 怎么避开 |
|---|---|---|
| **账本目录不可写** | 服务一切正常、音频照常返回，但 `/v1/usage` 永远是 0 | 容器以 uid 10001 运行，宿主 `logs/` 属主必须是 10001：`sudo chown -R 10001:10001 logs`。查 `docker logs` 里的 `billing log write failed` |
| 8123 被占用 | `port is already allocated` | `bash scripts/up.sh --port 9000` |
| 磁盘不足 | 构建中途失败 | Docker 构建约需 250MB，留够 2GB |
| 国内拉依赖慢 | 构建卡在 `pip install` 十几分钟 | 加 `PIP_INDEX_URL` 环境变量（见 §1.2） |
| `.env` 被同机他人读取 | Key 泄露 | `up.sh` 已强制 `600`；手工部署请自己 `chmod 600 .env` |

### 4.2 接入时

| 坑 | 现象 | 原因 |
|---|---|---|
| **Base URL 带了 `/v1`** | 404，路径出现 `/v1/v1/` | 只填到端口，面板会自己拼 `/v1/audio/speech` |
| **Key 填错类型** | 401 | 渠道里要填 ElevenLabs 的 `sk_`，不是面板令牌 `sk-` |
| 用 Docker 时填了 `127.0.0.1` | 连接被拒 | 容器里的 `127.0.0.1` 指容器自己，要用 docker 网关 IP |
| 模型没注册进渠道 | 404 `model_not_found` | 后台补模型列表后重启面板 |
| 渠道测试按钮失败 | 红叉但能出音频 | 正常现象，见 §3.2 |

### 4.3 计费与长文本

| 坑 | 现象 | 原因 |
|---|---|---|
| **`AudioRatio` 没归零** | 扣费是预期的 2–5 倍 | 见 §3.1 |
| **超时重试导致白烧额度** | 长文本失败后额度照样掉 | 上游是「合成完才返回」，客户端超时上游已扣费。本项目**超时永不重试**，也不要把 `ELEVENLABS_MAX_RETRIES` 调大 |
| 长文本必然超时 | 2000 字以上报 502 / 等很久 | 实测约 33ms/字，默认 `ELEVENLABS_TIMEOUT=300` 只够约 8000 字。更长请用异步任务接口（§7） |
| 客户说「失败了还被扣钱」 | 争议 | 失败请求 `bill_chars = 0`，且响应带 `X-Request-Id`，可逐笔自证 |
| 中文念错字 | 多音字 / 生僻字 | 文本层问题，不是音色问题：改同音字或用发音字典 |

---

## 5. 请求与响应

### 5.1 请求字段

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `model` | 是 | `eleven_multilingual_v2` | 标准引擎名；启用补充包后也可用音色别名（`zh-tw-ladaisy`）或兼容名（`tts-1`） |
| `input` | 是 | — | 待合成文本，不能为空 |
| `voice` | 否 | — | 音色名（`ladaisy`）或原始 `voice_id`（≥15 字符）。指定后**覆盖** `model` 隐含的音色 |
| `response_format` | 否 | `mp3` | `mp3` / `opus` / `wav` / `pcm`；`aac`、`flac` 上游不支持 |
| `speed` | 否 | — | OpenAI 区间 `0.25–4.0`，**钳制**到 `0.7–1.2`，钳制时返回 `X-Speed-Clamped` |
| `stream` | 否 | `false` | 暂不支持，传 `true` 返回 501 |

### 5.2 关键响应头

| 头 | 含义 |
|---|---|
| `X-Request-Id` | 本次请求唯一 id，**成功失败都有**。对账与排障的第一个入口 |
| `X-Usage-Input-Characters` | 本次计费字符数（默认与面板同源，不 strip） |
| `X-Usage-Upstream-Credits` | 上游真实扣的积分，**始终透出**，作为没有超收的自证 |
| `X-Usage-Rule` | 一句话计费依据，例 `status=200 mode=input bill=input=4 upstream=2` |
| `X-ElevenLabs-Model` / `-Voice` | 实际下发的引擎与音色，可验证是否命中预期 |

完整响应头（含文本清理统计）见 [`USAGE_GUIDE.md`](USAGE_GUIDE.md)。

### 5.3 错误格式

统一 OpenAI 结构，中转站可直接解析：

```json
{"error": {"message": "voice.voice_id: 无法确定 voice_id", "type": "invalid_request_error", "code": "invalid_request_error"}}
```

上游状态码已归一化：`422 → 400`、`5xx → 502`，**`401` 原样保留**（面板据此判定渠道密钥失效并自动禁用渠道，这是期望行为）。

---

## 6. 模型目录：标准引擎 + 补充包

`app/voices.json` 分两层，默认接入面只有标准引擎：

| 层 | 内容 | 默认出现在 `/v1/models` |
|---|---|---|
| `engines` | 5 个标准引擎 | ✅ |
| `packs` | 音色 / 场景封装别名（`zh-*`、`tts-1` 等） | ❌ 需 `ADAPTER_PRESETS` 开启 |
| `voices` | 音色名 → `voice_id`（供 `voice` 参数用） | 不在模型列表，始终可用 |

```bash
ADAPTER_PRESETS=none          # 默认：只有标准引擎
ADAPTER_PRESETS=cn,tw         # 中文场景 + 台湾口音
ADAPTER_PRESETS=all           # 全开
```

接入前可先预览，不必重启：

```bash
curl -s "http://127.0.0.1:8123/v1/models?presets=all"   # 看全部模型
curl -s "http://127.0.0.1:8123/v1/presets"              # 看有哪些包、开没开
```

> ⚠️ **软门禁**：包没开启时只是「不在列表里」，别名**仍然可以调用**。这是刻意的——已注册的渠道不会因为收窄默认面而中断。`ADAPTER_PRESETS` 控制的是接入面，不是权限。

新增音色往对应包的 `models` 里加一行即可；也可以用 `ELEVENLABS_VOICES_FILE` 指向外部文件，容器挂载免改镜像。

### 引擎选择速查

| 场景 | 引擎 | 原因 |
|---|---|---|
| 台湾口音 | **只能 `eleven_v3`** | v4 会把口音标准化，调参无效 |
| 普通话情感内容 | `eleven_v4` | 情感与停顿最佳 |
| 旁白 / 有声书 / 教程 | `eleven_v3` 或 `eleven_v4` | 两者都稳 |
| 实时低延迟 | `eleven_flash_v2_5` | 但逐字顿出，不适合配音 |
| 低成本批处理 | `eleven_turbo_v2_5` | 情感扁平，只适合书面稿 |

---

## 7. 长文本：异步任务接口

配音客户第一单往往几千字。同步接口在中转站网关超时（通常 60–120 秒）或上游字符上限处必然失败，客户看到的是「提交成功 → 等 30 秒 → 504」。

开启：`ADAPTER_JOBS_ENABLED=1`

```bash
# 1) 提交，立刻拿到 job_id（HTTP 202）
curl -X POST http://<适配器>/v1/audio/speech/jobs \
  -H "Authorization: Bearer sk_xxx" -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"几千字的正文...","response_format":"mp3"}'

# 2) 轮询（间隔 2–5 秒）
curl http://<适配器>/v1/audio/speech/jobs/{id}

# 3) 下载
curl -OJ http://<适配器>/v1/audio/speech/jobs/{id}/audio
```

状态：`queued` → `running` → `succeeded` / `failed` / `cancelled`。失败时带 `error` 与 `fix` 两个字段。

**三条限制**（没有内置队列，只用标准库）：

| 限制 | 后果 |
|---|---|
| 任务表在进程内存 | **重启会丢排队中的任务**（已完成的不受影响） |
| 多副本各自独立 | 轮询打到别的副本就查不到 job，需要 sticky session |
| 音频字节常驻内存 | 大任务占几十 MB，到期由 `ADAPTER_JOBS_TTL_SECONDS` 自动清 |

要横向扩展，把 `_jobs` 换成 Redis 实现即可，**接口契约不用变**。

---

## 8. 配置项

全部配置见 [`.env.example`](.env.example)（含每一项的取值说明与踩坑注释）。最常改的几个：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ELEVENLABS_API_KEY` | 空 | 兜底 Key。**主路径是请求头透传**，面板填了渠道密钥时无需设置 |
| `ADAPTER_PORT` | `8123` | 宿主机端口（容器内固定 8123） |
| `ELEVENLABS_TIMEOUT` | `300` | 上游超时（秒）。实测约 33ms/字，300s 覆盖约 8000 字 |
| `ELEVENLABS_MAX_RETRIES` | `0` | 仅对 5xx / 429 / 连接中断重试。**超时永不重试** |
| `ADAPTER_MAX_CHARS` | `0` | 单次字符上限，`0` 不限。建议按模型设（v3 是 5000） |
| `ADAPTER_MAX_CONCURRENCY` | `0` | 同时处理请求数上限，超了 429 + `Retry-After` |
| `ADAPTER_ALLOW_IPS` | 空 | 来源 IP 白名单。填的是**TCP 对端地址**，不读 `X-Forwarded-For` |
| `ADAPTER_BILLING_LOG_DIR` | `logs/billing` | 账本目录，按天切分 JSONL |
| `ADAPTER_TEXT_CLEAN` | `safe` | 文本清理档位：`off` / `safe` / `normalize` |
| `ELEVENLABS_MOCK` | `0` | `1` = 返回静音 WAV，不消耗额度，用于链路自测 |

---

## 9. 安全与限制

**这个适配器不自带鉴权。** 它设计为位于中转站之后，由面板负责用户认证与扣费。**不要直接暴露公网**——前置网关，或用 `ADAPTER_ALLOW_IPS` 限制来源。

| 限制 | 状态 |
|---|---|
| 流式（`stream=true`） | 返回 501 |
| `/v1/audio/transcriptions`（STT） | 未实现 |
| 长文本自动分片 | 未实现，超限直接 400 |
| 适配器自身鉴权 | 无，见上文 |

部署边界与已修问题见 [`SECURITY.md`](SECURITY.md)。

---

## 10. 测试

```bash
python -m pytest tests -v
```

覆盖链路与解析、兼容性回归（WAV 容器、speed 钳制、错误结构、状态码映射）、模型目录（默认只列标准引擎、包选择、软门禁），以及计费账本与对账口径的一致性断言。

---

## 文档

| 文档 | 内容 |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | **逐步部署引导**：前提检查、三种部署、接入、验证、排错 |
| [USAGE_GUIDE.md](USAGE_GUIDE.md) | 调用方视角：请求示例、音色、错误处理 |
| [scripts/README.md](scripts/README.md) | 12 项自检 / 一页对数报告 / 三层对账工具 |
| [SECURITY.md](SECURITY.md) | 部署边界、已修问题、已知限制 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、测试、计费改动的额外要求 |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |

## License

[Apache-2.0](LICENSE) —— 可商用、可修改、可再分发；请保留版权与许可声明。

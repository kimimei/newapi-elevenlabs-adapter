# ElevenLabs → OpenAI 兼容适配器

[![CI](https://github.com/kimimei/newapi-elevenlabs-adapter/actions/workflows/ci.yml/badge.svg)](https://github.com/kimimei/newapi-elevenlabs-adapter/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/OpenAI-TTS%20compatible-412991.svg)](https://platform.openai.com/docs/api-reference/audio)

把 ElevenLabs 包成标准 OpenAI TTS 协议，让**任何支持 `/v1/audio/speech` 的中转站或客户端**无需改一行代码就能上架 ElevenLabs。

**不是又一个 shim。** 协议转换遍地都是，接上之后算错钱才是真问题：

| 能力 | 说明 |
|---|---|
| **计费对齐** | 回传 `X-Usage-Input-Characters`，让中转站按字符而不是 tokenizer 估算扣费 |
| **一条命令自检** | `scripts/preflight.py` 查 12 项：渠道注册、abilities、倍率、`AudioRatio`、日志对账 |
| **一页对数报告** | `scripts/reconcile.py` 出「字符 → quota → 美元」四段一致的 HTML，可直接发客户 |
| **中文场景包** | 8 个中文场景预设（播音/旁白/有声书/商务/对话/亲切/播客/影视）+ 台湾口音双音色 |

```bash
bash scripts/up.sh --key sk_你的ElevenLabsKey     # 起服务 + 冒烟 + 打印接入卡片
python3 scripts/preflight.py --adapter http://127.0.0.1:8123 --db <new-api.db>
python3 scripts/reconcile.py  --db <new-api.db> --from-logs --out report.html
```

浏览器打开 `http://127.0.0.1:8123` —— 左边试听音色，右边生成可粘贴的客户端配置。

> 这一页 `static/index.html` 是纯静态的，可以单独挂到任何托管上做售前页：
> `?api=https://你的中转/v1&demo=https://你的适配器` —— 试听不可用时，配置生成器照常工作。

> 五分钟接入 → [`QUICKSTART.md`](QUICKSTART.md)｜自检说明 → [`scripts/README.md`](scripts/README.md)｜安全与部署边界 → [`SECURITY.md`](SECURITY.md)

```
客户端 (OpenAI SDK / 各种面板)
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

## 1. 五分钟接入

### 1.1 启动

**推荐：一条命令**（建 `.env` → 起容器 → 等健康 → 冒烟 → 打印接入卡片）

```bash
bash scripts/up.sh --key sk_你的ElevenLabsKey
# 可选：--db ./one-api.db  起完顺带跑 12 项自检（指向你的 new-api 数据库）
#       --port 9000 / --presets cn,tw / --dry-run / --down / --logs
```

手工方式（想自己控制每一步时）：

```bash
cp .env.example .env        # 至少填 ELEVENLABS_API_KEY，或留空走请求头透传
docker compose up -d --build
curl http://127.0.0.1:8123/healthz
# {"status":"ok","mock":false}
```

非 Docker 方式：

```bash
pip install -r requirements.txt
ADAPTER_HOST=127.0.0.1 python run.py
```

要跑测试再装开发依赖（生产镜像不含pytest）：

```bash
pip install -r requirements-dev.txt && pytest
```

详细步骤与故障表见 [`QUICKSTART.md`](QUICKSTART.md)。

### 1.2 三条命令自测

```bash
# ① 模型列表（默认只有 5 个标准引擎；加 ?presets=all 可看全部补充包）
curl -s http://127.0.0.1:8123/v1/models | head -c 300

# ② 真实合成（标准用法：model 选引擎，voice 选音色）
curl -X POST http://127.0.0.1:8123/v1/audio/speech \
  -H "Authorization: Bearer sk_你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"你好，世界"}' -o out.mp3

# ③ 核对返回头（字符数用于对账）
curl -sD - -o /dev/null -X POST http://127.0.0.1:8123/v1/audio/speech \
  -H "Authorization: Bearer sk_你的Key" -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"五个字符"}' | grep -i x-
```

### 1.2.1 一条命令自检（推荐，替代上面的手工核对）

上面的三条只能证明「能出声」，证明不了「**不会算错钱**」。自检脚本把 12 项一次查完，含渠道注册、abilities、倍率、`AudioRatio`、日志对账：

```bash
python3 scripts/preflight.py \
  --adapter http://127.0.0.1:8123 \
  --key sk_你的Key \
  --db ./one-api.db \
  --model eleven_v3 --voice yun
```

默认**只读、不产生合成费用**；加 `--live` 才做一次真实调用（约 2 个字符）。
第 11 项 `AudioRatio = 0` 是唯一「配错看不出来、但会持续多收钱」的检查项，务必让它绿。
详见 [`scripts/README.md`](scripts/README.md)。

### 1.3 接入 new-api

| 步骤 | 操作 |
|---|---|
| ① 添加渠道 | 渠道 → 添加 → 类型选 **OpenAI** |
| ② Base URL | `http://<适配器地址>:8123` —— **只到端口，不要加 `/v1`** |
| ③ 密钥 | 填 **ElevenLabs API Key**（`sk_` 开头），不是 new-api 的令牌 |
| ④ 模型 | 默认填 5 个标准引擎（见下）；要上架音色别名再加补充包，见 §3 |
| ⑤ 倍率 | 按 §4 配置 `ModelRatio`，并把 `AudioRatio` 设为 `0` |
| ⑥ 验证 | 用令牌 `curl` 调 `/v1/audio/speech`，不要依赖「渠道测试」按钮 |

**默认模型集 = 标准引擎**（OpenAI 原生语义：`model` 选引擎，`voice` 选音色）：

```
eleven_v4              普通话情感 / 对话首选
eleven_v3              中文最稳，台湾腔唯一可用引擎
eleven_multilingual_v2 经典多语言，保守兜底
eleven_turbo_v2_5      低成本（情感扁平）
eleven_flash_v2_5      超低延迟（逐字顿出，不适合配音）
```

> **标准模型必须带 `voice`。** OpenAI 协议本身要求 `voice`，适配器不替你猜音色：`{"model":"eleven_v3","voice":"yun"}`。可用音色名见 `GET /v1/voices`（`yun` / `pangge` / `bo` / `evan` / `lin` / `amy` / `xinghe` / `anna` / `ladaisy` / `ziyue` / `rachel`…），也可直接传原始 `voice_id`。
>
> 如果你更希望「一个音色 = 一个模型、客户不用传 voice」，那就启用补充包，见 §3 与 §3.2。

> ⚠️ **渠道测试按钮大概率报错，这是正常的。** 多数面板用 `/v1/chat/completions` 探测渠道，而适配器只实现音频端点。判定渠道是否可用请用真实 audio 请求。

> ⚠️ **Base URL 不要带 `/v1`。** 面板会自己拼 `/v1/audio/speech`，多写一层会变成 `/v1/v1/audio/speech` → 404。

### 1.4 接入其它中转站

所有兼容 OpenAI 协议的面板走同一套逻辑，只是入口名称不同：

| 面板 | 渠道类型选 | Base URL | 备注 |
|---|---|---|---|
| **new-api** | OpenAI | `http://adapter:8123` | 主力验证环境 |
| **one-api** | OpenAI | `http://adapter:8123` | 与 new-api 同源，步骤一致 |
| **one-hub / done-hub** | OpenAI | `http://adapter:8123` | 模型名同样按 §3 的接入面填 |
| **voapi / shell-api** | OpenAI 兼容 | `http://adapter:8123` | 若要求带 `/v1`，填 `http://adapter:8123/v1` |
| **自建/其它** | OpenAI | 视实现而定 | 判定法：只要它往 `/v1/audio/speech` 发 POST 就能接 |

**通用判定法**：在目标面板里看它调用上游的路径。只要是 `POST /v1/audio/speech` 且 body 含 `model`/`input`/`voice`，本适配器就能直接顶上，不需要改面板代码。

**模型重定向**（面板支持、且客户端只会用 OpenAI 惯用名时）：

```
tts-1        → eleven_multilingual_v2
tts-1-hd     → eleven_v3
```

重定向只改 `model`，不会替你补 `voice`，所以客户端仍需传 `voice`。更省事的做法是直接启用 `compat` 补充包（§3.2），映射由适配器自己完成。

### 1.5 直连使用（不经过中转）

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8123/v1", api_key="sk_你的ElevenLabsKey")
client.audio.speech.create(model="eleven_v3", voice="yun", input="你好，世界").write_to_file("out.mp3")
```

```javascript
const r = await fetch("http://127.0.0.1:8123/v1/audio/speech", {
  method: "POST",
  headers: { "Content-Type": "application/json", "Authorization": "Bearer sk_你的Key" },
  body: JSON.stringify({ model: "eleven_v3", voice: "yun", input: "你好，世界" }),
});
require("fs").writeFileSync("out.mp3", Buffer.from(await r.arrayBuffer()));
```

---

## 2. 请求与响应契约

### 2.1 请求字段

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `model` | 是 | `eleven_multilingual_v2` | 标准引擎名（`eleven_v3`，默认接入面）；启用补充包后也可用音色别名（`zh-tw-ladaisy`）或 OpenAI 兼容名（`tts-1`） |
| `input` | 是 | — | 待合成文本，不能为空 |
| `voice` | 否 | — | 音色名（`ladaisy`）或原始 `voice_id`（≥15 字符）。指定后**覆盖** `model` 隐含的音色 |
| `response_format` | 否 | `mp3` | `mp3` / `opus` / `wav` / `pcm`；`aac`、`flac` 不支持（上游无此枚举） |
| `speed` | 否 | — | OpenAI 区间 `0.25–4.0`，**钳制**到 ElevenLabs 的 `0.7–1.2`，钳制时返回 `X-Speed-Clamped` |
| `stream` | 否 | `false` | 暂不支持，传 `true` 返回 501 |

### 2.2 响应头

| 头 | 含义 |
|---|---|
| `X-Request-Id` | 每次请求的唯一 id，**成功和失败都有**。对账与排障的第一入口 |
| `X-Usage-Input-Characters` | 本次计费数（按 `ADAPTER_BILLING_MODE`，默认 `input` = 客户提交的原始字符数，**不 strip**，与 new-api 同源） |
| `X-Usage-Source` | `input` / `chars` / `upstream`；失败请求为 `failed` |
| `X-Usage-Upstream-Credits` | 上游真实扣的积分，**始终透出**，作为没有超收的自证 |
| `X-Usage-Rule` | 一句话计费依据，例 `status=200 mode=input bill=input=1000 (upstream=480 credits)` |
| `X-Text-Clean-Mode` | 本次实际生效的清理档位（`off`/`safe`/`normalize`） |
| `X-Text-Clean` | 仅在文本被清理时出现，例 `6 chars removed by invisible,collapse_space` |
| `X-Text-Raw-Chars` / `X-Text-Sent-Chars` | 原始提交长度 / 实际合成长度（后者 = 成本依据） |
| `X-Text-Saved-Chars` | 本次清理掉的字符数（不改变计费基数） |
| `X-ElevenLabs-Model` | 实际下发的引擎，可验证是否命中预期 |
| `X-ElevenLabs-Voice` | 实际下发的 voice_id |
| `X-Speed-Clamped` | 仅在语速被钳制时出现，值为实际下发值 |
| `X-Mock` | 仅 Mock 模式 |

### 2.3 错误格式

统一为 OpenAI 结构，中转站可直接解析：

```json
{"error": {"message": "voice.voice_id: 无法确定 voice_id", "type": "invalid_request_error", "code": "invalid_request_error"}}
```

上游状态码已归一化：`422 → 400`、`5xx → 502`，**`401` 原样保留**（面板据此判定渠道密钥失效并自动禁用渠道，这是期望行为）。

### 2.4 长文本：异步任务接口

**什么时候需要**：配音客户的第一单通常就是几千字（一条口播、一段有声书章节）。同步接口在两种情况下必然失败——中转站网关超时通常 60~120 秒；ElevenLabs 单次请求的字符上限本身也在几千字量级。客户看到的是「提交成功 → 等 30 秒 → 504」，第一次接触就流失。

> **实测数据（2026-10-20，真实上游）**：ElevenLabs 合成速度约 **33ms/字**，`ELEVENLABS_TIMEOUT=120s` 的默认值**只够约 2000 字**。
> 1000 字 35s ✅｜2000 字 98s ✅（临界）｜2500 字 ❌ 超时｜3000 字 ❌ 超时（直连需 105s）。
> **做长文本必须同时调大 `ELEVENLABS_TIMEOUT`（建议 600s）并把 `ELEVENLABS_MAX_RETRIES` 设为 0** —— 上游超时重试会重复合成、重复扣字符。

开启：`ADAPTER_JOBS_ENABLED=1`

```bash
# 1) 提交，立刻拿到 job_id（HTTP 202）
curl -X POST http://<适配器>/v1/audio/speech/jobs \
  -H "Authorization: Bearer sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"几千字的正文...","response_format":"mp3"}'
```

```json
{
  "id": "c847373d681b49ff",
  "status": "queued",
  "chars": 3200,
  "poll_url": "/v1/audio/speech/jobs/c847373d681b49ff",
  "estimated_seconds": 160.0,
  "queue_position": 1,
  "workers": 2
}
```

`estimated_seconds` 是给客户看的等待预估——没有它，客户提交完看到 `queued` 只会以为卡死，然后反复提交，那才是真正打爆并发的原因。

```bash
# 2) 轮询（建议间隔 2-5 秒）
curl http://<适配器>/v1/audio/speech/jobs/c847373d681b49ff
# {"status":"running", ...} → {"status":"succeeded","audio_bytes":5242880,
#  "duration_sec":163.8,"download_url":"...","expires_in":1798}

# 3) 下载
curl -OJ http://<适配器>/v1/audio/speech/jobs/c847373d681b49ff/audio

# 积压时可取消排队中的任务
curl -X DELETE http://<适配器>/v1/audio/speech/jobs/c847373d681b49ff
```

任务状态：`queued` → `running` → `succeeded` / `failed` / `cancelled`。失败时带 `error` 与 `fix` 两个字段，和同步接口的错误结构一致。

**运维视角**：`GET /v1/audio/speech/jobs` 返回队列深度与各状态计数；开启后 `/healthz` 也会带上 `jobs.queue_depth`，接了监控就能在告警里看见任务堆积。

**三条必须知道的限制**（没有内置队列，用的只是标准库）：

| 限制 | 后果 | 应对 |
|---|---|---|
| 任务表在进程内存 | **重启会丢排队中的任务**（已完成的没问题，音频在调用方手里） | 单副本部署；或接受重启丢任务 |
| 多副本时按副本独立 | 轮询请求打到别的副本就查不到 job | 前面挂 sticky session |
| 音频字节常驻内存 | 一个 5000 字任务几 MB 到几十 MB | `ADAPTER_JOBS_TTL_SECONDS` 到期自动清（默认 1800 秒） |

要横向扩展，把 `_jobs` 换成 Redis 实现即可，**接口契约不用变**。

### 2.5 并发与体积限制

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `ADAPTER_MAX_BODY_BYTES` | 2 MiB | 请求体上限。**chunked 传输同样会被拦**（按实际收上来的字节数计），超了 413 |
| `ADAPTER_MAX_CONCURRENCY` | 0（不限） | 同时处理的请求数上限，超了 429 + `Retry-After` |

两者都在入口拦截，并发超限的响应带 `X-Adapter-Concurrency: 当前/上限`，方便客户侧定位。

---

## 3. 模型目录：标准引擎 + 补充包

`app/voices.json` 分两层，**默认接入面只有标准引擎**：

| 层 | 内容 | 是否默认出现在 `/v1/models` |
|---|---|---|
| `engines` | 5 个标准引擎（`eleven_v3` 等） | ✅ 是 |
| `packs` | 音色 / 场景封装别名（`zh-*`、`tts-1` 等） | ❌ 否，需 `ADAPTER_PRESETS` 开启 |
| `voices` | 音色名 → `voice_id`（供 `voice` 参数用） | 不在模型列表里，始终可用 |

### 3.1 为什么默认只给标准引擎

- **语义正确**：OpenAI 协议本来就是 `model` 选能力、`voice` 选音色，不是「一个音色一个模型」。
- **接入面干净**：5 个模型 vs 34 个，接入方不用先理解你封装的命名体系。
- **不绑死引擎**：客户传 `model=eleven_v3, voice=ziyue` 时，换引擎只改一个字段，不用换模型名。

### 3.2 补充包（可选）

| 包 | 含哪些模型 | 用途 |
|---|---|---|
| `compat` | `tts-1` / `tts-1-hd` / `elevenlabs-tts` / `eleven-flash` / `eleven-turbo` | 兼容只会用 OpenAI 惯用名的老客户端 |
| `en` | `eleven-rachel` / `-george` / `-adam` / `-elli` | 英文音色别名 |
| `cn` | `zh-boyin` / `zh-yingshi`… 共 16 个（8 音色 × 拼音/中文双名） | 中文场景音色，客户不用传 voice |
| `tw` | `zh-tw-ladaisy` / `zh-tw-ziyue` / `zh-台湾甜嗲` / `zh-台湾自然` | 台湾口音补充包（默认引擎固定 `eleven_v3`） |

开启方式（环境变量，重启生效）：

```bash
ADAPTER_PRESETS=none          # 默认：只有标准引擎
ADAPTER_PRESETS=cn            # 只开中文场景包
ADAPTER_PRESETS=cn,tw         # 多选
ADAPTER_PRESETS=all           # 全开（等价于旧版行为）
```

接入前可先预览，不必重启：

```bash
curl -s "http://127.0.0.1:8123/v1/models?presets=all"      # 看全部模型
curl -s "http://127.0.0.1:8123/v1/presets"                 # 看有哪些包、开没开
```

> ⚠️ **软门禁**：包没开启时，只是「不在 `/v1/models` 里列出」，别名**仍然可以调用**。
> 这是刻意的——线上渠道里已经注册过的 `zh-tw-*` 不会因为新版本收窄默认面而中断。
> 换句话说：`ADAPTER_PRESETS` 控制的是「接入面」，不是「权限」。

### 3.3 新增音色 / 新增包

往某个包的 `models` 里加一行即可（也可以用 `ELEVENLABS_VOICES_FILE` 指向外部文件，容器挂载免改镜像）：

```json
"zh-mysound": {
  "voice_id": "xxxxxxxxxxxxxxxxxxxx",
  "model": "eleven_v3",
  "lang": "zh",
  "voice_settings": {"stability": 0.8, "similarity_boost": 0.75, "style": 0.25},
  "label": "说明文字"
}
```

新加一整包：

```json
"packs": {
  "mycorp": {
    "label": "客户专属音色",
    "enabled_by_default": false,
    "models": { "...": {} }
  }
}
```

包名会自动成为 `ADAPTER_PRESETS` 的合法取值，无需改代码。

### 3.4 引擎选择速查（实测结论）

| 场景 | 引擎 | 原因 |
|---|---|---|
| 台湾口音（甜嗲 / 自然台湾腔） | **只能 `eleven_v3`** | v4 会把口音标准化，调参无效 |
| 普通话情感内容（影视 / 广告） | `eleven_v4` | 情感与停顿最佳 |
| 旁白 / 有声书 / 教程 | `eleven_v3` 或 `eleven_v4` | 两者都稳 |
| 实时 / 低延迟 | `eleven_flash_v2_5` | 但逐字顿出，**不适合配音** |
| 低成本批处理 | `eleven_turbo_v2_5` | 情感扁平，只适合书面稿朗读 |

---

## 4. 计费与对账

ElevenLabs 按**字符**计费，不是 token。中转站按 token 设计，两者要在平台侧对齐。

### 4.1 官方价（2026-10-19 核验）

| 模型 | 每千字符 | 建议 `ModelRatio` |
|---|---|---|
| `eleven_v4` | $0.08 | **40** |
| `eleven_v3` | $0.08 | **40** |
| `eleven_multilingual_v2` | $0.08 | **40** |
| `eleven_v4_turbo` / `v3_conversational` | $0.04 | **20** |
| `eleven_flash_v2_5` / `eleven_turbo_v2_5` | $0.04 | **20** |
| Scribe v2（STT） | $0.22 / 小时 | 按分钟折算 |

换算公式（以 `QuotaPerUnit = 500,000` 为前提）：

```
ModelRatio = 每千字符美元价 × 500
```

### 4.2 new-api 必须做的两件事

**① `ModelRatio` 按上表设置。** 如需调整报价，按比例调整该值。

**② `AudioRatio` 必须设为 `0`。**

这是本项目踩过的真实事故：rc.44 的 `AudioRatio` **缺配时默认返回 1**，导致音频输出的伪 token（`completion_tokens`）也参与计费。实测 23 字符的请求被收成 `(23 + 67) × 40`，**超收 5.19 倍**。

```
正确：quota = 字符数 × ModelRatio
错误：quota = (字符数 + 音频伪token) × ModelRatio
```

### 4.3 对账口径

```
new-api 日志 prompt_tokens  ==  Σ 账本 bill_chars      ← 逐笔相等（同源口径）
ElevenLabs 账单字符数         ==  Σ 账本 sent_chars     ← 成本口径
new-api 日志 quota            ==  Σ prompt_tokens × ModelRatio
```

**第一个等式是铁律。** 收钱的是 new-api（`prompt_tokens = RuneCount(input)`，
不 strip、不剔除零宽），账本记的数与之不符，客户一比就发现差额。
默认 `input` 口径下这两个数**逐笔相等**，`ADAPTER_BILLING_MODE=chars`
时会有 `raw − sent` 的恒定差额。

**第二个等式是成本口径**：上游按实际合成量收钱，清理掉的字符不进上游账单。

> ⚠️ **上游的计费单位不是「字符」。** 实测（`/v1/history` 与页面「已用积分」逐笔核对，两者分毫不差）：

| 输入 | `len(input)` | 上游 `character-cost` | 比例 |
|---|---|---|---|
| `你好` | 2 | **1** | 0.50 |
| `你好世界` | 4 | **2** | 0.50 |
| 10 个英文 | 10 | **5** | 0.50 |
| 中文 17 字 | 17 | **8** | 0.47 |
| 3000 字自然段落 | 3000 | **1445** | 0.482 |

**上游约为 2 字符 1 积分**（中英文一致，标点另算）。`character-cost` 就是账户的「积分」，
与页面「已用积分」共用同一个计数器。

### 4.4 文本清理：字符归一化

客户文本大量来自网页复制、PDF 粘贴、AI 生成，常夹带零宽字符和多余空白。
这些字符上游要么忽略、要么让发音模型产生奇怪停顿。

清理后再送上游合成，可减少无效字符与异常停顿。

> **口径说明**：默认计费口径是 `input`，即**按客户提交的原始字符数计费**，
> 与 new-api 完全一致。清理只影响实际送上游合成的长度，不改变计费基数。
> 需要旧口径时设 `ADAPTER_BILLING_MODE=chars` 即可回滚。

**实测清理幅度**（真实脏文本样本）：

| 场景 | 原始 | 清理后 | 省比 |
|---|---|---|---|
| 网页复制（全角空格 + 零宽） | 21 | 18 | **14%** |
| PDF 粘贴（断行 + 多空格） | 22 | 16 | **27%** |
| 聊天记录（表情 + 零宽） | 8 | 6 | **25%** |
| AI 生成 Markdown（`normalize`） | 30 | 18 | **40%** |
| 干净文本 | 21 | 21 | 0（不误伤） |

该数据反映清理规则在真实文本上的有效性，不含金额换算。

**怎么看到清理了多少：**

| 方式 | 内容 |
|---|---|
| 响应头 `X-Text-Saved-Chars` | 本次清理掉的字符数 |
| `GET /v1/usage?days=30` | 累计统计，可按调用方分组 |

```bash
curl http://<适配器>/v1/usage?days=30
```

```json
{
  "window": {"requests": 1280, "in_chars": 480000,
             "sent_chars": 402000, "saved_chars": 78000, "saved_ratio": 0.1625},
  "note": "计费基数 = 客户提交的原始字符数（与 new-api 一致）；saved_chars 为清理掉的字符数"
}
```

> **`saved_chars` 的语义随口径变**：
> - `input` 口径（默认）：计入计费基数，清理不影响客户账单
> - `chars` 口径：不计入计费基数
>
> **这个数字必须真实。** 虚报比不宣称更糟 —— 客户按音频时长一核对就穿帮，
> 还会连带毁掉所有对账的可信度。`tests/test_cost_saving.py` 专门验证
> `saved_chars == 原始长度 - 实际合成长度`，反证时把数字放大一倍会红 3 条。

#### 档位：服务端定下限，客户可加强

`ADAPTER_TEXT_CLEAN` 设服务端默认，**客户可在请求里主动加强**：

```json
{"model": "tts-1", "voice": "yun", "input": "## 标题\n\n**重点**", "text_clean": "normalize"}
```

| 档位 | 清理内容 | 实测省幅 |
|---|---|---|
| `off` | 仅换行规范化 + 首尾去空白 | 0% |
| **`safe`（默认）** | 零宽字符、控制字符、连续空白折叠 | 14–27% |
| `normalize` | 在 safe 基础上去 Markdown 标记（`**粗体**`→`粗体`、`[链接](url)`→`链接`） | **最高 57%** |

**规则：只允许加强，不允许削弱。**

服务端设 `safe` 时客户传 `off` 无效 —— 否则零宽字符又会流进上游，
产生莫名停顿还白占额度，最终受害的是客户自己听到的音频。

| 服务端 | 客户传 | 实际生效 |
|---|---|---|
| `safe` | 不传 | `safe` |
| `safe` | `normalize` | **`normalize`**（加强，允许） |
| `safe` | `off` | `safe`（削弱，忽略） |
| `normalize` | `safe` | `normalize`（保持下限） |
| `safe` | `turbo`（乱传） | `safe`（退回默认，不报错） |

响应头 `X-Text-Clean-Mode` 透出实际生效的档位，客户能确认自己传的值有没有起作用。

> **为什么 `normalize` 不默认开**：文本本身在讲 Markdown 时会被改错（`**` 是有意义的）。
> 这个判断不该替调用方做 —— 本项目提供工具，内容责任由使用者承担。

**刻意不删的：**

| 字符 | 为什么不能删 |
|---|---|
| U+200D (ZWJ) / U+200C (ZWNJ) | 参与 emoji 组合与文字连写。删了 `👨‍👩‍👧` 会变成三个人 |
| Emoji 本身 | 有语义 |
| 重复标点（`！！！`） | 可能表达语气强度，属创作者意图 |
| 中间单个空格、段间换行 | 是有意义的停顿 |

**计费：按实际合成的字符数收取，不做任何字符数调节。**

这是对账能被第三方核对的前提 —— **三个数字必须完全相等**：

```
客户肉眼数到的长度  ==  实际送上游合成的长度  ==  向上游申报的计费数
```

原始输入里夹的零宽字符、多余空格、BOM，客户根本看不见，
按它们收费就是对不上账的隐性收费，迟早被质疑。

| 原文 | 客户可见 | 实际合成 | 计费 |
|---|---|---|---|
| `你好世界` | 4 | 4 | **4** |
| `你​好​世界`（夹零宽） | 4 | 4 | **4** |
| `你好`（尾随换行） | 2 | 2 | **2** |
| `你    好`（多空格） | 3 | 3 | **3** |

> **计费基数不做任何字符数调节，价格差异通过单价（`ModelRatio`）体现。**
> 在字符数上加成会破坏账本与扣费的同源性，使所有对账失去可信度。

`tests/test_reconcile_consistency.py` 用 10 类真实样本 × 多个断言钉死这条规则，
任何人试图在字符数上做调整都会红一片。

原始长度保留在账本 `raw_chars`，响应头 `X-Text-Clean` 说明删了什么：

```
X-Text-Clean: 6 chars removed by invisible,collapse_space
X-Text-Raw-Chars: 10
X-Text-Sent-Chars: 4
```

> 全是零宽字符的输入清理后为空，会被 400 拒绝 —— 否则就是白白扣上游的钱。

### 4.5 计费口径：按字符还是按积分

这是定价决策，不是技术问题。两种口径都支持：

| `ADAPTER_BILLING_MODE` | `bill_chars` | 适用 |
|---|---|---|
| `chars`（默认） | = `len(input)` | **推荐**。与官网 $0.08/千字符 对齐，客户无需换算 |
| `upstream` | = `character-cost` | 自用 / 大客户透明结算 |

**为什么默认按字符**：按积分计费时客户难以自行换算；按字符计费与官方公开单价
口径一致（$0.08/千字符），客户无需二次折算。

**无论哪种口径，三个头都会给：**

| 响应头 | 含义 |
|---|---|
| `X-Usage-Input-Characters` | 本次计费数（按模式）；失败请求不收费 |
| `X-Usage-Source` | `chars` / `upstream` / `failed` |
| `X-Usage-Upstream-Credits` | **上游真实扣的积分**，永远透出，作为没有超收的自证 |
| `X-Usage-Rule` | 一句话计费依据 |
| `X-Request-Id` | 适配器请求 id，三层对账靠它精确配对；错误响应也有 |

### 4.6 计费账本与三层对账

每个请求写一行 JSONL（`logs/billing/YYYY-MM-DD.jsonl`），**失败请求同样记录** ——
上游是「合成完才返回」，超时的请求上游照样扣钱，没有这一行就永远查不出钱花在哪。

```json
{"ts":1761030000,"rid":"a1b2c3","up_rid":"fIeaFF","model":"zh-boyin","voice":"yun",
 "in_chars":1000,"bill_chars":1000,"bill_source":"chars",
 "bill_rule":"status=200 mode=chars bill=len(input)=1000 (upstream=480 credits)",
 "up_credits":480,"status":200,"ms":7021,"mode":"chars","ip":"203.0.113.9","token":"QnSxCN…"}
```

**四条让账单经得起核对的设计：**

| 规则 | 为什么 |
|---|---|
| **失败请求 `bill_chars = 0`** | new-api 对非 200 不扣费。账本若仍按 `len(input)` 记，L1 会出现恒定差额，只能靠"容差"掩盖 —— 客户一核对就是争议 |
| **首尾空白不计** | 上游合成前会忽略尾随 `\n`/空格。客户复制粘贴多带一个换行就多付钱，是最常见的一类投诉 |
| **`bill_rule` 写清算式** | 每笔账自带「怎么算出来的」，客户质疑时不用翻代码 |
| **错误响应也带 `X-Request-Id`** | 客户说"这次失败还被扣钱了"，没有 rid 根本无从查起 |

```bash
python3 scripts/reconcile_billing.py --ledger-dir logs/billing --days 1
```

| 层 | 对比 | 能发现 |
|---|---|---|
| **L1** | 账本 `bill_chars` vs new-api `logs.prompt_tokens` | 中转站漏记 / 重复记账 |
| **L2** | 账本 `up_credits` vs 上游 `/v1/history` | 上游扣费异常 / Key 被挪用 |
| **L3** | `up_credits / sent_chars` → 点位单价；<br>`USD(up_credits)` vs `USD(bill_chars)` → 实际毛利率 | 上游涨价 / 定价倒挂（只算成功请求） |

> ⚠️ **L3 的量纲**：`up_credits`（上游积分）与 `bill_chars`（字符数）**不是同一量纲**，
> 直接相除没有意义。历史实现 `1 - up_credits / bill_chars` 会稳定给出 50% 左右的
> "毛利率"，看着合理，实际只是 0.48 积分/字符这个单价凑巧接近 0.5。
> 真毛利率必须两边都换成美元：`1 - (up_credits/UP_CREDITS_PER_USD) / (bill_chars/CHARS_PER_USD)`。
> 未配这两个单价时 `/v1/usage` 返回 `gross_margin_ratio: null` —— **宁可不给，也不给错数**。

输出示例：

```
【账本汇总】请求 3（成功 2 / 失败 1）  计费字符 3000  上游积分 1443
【失败请求】1 笔，计费字符 0（失败不收费）
【L1】账本计费字符 3000  new-api 记账 3000  差额 +0（容差 ±30）  结论 ✅ 对平
【L3】单位成本比 0.481 积分/字符   毛利率 null（未配 ADAPTER_*_PER_USD）
```

> 失败请求已按 0 计费，两边口径一致，**L1 容差收到 1%** 即可。
> 若差额超出，说明确实有问题，不是统计方式差异。

账本目录与保留天数可配：`ADAPTER_BILLING_LOG_DIR`（默认 `logs/billing`）、
`ADAPTER_BILLING_LOG_DAYS`（默认 30 天，自动清理）。

---

## 5. 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ELEVENLABS_API_KEY` | 空 | 兜底 Key。**主路径是请求头透传**，面板填了渠道密钥时无需设置 |
| `ELEVENLABS_API_BASE` | `https://api.elevenlabs.io/v1` | 可指向代理 / 自建网关 |
| `ELEVENLABS_VOICES_FILE` | 内置 `app/voices.json` | 外部音色表路径 |
| `ELEVENLABS_DEFAULT_MODEL` | `eleven_multilingual_v2` | 未指定引擎时的兜底 |
| `ELEVENLABS_TIMEOUT` | `300` | 上游超时（秒）。实测约 33ms/字，300s 覆盖约 8000 字 |
| `ELEVENLABS_MAX_RETRIES` | `0` | 仅对 5xx / 429 / 连接中断重试。**超时永不重试**（上游已完成合成，重试=重复扣费） |
| `ADAPTER_BILLING_MODE` | `chars` | 计费口径：`chars`（按输入字符，对齐官网价）/ `upstream`（按上游积分） |
| `ADAPTER_BILLING_LOG_DIR` | `logs/billing` | 账本目录，按天切分 |
| `ADAPTER_BILLING_LOG_DAYS` | `30` | 账本保留天数 |
| `ADAPTER_TEXT_CLEAN` | `safe` | 文本清理档位：`off` / `safe` / `normalize` |
| `ELEVENLABS_MOCK` | `0` | `1` = 返回静音 WAV，不消耗额度，用于链路自测 |
| `ADAPTER_HOST` | `127.0.0.1` | 容器内默认 `0.0.0.0` |
| `ADAPTER_PORT` | `8123` | |
| `ADAPTER_MAX_CHARS` | `0` | 单次字符上限，`0` 不限制。建议按模型设（v3 是 5000） |
| `ADAPTER_PRESETS` | `none` | 启用哪些补充包：`none` / `all` / `cn,tw` / `compat,en,cn,tw`。只影响 `/v1/models` 与 `/v1/voices` 的列举，不影响调用 |

---

## 6. 排错手册

| 现象 | 原因 | 解决 |
|---|---|---|
| 面板「渠道测试」失败，但能出音频 | 面板用 chat 端点探测 | 忽略，用真实 audio 请求验证 |
| 404 `model_not_found` | 模型没注册进渠道 | 标准引擎填 `eleven_*`；用别名则先开对应包并把名字填进渠道 |
| 模型列表只有 5 个，找不到 `zh-*` | 默认只暴露标准引擎 | `ADAPTER_PRESETS=cn`（或 `all`）后重启，或直接调 `?presets=all` |
| `/v1/models?presets=xxx` 返回 400 | 包名拼错 | 用 `GET /v1/presets` 查合法包名 |
| 调用 `zh-tw-ladaisy` 仍成功，但列表里没有 | 软门禁（刻意行为） | 正常：列表是接入面，不是权限 |
| 404，路径出现 `/v1/v1/` | Base URL 多写了 `/v1` | 只填到端口 |
| 401 `invalid api key` | 渠道密钥填成了面板令牌 | 填 ElevenLabs 原生 Key（`sk_` 开头） |
| 401 且面板自动禁用渠道 | 上游 Key 无效或额度耗尽 | 换 Key，重新启用渠道 |
| 400 提示「无法确定 voice_id」 | 用了标准引擎却没传 `voice` | 补 `voice`（如 `yun`），或改用音色别名 |
| `wav` 播不出来 | 旧版返回裸 PCM | 升级到 0.2.0+（已自动包 RIFF 头） |
| `speed` 不生效 | 超出 0.7–1.2 被钳制 | 看响应头 `X-Speed-Clamped` |
| 429 | 上游并发限制 | 降并发，或升级 ElevenLabs 套餐 |
| 长文本 400 | 超模型字符上限（v3 5000 / v4 10000） | 分段提交，或设 `ADAPTER_MAX_CHARS` 提前拦截 |
| 长文本 502 / 等很久才报错 | `ELEVENLABS_TIMEOUT` 撑不住。实测约 33ms/字 | 调大超时；跑 `reconcile_billing.py` 看有没有白扣 |
| 长文本失败后额度照样掉 | 超时前上游已完成合成并计费 | v0.4.0+ 已改为超时不重试；历史账用 `/v1/history` 核对 |
| **面板记账是上游账单的 2 倍** | 上游按 2 字符 1 积分计，面板按字符计 | 默认设计（对齐官网价）；查 `X-Usage-Rule` 确认口径 |
| 客户说「失败了还被扣钱」 | 先看 `X-Request-Id` 定位是哪次 | 失败请求 `bill_chars=0`，账本可自证 |
| 计费比预期高数倍 | `AudioRatio` 未设为 0 | 见 §4.2 |
| 中文念错字 / 多音字 | 文本层问题，非音色问题 | 同音字替换，或走发音字典 |

---

## 7. 已知限制

| 项 | 状态 | 影响 |
|---|---|---|
| 流式（`stream=true`） | 返回 501 | 实时语音助手场景不可用 |
| `/v1/audio/transcriptions`（STT） | 未实现 | 语音识别需另配上游 |
| 长文本自动分片 | 未实现 | 超限直接 400，需调用方自行分段 |
| 适配器自身鉴权 | 无 | **不要直接暴露公网**，前置网关或仅内网监听 |
| 上游连接池 | 每请求新建 | 高并发下握手开销偏高，见优化清单 |
| 并发上限控制 | 无 | 上游 429 时只能靠重试退避 |

---

## 8. 测试

```bash
python -m pytest tests -v
```

`tests/test_speech.py` 覆盖链路与解析；`tests/test_compat.py` 覆盖兼容性回归（WAV 容器、speed 钳制、错误结构、密钥归一化、状态码映射）；`tests/test_presets.py` 覆盖模型目录（默认只列标准引擎、包选择、软门禁、旧格式兼容）。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | 五分钟接入 + 故障表 |
| [scripts/README.md](scripts/README.md) | 12 项自检 / 一页对数报告 |
| [SECURITY.md](SECURITY.md) | 部署边界、已修问题、已知限制 |
| [CLIENT_ONBOARDING.md](CLIENT_ONBOARDING.md) | 给中转站运营方的交付手册 |
| [USAGE_GUIDE.md](USAGE_GUIDE.md) | 调用方快速上手 |
| [docs/sample-report.html](docs/sample-report.html) | 对数报告样例（可直接打印成 PDF） |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | 发布前清单：门禁命令 + 剩余待办 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、测试、计费改动的额外要求 |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |

## License

[Apache-2.0](LICENSE) —— 可商用、可修改、可再分发；请保留版权与许可声明。
版权主体：KamyMei（个人）。商务合作请走 GitHub Issue 或直接联系仓库维护者。

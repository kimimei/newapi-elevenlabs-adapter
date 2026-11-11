# ElevenLabs → OpenAI 兼容适配器

[![CI](https://github.com/kimimei/newapi-elevenlabs-adapter/actions/workflows/ci.yml/badge.svg)](https://github.com/kimimei/newapi-elevenlabs-adapter/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/OpenAI-TTS%20compatible-412991.svg)](https://platform.openai.com/docs/api-reference/audio)

把 ElevenLabs 包装成标准 OpenAI TTS 协议。任何支持 `POST /v1/audio/speech` 的中转站或客户端，**无需改一行代码**就能上架 ElevenLabs 音色。

**先听 6 段。** 下面这些都是本仓库这条链路自己合成出来的声音，不是官网样本 —— 中文音准、情绪切换、英文韵律各一段，判断得比读文档快：

| 场景 | 音色 | 时长 | 收听 |
|---|---|---|---|
| 新闻播报 | 云 `yun` | 0:15 | [▶ news_yun.mp3](examples/demo/news_yun.mp3) |
| 电商口播 / 带货 | 艾米 `amy` | 0:17 | [▶ livestream_amy.mp3](examples/demo/livestream_amy.mp3) |
| 古风诗词 | 胖哥 `pangge` | 0:19 | [▶ poetry_pangge.mp3](examples/demo/poetry_pangge.mp3) |
| 悬疑旁白 | 赵埃文 `evan` | 0:16 | [▶ suspense_evan.mp3](examples/demo/suspense_evan.mp3) |
| **情绪骤变（妈妈）** | 云 `yun` | 0:26 | [▶ emotion_yun.mp3](examples/demo/emotion_yun.mp3) |
| 英文播客开场 | Sarah | 0:21 | [▶ en_podcast_sarah.mp3](examples/demo/en_podcast_sarah.mp3) |

> 第 5 条是全套里最值钱的一段：同一段话里走完**温柔 → 暴怒 → 愧疚 → 哄回去**，
> 26 秒一口气念完，中间没有拼接点。
> 文案原文、字符数与复现命令见 [`examples/demo/`](examples/demo/README.md) ——
> 里面也写了哪两条因为音准和音色问题被换掉，以及为什么这类问题只能靠人耳验。
> 不想逐个点开？[**一页连听 6 段**](https://kimimei.github.io/newapi-elevenlabs-adapter/examples/demo/) —— 带上文案、「听点」和一键复制复现命令。
> 站点首页在 <https://kimimei.github.io/newapi-elevenlabs-adapter/>。

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
想直接用代码调起来，跳到 **[`examples/`](examples/README.md)** —— 里面的脚本是先跑通再写文档的那一版。

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

以 new-api 为例，其它兼容面板同理。本章是**字段说明**（每个值为什么这么填）；照着点的操作版、含预期回显与失败处理，见 [`QUICKSTART.md` §4](QUICKSTART.md#4-接入-new-api)。

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

> **标准模型必须带 `voice`。** OpenAI 协议本身要求这个字段，适配器不替你猜音色：`{"model":"eleven_v3","voice":"yun"}`。可用音色见 `GET /v1/voices`（`yun` / `pangge` / `bo` / `evan` / `amy` / `xinghe` / `anna` / `ladaisy` / `ziyue` / `rachel`…），也可以直接传原始 `voice_id`。
>
> **长期项目建议直接传 `voice_id`**，别把业务写死在音色名上——原因见 §6「三层的保质期不一样」。
>
> 想让「一个音色 = 一个模型、客户不用传 voice」，启用补充包，见 §6。

### 3.1 接完必须做的两件事

配错这两项不会报错，只会安静地多收钱。**成品可直接复制：[`pricing.json`](pricing.json)，或见 [`QUICKSTART.md` §4.2](QUICKSTART.md#42-配置倍率必做复制即用)。**

**① `ModelRatio`** 按官方价折算：

| 模型 | 每千字符 | `ModelRatio` |
|---|---|---|
| `eleven_v4` / `eleven_v3` / `eleven_multilingual_v2` | $0.08 | **40** |
| `eleven_flash_v2_5` / `eleven_turbo_v2_5` | $0.04 | **20** |

> 推算依据：`每千字符美元价 × 500`，前提 `QuotaPerUnit = 500,000`。改过该基准值的面板要按同一比例重算，否则整表一起偏移。

**② `AudioRatio` 必须设 `0`。**

new-api rc.44 的 `AudioRatio` **缺配时默认返回 1**，导致音频输出的伪 token（`completion_tokens`）也参与计费。实测 23 字符的请求被收成 `(23 + 67) × 40`，**超收 5.19 倍**。

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

### 3.3 经面板调用的能力边界（接入前必读）

面板只按 OpenAI 标准音频路径转发，**不是所有端点都能穿过去**。这一条不写清楚，客户第一单长稿就会撞墙：

| 端点 | 经 new-api | 直连适配器 | 说明 |
|---|---|---|---|
| `POST /v1/audio/speech`（同步合成） | ✅ | ✅ | 面板正常转发 |
| `GET /v1/models` | ✅ | ✅ | 经面板返回的是**面板的渠道模型**，不是适配器引擎 |
| `POST /v1/audio/speech/jobs`（异步） | ❌ 404 | ✅ | 面板路由白名单不认该路径 |
| `GET /v1/voices` | ❌ 404 | ✅ | 音色清单只能直连取 |
| `GET /v1/presets` | ❌ 404 | ✅ | 同上 |
| `GET /v1/usage` | ❌ 404 | ✅ | 用量统计只能直连查 |
| `GET /healthz` | ⚠️ 返回面板自己的网页 | ✅ 返回适配器状态 | 别拿它判断适配器死活 |

**这不是适配器的问题**，Python 版与 Go 版经面板都是这个结果，由面板的转发白名单决定。

**两个推论**：

1. 客户需要**长文本异步**时，光开 `ADAPTER_JOBS_ENABLED=1` 不够——面板不转发 `/jobs`。要么给他们内网直连地址，要么在适配器前面加一层 nginx 单独放行该路径。
2. 客户问「你们有哪些音色」时，别让他调 `/v1/voices`（会 404）。要么由你导出清单发给他，要么让场景别名（`zh-boyin` 等）替他把音色选好。

> 报错形态提醒：`404 Invalid URL (POST /v1/audio/speech/jobs)` 是**面板**回的，不是适配器。看到它去改适配器，方向就错了。

### 3.1 流式（SSE）经面板能不能穿过去

**能，但只有一个参数管用。** 2026-11-11 在 new-api `v1.0.0-rc.44` 上实测（独立实例，非生产渠道）：

| 请求体 | 面板返回 | 结论 |
|---|---|---|
| `{"stream_format":"sse"}` | `text/event-stream`，首块 630 ms | ✅ 唯一有效写法 |
| `{"stream_format":"sse","stream":true}` | `text/event-stream` | ✅ 同上 |
| `{"stream":true}` | `audio/mpeg`（整段） | ❌ 面板直接吃掉这个字段 |
| `{"stream_format":"audio"}` | `audio/mpeg` | ✅ 等价于不要流式 |

`stream: true` 是 OpenAI 的写法，new-api 的 `AudioRequest` 里这个字段**没有绑定**，静默丢弃，不报错也不流式。所以「客户说开了流式但还是等 3 秒」的第一嫌疑是**他只传了 `stream:true`**。

**两个必须知道的差异**（同一请求直连 vs 经面板对比）：

1. **`event:` 行会被面板吞掉**。面板按行扫描，只转发 `data:` 开头的行，再统一重拼成 `data: {...}`。所以经面板拿到的 SSE **没有事件名**，只能靠 JSON 里的 `type` 字段分辨（适配器两个都写了，所以能跑）。依赖 `event: speech.audio.delta` 的 SDK 会挂。
2. **`data` 行数会变**（实测 130 → 71），但字节数一致、`speech.audio.done` 保留。播放侧无影响，只是别用「收到几条 delta」判断完整性。

---

## 4. 避坑清单

> 本章讲**为什么会踩**和**怎么预防**（按阶段组织：部署前 / 接入时 / 计费与长文本）。
> 已经在报错了、想按现象直接找解法，用 [`QUICKSTART.md` §6 部署阶段排障](QUICKSTART.md#6-部署阶段排障)。

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
| **用了标准引擎却没传 `voice`** | `400`「无法确定 voice_id」 | 补 `voice`（如 `yun`）；或改用场景别名，别名已隐含音色（见 §6） |

### 4.3 计费与长文本

| 坑 | 现象 | 原因 |
|---|---|---|
| **`AudioRatio` 没归零** | 扣费是预期的 2–5 倍 | 见 §3.1 |
| **超时重试导致白烧额度** | 长文本失败后额度照样掉 | 上游是「合成完才返回」，客户端超时上游已扣费。本项目**超时永不重试**，也不要把 `ELEVENLABS_MAX_RETRIES` 调大 |
| 长文本超时 | 超过约 800 字就报 502 / 等很久 | 瓶颈通常是**面板 / 网关的 60–120 秒读超时**，不是适配器的 300 秒。约 33ms/字 → 60 s 只够 1800 字。>800 字请走异步（§7）或分段 |
| 客户说「失败了还被扣钱」 | 争议 | 失败请求 `bill_chars = 0`，且响应带 `X-Request-Id`，可逐笔自证 |
| 中文念错字 | 多音字 / 生僻字 | 文本层问题，不是音色问题：改同音字或用发音字典 |

---

## 5. 请求与响应

### 5.1 请求字段

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `model` | 是 | `eleven_v3` | 标准引擎名；启用补充包后也可用场景别名（`zh-boyin`）或兼容名（`tts-1`）。默认值可由 `ELEVENLABS_DEFAULT_MODEL` 覆盖 |
| `input` | 是 | — | 待合成文本，不能为空 |
| `voice` | 视模型 | — | 音色名（`ladaisy`）或原始 `voice_id`（≥15 字符）。**用标准引擎时必填**（否则 400「无法确定 voice_id」）；用场景别名时由别名隐含，无需传。指定后**覆盖** `model` 隐含的音色 |
| `response_format` | 否 | `mp3` | `mp3` / `opus` / `wav` / `pcm`；`aac`、`flac` 上游不支持 |
| `speed` | 否 | — | OpenAI 区间 `0.25–4.0`，**钳制**到 `0.7–1.2`，钳制时返回 `X-Speed-Clamped` |
| `stream` | 否 | `false` | 兼容旧客户端：单独传 `true` 仍返回完整音频，响应头 `X-Stream-Ignored: true` |
| `stream_format` | 否 | `audio` | `sse` 开启真正 MP3 流式；`audio` 保持完整音频 |
| `text_clean` | 否 | 服务端 `ADAPTER_TEXT_CLEAN` | 单次清理档位 `off` / `safe` / `normalize`；只影响送上游的合成量，**不改变计费基数** |

### SSE 流式（MP3）

请求示例：`{"model":"tts-1","voice":"rachel","input":"你好","response_format":"mp3","stream_format":"sse"}`。
上游使用官方 `/text-to-speech/{voice_id}/stream`，不缓冲完整音频、不自动重试。
返回 `text/event-stream`：`speech.audio.delta` 的 `delta` 是 base64 MP3 字节；逐块解码后按顺序拼接。
正常结束为 `speech.audio.done`，其 `usage.input_tokens` / `total_tokens` 是原始 `input` 的 Unicode 字符数（不 strip，与默认 input 计费一致），不是模型 tokenizer 数。
流中失败发送 `error`，不发送 done；客户端需将缺少 done 视为未完成。HTTP 头已发出后不能再更改 200 状态；账本把失败/取消记为 502/499、计费 0；**成功时记上游真实积分**（`character-cost` 在响应头里，流式也拿得到），拿不到才写 `null`。
上游非 200 在 SSE 响应头发送前按原 OpenAI 错误格式返回。流结束或断开时关闭上游连接，并发槽保持至响应结束。
首版仅支持 MP3，WAV/PCM/Opus SSE 返回 400；不伪造 WAV RIFF 头。Mock 模式（`ELEVENLABS_MOCK=1`）也能跑 SSE：按同一套事件发 mock WAV 字节，响应头 `X-Mock-Format: wav` 明说容器是 WAV，测试环境不必再靠离线桩才能验证流式链路。
反代请关闭响应缓冲（响应已带 `X-Accel-Buffering: no`）。

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

### 三层的"保质期"不一样，别当一回事

这是接入设计里最容易被忽略的一点：**引擎是契约，别名只是便利**。

| 层 | 是什么 | 谁在维护 | 会不会失效 | 适合 |
|---|---|---|---|---|
| `engines` | 模型版本：`eleven_v3` / `v4` / `flash` / `turbo` / `v2` | ElevenLabs 官方版本化 | 有退出周期，**但会提前公告** | 生产 |
| `voices` | 名字 → `voice_id` 的一层映射 | 本仓库 | 底层音色失效即失效 | 过渡 |
| `packs` 别名 | 音色 + 引擎 + `voice_settings` 打包成一个名字（`zh-boyin`） | 本仓库 | **底层音色一挂，整条别名立刻不可用** | 体验 / 演示 |

**别名把「音色」包装成了「模型名」卖出去，这会让人误以为它和 `eleven_v3` 一样稳定——它并不。**

已经发生过两次：`lin` 被原作者关闭共享导致 `zh-duihua` 只能临时改指；ElevenLabs 的 Default voices 于 2026-12-31 集体失效。

**所以给接入方的建议是**：

- **快速试用 / 演示** → 用别名，一个字符串就出声，不用挑音色；
- **长期生产** → 让调用方**自己传 `voice_id`**（`voice` 字段本来就支持 ≥15 字符的原始 ID）。这样音色由他自己掌控，我们换别名不会动到他的业务；他自己的克隆音色也不会被我们替换掉。

> 换句话说：别名负责"好上手"，`voice_id` 负责"可长期依赖"。前者是体验，后者才是契约。

### 别名里的音色是「建议」，不是「绑定」

`packs` 里的别名 = **场景定义**（语言、引擎、`voice_settings`）+ **一个默认音色**。

那个默认音色只是"不挑也能出声"的兜底，**不构成绑定**。三种改法，都不用改我们的代码：

| 谁要改 | 怎么改 | 生效范围 |
|---|---|---|
| **调用方**：这次请求换个声音 | 照常传 `voice`，它**优先于**别名自带的音色 | 单次请求 |
| **部署方**：换全局的默认建议音色 | 改 `app/voices.json` 里别名的 `voice_id` | 整个实例 |
| **部署方**：不改镜像，挂自己的表 | `ELEVENLABS_VOICES_FILE=/etc/adapter/voices.json` | 整个实例 |

```bash
# 用 zh-boyin 的场景参数（中文 / 语速 / 稳定度），但换成自己的音色
curl -X POST http://127.0.0.1:8123/v1/audio/speech \
  -H "Authorization: Bearer sk_你的Key" -H "Content-Type: application/json" \
  -d '{"model":"zh-boyin","voice":"你自己的voice_id","input":"这句话用我的声音读"}'
```

**换音色只换音色，场景参数照旧继承**：`lang`、`voice_settings`、引擎仍来自别名。这条语义有测试钉住（`test_alias_scene_params_survive_voice_override`），改实现会让它变红。

> ⚠️ **用 `ELEVENLABS_VOICES_FILE` 时必须先挂载、再验证。**
> 该路径**不存在时会静默回落到内置表**，不报错。容器里默认只挂了 `./logs`，外部音色表要自己加一行：
>
> ```yaml
> volumes:
>   - ./logs:/app/logs
>   - ./voices.json:/etc/adapter/voices.json:ro   # 路径要和环境变量一致
> ```
>
> 改完用 `curl -s http://127.0.0.1:8123/v1/voices | grep 你自己的音色名` 确认真的生效——
> 「设了环境变量」和「表真的换了」是两件事。

> 需要绕开某个场景参数时，就别用别名——直接 `model` 填引擎名 + `voice` 填音色，由你自己全权控制。别名的价值是省配置，不是替你决定。

> ⚠️ **音色是有保质期的。** `voices` 段里的 `rachel` / `adam` / `josh` / `george` 等 10 个英文音色属于 ElevenLabs **Default voices**，官方已公告 **2026-12-31 全部失效**；`compat` 包的 `alloy`/`echo`/`nova` 等 6 个别名映射到同一批 ID，同样到期。
> 上线前请用 `GET /v1/voices` 确认目标音色仍在，长期方案优先**账号自有克隆音色**——公共库音色（如曾经的 `lin`）随时可能被收回。

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

> ⚠️ **开了也不等于客户能用。** 面板不转发 `/v1/audio/speech/jobs`（返 404），异步链路必须**直连适配器**，或在适配器前面加 nginx 单独放行该路径。详见 §3.3「经面板调用的能力边界」——不知道这条，开了开关也只会收到 404。

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

## 9. 账本字段：怎么回答「谁在用流式」「哪个档位在烧钱」

账本是 JSONL，一行一个请求，写在 `ADAPTER_BILLING_LOG_DIR`。做对账时最容易读错的几个字段：

| 字段 | 含义 | 常见误用 |
|---|---|---|
| `model` | **客户端请求里写的值**，可能只是音色别名（`rachel` / `lin`） | ❌ 拿它算成本 |
| `model_id` | **上游真实模型**（`eleven_flash_v2_5` / `eleven_v3`） | ✅ 只有它能对上 ElevenLabs 的计费 |
| `voice` | 真实 `voice_id`（解析后回填，不是别名） | — |
| `stream` | 本次**实际**是否走了 SSE（`true` / `false`） | 旧行没有这个字段，一律按 `false` 计 |
| `stream_format` | `sse` / `audio` | — |
| `stream_ignored` | 客户端传了 `stream:true` 但没给 `stream_format=sse` | 排查「说好的流式怎么还是慢」的第一入口 |
| `in_chars` / `sent_chars` | 客户提交量 / 实际合成量 | 前者对账、后者算成本，别混 |
| `up_credits` | 上游 `character-cost` 真实积分 | 失败/取消行是 `null`（不是 0） |

**为什么要单独记 `model_id`**：flash 档实测约 **0.24 积分/字符**，v2/v3 档约 **0.48**——差一倍。只用 `model`（音色别名）聚合，两个档位会混成一个既不代表谁的平均数，毛利怎么算都是错的。

**为什么要单独记 `stream`**：不然上线三个月后，没人能回答「有多少客户真的在用流式」「流式的首块 P95 是多少」。这两个问题在商务谈判里一定会被问到。

`GET /v1/usage` 已直接给出 `sse_requests` / `audio_requests` / `sse_ratio`，以及按档位拆开的 `by_model`（含每档 `credits_per_char`）。

> 2026-11-11 之前写入的账本行没有 `stream` / `model_id`。聚合逻辑对旧行向后兼容：`stream` 缺省按非流式、`model_id` 缺省退回 `model`。**不要把旧行和新行的 `by_model` 直接对比**，旧行的 key 是音色别名。

---

## 10. 安全与限制

**这个适配器不自带鉴权。** 它设计为位于中转站之后，由面板负责用户认证与扣费。**不要直接暴露公网**——前置网关，或用 `ADAPTER_ALLOW_IPS` 限制来源。

| 限制 | 状态 |
|---|---|
| 流式 | `stream_format=sse` 支持 MP3；单独 `stream=true` 仍忽略并返回完整音频 |
| `/v1/audio/transcriptions`（STT） | 未实现 |
| 长文本自动分片 | 未实现，超限直接 400 |
| 适配器自身鉴权 | 无，见上文 |

部署边界与已修问题见 [`SECURITY.md`](SECURITY.md)。

---

## 11. 测试

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
| [examples/](examples/README.md) | **可直接运行的调用脚本**：Python / Node / curl，长稿分段与异步任务 |
| [examples/demo/](examples/demo/README.md) | **在线试听**：6 段真实合成音频 + 文案原文 + 复现命令，[网页版一页连听](https://kimimei.github.io/newapi-elevenlabs-adapter/examples/demo/) |
| [scripts/README.md](scripts/README.md) | 12 项自检 / 一页对数报告 / 三层对账工具 |
| [SECURITY.md](SECURITY.md) | 部署边界、已修问题、已知限制 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、测试、计费改动的额外要求 |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |

## License

[Apache-2.0](LICENSE) —— 可商用、可修改、可再分发；请保留版权与许可声明。

# 可运行示例

文档里的代码块只能"看"，这里的脚本能**跑完、能报错、能对账**。

| 你想做的事 | 用这个 |
|---|---|
| 先听一遍成品，判断值不值得接 | `demo/` —— 6 段真实合成音频，附文案原文与复现命令 |
| 先确认能出声 | `python/tts.py` 或 `node/tts.mjs` |
| 已经用 OpenAI SDK 写的代码要接过来 | `python/tts_openai_sdk.py` |
| 一条几千字的口播 / 有声书章节 | `python/batch.py`（分段）或 `python/long_text_job.py`（异步） |
| 排障：到底通没通、账本对不对 | `curl/probe.sh` |
| 不想装 Python/Node，只想用 curl | `curl/tts.sh`、`curl/jobs.sh` |

---

## 一、30 秒跑通

前提只有一个：**有一个在跑的适配器**（本机 Docker、裸 Python、或别人给你的中转站地址）。

```bash
cp examples/.env.example examples/.env
#   编辑 examples/.env：填 ADAPTER_BASE_URL 和 ADAPTER_TOKEN

python examples/python/tts.py "你好，世界"
```

真实输出：

```
→ POST http://127.0.0.1:8123/v1/audio/speech
  model=eleven_v3 voice=yun format=mp3 chars=5
✓ 200
  X-Request-Id        323ff4096fdb492c
  计费字符 / 上游积分  5 / 2
  实际下发 model/voice eleven_v3 / 4AfodMgwXps9oZFhHzoj
  耗时 / 体积          0.12s / 46.9 KB
  x-usage-rule        status=200 mode=input bill=input=5 upstream=2
  已保存              out.mp3
```

> mock 模式下「上游积分」显示 `-`（没真的调上游），并会多一行 `x-mock-format-mismatch: mp3` 提醒你文件实际是 WAV。

**依赖**：Python 3.9+（只用标准库）／Node 18+（内置 fetch）／bash + curl。三种任选，都不需要 `pip install` 或 `npm install`。

---

## 二、配置：三种给法，优先级从高到低

| 方式 | 例子 | 适合 |
|---|---|---|
| 命令行参数 | `--base-url http://127.0.0.1:8123/v1 -k sk-xxx` | 临时试、CI |
| 环境变量 | `ADAPTER_BASE_URL=... ADAPTER_TOKEN=... python tts.py` | 容器 / 流水线 |
| `examples/.env` | 复制 `.env.example` 改一改 | 日常开发（推荐） |

`examples/.env` 已被 `.gitignore` 排除，填真实令牌不会进仓库。

| 变量 | 说明 |
|---|---|
| `ADAPTER_BASE_URL` | 适配器地址，**要带 `/v1`**（漏了脚本会自动补并提示） |
| `ADAPTER_TOKEN` | 令牌，见下方「两种 Key」 |
| `ADAPTER_MODEL` | 引擎，默认 `eleven_v3`（中文最稳） |
| `ADAPTER_VOICE` | 音色，默认 `yun` |
| `REQUEST_TIMEOUT` | 客户端超时秒数，默认 180（和服务器上的 `ELEVENLABS_TIMEOUT` 不是一回事） |

### 两种 Key 别搞混

| 场景 | `ADAPTER_TOKEN` 填什么 |
|---|---|
| **直连自建适配器**（本机/自己的服务器） | 你的 ElevenLabs Key（`sk_` 开头）。适配器拿它去调上游；若服务端 `.env` 已配 `ELEVENLABS_API_KEY`，这里可以留空 |
| **经 new-api 中转站** | 面板发你的令牌（通常 `sk-` 开头）。上游 Key 你看不到，也不该知道 |

填错的表现都是 `401`，但原因完全相反 —— 这是接入期最常见的误判。

---

## 三、逐个脚本

### `python/tts.py` — 最短路径，零依赖

```bash
python examples/python/tts.py "你好，世界"
python examples/python/tts.py --file 稿子.txt -o 成品.mp3 --speed 0.9
python examples/python/tts.py "测试" --format wav --voice pangge
cat 稿子.txt | python examples/python/tts.py -o 成品.mp3      # 管道
```

**为什么留这个而不是只给 SDK 示例**：线上排障时 SDK 会套一层自己的错误处理，看不出到底是 401 还是 404。这个脚本把状态码、响应头、响应体原样摊开，先确认链路通了再上 SDK。

完整参数：`python examples/python/tts.py --help`

### `python/tts_openai_sdk.py` — 官方 SDK 版

```bash
pip install openai
python examples/python/tts_openai_sdk.py "你好，世界" -o out.mp3
```

和写 OpenAI 语音代码只差 `base_url`/`api_key` 两个变量。多出来的能力（`text_clean`）走 `extra_body`，不会破坏老代码 —— 这正是"兼容 OpenAI 协议"的价值：换供应商不用改业务。

### `python/batch.py` — 长稿分段（推荐给长文本）

```bash
python examples/python/batch.py --file 口播稿.txt -o 成品.mp3
python examples/python/batch.py --file 口播稿.txt --segment 500 --no-concat
```

做四件事：

1. **切在句号上**，不切断语义（逗号不算边界，切在逗号上语调会断在半句）
2. **逐段落盘**到 `成品.parts/part-01.mp3`…，想重录某句就删掉那一个文件再跑
3. **断点续跑**：第 7 段失败不用从第 1 段重来，重跑同一条命令即可
4. 有 ffmpeg 就自动拼接，没有就如实说明并保留分段

为什么必须分段：上游是"整段合成完才返回"，2000 字以上大概率撞上网关 60–120 秒超时，**而超时的请求上游可能已经扣费**。分段后单次请求短，失败只损失一段。

### `python/long_text_job.py` — 异步任务（提交→轮询→下载）

```bash
python examples/python/long_text_job.py --file 有声书一章.txt -o 成品.mp3
```

前提：服务端 `ADAPTER_JOBS_ENABLED=1`，否则提交直接 404（脚本会把这句话打出来）。没开也能处理长稿 —— 用 `batch.py` 分段。

状态流转 `queued → running → succeeded / failed / cancelled`。看到 `queued` 不要重提，返回里的 `estimated_seconds` 就是预估等待；**反复重提才是打爆并发的主因**。

### `node/tts.mjs` / `node/long_text_job.mjs`

```bash
node examples/node/tts.mjs "你好，世界" -o out.mp3
node examples/node/long_text_job.mjs --file 稿子.txt -o out.mp3
```

Node 18+ 内置 fetch，零依赖。共用的解析/报错在小工具 `node/_shared.mjs` 里，两个脚本行为与 Python 版一致。

### `curl/tts.sh` — 一次典型调用

```bash
bash examples/curl/tts.sh "你好，世界" -o out.mp3
bash examples/curl/tts.sh "只看计费头" --headers-only
bash examples/curl/tts.sh --file 稿子.txt -o 成品.mp3
```

比手写一行 curl 多做了三件必做的事：先看 `Content-Type` 再落盘、JSON 正确转义（引号/换行/反斜杠）、错误体不会被写进 `.mp3`。

> Windows 用户：PowerShell 里 `curl` 是 `Invoke-WebRequest` 的别名，要么用 `curl.exe`，要么直接在 Git Bash / WSL 里跑本脚本。

### `curl/probe.sh` — 只读自检（排障第一步）

```bash
bash examples/curl/probe.sh
```

四段：健康检查 → 模型目录 → 音色 → 用量账本。全是 GET，不花钱。服务通不通、模型有没有上架、账本是不是一直 0，一次就能看出来。

### `curl/jobs.sh` — 异步任务（纯 shell）

```bash
bash examples/curl/jobs.sh --file 稿子.txt -o 成品.mp3 --poll 2
```

---

## 四、坑：脚本已经处理了，但你得知道为什么

| 坑 | 现象 | 脚本怎么处理 |
|---|---|---|
| `-o out.mp3` 直接写盘 | 文件只有几百字节、播放器打不开 | 先验 `Content-Type: audio/*`，不是音频就把错误体打出来 |
| 内联 `-d '{"input":"..."}'` | 稿子里有引号/换行就 `400 invalid JSON` | JSON 转义（先攒行再转义，顺序反了第二行开始就漏转义） |
| base 漏 `/v1` | 真实路径变成 `/audio/speech` → 404 | 自动补全**并提示一句**（静默修补会让人以为自己填对了） |
| 空 `Authorization` 头 | 部分网关直接 401 | token 为空时干脆不发这个头 |
| 客户端超时即重试 | 上游已扣费，重试是二次扣费 | **一律不重试**，把失败摊开给人看 |
| 长文本一次丢进去 | 等 60–120 秒后 502 | `batch.py` 分段 / `long_text_job.py` 异步 |
| 502 且响应不是 `{"error":...}` | 其实是反代/网关/别的服务在应答 | 提示"这不是适配器的错误结构，先确认端口上跑的是什么" |

---

## 五、从示例到生产

**1）换成 SDK**（Python）

```python
from openai import OpenAI
client = OpenAI(base_url="https://your-host/v1", api_key="sk-你的令牌")
resp = client.audio.speech.create(model="eleven_v3", voice="yun",
                                  input="你好，世界", response_format="mp3")
resp.write_to_file("out.mp3")
```

Node 的 `openai` 包同理，只需 `new OpenAI({ baseURL, apiKey })`。

**2）超时就失败，不要重试**

适配器是同步语义，上游"合成完才返回"。客户端超时的那一刻，上游很可能已经扣了费 —— 重试一次就是扣两次。要保证完成率，改用异步任务（`/v1/audio/speech/jobs`）或分段。

**3）并发别超过服务端上限**

服务端 `ADAPTER_MAX_CONCURRENCY` 控制同时在合成几条；客户端并发更高只会排队、超时、然后被误判成"服务不稳定"。批处理时把并发压到 2–4 起步。

**4）用 `X-Usage-Input-Characters` 对账**

每次响应都带这个头，和后台扣量逐笔一致，可自行核对。`X-Request-Id` 成功失败都有，提工单时带上它。

---

## 六、失败速查

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| `✗ HTTP 401` | 令牌类型错 / 过期 | 对照「两种 Key」表 |
| `✗ HTTP 404` | base 多了或少了 `/v1`；模型没注册 | 跑 `curl/probe.sh` 看模型列表 |
| `✗ HTTP 400` + `response_format` | 传了 `aac`/`flac` | 改 `mp3`（最通用） |
| `✗ HTTP 502` + `upstream` | 上游超时 / 服务器出网不通 | 长文本改异步；短文本查服务器网络 |
| `连不上 ...` | 服务没起 / 端口错 / 防火墙 | `curl -s http://127.0.0.1:8123/healthz` |
| 音频正常但账本全 0 | 容器内 `logs/` 不可写 | 宿主执行 `chown -R 10001:10001 logs` |
| 中文多音字念错 | 文本层问题，不是音色问题 | 改同音字，或问服务商是否支持发音字典 |

---

## 七、没有真实 Key 也能试

适配器支持 mock 模式，不调用上游、不花钱，返回静音 WAV（`X-Mock: true`）：

```bash
ELEVENLABS_MOCK=1 ADAPTER_PORT=8123 python run.py
```

上面所有脚本都能在 mock 下跑通链路 —— **除了异步任务**：`/v1/audio/speech/jobs` 需要真实 Key 才会真正合成长文本。

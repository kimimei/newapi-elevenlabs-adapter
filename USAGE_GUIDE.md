# 调用方使用指南

> 面向**使用**这些语音接口的开发者。兼容 OpenAI 协议，任何 OpenAI SDK 或客户端都能直接调。
> 部署方看 [`QUICKSTART.md`](QUICKSTART.md)。

---

## 1. 接入三要素

| 项 | 值 |
|---|---|
| Base URL | `https://<服务商域名>/v1` |
| API Key | 服务商发给你的令牌（通常是 `sk-` 开头） |
| 模型 | 见 §2；中文推荐 `eleven_v3` |

> 注意区分两种 Key：**你手里的是服务商令牌**（`sk-`）。`sk_` 开头的是服务商自己的上游 ElevenLabs Key，不需要也不应该给你。

## 2. 模型怎么选

`model` 选**引擎**，`voice` 选**音色**——这是 OpenAI 原生的语义，一个引擎可以配很多音色。

| 推荐用途 | 模型 | 特点 |
|---|---|---|
| **中文首选** | `eleven_v3` | 中文最稳，也是台湾口音唯一可用引擎 |
| 普通话情感内容 | `eleven_v4` | 情感与停顿最佳，适合影视/广告 |
| 保守兜底 | `eleven_multilingual_v2` | 经典多语言模型 |
| 低延迟 | `eleven_flash_v2_5` | 快，但逐字顿出，**不适合成品配音** |
| 低成本批处理 | `eleven_turbo_v2_5` | 情感扁平，适合书面稿朗读 |

如果服务商开启了中文场景包，还可以直接用场景别名（`zh-boyin`、`zh-yingshi` 等），此时 `model` 已隐含音色，不用再传 `voice`。

## 3. 音色

- **音色名**：`yun`、`pangge`、`bo`、`evan`、`lin`、`amy`、`xinghe`、`anna`、`ladaisy`、`ziyue`、`rachel` 等
- **原始 voice_id**：也可以直接传 ElevenLabs 的 voice_id（15 位以上）
- **查完整列表**：`GET /v1/voices`

> 用标准引擎模型时 **`voice` 是必需的**。不传会返回 400「无法确定 voice_id」。

## 4. 调用示例

> 下面是最短示例，方便直接抄进代码。**能直接跑、带排错的完整脚本在 [`examples/`](examples/README.md)**（Python / Node / curl，含长稿分段与异步任务）。

### Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="https://<服务商域名>/v1", api_key="sk-你的令牌")

resp = client.audio.speech.create(
    model="eleven_v3",
    voice="yun",
    input="你好，这是一段合成语音。",
    response_format="mp3",     # mp3 / opus / wav / pcm
)
resp.write_to_file("output.mp3")
```

### curl

```bash
curl -X POST https://<服务商域名>/v1/audio/speech \
  -H "Authorization: Bearer sk-你的令牌" \
  -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"你好，世界"}' \
  -o output.mp3
```

> **Windows PowerShell 注意**：必须用 `curl.exe`（`curl` 在 PowerShell 里是 `Invoke-WebRequest` 的别名）；JSON 建议先写文件再 `-d @req.json`，能避开引号转义问题。

### 需要看计费字符数时

```bash
curl -sD - -o /dev/null -X POST https://<服务商域名>/v1/audio/speech \
  -H "Authorization: Bearer sk-你的令牌" \
  -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"五个字符"}' | grep -i x-
```

## 5. 请求与响应

### 请求字段

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `model` | 是 | `eleven_multilingual_v2` | 引擎名，或场景别名 |
| `input` | 是 | — | 待合成文本 |
| `voice` | 视模型 | — | 音色名或 voice_id。标准引擎必填 |
| `response_format` | 否 | `mp3` | `mp3` / `opus` / `wav` / `pcm`。`aac`、`flac` 不支持 |
| `speed` | 否 | — | 传 0.25–4.0，实际会被钳制到 0.7–1.2，钳制时返回 `X-Speed-Clamped` |
| `stream` | 否 | `false` | 暂不支持，传 `true` 返回 501 |

### 响应头

| 头 | 含义 |
|---|---|
| `X-Request-Id` | 本次请求唯一 id，**成功失败都有**。和客服对账时提供这个 |
| `X-Usage-Input-Characters` | 本次计费字符数 |
| `X-Usage-Rule` | 一句话计费依据，例 `status=200 mode=input bill=input=5 upstream=2` |
| `X-ElevenLabs-Model` / `-Voice` | 实际使用的引擎与音色 |
| `X-Speed-Clamped` | 仅语速被钳制时出现 |
| `X-Text-Clean` | 仅文本被清理时出现，说明删了什么 |

### 错误格式

统一 OpenAI 结构，SDK 可直接 catch：

```json
{"error": {"message": "voice.voice_id: 无法确定 voice_id", "type": "invalid_request_error", "code": "invalid_request_error"}}
```

## 6. 长文本

同步接口在**一两千字以上**容易失败：网关通常 60–120 秒超时，上游合成速度约 33ms/字。

如果服务商开启了异步任务接口，长文本请走这条：

```bash
# 1) 提交，立刻拿 job_id（HTTP 202）
curl -X POST https://<服务商域名>/v1/audio/speech/jobs \
  -H "Authorization: Bearer sk-你的令牌" -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"几千字正文...","response_format":"mp3"}'

# 2) 轮询（建议 2–5 秒一次）
curl https://<服务商域名>/v1/audio/speech/jobs/{id}

# 3) 完成后下载
curl -OJ https://<服务商域名>/v1/audio/speech/jobs/{id}/audio
```

状态流转：`queued` → `running` → `succeeded` / `failed` / `cancelled`。

> 提交后看到 `queued` 不要反复重提——返回里的 `estimated_seconds` 就是预估等待时间。反复提交才是打爆并发的主因。

**建议**：2000 字以上直接分段提交，比一个巨大的请求可靠。段与段之间留一个自然停顿点，成品衔接更自然。

## 7. 计费怎么算

- 按**输入字符数**计费，中英文都是 1 字符 = 1 计费单位
- 单价由你的服务商决定，具体价格问服务商，接口本身不带价目表
- 每次调用都能拿到 `X-Usage-Input-Characters`，与后台扣量逐笔一致，可自行核对
- **失败请求不计费**

## 8. 对话类客户端

Cherry Studio / LobeChat / ChatGPT Next Web 等可以在「语音」设置里填：

- 服务地址：`https://<服务商域名>/v1`
- Key：你的令牌

> 这是 **TTS 语音合成**接口，不是聊天模型。用它合成语音，不能当对话模型用。

## 9. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 401 | 令牌错误或已过期，检查 Bearer 值；确认没把服务商的 `sk_` 和自己的 `sk-` 搞混 |
| 400 invalid JSON | shell 转义问题，改用 `-d @req.json` |
| 400「无法确定 voice_id」 | 用了标准引擎却没传 `voice`，补上即可 |
| 400 参数错误 | 检查 `response_format`（`aac`/`flac` 不支持） |
| 404 model_not_found | 模型名拼错，或服务商没上架该模型 |
| 501 | 传了 `stream=true`，去掉该参数 |
| 输出文件很小、内容是 JSON | 请求失败了，错误体被写进文件。先不加 `-o` 看响应 |
| 合成慢 | 正常 1–2 秒（短文本）；文本越长越慢，`flash`/`turbo` 档更快 |
| 中文多音字念错 | 文本层问题，不是音色问题。改同音字，或问服务商是否支持发音字典 |
| 长文本超时 | 走异步任务接口，或分段提交 |

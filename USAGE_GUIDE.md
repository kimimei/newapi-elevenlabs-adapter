# ElevenLabs 中转使用文档（调用方）

> 更新时间：2026-10-16 ｜ 兼容 OpenAI 协议，任何 OpenAI SDK / 客户端可直接使用

---

## 一、接入三要素

| 项 | 值 |
|---|---|
| Base URL | `https://<你的中转域名>/v1` |
| API Key | 你的 new-api 令牌（`sk-` 开头，后台令牌管理获取） |
| 模型 | 见下方模型清单（中文推荐 `eleven-rachel`） |

## 二、模型与中文建议

| 推荐用途 | 模型名 | 实际引擎 | 单价 |
|---|---|---|---|
| **中文 / 中英混合（推荐）** | **`eleven-rachel`** | eleven_multilingual_v2 | $0.10/千字符 |
| 中文备选 | `eleven_multilingual_v2` / `eleven_v3` | 自身 | $0.10/千字符 |
| 中文低成本 | `eleven-flash` | Flash v2.5 | $0.05/千字符 |
| 其他音色别名 | eleven-george / eleven-adam / eleven-elli | eleven_multilingual_v2 | $0.10/千字符 |
| OpenAI 兼容名 | tts-1 / tts-1-hd / elevenlabs-tts | eleven_multilingual_v2 | $0.10/千字符 |
| Turbo 档 | eleven-turbo / eleven_turbo_v2_5 | Turbo v2.5 | $0.05/千字符 |
| Flash 档 | eleven_flash_v2_5 | Flash v2.5 | $0.05/千字符 |

**结论：中文验证直接用 `eleven-rachel`**（模型名即音色，自动用 Multilingual v2 引擎，中文发音最自然）。

## 三、音色

- **模型名即音色**：`eleven-rachel` 的 voice 就是 rachel 女声；george / adam / elli 同理；
- **单独指定音色**：请求体 `voice` 字段传音色名即可（支持 rachel、george、adam、antoni、bella、domi、elli、josh、arnold、sam）；
- **完整音色列表**：`GET /v1/voices` 可拉取。

## 四、调用示例

### 1. Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<你的中转域名>/v1",
    api_key="sk-你的令牌",
)

resp = client.audio.speech.create(
    model="eleven-rachel",        # 中文推荐
    input="你好，这是通过中转平台合成的中文语音。",
    voice="rachel",               # 可省略（model 已含音色）
    response_format="mp3",        # mp3 / opus / wav / pcm
)
resp.write_to_file("output.mp3")
```

### 2. curl（Windows PowerShell）

```powershell
echo {"model":"eleven-rachel","input":"你好，这是中文测试语音","voice":"rachel"}> req.json
curl.exe -X POST https://<你的中转域名>/v1/audio/speech -H "Authorization: Bearer sk-你的令牌" -H "Content-Type: application/json" -d @req.json -o output.mp3
```

> Windows 注意：必须用 `curl.exe`（PowerShell 的 `curl` 是别名）；JSON 建议写文件后 `-d @req.json`，避免引号转义问题。

### 3. 对话类客户端（Cherry Studio / LobeChat 等）

- 服务地址填 `https://<你的中转域名>/v1`，Key 填令牌；
- 注意：这是 **TTS 语音合成**，不是聊天对话模型——用于合成语音，不能当 ChatGPT 用。

## 五、计费口径

- **按输入字符数计费**（中英文均 1 字符 = 1 计费单位）；
- 每次调用响应头返回 `X-Usage-Input-Characters`，可用于本地对账（与后台扣量完全一致，财务级精度）；
- 单价：Multilingual 档 **$0.10/千字符**、Flash/Turbo 档 **$0.05/千字符**（new-api 后台按此自动扣量）。

## 六、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 返回 401 | 令牌错误或已过期，检查 Bearer 值 |
| 返回 400 invalid JSON | 请求体 JSON 被 shell 转义坏了，改用 `-d @req.json` 写法 |
| 返回 400 参数错误 | 检查 model / response_format（aac、flac 不支持） |
| stream=true 返回 501 | 流式暂不支持（二期），去掉该参数 |
| 输出文件很小且是 JSON | 请求失败，错误被写进了文件；先不加 `-o` 看响应 |
| 合成慢 | 正常 1-2 秒（文本越长越慢），Flash/Turbo 档更快 |

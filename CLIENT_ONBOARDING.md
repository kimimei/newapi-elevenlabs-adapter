# ElevenLabs 中转接入方案 —— 给中转平台客户的操作手册

> 适用对象：new-api /one-api 类中转平台运营方 ｜ 方案形态：零侵入 OpenAI 兼容适配器
> 一句话：
>
> **不加插件、不改源码，新增一个 OpenAI 渠道即可开卖 ElevenLabs TTS**



***

## 一、为什么是 "适配器" 而不是改源码



| 方案                   | 代价                       | 风险                            | 结论       |
| -------------------- | ------------------------ | ----------------------------- | -------- |
| Fork new-api 新增渠道包   | 要跟上游合并、重编译、维护 fork       | 上游升级全被挡住                      | ❌ 不推荐    |
| Task Plugin（渠道类型 61） | new-api 官方插件体系           | 文档自标 API v1 未正式发布、audio 覆盖未确认 | ⚠️ 契约未稳  |
| **外部 OpenAI 兼容适配器**  | **一个 FastAPI 服务，1 天可交付** | **零侵入，随时可拆，可复制到任意中转**         | ✅ **推荐** |

适配器做三件事：协议翻译（OpenAI `/v1/audio/speech` ↔ ElevenLabs 原生接口）、鉴权透传（new-api 渠道 key → `xi-api-key`）、字符计费回传（`X-Usage-Input-Characters` 精确对账）。

## 二、三步接入

### 第 1 步：部署适配器（约 10 分钟）



```
# 服务器上（任意 Linux，Python 3.10+）
git clone https://github.com/kimimei/elevenlabs-adapter.git
cd elevenlabs-adapter
python -m venv .venv && .venv/bin/pip install -r requirements.txt
nohup .venv/bin/python run.py &        # 监听 0.0.0.0:8123
curl http://127.0.0.1:8123/healthz    # 期望 {"status":"ok",...}
```

### 第 2 步：new-api 加渠道（Web 后台 1 分钟）



1. 进入管理后台 → **渠道 → 添加渠道**

2. 填：



| 字段           | 值                                                                                                                                                                                       |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 类型           | **OpenAI**（type=1）                                                                                                                                                                      |
| 名称           | elevenlabs-adapter                                                                                                                                                                      |
| 代理（Base URL） | `http://<适配器IP>:8123`                                                                                                                                                                   |
| 密钥（Key）      | **你的 ElevenLabs API Key**（形如 `sk_xxx`）                                                                                                                                                  |
| 模型           | 建议全量：`eleven-rachel,eleven-george,eleven-adam,eleven-elli,tts-1,tts-1-hd,elevenlabs-tts,eleven-flash,eleven-turbo,eleven_multilingual_v2,eleven_v3,eleven_flash_v2_5,eleven_turbo_v2_5` |
| 分组           | default                                                                                                                                                                                 |

> ⚠️ 若适配器与 new-api 同机且 new-api 跑在 Docker 里，Base URL 要用 
>
> **docker 网关 IP**
>
> （
>
> `docker network inspect bridge`
>
>  查，如 172.18.0.1），不能用 127.0.0.1。

### 第 3 步：发令牌给客户，开卖



1. 后台 → **令牌 → 添加令牌**，生成 `sk-xxx`；

2. 客户拿这个令牌按 OpenAI 格式直接调用：



```
curl http://<你的中转>/v1/audio/speech \
  -H "Authorization: Bearer sk-你的令牌" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "eleven-rachel",      # 模型名 = 音色名（eleven-<name>）
    "input": "你好，这是一段通过中转平台合成的语音",
    "voice": "rachel",             # 可选：直接传音色名
    "response_format": "mp3"       # mp3/opus/wav/pcm
  }' \
  -o out.mp3
```

**完成。** 客户侧 0 改动，OpenAI SDK 直接用：



```
from openai import OpenAI
client = OpenAI(base_url="http://<你的中转>/v1", api_key="sk-你的令牌")
resp = client.audio.speech.create(model="eleven-rachel", input="你好", voice="rachel")
resp.write_to_file("out.mp3")
```

## 三、模型与音色清单（开卖前先对齐）



| 客户可用模型名                                     | 实际 ElevenLabs 模型 / 音色        | 成本基准         |
| ------------------------------------------- | ---------------------------- | ------------ |
| eleven-rachel / george / adam / elli        | 官方音色别名（模型名即音色）               | 按所选引擎        |
| tts-1 / elevenlabs-tts                      | eleven\_multilingual\_v2（默认） | \$0.08 / 千字符 |
| eleven\_multilingual\_v2 / eleven\_v3       | Multilingual v2/v3           | \$0.08 / 千字符 |
| eleven-flash / eleven-turbo                 | Flash / Turbo                | \$0.04 / 千字符 |
| eleven\_flash\_v2\_5 / eleven\_turbo\_v2\_5 | Flash v2.5 / Turbo v2.5      | \$0.04 / 千字符 |



* 完整音色列表可拉：`GET /v1/voices`（适配器已实现，转发给客户自助选音色）；

* 计费：适配器按**实际输入字符数**计费，`X-Usage-Input-Characters` 响应头可用于和上游账单对账（精确到字符，财务级精度）。

## 四、常见问题（客服口径）

**Q1：支持哪些格式？**

mp3 /opus/wav /pcm。aac /flac 暂不支持（会明确报 400，不静默降级）。

**Q2：支持流式（stream=true）吗？**

当前明确返回 501，二期规划支持。不要承诺。

**Q3：语音多久出？**

真实合成约 1-2 秒（取决于文本长度与引擎）。

**Q4：会不会断流 / 报错？**

错误按 OpenAI error 格式透传（如 401 无效 key、400 参数错误），客户端可正常 catch。

**Q5：能加自己的音色吗？**

能。适配器 `app/voices.json` 是音色映射表，运营方可自行增删别名；也支持客户直接传 ElevenLabs voice\_id。

**Q6：怎么对账？**

每次调用响应头 `X-Usage-Input-Characters` = 实际计费字符数；new-api 后台日志 / 配额同样记录，可交叉核对。单价按官方公开价（上表为 2026-10 快照，随官方调整而变）。

## 五、给客户的售前一句话

> "你们不需要改任何中转代码。部署一个适配器服务，在后台加一个 OpenAI 渠道填上 ElevenLabs Key，就能把 ElevenLabs 的几十种真人音色像 GPT-4o 语音一样卖给客户，按字符计费、后台自动扣量。"

## 六、边界与红线



* 适配器与中转解耦：随时可拆、可迁、可多实例；

* ElevenLabs Key 只存在于你的中转后台（渠道密钥），适配器侧不落明文；

* 本手册假设中转 = new-api/one-api 兼容 OpenAI 协议；其他协议（如 Anthropic）需确认 audio 中继支持。
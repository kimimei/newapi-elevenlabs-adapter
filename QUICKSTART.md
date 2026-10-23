# 从零部署：完整引导

> 面向第一次接触这个项目的人。按顺序做即可，全程约 15 分钟。
> 每一步都写了**预期输出**；对不上就直接跳到 [§6 排错](#6-出问题按现象查)。

---

## 0. 先选一条路

| 方式 | 适合谁 | 耗时 | 常驻方式 |
|---|---|---|---|
| **A. 一条命令**（推荐） | 想最快跑起来 | ~5 分钟（首次构建含拉依赖） | Docker + 自动重启 |
| **B. 手工 Docker** | 想自己控制每一步 / 已有 compose 编排 | ~5 分钟 | 同上 |
| **C. 裸 Python** | 没有 Docker，或要集成进现有 Python 环境 | ~2 分钟 | 需自己配 systemd |

三条路的效果完全一样。下面从 §1 的共同准备工作开始。

---

## 1. 开工前的检查

### 1.1 需要准备什么

| 项 | 要求 | 说明 |
|---|---|---|
| 服务器 | Linux（x86_64 / arm64 均可） | 本地电脑也能跑，只要能出网 |
| Docker | 20.10+，含 `docker compose` v2 | 走方式 C 则不需要 |
| Python | 3.10+（仅方式 C） | 方式 A/B 由镜像自带 |
| 出站网络 | 能访问 `api.elevenlabs.io:443` | 内网机器需要放行或配代理 |
| 端口 | 8123 空闲（可改） | 容器内固定 8123，只改宿主机端口 |
| ElevenLabs Key | `sk_` 开头，有 TTS 权限 | 见 §1.3 |
| 磁盘 | ≥ 2 GB 可用 | 镜像约 250 MB，构建缓存另算 |

### 1.2 逐项确认（5 条命令）

```bash
# ① Docker 与 compose 都在
docker --version && docker compose version
# 预期：Docker version 2x.x.x / Docker Compose version v2.x.x

# ② 端口没被占用（无输出 = 空闲）
ss -lntp | grep 8123

# ③ 能访问上游
curl -sI https://api.elevenlabs.io/v1/models | head -1
# 预期：HTTP/2 200（或 HTTP/1.1 401 —— 说明网络通，只是没带 Key）

# ④ 磁盘够用
df -h / | tail -1
# 看 Avail 列，要 ≥ 2G

# ⑤ git 可用
git --version
```

任何一项不符合：

| 情况 | 处理 |
|---|---|
| 没有 Docker | 走方式 C（§2.3），只需 Python 3.10+ |
| 8123 已占用 | 后面用 `--port 9000` 换端口，不用改别的 |
| `curl` 超时 / 拒绝连接 | 服务器出不了网。配代理：`export HTTPS_PROXY=http://...`，或在 `.env` 改 `ELEVENLABS_API_BASE` 指向你的中转 |
| 磁盘不足 | 先 `docker system prune` 清缓存 |

### 1.3 准备上游 Key

1. 登录 ElevenLabs → 右上角头像 → **Profile** → **API Keys** → 创建
2. 复制 `sk_` 开头那一串（**不是** `sk-`，两者长得像但完全不通用）

先确认这个 Key 真的有 TTS 权限（零成本，不消耗额度）：

```bash
curl -s -H "xi-api-key: sk_你的Key" https://api.elevenlabs.io/v1/user/subscription
```

预期返回一段 JSON，含 `"tier"` 和 `"character_limit"`。如果返回 `401` 或 `"detail"` 报错，说明 Key 无效或权限不足，先解决再往下走。

> **概念澄清**：这个 Key 是**上游** ElevenLabs 的。如果你还要接 new-api，面板里填的也是它——不是 new-api 自己生成的 `sk-` 令牌。这两个搞混是最高频的 401 来源。

### 1.4 准备 new-api 侧（可选）

只有打算接中转站时才需要：

| 项 | 怎么拿 |
|---|---|
| 面板地址 | 你的 new-api 后台 |
| 数据库路径 | 用 SQLite 时才有，例如 `./one-api.db`，自检要用 |
| 是否跑在 Docker 里 | 决定 Base URL 填 `127.0.0.1` 还是 docker 网关 IP |

---

## 2. 部署

### 2.1 方式 A：一条命令（推荐）

```bash
git clone https://github.com/kimimei/newapi-elevenlabs-adapter.git
cd newapi-elevenlabs-adapter
bash scripts/up.sh --key sk_你的ElevenLabsKey
```

脚本会依次做四件事，每步都有输出：

| 步骤 | 它在干什么 | 预期看到 |
|---|---|---|
| 1/4 | 从 `.env.example` 生成 `.env`，写入 Key 与端口，并 `chmod 600` | `✓ 已从 .env.example 生成 .env` |
| 2/4 | `docker compose up -d --build` | 构建日志，最后 `Started` |
| 3/4 | 轮询健康检查，最多等 60 秒 | `✓ 健康检查通过` |
| 4/4 | 拉 `/v1/models` 冒烟，有 `--db` 时顺带跑 12 项自检 | 模型数量 + 自检结果 |

结束时打印一张接入卡片：

```
────────────────────────────────────────────────────────────
 已就绪。把下面填进 new-api「添加渠道」：

   类型        OpenAI
   Base URL    http://<本机IP>:8123        ← 只到端口，不要加 /v1
   密钥        你的 ElevenLabs Key（sk_ 开头，不是中转令牌）
   模型        eleven_v4,eleven_v3,eleven_multilingual_v2,eleven_flash_v2_5,eleven_turbo_v2_5
   分组        default

 接着必做两件事（否则会算错钱）：
   ① 定价 → ModelRatio：v4 / v3 / multilingual_v2 = 40，flash / turbo = 20
   ② 同一面板 → AudioRatio：这 5 个模型全部设 0
────────────────────────────────────────────────────────────
```

常用参数：

```bash
bash scripts/up.sh --key sk_xxx --port 9000        # 换端口
bash scripts/up.sh --key sk_xxx --presets cn,tw   # 同时启用中文 + 台湾口音包
bash scripts/up.sh --key sk_xxx --db ./one-api.db # 起完顺带跑 12 项自检
bash scripts/up.sh --dry-run                      # 只打印要做什么，不落地
bash scripts/up.sh --logs                         # 跟日志
bash scripts/up.sh --down                         # 停服
```

**部署完直接跳到 §3 验证。**

### 2.2 方式 B：手工控制每一步

```bash
git clone https://github.com/kimimei/newapi-elevenlabs-adapter.git
cd newapi-elevenlabs-adapter

cp .env.example .env
chmod 600 .env                     # 不设的话同机其他用户能读到 Key
vi .env                            # 至少填 ELEVENLABS_API_KEY

docker compose up -d --build
curl -s http://127.0.0.1:8123/healthz
```

`.env` 里唯一必填的是 `ELEVENLABS_API_KEY`。**留空也能启动**——此时必须由调用方在 `Authorization` 头里透传自己的 Key，适合「一个适配器服务多个客户」的场景。

### 2.3 方式 C：裸 Python

```bash
git clone https://github.com/kimimei/newapi-elevenlabs-adapter.git
cd newapi-elevenlabs-adapter

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && vi .env    # 填 Key

ADAPTER_HOST=0.0.0.0 python run.py
```

`ADAPTER_HOST=0.0.0.0` 才会监听局域网；默认只监听 `127.0.0.1`。

要开机自启，用 systemd（注意 `User` 和路径按实际改）：

```ini
# /etc/systemd/system/elevenlabs-adapter.service
[Unit]
Description=ElevenLabs OpenAI-compatible adapter
After=network-online.target

[Service]
Type=simple
User=adapter
WorkingDirectory=/opt/elevenlabs-adapter
EnvironmentFile=/opt/elevenlabs-adapter/.env
ExecStart=/opt/elevenlabs-adapter/.venv/bin/python run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now elevenlabs-adapter
sudo systemctl status elevenlabs-adapter
```

> 非 Docker 方式下，账本目录就是运行目录下的 `logs/billing`，属主跟随运行用户，不会遇到权限问题。

### 2.4 首次构建很慢？

默认从 `pypi.org` 拉依赖。国内网络实测能卡十几分钟。

```bash
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple bash scripts/up.sh --key sk_你的Key
```

实测从 16 分钟降到 **109 秒**。这个变量只影响构建，不影响运行时行为。

Docker 镜像本身也可能慢。如果 `docker compose build` 卡在 `FROM python:3.12-slim`，给你的 dockerd 配镜像加速器：

```json
// /etc/docker/daemon.json
{ "registry-mirrors": ["https://docker.1ms.run"] }
```

改完 `sudo systemctl restart docker`。

---

## 3. 分四层验证

别只看「容器起来了」。按下面四层走，每层都能独立判断。

### L1 进程活着

```bash
docker ps --filter name=elevenlabs-adapter
# 预期：Up x minutes (healthy)

curl -s http://127.0.0.1:8123/healthz
# 预期：{"status":"ok","mock":false,"jobs":{"enabled":false}}
```

`"mock":true` 说明在 Mock 模式，此时返回的是**静音音频**——能跑通链路，但别拿去给客户听。

### L2 端点和页面

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8123/          # 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8123/openapi.json  # 200
curl -s http://127.0.0.1:8123/v1/models | head -c 300
```

默认 `/v1/models` 只列 **5 个标准引擎**。看不到 `zh-*` 是正常的，那是补充包（见 README §6）。

浏览器打开 `http://<服务器IP>:8123` 应该能看到一页配置生成器：左边试听音色，右边生成可粘贴的客户端配置。

> 打开页面能看能选，但**点试听没反应**？因为 `ADAPTER_DEMO_ENABLED` 默认 0。开启等于公开消耗服务端 Key，想开请同时在 `.env` 配好 `ADAPTER_DEMO_MAX_CHARS` / `ADAPTER_DEMO_PER_HOUR`。

### L3 真的能合成

**先花 0 成本验链路**（Mock 模式，返回静音 WAV，不扣额度）：

```bash
docker run -d --rm --name adapter-mock -p 127.0.0.1:8124:8123 \
  -e ADAPTER_HOST=0.0.0.0 -e ELEVENLABS_MOCK=1 elevenlabs-adapter:latest

curl -s -X POST http://127.0.0.1:8124/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","input":"你好","voice":"yun"}' -o /tmp/mock.wav
file /tmp/mock.wav        # 预期：RIFF (little-endian) data, WAVE audio
docker stop adapter-mock
```

**再花 2 个字符验真实上游**：

```bash
curl -s -D /tmp/h.txt -o /tmp/out.mp3 -X POST http://127.0.0.1:8123/v1/audio/speech \
  -H "Authorization: Bearer sk_你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","input":"你好","voice":"yun","response_format":"mp3"}'

grep -iE "^HTTP|x-usage|content-length" /tmp/h.txt
file /tmp/out.mp3         # 预期：MPEG ADTS, layer III ... 
```

关键返回头：

```
HTTP/1.1 200 OK
x-usage-input-characters: 2
x-usage-rule: status=200 mode=input bill=input=2 upstream=1
```

`bill=2 upstream=1` 就是计费口径在起作用：向客户计 2 个字符，向上游实际扣 1 个积分（上游约 2 字符 1 积分）。

### L4 计费没算错（最重要）

```bash
python3 scripts/preflight.py \
  --adapter http://127.0.0.1:8123 \
  --key sk_你的Key \
  --db ./one-api.db \
  --model eleven_v3 --voice yun
```

预期 12 行红绿，例如：

```
 [✓]  1  适配器健康         status=ok
 [✓]  2  上游 Key 有效      上游可用模型 12 个（未消耗字符）
 [✓]  3  模型目录非空       5 个
 [✓]  4  目标模型在目录     eleven_v3
 [✓]  5  音色可解析         yun
 [–]  6  音频可解码         未加 --live（默认不产生合成费用）
 [–]  7  字符数回传         未加 --live
 [✓]  8  渠道已注册该模型   渠道 #1 ...
 [✓]  9  abilities 行存在   default(enabled=1)
 [✓] 10  ModelRatio 已配    eleven_v3 = 40
 [✓] 11  AudioRatio = 0     已归零
 [✓] 12  计费对账           最近一条日志 #28 ✓
```

几个说明：

- **第 2 项**用零成本的 `/v1/models` 校验 Key，不消耗字符
- **第 6、7 项**默认 SKIP，加 `--live` 才做一次约 2 字符的真实合成
- **第 8–12 项**需要 `--db` 指向 new-api 的 SQLite；用 MySQL 时这几项会 SKIP
- **第 11 项 `AudioRatio = 0` 是唯一「配错看不出来、但会持续多收钱」的检查**，务必让它绿

不接 new-api、只想验适配器本身？

```bash
python3 scripts/preflight.py --adapter http://127.0.0.1:8123 --skip-upstream --no-color
```

---

## 4. 接入 new-api

### 4.1 加渠道

管理后台 → **渠道 → 添加渠道**：

| 字段 | 值 | 注意 |
|---|---|---|
| 类型 | **OpenAI** | 没有 ElevenLabs 类型，必须选这个 |
| 名称 | `elevenlabs-adapter` | 随意 |
| Base URL / 代理 | `http://<适配器IP>:8123` | **只到端口** |
| 密钥 | ElevenLabs Key（`sk_`） | 不是面板令牌 |
| 模型 | 5 个标准引擎，逗号分隔 | 见下 |
| 分组 | `default` | — |

模型列表直接复制：

```
eleven_v4,eleven_v3,eleven_multilingual_v2,eleven_flash_v2_5,eleven_turbo_v2_5
```

**适配器和 new-api 同机、且 new-api 在 Docker 里**时，Base URL 必须用 docker 网关 IP：

```bash
docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
# 通常输出 172.17.0.1 或 172.18.0.1
```

填 `127.0.0.1` 一定连不上——那是容器自己。

### 4.2 必做两件事（否则安静地多收钱）

**① `ModelRatio`**：设置 → 运营设置 → 模型倍率

| 模型 | 填 |
|---|---|
| `eleven_v4` / `eleven_v3` / `eleven_multilingual_v2` | **40** |
| `eleven_v4_turbo` / `eleven_flash_v2_5` / `eleven_turbo_v2_5` | **20** |

**② `AudioRatio`**：同一页，这些模型**全部设 `0`**。

缺配时 new-api 按 1 处理，音频输出的伪 token 会被二次计价。实测 23 字符的请求被收成 `(23 + 67) × 40`，超收 5.19 倍。这一项配错不会报错，只会在账单上体现。

### 4.3 验证渠道

**不要点「测试渠道」按钮。** 多数面板用 `/v1/chat/completions` 探测，而本适配器只有音频端点，必然失败。

用真实令牌调一次：

```bash
# 先在后台「令牌」页生成一个 sk- 开头的令牌
curl -X POST http://<面板地址>/v1/audio/speech \
  -H "Authorization: Bearer sk-你的面板令牌" \
  -H "Content-Type: application/json" \
  -d '{"model":"eleven_v3","voice":"yun","input":"渠道测试"}' \
  -o /tmp/test.mp3

file /tmp/test.mp3     # 出声即通
```

再做端到端对账，确认面板扣的 quota 等于 字符数 × 倍率：

```bash
python3 scripts/preflight.py --adapter http://127.0.0.1:8123 \
  --key sk_上游Key --db ./one-api.db --live \
  --newapi http://127.0.0.1:3000 --token sk-你的面板令牌
```

### 4.4 交付给调用方

在后台「令牌」页生成令牌（`sk-` 开头）发给调用方。对方侧零改动，直接用 OpenAI SDK：

```python
from openai import OpenAI
client = OpenAI(base_url="https://<你的中转>/v1", api_key="sk-你的令牌")
client.audio.speech.create(model="eleven_v3", voice="yun", input="你好").write_to_file("out.mp3")
```

调用方需要的完整说明见 [`USAGE_GUIDE.md`](USAGE_GUIDE.md) —— 直接把这份发给对方即可。

---

## 5. 日常运维

```bash
# 日志（看账本写入失败、上游报错都在这里）
docker logs -f --tail=100 elevenlabs-adapter

# 重启 / 停服
docker compose restart
docker compose down

# 升级到新版本（账本在宿主 ./logs，重建容器不丢）
git pull && docker compose up -d --build

# 换端口：改 .env 的 ADAPTER_PORT 后重建
docker compose up -d
```

**备份账本**（这是对账的唯一凭据，别丢）：

```bash
tar czf billing-$(date +%F).tar.gz logs/billing/
```

账本按天切分 `logs/billing/YYYY-MM-DD.jsonl`，默认保留 30 天（`ADAPTER_BILLING_LOG_DAYS`）。

查看累计统计：

```bash
curl -s "http://127.0.0.1:8123/v1/usage?days=30" | python3 -m json.tool
```

一条命令出三层对账报告：

```bash
python3 scripts/reconcile_billing.py --ledger-dir logs/billing --days 1
```

---

## 6. 出问题按现象查

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| `port is already allocated` | 8123 被占用 | `bash scripts/up.sh --port 9000` |
| 容器反复重启 | `.env` 语法错 / Key 含空格 | `docker logs elevenlabs-adapter` 看首行报错 |
| 健康检查一直不过 | 构建没完成就起了 | `docker compose logs --tail=50` |
| 面板「测试渠道」失败但能出音频 | 它用 chat 端点探测 | **正常现象**，用 §4.3 方式验证 |
| 404，路径有 `/v1/v1/` | Base URL 多写了 `/v1` | 只填到端口 |
| 401 `invalid api key` | 填成了面板令牌 `sk-` | 渠道里必须是上游 Key `sk_` |
| 401 且面板自动禁用渠道 | 上游 Key 失效 / 额度耗尽 | 换 Key 后重新启用渠道 |
| 404 `model_not_found` | 模型没写进渠道 | 后台补模型列表后重启面板 |
| 模型能列出、调用 404 | `abilities` 表缺行 | 自检第 9 项；重启面板重新同步 |
| 400「无法确定 voice_id」 | 用了标准引擎却没传 `voice` | 补 `voice`（如 `yun`） |
| 拿到的是静音文件 | `ELEVENLABS_MOCK=1` | 改 0 后重启；自检第 1 项会报警 |
| 扣费是预期的 2–5 倍 | `AudioRatio` 没归零 | 见 §4.2 |
| `/v1/usage` 一直是 0 | 账本目录不可写 | `sudo chown -R 10001:10001 logs` 后重启；`docker logs` 里会有 `billing log write failed` |
| 长文本 502 / 等很久 | 超时不够（约 33ms/字） | 调大 `ELEVENLABS_TIMEOUT`，或用异步任务接口 |
| 长文本失败但额度掉了 | 超时前上游已完成合成并计费 | 本项目超时**永不重试**；别调大 `ELEVENLABS_MAX_RETRIES` |
| 中文念错字 / 多音字 | 文本层问题 | 同音字替换或发音字典，与音色无关 |
| 打开页面但试听没反应 | `ADAPTER_DEMO_ENABLED=0` | 需要试听就开启，并配好限流 |

还查不到，带上这两样去提 issue：`docker logs --tail=200` 的输出，以及出问题那次请求的 `X-Request-Id`。

---

## 7. 完全清理

```bash
cd elevenlabs-adapter
docker compose down
cd .. && rm -rf elevenlabs-adapter     # 账本在 logs/ 里，需要留档的先备份
docker rmi elevenlabs-adapter:latest
```

上游 Key 若有泄露疑虑，去 ElevenLabs 控制台吊销重建。

---

## 下一步

- 请求字段、响应头完整列表 → [`USAGE_GUIDE.md`](USAGE_GUIDE.md)
- 中文场景包 / 台湾口音 / 自定义音色 → [README §6](README.md#6-模型目录标准引擎--补充包)
- 长文本异步任务 → [README §7](README.md#7-长文本异步任务接口)
- 自检与对账工具详解 → [`scripts/README.md`](scripts/README.md)
- 安全边界（能不能直接暴露公网）→ [`SECURITY.md`](SECURITY.md)

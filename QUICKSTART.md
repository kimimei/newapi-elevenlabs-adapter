# 五分钟接入

> 前提：Docker 20.10+ ｜ 一个 ElevenLabs Key（`sk_` 开头）｜ 服务器放行 8123 端口

## 一条命令

```bash
bash scripts/up.sh --key sk_你的ElevenLabsKey
```

它会依次做完 4 件事，全程无需手工编辑配置：

| 步骤 | 动作 |
|---|---|
| 1/4 | 从 `.env.example` 生成 `.env`，写入 Key 与端口 |
| 2/4 | `docker compose up -d --build` 起服务（首次 1–2 分钟） |
| 3/4 | 轮询容器健康检查，最多等 60 秒 |
| 4/4 | 拉 `/v1/models` 冒烟，有 `--db` 时顺带跑 12 项自检 |

> **国内网络首次构建慢？** 默认从 `pypi.org` 拉依赖，实测能卡十几分钟。加镜像源可降到 2 分钟内：
>
> ```bash
> PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple bash scripts/up.sh --key sk_你的Key
> ```

账本写在 `./logs/billing/`（按天切分 JSONL），已挂到宿主机，容器重建不丢，`scripts/reconcile.py` 可直接读。

起完直接打开 `http://<IP>:8123` —— 左边试听音色，右边生成可复制的配置（无需装任何东西）。
开启试听需在 `.env` 设 `ADAPTER_DEMO_ENABLED=1`（默认关闭，因为会公开消耗服务端 Key）。

> 还没部署也能用配置生成器：把 `static/index.html` 挂到任意静态托管，加参数
> `?api=https://你的中转/v1`；试听需要一台在线适配器，用 `?demo=` 指过去。

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

## 填进 new-api

| 字段 | 值 | 注意 |
|---|---|---|
| 类型 | **OpenAI** | new-api 没有 ElevenLabs 渠道类型 |
| Base URL | `http://<IP>:8123` | **只到端口**；多写 `/v1` 会 404 |
| 密钥 | ElevenLabs Key（`sk_`） | 不是 new-api 的令牌（`sk-`） |
| 模型 | 上表 5 个 | 要上架中文场景别名，加 `--presets cn,tw` |
| 分组 | default | — |

**若 new-api 跑在 Docker 里**：Base URL 用 `http://172.18.0.1:8123`（docker0 网关），不能用 `127.0.0.1`。

## 必做的两件事（否则会算错钱）

1. **`ModelRatio`**：`eleven_v4` / `eleven_v3` / `eleven_multilingual_v2` = **40**（$0.08/千字符），`eleven_flash_v2_5` / `eleven_turbo_v2_5` = **20**（$0.04/千字符）。
2. **`AudioRatio` 设 0**：音频伪 token 不能二次计价。缺省按 1 处理，实测会多收 **5 倍**。

## 验一遍（推荐，客户可自助）

```bash
python3 scripts/preflight.py \
  --adapter http://127.0.0.1:8123 \
  --key sk_你的Key \
  --db ./one-api.db \
  --model eleven_v3 --voice yun
```

12 项红绿，末行给「该改哪一行」。默认只读不花钱，加 `--live` 才做一次约 2 个字符的合成。

## 不想用 Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # 填 ELEVENLABS_API_KEY
python run.py             # 默认 127.0.0.1:8123
```

监听局域网需 `ADAPTER_HOST=0.0.0.0 python run.py`。

## 出问题先看这张表

| 现象 | 原因 | 处理 |
|---|---|---|
| `port is already allocated` | 8123 被占用 | `bash scripts/up.sh --port 9000` |
| 调用返回 **401** | Key 写错：填成了 `sk-` 中转令牌 | 渠道里必须填 `sk_` 开关的上游 Key |
| 调用返回 **404 model_not_found** | 模型没加进渠道 | 后台补模型列表后重启 new-api |
| 模型能列出、调用 404 | `abilities` 缺行 | 见自检第 9 项 |
| 面板「测试渠道」失败 | 它用 chat 端点探测，TTS 渠道必然失败 | **正常现象**，用 `/v1/audio/speech` 验证 |
| 拿到的是静音文件 | `ELEVENLABS_MOCK=1` | 改成 0 后重启；自检第 1 项会报警 |
| 调用成功但扣费异常 | `AudioRatio` 没归零 / 倍率填错 | 跑自检，看第 10–12 项 |
| `/v1/usage` 一直是 0，但调用成功 | 账本目录不可写（容器内 uid 10001，宿主 `logs/` 属主不对） | `sudo chown -R 10001:10001 logs` 后重启；`docker logs` 里会有 `billing log write failed` |
| 毛利率是 `null` | 没配单价，按设计不给错数 | 在 `.env` 填 `ADAPTER_UP_CREDITS_PER_USD` / `ADAPTER_CHARS_PER_USD` |

完整参数与错误码见 [`README.md`](README.md)，自检说明见 [`scripts/README.md`](scripts/README.md)。

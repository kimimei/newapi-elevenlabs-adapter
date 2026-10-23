# 安全说明

> 版本：0.3（2026-10-20）｜ 适用范围：本仓库全部代码与默认部署方式

## 一、这个组件是什么、不是什么

**是**：跑在你内网 / 容器网络里的协议转换层，替你把 ElevenLabs 包装成 OpenAI 接口。

**不是**：一个可以直接暴露到公网的 API 网关。它**没有内建用户体系、没有鉴权、没有配额**——
按设计，鉴权与计费由上游的中转站（new-api / one-api）负责。

## 二、部署边界（必读）

| 要求 | 原因 | 做法 |
|---|---|---|
| **不要把 8123 直接暴露公网** | 任何能访问该端口的人都能消耗你配置的 Key | 只监听 `127.0.0.1`；或让 new-api 走容器网络访问 |
| 必须对外时，加来源白名单 | 同上 | `ADAPTER_ALLOW_IPS=172.18.0.1,10.0.0.0/8` 的精确 IP 列表 |
| 容器不要用 root | 降低逃逸后的影响面 | 镜像已默认 `USER adapter`（uid 10001） |
| Key 只放在渠道配置里 | 适配器不需要知道你的中转令牌 | 渠道密钥填 ElevenLabs Key；适配器侧 `.env` 可留空 |
| 长文本分段 | 单次请求有 2 MiB 体积上限 | 加 `ADAPTER_MAX_CHARS` 限制业务侧长度 |

## 三、已修复的问题（有回归测试）

| 编号 | 问题 | 影响 | 回归用例 |
|---|---|---|---|
| S1 | `voice_id` 未校验就拼进上游 URL 路径 | 构造含 `/`、`..` 的 voice_id 可把请求指到 `api.elevenlabs.io` 的其它路径，且用的是服务端 Key（路径穿越 / 越权调用） | `tests/test_security.py::test_voice_id_with_slash_rejected` |
| S2 | 无内建鉴权，默认监听 `0.0.0.0` | 任何能访问该端口的人都能消耗你配置的 Key | 设计如此（鉴权在 new-api）；已加 `ADAPTER_ALLOW_IPS` 白名单（`/healthz` 豁免，便于配错时自查） |
| S3 | 请求体无上限 | 单个超大 POST 即可打满内存（FastAPI 不做限制） | `test_oversize_body_returns_413`；后续又补了 chunked 绕过（见 S7） |
| S4 | 无 Key 时回落到 `~/.elevenlabs/config.json` 明文 Key | 共享主机上，任何调用方都能白嫖运营者的个人 Key | `test_local_key_fallback_disabled_by_default` |
| S5 | 容器以 root 运行 | 逃逸后影响面放大 | —（构建期修复） |
| S6 | 依赖只写下界、无上界 | 大版本升级时静默装到不兼容版本 | —（构建期修复） |
| S7 | 体积上限只查 `Content-Length` | 客户端用 `Transfer-Encoding: chunked` 发送时压根不带该头，检查直接跳过，超大请求照样进到解析里打满内存 | `tests/test_limits.py::test_chunked_body_also_blocked` |
| S8 | **`uvicorn[standard]` 的 `proxy_headers` 默认 `True`，会用 `X-Forwarded-For` 覆盖 `request.client.host`** | `ADAPTER_ALLOW_IPS` 被彻底绕过：任何能连到适配器的人，只要带一个 `X-Forwarded-For: <白名单里的IP>` 头就能进来。实测确认（`scripts/check_proxy_ip.py` 第④例，修复前会打印"危险"） | `run.py` 显式 `proxy_headers=False`；`tests/test_limits.py::test_run_py_disables_proxy_headers` + `test_forwarded_header_cannot_bypass` |

S8 值得单独说明：它的危险之处在于**看不见**。适配器自己的代码里一处XFF 都没读，是框架层在进应用之前就把 `request.client.host` 换掉了。审计代码看不出来，只有实测能发现——这也是 `scripts/check_proxy_ip.py` 存在的理由。

需要「按真实客户端 IP 限流」时，正确做法是在反代层（nginx `limit_req` / `allow` / `deny`）做，而不是让适配器信任一个可伪造的头。

修复策略是**默认安全**：S4 由默认开启改为默认关闭（需要时 `ADAPTER_ALLOW_LOCAL_KEY=1`），
S3 给出 2 MiB 默认上限（`ADAPTER_MAX_BODY_BYTES`），S1 只允许 `[A-Za-z0-9_-]{10,64}`。

## 四、已知限制（不是漏洞，但要清楚）

| 限制 | 说明 | 缓解 |
|---|---|---|
| 无内建鉴权 | 设计如此，鉴权在 new-api | 网络边界 + `ADAPTER_ALLOW_IPS` |
| `ADAPTER_ALLOW_IPS` 填的是 TCP 对端 | 反代转发时看到的是**反代的地址**，不是真实客户端 IP。这是刻意的（信任 XFF 就能被伪造绕过，见 S8） | `python scripts/check_proxy_ip.py` 实测确认该填谁 |
| 无按调用方限流 | 单一进程、单一 Key 池 | 在 new-api 侧做令牌限速；或部署多实例 |
| 异步任务表在进程内存 | 重启丢排队中的任务；多副本需sticky session | 见 README §2.4「三条必须知道的限制」 |
| 重试会重复计费 | TTS 按字符计费，5xx 重试成功后上游计两次 | `ELEVENLABS_MAX_RETRIES` 默认 2，可设 0 |
| `ELEVENLABS_MOCK=1` 返回静音 | 看起来「调用成功」，实际没有声音。响应头带 `X-Mock: true` 与 `X-Mock-Format: wav` | 自检第 1 项会告警；上线前务必确认 `0` |
| 错误日志含上游响应体 | 最长 500 字符，可能包含被拒的输入片段 | 生产环境收紧日志级别 |
| 依赖用范围锁 | `>=x,<y` 而非精确版本 | 发布镜像时打 tag；CI 固定 lock |

## 五、报告漏洞

请**不要**开公开 issue。发送到：`m1501816@163.com`，
或在 GitHub 上用 **Security → Report a vulnerability** 私密报告。

请附：复现步骤、影响范围、你的环境（版本 / 部署方式）。48 小时内回复。

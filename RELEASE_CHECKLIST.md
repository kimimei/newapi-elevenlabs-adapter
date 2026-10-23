# 发布前清单（GitHub 开源）

> 日期：2026-10-20｜适用版本：v0.4.0（版本号以 `app/main.py` 的 `FastAPI(version=...)` 为准）｜仓库：`elevenlabs-adapter`

## 一、当前状态

| 项 | 状态 |
|---|---|
| 自动化门禁 | ✅ `scripts/release_check.py` 已建，9 条规则，含反证自测 |
| 唯一阻塞项 | ✅ 已解除（邮箱已填，门禁零阻塞） |
| LICENSE 版权主体 | ✅ 已改为 `Copyright 2026 KamyMei` |
| 漏洞报告邮箱 | ✅ 已填 `m1501816@163.com` |
| 真实 IP / 密钥 / 测试音频 | ✅ 全仓扫描无残留（门禁可复现） |
| 回归测试 | ✅ 78 passed |
| 文档 | ✅ README / QUICKSTART / SECURITY / CLIENT_ONBOARDING / USAGE_GUIDE / scripts/README |

---

## 二、两个"人工事"怎么做

### 2.1 改 LICENSE 版权主体 —— 已完成

**为什么必须改**：原文件写的是 `Copyright 2026 ElevenLabs Adapter contributors`。
`ElevenLabs` 是第三方公司名，把版权挂给它会造成"这是 ElevenLabs 官方项目"的误认——
对一个要卖 ElevenLabs 额度的中转服务来说，这是实打实的法律与商业风险。

**改法**：只改 `LICENSE` 末尾 APPENDIX 里的那一行，**正文一个字都不动**（Apache-2.0 正文是标准文本，改了就失去标准许可证身份）。

```diff
-   Copyright 2026 ElevenLabs Adapter contributors
+   Copyright 2026 KamyMei
```

已改。README 末尾的 License 段也同步补了主体说明。

**注意**：
- 主体写个人（KamyMei）时，Apache-2.0 的 §4 依然要求分发时附带 LICENSE 副本 —— 仓库根目录已有，别删。
- 如果以后要换公司主体，**只改这一行**即可，无需重写许可证。
- `NOTICE` 文件是可选的（只有当你想额外署名时才需要），当前不建。

### 2.2 填 SECURITY.md 邮箱 —— 已完成

**为什么必须改**：原来填的是占位邮箱。原样发布等于告诉所有人
"这个项目没人管漏洞"，企业客户做安全评估时会直接扣分。

**已改**（`SECURITY.md` 第五节）：

```diff
> - 请**不要**开公开 issue。发送到：<旧占位邮箱>（替换为你自己的邮箱），
> + 请**不要**开公开 issue。发送到：`m1501816@163.com`，
```

**怎么验证改对了**：跑门禁，占位符会被拦下，替换后自动放行：

```bash
python3 scripts/release_check.py
```

> - 改之前 → `[BLOCK] 漏洞报告邮箱 SECURITY.md 邮箱 ... 是占位符`
- 改之后 → 该条 BLOCK 消失（当前状态：**阻塞 0 项**，exit 0）
- 想确认门禁本身没坏 → `python3 scripts/release_check.py --self-test`（会故意放一个违规文件，5 类违规必须全部被捕获，同时正向验证正常邮箱放行）

**已保留的备份通道**：SECURITY.md 里同时保留 GitHub 私密报告入口。
建议再在 GitHub 仓库设置里把 **Private vulnerability reporting** 打开 ——
企业安全流程往往只认"有正式报告渠道"，两条路并行最稳。

---

## 三、发布前必须跑的命令（按顺序）

```bash
cd elevenlabs-adapter

# 1. 回归测试
python -m pytest tests -q                       # 期望：86 passed

# 2. 发布门禁
python3 scripts/release_check.py --self-test    # 期望：反证通过 + 正向验证放行
python3 scripts/release_check.py                # 期望：无 BLOCK（当前 exit 0）

# 3. 密钥历史复查（首次提交后也要再跑一次）
git log -p --all | grep -iE "sk[-_][A-Za-z0-9]{16,}"   # 期望：无输出
```

**顺序有讲究**：先跑门禁再提交。反过来（先提交再修）会留下一条含占位符的历史提交。

---

## 四、剩余待办（按优先级）

### 已完成 · 代码逻辑复查（v0.3.0）

复查 `app/` 全部模块 + 部署文件，逐条写探针实测而非推理。确认属实并已修的 5 项：

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 1 | 别名分支不校验 `voice_id` | `voices.json` 里写进带斜杠的 voice_id 即可绕过 `body.voice` 的正则（探针实测 `../../evil` 可穿透） | 抽 `_check_voice_id()`，别名分支复用 |
| 2 | `DEMO_VOICES` 解析为空时 `DEMO_VOICES[0]` IndexError | `ADAPTER_DEMO_VOICES=","` →试听端点 500（实测） | 回落默认音色 + 启动告警 |
| 3 | demo 五处手拼 JSON 字面量 | 报错文案带引号/反斜杠即产出非法 JSON | 统一走 `_err()`（`json.dumps`） |
| 4 | 限流表 `_hits` 只增不减 | 扫一遍来源 IP 就能把内存撑起来 | 超1024 条时清理过期 IP |
| 5 | `pytest` 打进生产镜像 | 镜像多带一个测试框架 | 拆出 `requirements-dev.txt` |

另有2 项修正：MOCK 模式恒返回 WAV 却不说（新增 `X-Mock-Format` / `X-Mock-Format-Mismatch` 头，避免下游误判成格式转换 bug）；`version` 与发布清单声明漂移（统一 v0.3.0，并加测试锁住）。

每条修复都做了**反证**：改回旧写法确认测试变红。其中两条第一版测试是假阳性（只断言常量非空 / 只测函数存在），重写成真正端到端后才算数 —— 详见「门禁不可自证只能反证」。

### P0 · 发布前最后一步

| # | 事项 | 说明 |
|---|---|---|
| 1 | ~~填 SECURITY.md 邮箱~~ | ✅ 已完成，门禁零阻塞 |
| 2 | 首次 git 提交 | 门禁已覆盖；提交后再跑一次 `git log -p` 历史扫描 |
| 3 | GitHub 仓库设置打开 Private vulnerability reporting | 与邮箱并行，作为第二条报告通道 |

### P1 · 影响成交，但不影响发布

| # | 事项 | 状态 |
|---|---|---|
| 3 | **T5 长文本异步任务** | ✅ 已完成。新增 `app/jobs.py`：提交 / 轮询 / 下载 / 取消，默认关闭（`ADAPTER_JOBS_ENABLED=1`）。16 条回归测试 |
| 4 | **并发压测 + chunked 绕过** | ✅ 已完成。chunked 按实收字节计数（S7）；新增并发闸门 `ADAPTER_MAX_CONCURRENCY`；`tests/test_limits.py` 15 条 |
| 5 | **真实上游长文本回归** | ✅ 已完成（2026-10-20 实测）。**结论：`ELEVENLABS_TIMEOUT=120s` 撑不住 2500 字以上，必须调大**。详见下方「长文本实测数据」 |

### 长文本实测数据（2026-10-20，真实 Key / 真实上游 / 香港服务器）
**上游吞吐率：约 33ms/字**（ElevenLabs `eleven_v3` + `zh-boyin` 音色，实测线性）

| 路径 | 字数 | 耗时 | 结果 | 说明 |
|---|---|---|---|---|
| 直连上游 | 1000 | **35.25s** | ✅ 200 / 3.9MB | 基准吞吐 28 字/秒 |
| 直连上游 | 3000 | **105.08s** | ✅ 200 / 11.7MB | 线性外推成立 |
| new-api → 适配器 | 300 | 24.36s | ✅ 200 / 1.3MB | 固定开销约 15s |
| new-api → 适配器 | 906 | 70.48s | ✅ 200 / 3.8MB | 计费头 `X-Usage-Input-Characters: 906` 正确 |
| new-api → 适配器 | **2000** | **98.34s** | ✅ 200 / 9.1MB | **临界点：120s 内能过的最大值** |
| new-api → 适配器 | 2500 | >120s | ❌ 502 | 适配器超时 ×3 重试（耗时约 6 分钟才报错） |
| new-api → 适配器 | 3000 | >120s | ❌ 502 | 同上 |

**三条结论（均已通过上游 `/v1/history` 逐笔核实）：**

1. **`ELEVENLABS_TIMEOUT` 默认 120s 太小**。按 33ms/字 + 15s 固定开销算，120s 只能覆盖约 **2000 字**；`ADAPTER_JOBS_MAX_CHARS=20000` 这个上限**永远不可能在一次上游请求内完成**（需 660s）。v0.4.0 已把默认超时改为 **300s**、`JOBS_MAX_CHARS` 改为 **8000**（两者匹配）。
2. **超时重试是最大的烧钱机器**。默认 `MAX_RETRIES=3` + 120s 超时 = 单个长请求要 **6 分钟才返回 502**。v0.4.0 已改为**超时一律不重试**（上游已完成合成，重试=再扣一次），仅 5xx / 429 / 连接中断重试，且默认次数降为 **0**。
3. **失败请求确实扣费，且是多次**。上游是「合成完才返回」，客户端超时只代表自己不等了。实测今晚失败请求扣费 **13,727 字符**，占总消耗 **87%**。

**上游计费单位不是「字符」**：`character-cost = floor(len(text) / 2)`，中英文一致。适配器原来用 `len(input)` 回传，比上游多报一倍。v0.4.0 已改为直接读上游 `character-cost` 响应头，并新增 `X-Usage-Source` / `X-Usage-Local-Chars` 便于对账。

**本次测试实际消耗（上游 `/v1/history` 精确值）：15,826 字符。**
其中成功请求 1,543（10%）、超时重试白烧 13,727（87%）、短文本验证 556（3%）。

**新增工具：`scripts/reconcile_billing.py`** —— 对比上游真实扣费与 new-api 记账，两个数据源都不花钱，能查出三类问题（记账 2 倍 / 记账远低 / 有记账上游无）。

| 6 | **依赖 CVE 扫描** | ✅ 已完成。`pip-audit -r requirements.txt` → **No known vulnerabilities found** |
| 7 | **反代 IP 实测** | ✅ 已完成。**抓到一个真实漏洞（S8）**：`uvicorn[standard]` 的 `proxy_headers` 默认 `True`，会用 XFF 覆盖 `request.client.host`，导致 `ADAPTER_ALLOW_IPS` 可被一个伪造头绕过。已在 `run.py` 显式关闭并加回归测试 |

### 顺带修掉的两个真问题

| 问题 | 说明 |
|---|---|
| 非 `httpx.HTTPError` 的上游异常直接 500 | DNS 失败 / 连接被拒这类异常没有 `error.fix`，客户侧只看到「未知错误」，排查成本全压在支持邮件里。现统一转 502 + 可执行 fix |
| `/healthz` 也被 IP 白名单拦 | 白名单配错时唯一的自查手段就是 `/healthz`，把它也拦住等于让人瞎填。已豁免 |

反代 / 白名单的实测结论已写进 `.env.example` 与 README：白名单填的是**TCP 对端地址**（反代的地址），不是真实客户端 IP；拿不准跑 `python scripts/check_proxy_ip.py`。

### P2 · 可延后

| # | 事项 | 说明 |
|---|---|---|
| 8 | T8 并发/限流可见 | 429 响应体带"当前并发 X / 上限 Y"；`/healthz` 暴露队列深度 |
| 9 | T7 用量回执与月报 | 响应头已有 `X-Usage-Input-Characters`；缺 `X-Estimated-Cost` 与月度 CSV |
| 10 | T11 幂等键 | 带 `Idempotency-Key` 的重试不重复计费 |
| 11 | T9 流式 SSE | `stream=true` 返回 501；只对对话/Agent 类客户做 |
| 12 | T12 白标/多租户 | 客户换 Logo/域名、key 池隔离 |

**T10 中文预处理**：不重做。复用 `~/.workbuddy/skills/elevenlabs-tts/scripts/check_numbers.py`。
接入时的硬约束是**汉字化会改变字符数**（`120%` → `百分之一百二十`，2 字变 7 字），
而计费按输入字符数 —— 自动 apply 必须按改写后长度计费，否则差额自己吃。
建议形态 `ADAPTER_PREPROCESS=off|report|apply`，**默认 report**。

---

## 五、docs/sample-report.html 的处置：留

**结论：留在 `docs/`，不归档。**

理由：

| 维度 | 判断 |
|---|---|
| 敏感性 | ✅ 已扫描：无 IP、无密钥、无 URL、无内部域名。渠道名 `#1 elevenlabs-adapter` 只是本仓库自己的渠道名，不是客户信息 |
| 引用关系 | README 文档索引里直接链它。删了链接就断 |
| 销售价值 | ✅ 它是 D3 交付物本身 —— "计费可复算"是这方案的核心卖点，报告是唯一能证明的实物 |
| 体量 | 4.3 KB，纯静态无依赖 |

**但顺手修了它两个真问题**（原报告在替它没验过的东西背书）：

1. 复盘模式拿不到响应头，第②段（回传字符数）是空的，报告却写"**四段**一致"。
   → 改为"三段一致（未含回传字符数）"，并在结论里注明需实测模式才能验全四段。
2. footer 的复现命令写死了本地路径 `../newapi-verify/_tmp_one-api.db`（会泄露目录结构）
   → 改为 `<your-new-api-db>` 占位。
3. footer 声称"数据取自 new-api 日志与响应头"，但复盘模式没有响应头 → 改为按模式区分。

这些修复落在 `scripts/reconcile.py` 的模板里，**不是手改 HTML** —— 重新生成会覆盖回去。

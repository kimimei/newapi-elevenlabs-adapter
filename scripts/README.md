# scripts/ —— 接入自检

一条命令回答两件事：**接对了吗？会不会算错钱？**

```
python3 scripts/preflight.py --adapter http://127.0.0.1:8123 \
    --key sk_xxx --db ./one-api.db \
    --model eleven_v3 --voice yun
```

输出 12 行红绿，末行给结论与「该改哪一行」。

```
 [✓]  1  适配器健康         status=ok
 [✓]  2  上游 Key 有效      上游可用模型 12 个（未消耗字符）
 [✓]  3  模型目录非空       34 个（含未启用包）
 [✓]  4  目标模型在目录     eleven_v3
 [✓]  5  音色可解析         yun（目录内）
 [–]  6  音频可解码         未加 --live（默认不产生合成费用）
 [–]  7  字符数回传         未加 --live
 [✓]  8  渠道已注册该模型   渠道 #1 elevenlabs-adapter，共 34 个模型
 [✓]  9  abilities 行存在   default(enabled=1)
 [✓] 10  ModelRatio 已配    eleven_v3 = 40（隐含 $0.0800/千字符）
 [✓] 11  AudioRatio = 0     已归零
 [✓] 12  计费对账           最近一条日志 #28：23 字符 × 50 = 1150 quota，实际 1150 ✓
```

## 为什么不是"三条 curl"

`curl` 只能证明「能出声」；这 12 项证明的是「**不会算错钱**」。其中第 11 项是唯一
「配错看不出来、但会持续多收钱」的检查——`AudioRatio` 缺省按 1 处理，音频伪 token
会被二次计价，实测多收过 5 倍。

## 依赖

只用标准库，Python 3.6+ 直接跑，不需要 `pip install`。
脚本以**只读**方式打开 SQLite（`mode=ro`），不会改任何配置。

## 三种用法

```bash
# 1) 默认：只读，不花钱（推荐给客户自助）
python3 scripts/preflight.py --adapter http://127.0.0.1:8123 \
    --key sk_xxx --db ./one-api.db --model eleven_v3 --voice yun

# 2) 实调：多加一次约 2 个字符的合成，验「音频可解码 + 字符回传」
python3 scripts/preflight.py ... --live

# 3) 端到端对账：经 new-api 调一次，核对日志 quota 是否等于 字符 × 倍率
python3 scripts/preflight.py ... --live \
    --newapi http://127.0.0.1:8300 --token sk-你的中转令牌
```

其它开关：

| 开关 | 用途 |
|---|---|
| `--json` | 机读输出，给 CI / 监控用 |
| `--skip-upstream` | 服务器不出网时跳过上游 Key 校验 |
| `--no-color` | 关掉 ANSI 颜色（写日志时用） |

退出码：`0` 全绿 / `1` 有失败 / `2` 参数或环境错误。

## 12 项对应关系

| # | 检查 | 失败意味着 |
|---|---|---|
| 1 | 适配器 `/healthz` | 服务没起，或 MOCK 模式在跑（会返回静音音频） |
| 2 | 上游 Key | Key 失效/带空格；不出网时用 `--skip-upstream` |
| 3 | `/v1/models` 非空 | 目录为空，或适配器版本过老 |
| 4 | 模型在目录 | 拼错，或别名没加进 `voices.json` |
| 5 | 音色可解析 | 音色名不在表里，也不是合法 voice_id |
| 6 | 音频可解码 `[--live]` | 返回了 JSON 错误体却被当成音频 |
| 7 | 字符数回传 `[--live]` | 没有 `X-Usage-Input-Characters`，计费只能按 tokenizer 估 |
| 8 | 渠道已注册 | `channels.models` 缺这一项 |
| 9 | abilities 行 | 有模型名但无路由，调用必 404 |
| 10 | ModelRatio | 未配倍率 → 按默认倍率计费或直接报错 |
| 11 | **AudioRatio = 0** | **音频伪 token 被二次计价，持续多收钱** |
| 12 | 计费对账 | 日志 quota ≠ 字符 × 倍率，账单不可复算 |

## 自测

```bash
python3 scripts/selftest.py     # 起 mock 适配器 + 自建最小 DB，5 个场景
```

覆盖：离线只读 / `--live` / `--json` / 全错 / 计费陷阱（`AudioRatio=1` + abilities 缺行）。

## 常见问题

| 现象 | 原因 |
|---|---|
| 8/9 项全 SKIP | `--db` 路径不对，或 new-api 用 MySQL（本脚本只支持 SQLite） |
| 12 项 WARN「还没有调用日志」 | 该模型从未被调用；跑一次 `--live` 或等客户调用 |
| 2 项 SKIP「网络不可达」 | 服务器访问不了 `api.elevenlabs.io`，不影响其余检查 |
| 11 项显示「约 X 倍」 | 用近期日志的 completion/prompt 估算，仅作量级参考 |


---

# scripts/reconcile.py —— 一页对数报告

自检回答「配得对不对」，对数报告回答「钱算得对不对」，并且是**能直接发给客户的一页纸**。

```bash
# 复盘模式：只用历史日志，不调用、不花钱
python3 scripts/reconcile.py --db ./one-api.db \
    --model eleven_v3 --from-logs --out report.html

# 实测模式：经 new-api 发 3 次采样（默认 10/30/100 字符，约 $0.011）
python3 scripts/reconcile.py --db ./one-api.db \
    --newapi http://127.0.0.1:8300 --token sk-xxx \
    --model eleven_v3 --voice yun --out report.html
```

输出 `report.html`（A4 打印样式，浏览器「另存为 PDF」即可发客户）+ 同名 `report.json`。

报告含四段数字：**发送字符 → 回传字符 → 日志 quota → 折算美元**，逐笔列出，
任一段不一致就判定失败并指出断点；`AudioRatio` 非 0 时额外给出「约多收几倍」的风险提示。

退出码：`0` 一致 / `1` 有偏差。

自测：`python3 scripts/selftest_reconcile.py`（复盘 + 实测 + 偏差 三个场景）

---

## release_check.py —— 发布前门禁

开源前跑一次，把"人工事"（改版权主体、填安全邮箱）变成可自证的检查。

```bash
python3 scripts/release_check.py            # 人类可读报告；exit 1 = 有阻塞项
python3 scripts/release_check.py --json     # 机器可读
python3 scripts/release_check.py --self-test # 规则反证：故意放违规文件，必须被捕获
```

9 条规则：

| 规则 | 级别 | 查什么 |
|---|---|---|
| LICENSE 版权主体 | BLOCK | 署名不能是 `contributors` 这类模糊写法，也不能含 `ElevenLabs`（第三方公司名，会造成误认） |
| 漏洞报告邮箱 | BLOCK | SECURITY.md 里的邮箱不能是占位符 |
| 真实 IP 泄露 | BLOCK | 白名单外的任何 IPv4 |
| 密钥泄露 | BLOCK | 形似 `sk-` + 16 位以上的令牌 |
| 测试音频入仓 | BLOCK | 仓库里的 .mp3 / .wav |
| .gitignore 覆盖 | BLOCK | 必须忽略 .env / __pycache__ / *.mp3 / *.wav |
| git 历史密钥 | BLOCK | `git log -p` 里扫令牌（删文件不等于删历史） |
| 未清理的占位符 | WARN | 待办标记 / 未替换的域名邮箱（引用块里的提示语不算） |
| 私密报告通道 | WARN | SECURITY.md 应提 GitHub 私密报告入口 |
| 文档缺失 | WARN | README / SECURITY / LICENSE / QUICKSTART |

**设计原则**：门禁不可自证只能反证，所以 `--self-test` 会往临时目录塞一个
含占位邮箱、假 IP、假密钥、假音频的仓库，验证 5 类规则确实都会报警；
同时做正向验证 —— 换成正常邮箱后必须放行（不能只会拦不会放）。

---

## check_proxy_ip.py —— 反代 / IP 白名单实测

`ADAPTER_ALLOW_IPS` 该填什么，是个纯靠猜就会填错的问题：填真实客户端 IP → 全部 403；
填反代地址 → 白名单形同虚设。这个脚本起真进程、真发请求，把行为打出来。

```bash
python3 scripts/check_proxy_ip.py     # 四个场景，约 20 秒
```

| # | 场景 | 期望 |
|---|---|---|
| ① | 无白名单 | 放行 |
| ② | 白名单 = 127.0.0.1（本机直连，等价同机反代） | 放行 |
| ③ | 白名单 = 别的地址 | 403，且报错里能看到适配器认定的来源 IP |
| ④ | 白名单 = 别的地址 + 伪造 `X-Forwarded-For` | **403** —— 伪造无效 |

第④ 例是重点。它抓到过一个真实漏洞：`uvicorn[standard]` 的 `proxy_headers`
默认 `True`，会在进应用之前用 XFF 覆盖 `request.client.host`，于是白名单形同虚设。
修法是 `run.py` 显式 `proxy_headers=False`，并有回归测试锁住。

**造用例的坑**：白名单里必须放一个「本机一定连不上」的地址（如 `203.0.113.9`）。
早先一版填了 `127.0.0.1`，结果无 XFF 也放行，测不出任何东西 ——
造用例时得先确认对照组会失败。

---

## revert_check.py —— 反证自测

改完安全 / 并发 / 计费这类边界后跑一次：脚本把关键实现逐处改回旧写法，
每次跑全量测试，**必须看到变红**才算这条改动有测试兜住。

```bash
python3 scripts/revert_check.py     # 12 处；退出码非0 = 有改动没被抓住
```

| 为什么需要 |
|---|
| 测试全绿不代表测到位。断言中间变量的话，把实现换成 `pass` 照样全绿 |
| 判定依据是 pytest **退出码**，不是输出里有没有 "failed" 字样 —— 第一版用关键词匹配，被 `-x` 提前退出骗过 |
| 脚本收尾会再跑一次全量，确认文件已完整还原 |

覆盖：队列满 429 / 任务置成功 / 过期清理 / 计费回传字符数 / 未完成不给下载 /
worker 并发闸门 / chunked body 回填 / 并发计数与归还 / healthz 豁免白名单 /
关闭 proxy_headers / 非 httpx 异常转 502。

---

## reconcile_billing.py —— 三层对账

`preflight.py` 回答「接对了吗」，这个回答「**跑了一段时间后，钱对得上吗**」。

```bash
# 今天
python3 scripts/reconcile_billing.py --ledger-dir logs/billing --days 1

# 近 3 天
python3 scripts/reconcile_billing.py --ledger-dir logs/billing --days 3

# 单个文件 + 指定上游 Key 做 L2
python3 scripts/reconcile_billing.py --ledger logs/billing/2026-10-21.jsonl --key sk_xxx

# 只看账本字段说明
python3 scripts/reconcile_billing.py --help-ledger
```

**为什么不能只靠时间戳配对**：并发请求时间重叠，失败请求压根没有 new-api 日志，
而失败恰恰是最烧钱的部分。所以适配器自己写账本（`app/billing.py`），
每行带 `rid` / `up_rid`，配对靠 ID 不靠猜。

**三层各解决一个问题：**

| 层 | 对比 | 能发现 |
|---|---|---|
| **L1** | 账本 `bill_chars` vs new-api `logs.prompt_tokens` | 中转站漏记 / 重复记账 |
| **L2** | 账本 `up_credits` vs 上游 `/v1/history` | 上游扣费异常 / Key 被挪用 / 有别的服务共用 Key |
| **L3** | `up_credits` 折算 USD vs `bill_chars` 折算 USD | 上游涨价、定价倒挂 |

> L3 需要你在 `.env` 填 `ADAPTER_UP_CREDITS_PER_USD` / `ADAPTER_CHARS_PER_USD` 才会计算；
> 不填就返回 `null`。**宁可不给，也不给一个错的** —— 这两个数一个是上游成本单价、
> 一个是你的计费单价，量纲不同，未配置时无法换算。

**典型输出：**

```
【账本汇总】
  请求      : 3（成功 2 / 失败 1）
  计费字符  : 6000
  上游积分  : 1443

【失败请求】1 笔，计费字符 0（失败不收费）
  上游积分未回传（超时类请求通常拿不到 character-cost）

【L1】账本 ↔ new-api 记账
  账本计费字符 : 6000    new-api 记账 : 6000    差额 +0    结论 ✅ 对平

【L3】单位成本比
  单位成本比 : 0.481 积分/字符
  收入成本比 : null（未配置 ADAPTER_*_PER_USD）
```

**账本字段：**

```json
{"ts":1761030000,"rid":"a1b2c3","up_rid":"fIeaFF","model":"zh-boyin","voice":"yun",
 "in_chars":1000,"bill_chars":1000,"bill_source":"chars","up_credits":480,
 "status":200,"ms":7021,"mode":"chars","ip":"203.0.113.9","token":"QnSxCN…"}
```

| 现象 | 含义 | 处置 |
|---|---|---|
| L1 记 > 账本 | 有请求绕过适配器，或重复记账 | 查渠道配置 |
| L1 记 < 账本 | 成功请求没被记账 | 查 new-api 渠道是否禁用 |
| L2 上游 > 账本 | Key 被别的服务共用，或超时请求上游仍扣费 | 换独立 Key；`ELEVENLABS_MAX_RETRIES=0` |
| 账本里大量 502 | 超时白扣钱 | 调大 `ELEVENLABS_TIMEOUT` |
| `up_credits` 大量为 null | `character-cost` 头被代理剥离 | 检查中间代理 |

> ⚠️ 上游 `character-cost` 实测约为字符数的 **0.48**（中英文一致，标点另算），
> 且与账户页面「已用积分」共用同一计数器。是**积分**，不是字符数。
> 计费口径由 `ADAPTER_BILLING_MODE` 控制，默认按字符（对齐官网价）。


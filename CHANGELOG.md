# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复

- **`lin` 音色已失效，表现为 403 而非静默降级**：`lin`（`UFDAUkGzdLAEJlINT3Fx`）是 Voice Library 的副本（`is_owner=false`），原作者于 2026-09-09 10:58 UTC 关闭共享（`sharing.status=copied_disabled`），此后真合成一律返 `403 voice_access_denied`。该音色在 `/v1/voices` 里仍正常列出，所以「看得见 ≠ 能用」——这段静默期靠枚举接口完全发现不了，只有真合成 1 个字符才能验出来。2026-10-26 实测三条恢复路径全部失败：重新 `add` 返 `400 Sharing is not enabled`、原始样本已被服务端删除（`does_not_exist`）、公共库索引残留但共享已关。现将 `lin` 映射到 `xinghe`（`lt7GBaCoAHWbT7JSZ5Xs`）——**名字保留只为不打断存量调用，实际声音已是 xinghe**。

### 变更

- 以下三处同步指向 `xinghe`：`voices.lin`、`packs.cn.models["zh-duihua"]`、`packs.cn.models["zh-对话"]`。映射横跨三处，漏改任一处该通路会单独失效，且报错形态像上游故障（403）而不像配置错误。
- 新增 `_deprecated_20261026` 段留存旧 ID 与失效溯源，仅供核对历史订单的音色归属，**不要用于合成**。加载器（`app/voices.py`）只读 `voices` / `engines` / `packs` 三个键，该未知顶层键不会被解析成别名——已实测其不出现在 `Voices.aliases` 与 `Voices.voices` 中。
- 改后与 `elevenlabs-adapter-go` 的 `internal/voices/voices.json` 逐字节一致，两个实现重新对齐（Go 侧已有 `catalog_test.go` 钉住该映射，Python 侧暂无等价用例）。

## [0.4.0] - 2026-10-23

首个公开版本。

### 能力

- **OpenAI 兼容协议**：`POST /v1/audio/speech`、`GET /v1/models`，支持 `Authorization: Bearer` 与请求头透传两种鉴权
- **计费对齐**：回传 `X-Usage-Input-Characters` / `X-Usage-Sent-Characters`，与中转站的 `prompt_tokens` 口径逐笔相等
- **三种计费口径**：`input`（默认，对齐网关）/ `chars`（清理后长度）/ `upstream`（上游积分）
- **三层对账**：账本 ↔ 网关日志（L1）、账本 ↔ 上游 `/v1/history`（L2）、单位成本与收入折算（L3）
- **长文本异步任务**：`/v1/audio/speech/jobs`，202 + 轮询 + 下载，规避网关超时
- **中文场景包**：播音 / 旁白 / 有声书 / 商务 / 对话 / 亲切 / 播客 / 影视，含台湾口音双音色
- **文本归一化**：零宽字符、多余空白清理，清理量单独记账，不影响计费基数
- **自检工具链**：`scripts/preflight.py`（12 项接入体检）、`scripts/reconcile.py`（HTML 对数报告）、`scripts/release_check.py`（发布门禁）
- **可直接运行的示例**：`examples/` 下 Python（标准库 / 官方 SDK）、Node、curl 三套脚本，含长稿分段与异步任务，输出计费凭据并给出具体排错方向
- **演示音频**：`examples/demo/` 6 段真实合成样本（新闻 / 电商口播 / 诗词 / 悬疑 / 情绪骤变 / 英文），附文案原文、字符数与复现命令，可在 README 首屏直接试听；文档里记录了被换下架的两条及原因（多音字误读、音色与角色不匹配），因为这类问题只有人耳能验
- **在线试听页**：`examples/demo/index.html` 一页连听 6 段（同时只播一条），仓库根另有 `index.html` 作站点首页。两者都是单文件、零依赖、无外链 CDN，可本地双击打开；经 GitHub Pages 发布
- **在线试听页**：`examples/demo/index.html` —— 单文件、零依赖、无外链 CDN，6 段一页连听，带「听点」与一键复制复现命令；可直接本地双击打开，也可挂 GitHub Pages
- **静态试音页**：`static/index.html`，可独立托管做售前页

### 修复

- **Docker 部署下账本写入被静默丢弃**：容器内 `/app/logs` 不存在且 `/app` 归 root，进程以 uid 10001 运行，写账本抛 `PermissionError` 后只留一条 warning —— 服务一切正常但 `/v1/usage` 永远是 0。现在镜像内预建 `/app/logs/billing` 并归 `adapter` 所有，`docker-compose.yml` 把 `./logs` 挂到容器，`up.sh` 负责在宿主机建目录并 `chown 10001`（账本因此也能被 `scripts/reconcile.py` 直接读到）。
- **`up.sh` 不收紧 `.env` 权限**：新生成或沿用的 `.env` 一律 `chmod 600`。此前是 644，同机任何用户都能读走里面的上游 Key。
- **`/v1/usage` 的 `note` 与计费口径矛盾**：写死成「计费 = 实际合成字符数」（chars 口径的说法），但默认口径是 `input` —— 那句话会让对账的人以为 `saved_chars` 没收费，正好反了。现按 `ADAPTER_BILLING_MODE` 输出对应措辞，并回传 `billing_mode` 字段。
- **发布门禁误报自身**：`release_check.py` 扫历史时会把自己的自测样例当成真实泄露，导致任何 clone 后跑门禁都红。已排除自身路径。

### 变更

- `Dockerfile` 新增 `ARG PIP_INDEX_URL`，`docker-compose.yml` 透传；境外默认 PyPI，国内网络首次构建可 `PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple bash scripts/up.sh ...`（实测从 16 分钟降到 109 秒）。
- 发布门禁的虚拟环境识别由固定目录名改为前缀匹配（`.venv*` / `venv*` / `env`），避免把 site-packages 里的示例 IP 判成泄露。
- 发布门禁的音频规则从「一律拦截」改为「只放行 `examples/demo/`」，并对**单文件 ≤800 KB、合计 ≤4 MB、文件名必须登记在 manifest** 三处做检查 —— 演示音频是资产，测试产物才是垃圾，靠白名单放行等于把门拆了。

### 已知限制

- 只实现 TTS 的协议转换，不含声音克隆、音乐、配音等接口；STT 未实现
- 订阅额度到积分的换算依赖你自己填 `ADAPTER_UP_CREDITS_PER_USD`，填错则单位成本会算错（填 0 则不算，不会给错数）
- 未内置重试队列，上游 5xx 由调用方自行重试；任务表在进程内存，重启会丢排队中的任务

[0.4.0]: https://github.com/kimimei/newapi-elevenlabs-adapter/releases/tag/v0.4.0

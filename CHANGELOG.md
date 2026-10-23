# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-10-23

首个公开版本。

### 能力

- **OpenAI 兼容协议**：`POST /v1/audio/speech`、`GET /v1/models`，支持 `Authorization: Bearer` 与请求头透传两种鉴权
- **计费对齐**：回传 `X-Usage-Input-Characters` / `X-Usage-Sent-Characters`，与 new-api 的 `prompt_tokens` 口径逐笔相等
- **三种计费口径**：`input`（默认，对齐网关）/ `chars`（清理后长度）/ `upstream`（上游积分）
- **三层对账**：账本 ↔ 网关日志（L1）、账本 ↔ 上游 `/v1/history`（L2）、售价 ↔ 成本（L3）
- **长文本异步任务**：`/v1/audio/speech/jobs`，202 + 轮询 + 下载，规避网关超时
- **中文场景包**：播音 / 旁白 / 有声书 / 商务 / 对话 / 亲切 / 播客 / 影视，含台湾口音双音色
- **文本归一化**：零宽字符、多余空白清理，清理量单独记账，不影响计费基数
- **自检工具链**：`scripts/preflight.py`（12 项接入体检）、`scripts/reconcile.py`（HTML 对数报告）、`scripts/release_check.py`（发布门禁）
- **静态试音页**：`static/index.html`，可独立托管做售前页

### 已知限制

- 只实现 TTS 与 STT 的协议转换，不含声音克隆、音乐、配音等接口
- 订阅额度到积分的换算依赖你自己填 `ADAPTER_UP_CREDITS_PER_USD`，填错则毛利率会错（填 0 则不算，不会给错数）
- 未内置重试队列，上游 5xx 由调用方自行重试

[0.1.0]: https://github.com/kimimei/elevenlabs-adapter/releases/tag/v0.1.0

# 贡献指南

感谢你愿意花时间改这个项目。它解决的问题很具体：**把 ElevenLabs 包成 OpenAI TTS 协议，并且让计费对得上账。** 第二半比第一半难，所以下面的规矩主要围绕它。

## 开发环境

```bash
git clone https://github.com/kimimei/newapi-elevenlabs-adapter.git
cd elevenlabs-adapter
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

跑起来不需要真实 key：

```bash
cp .env.example .env               # ELEVENLABS_API_KEY 留空 = mock 模式
python run.py                      # http://127.0.0.1:8123
curl http://127.0.0.1:8123/healthz # {"status":"ok","mock":true}
```

## 提交前必须跑

```bash
python -m pytest -q                # 全部用例，不联网、不花钱
python scripts/release_check.py    # 发布门禁：真实 IP / 疑似令牌 / 测试音频
```

两条都过了再提 PR。CI 会重复跑这两条，PR 里请附上输出。

## 三条硬规矩

### 1. 测试不许碰真实账本

`tests/conftest.py` 已经把 `BILLING_LOG_DIR` 钉到临时目录了。**不要为了“方便”绕开它**，也不要在测试里写死 `logs/billing/`。

历史教训：曾经有 10 个测试文件每跑一次就往真实账本追加几十行，攒出 3072 行假数据，把对账结果全污染了。测试流量和真实流量混在一起，比没有账本更糟。

### 2. 计费改动必须给对账证明

任何改动 `app/billing.py`、`app/config.py` 里计费口径的 PR，请在描述里回答：

1. 计费基数是什么（`input` 原始字符 / `chars` 清理后字符 / `upstream` 上游积分）
2. 与 new-api 的 `prompt_tokens`（= `utf8.RuneCountInString(input)`）是否仍然**逐笔相等**
3. 失败请求是否仍计 0

只写“测试通过”不够 —— 单测通过但口径漂移，正是这个项目最容易犯的错。

### 3. 量纲不同的数不能直接相除

`1 - up_credits / bill_chars` 这种写法在数学上无意义：分子是积分，分母是字符。任何单位成本计算都必须先把两边折成同一个单位（美元）。

**看到两个数相差约 2 倍时，先怀疑是 `1/x` 的单位换算关系，再怀疑是配置/档位差异。**

## 代码约定

- Python 3.10+，类型标注尽量写全
- 中文注释可以，但**面向日志和 API 报错的文案用中文，面向代码标识符的用英文**
- 不引入新依赖前先说明理由（依赖越少，供应链风险越小）
- 不要 `except Exception: pass` —— 静默失败在这个项目里等于算错钱
- 注释解释**为什么**，不解释**是什么**

## 提交信息

```
<type>: <一句话>

<可选正文：为什么这么改，而不是改了什么>
```

`type` 用 `feat` / `fix` / `docs` / `refactor` / `test` / `chore`。

## 不要提交

- 任何真实 API key（包括测试文件里的 `sk-` 样例会触发门禁）
- 真实服务器 IP / 域名 / 部署路径
- 音频文件（体积大且可能含版权内容）
- `logs/` 下的账本

`.gitignore` 已覆盖上述项，但**删文件不等于删历史**。如果不小心提交了密钥，请立刻在上游吊销该 key，而不只是再提交一次删除。

## 提 PR

1. Fork → 开分支（`fix/xxx`、`feat/xxx`）
2. 改完跑上面两条命令
3. 按 PR 模板填，尤其是「影响面」和「如果改了计费逻辑」
4. 一个 PR 只做一件事

## 报告安全问题

不要开公开 issue，见 [SECURITY.md](SECURITY.md)。

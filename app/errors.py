# -*- coding: utf-8 -*-
"""错误 → 「下一步改哪一行」。

中转站接入时 80% 的求助是同一批错误反复出现：key 填错、模型没注册、并发打满。
把「错误码 + 上游原文」翻译成一句可执行的话，能省掉大量一对一答疑。

原则：
  * `message` 给机器/日志看（保持 OpenAI 结构不变）
  * `fix` 给人看（一句话，动词开头，指向具体操作）
  * 宁可说「去确认 X」，也不要猜一个可能错的结论
"""
import re

# 状态码兜底：上游没给可读信息时用
_BY_STATUS = {
    400: ("请求参数不合法",
          "检查 model 是否为 eleven_* 引擎名、response_format 是否在 mp3/opus/wav/pcm 之内"),
    401: ("上游 Key 失效或未提供",
          "渠道密钥必须是 ElevenLabs 的 sk_ 开头 Key（不是中转站的 sk- 令牌）；"
          "到 ElevenLabs 后台重新复制一份，注意别带空格"),
    402: ("上游账户额度不足",
          "ElevenLabs 账户余额或套餐额度用尽；充值或换 Key 后重试"),
    403: ("上游拒绝访问",
          "该 Key 没有对应能力权限（如语音克隆需额外授权），或账户被限制"),
    404: ("上游找不到该资源",
          "voice_id 不存在或不属于该账户；先 GET /v1/models 与 /v1/voices 确认名字"),
    422: ("上游参数校验失败",
          "多为 voice_settings 或 output_format 不被该引擎支持；"
          "注意 eleven_v4 不接受 language_code=zh-TW"),
    429: ("上游并发或频率超限",
          "ElevenLabs 按套餐限制并发；降低并发、加退避重试，或升级套餐"),
    500: ("上游内部错误", "稍后重试；连续出现则查 ElevenLabs 状态页"),
    501: ("适配器不支持该功能",
          "去掉 stream=true（流式暂未实现）"),
    502: ("上游网络错误", "检查服务器出网与 DNS；重试一次"),
}

# 上游原文里的关键片段 → 更精确的解释（优先于状态码兜底）
_PATTERNS = [
    (r"language_code|language code",
     "该引擎不支持这个 language_code",
     "eleven_v4 只认 zh，不认 zh-TW；把 language 改成 zh，或改用 eleven_v3"),
    (r"voice.*(not found|does not exist)|invalid.*voice",
     "voice_id 不存在",
     "音色名写错，或该音色属于别人账户；用 /v1/voices 里的 voice_id 重试"),
    (r"model.*(not found|does not exist|not supported)",
     "模型不存在或当前账户不可用",
     "核对模型名（eleven_v3 / eleven_v4 / eleven_multilingual_v2 / eleven_flash_v2_5 / eleven_turbo_v2_5）"),
    (r"quota|credit|insufficient",
     "账户额度不足",
     "充值或换用其它 Key；在 ElevenLabs 后台查看剩余 credits"),
    (r"concurren|rate limit|too many requests",
     "并发超限",
     "降低并发数；批量任务请串行或加 1-3 秒间隔"),
    (r"text.*(too long|exceed)|max.*characters",
     "文本超过单次上限",
     "按 2000 字左右分段提交，再拼接音频"),
    (r"permission|scope|unauthorized",
     "Key 权限不足",
     "该 Key 缺少对应 API 权限；用具备 TTS 权限的 Key"),
]


def explain(status, upstream_text=""):
    """返回 (message, fix)。永不抛异常。"""
    text = (upstream_text or "")[:400].lower()
    for pattern, message, fix in _PATTERNS:
        if re.search(pattern, text):
            return message, fix
    message, fix = _BY_STATUS.get(
        status, ("上游返回 HTTP %s" % status, "把这段错误原文发给服务方，附上 request_id"))
    return message, fix

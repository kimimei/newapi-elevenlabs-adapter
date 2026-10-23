#!/usr/bin/env bash
# 只读自检：服务通不通、有哪些模型和音色、今天用了多少。
# 全是 GET，不花钱、不消耗额度 —— 排障第一步先跑这个。
#
#   bash examples/curl/probe.sh
#   bash examples/curl/probe.sh -b http://127.0.0.1:8123/v1
#
# 输出四段：健康 → 模型 → 音色 → 用量。哪段不对就先修那段，别急着调 speech。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/../.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

BASE="${ADAPTER_BASE_URL:-http://127.0.0.1:8123/v1}"
TOKEN="${ADAPTER_TOKEN:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    -b|--base-url) BASE="$2"; shift 2 ;;
    -k|--token)    TOKEN="$2"; shift 2 ;;
    -h|--help)     sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "不认识的参数：$1" >&2; exit 2 ;;
  esac
done

BASE="${BASE%/}"
case "$BASE" in */audio/speech) BASE="${BASE%/audio/speech}" ;; esac
case "$BASE" in */v1) ;; *) BASE="$BASE/v1" ;; esac

HOST="${BASE%/v1}"          # 健康检查在根路径，不带 /v1
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

# 有 jq 就用 jq 出表格；没有就退回缩进后的 JSON —— 不为了一个字段去装包
HAS_JQ=""
PY=""
if command -v jq >/dev/null 2>&1; then
  HAS_JQ=1
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
fi

pretty() {
  if [ "$HAS_JQ" = 1 ]; then jq . 2>/dev/null || cat
  elif [ -n "$PY" ]; then "$PY" -m json.tool 2>/dev/null || cat
  else cat; fi
}

echo "════ 1/4 健康检查  $HOST/healthz"
HEALTH=$(curl -sS --max-time 10 "$HOST/healthz" 2>/dev/null) || {
  echo "  ✗ 连不上 —— 服务没起？docker ps --filter name=elevenlabs-adapter"
  exit 1
}
echo "$HEALTH" | pretty | sed 's/^/  /'
echo

echo "════ 2/4 模型目录  GET /v1/models"
MODELS=$(curl -sS --max-time 15 ${AUTH[@]+"${AUTH[@]}"} "$BASE/models" 2>/dev/null)
if [ "$HAS_JQ" = 1 ]; then
  echo "$MODELS" | jq -r '.data[].id' 2>/dev/null | sed 's/^/  /'
else
  # 没有 jq 也不必打印整坨 JSON：模型名只出现在 "id" 上，抠出来就够看
  echo "$MODELS" | grep -o '"id": *"[^"]*"' | cut -d'"' -f4 | sed 's/^/  /'
fi
echo "  （补充包：$BASE/models?presets=all 可预览全部，不改服务端默认）"
echo

echo "════ 3/4 音色  GET /v1/voices"
VOICES=$(curl -sS --max-time 15 ${AUTH[@]+"${AUTH[@]}"} "$BASE/voices" 2>/dev/null)
if [ "$HAS_JQ" = 1 ]; then
  echo "$VOICES" | jq -r '"  共 \(.count) 个（前 12 个）：", (.voices[0:12][] | "  \(.name)\t\(.kind)\t\(.voice_id)")' 2>/dev/null
else
  printf '  共 %s 个（前 12 个）：\n' "$(echo "$VOICES" | grep -o '"voice_id"' | wc -l | tr -d ' ')"
  echo "$VOICES" | grep -o '"name": *"[^"]*"' | cut -d'"' -f4 | head -12 | sed 's/^/  /'
fi
echo

echo "════ 4/4 用量账本  GET /v1/usage"
USAGE=$(curl -sS --max-time 15 ${AUTH[@]+"${AUTH[@]}"} "$BASE/usage" 2>/dev/null)
if [ "$HAS_JQ" = 1 ]; then
  echo "$USAGE" | jq -r '
    "  今日请求 \(.today.requests)（成功 \(.today.succeeded) / 失败 \(.today.failed)）",
    "  今日计费 \(.today.bill_chars) 字符，毛利率 \(.today.gross_margin_ratio // "未配置单价")",
    "  口径 \(.billing_mode)：\(.note)"' 2>/dev/null
else
  echo "$USAGE" | pretty | sed 's/^/  /' | head -40
fi
echo
echo "排错备忘："
echo "  · 401            → 令牌不对（直连自建填上游 Key，经中转站填面板令牌）"
echo "  · 音频有、账本全 0 → 容器内 logs/ 不可写：chown -R 10001:10001 logs"
echo "  · 模型列表空      → ADAPTER_PRESETS 配错，或渠道里没注册模型"

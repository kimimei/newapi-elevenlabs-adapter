#!/usr/bin/env bash
# 异步任务三步曲：提交 → 轮询 → 下载。几千字的长稿用这个。
#
#   bash examples/curl/jobs.sh --file 有声书一章.txt -o 成品.mp3
#   bash examples/curl/jobs.sh "短一点也行" --poll 2
#
# 前提：服务端 ADAPTER_JOBS_ENABLED=1，否则提交直接 404（脚本会把这句话打出来）。
# 别因为看到 queued 就重提 —— 返回的 estimated_seconds 就是预估等待。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/../.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

OUT="job.mp3"; FORMAT="mp3"; MODEL="${ADAPTER_MODEL:-eleven_v3}"
VOICE="${ADAPTER_VOICE:-yun}"; BASE="${ADAPTER_BASE_URL:-http://127.0.0.1:8123/v1}"
TOKEN="${ADAPTER_TOKEN:-}"; POLL=3; MAX_WAIT=900; TEXT=""; FILE=""

die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -o|--out)      OUT="$2"; shift 2 ;;
    --format)      FORMAT="$2"; shift 2 ;;
    --model)       MODEL="$2"; shift 2 ;;
    --voice)       VOICE="$2"; shift 2 ;;
    --poll)        POLL="$2"; shift 2 ;;
    --max-wait)    MAX_WAIT="$2"; shift 2 ;;
    -b|--base-url) BASE="$2"; shift 2 ;;
    -k|--token)    TOKEN="$2"; shift 2 ;;
    -f|--file)     FILE="$2"; shift 2 ;;
    -h|--help)     sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)            die "不认识的参数：$1" ;;
    *)             TEXT="$1"; shift ;;
  esac
done

BASE="${BASE%/}"
case "$BASE" in */audio/speech) BASE="${BASE%/audio/speech}" ;; esac
case "$BASE" in */v1) ;; *) printf '· base_url 未以 /v1 结尾，已补全\n'; BASE="$BASE/v1" ;; esac

if [ -n "$FILE" ]; then
  [ -f "$FILE" ] || die "文件不存在：$FILE"
  TEXT="$(cat "$FILE")"
fi
[ -n "${TEXT//[[:space:]]/}" ] || die "没有要合成的文本。用法：bash jobs.sh --file 稿子.txt"

# 顺序不能反：必须先 N 把所有行攒起来，再依次转义反斜杠 → 引号 → 换行。
# 先转义再攒行的话，第二行之后的内容不会被转义（曾经就是这么 400 的）。
json_escape() {
  printf '%s' "$1" | sed -e ':a;N;$!ba' -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\n/\\n/g'
}
PAYLOAD=$(printf '{"model":"%s","voice":"%s","input":"%s","response_format":"%s"}' \
  "$(json_escape "$MODEL")" "$(json_escape "$VOICE")" "$(json_escape "$TEXT")" "$FORMAT")

# 空 token 时干脆不发这个头：`Authorization: Bearer `（尾部空）在部分网关会直接 401
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT

echo "→ 提交任务，${#TEXT} 字符"
CODE=$(curl -sS -X POST "$BASE/audio/speech/jobs" \
  ${AUTH[@]+"${AUTH[@]}"} -H 'Content-Type: application/json' \
  -d "$PAYLOAD" -o "$TMP" -w '%{http_code}')

if [ "$CODE" = "404" ]; then
  echo "✗ 异步接口没开（404）"
  cat "$TMP"; echo
  die "服务端加 ADAPTER_JOBS_ENABLED=1 后重启；不想改配置就按 2000 字分段调同步接口"
fi
[ "$CODE" = "202" ] || { echo "✗ 提交失败 HTTP $CODE"; cat "$TMP"; echo; exit 1; }

JOB_ID=""
if command -v jq >/dev/null 2>&1; then
  JOB_ID=$(jq -r '.id' "$TMP")
  echo "  job_id=$JOB_ID  预估 $(jq -r '.estimated_seconds' "$TMP")s"
else
  # 没 jq 就用 sed 抠 id —— 只抠这一个字段，够用
  JOB_ID=$(sed -n 's/.*"id": *"\([^"]*\)".*/\1/p' "$TMP")
  echo "  job_id=$JOB_ID"
fi
[ -n "$JOB_ID" ] || die "没解析出 job_id，原始响应：$(cat "$TMP")"

echo "→ 轮询（每 ${POLL}s 一次，最长 ${MAX_WAIT}s）"
DEADLINE=$(( $(date +%s) + MAX_WAIT ))
LAST=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  curl -sS "$BASE/audio/speech/jobs/$JOB_ID" ${AUTH[@]+"${AUTH[@]}"} -o "$TMP"
  if command -v jq >/dev/null 2>&1; then
    STATUS=$(jq -r '.status // "unknown"' "$TMP")
    ERR=$(jq -r '.error // empty' "$TMP")
  else
    STATUS=$(sed -n 's/.*"status": *"\([^"]*\)".*/\1/p' "$TMP")
    ERR=""
  fi
  if [ "$STATUS" != "$LAST" ]; then
    printf '  [%s] %s\n' "$(date +%H:%M:%S)" "$STATUS"
    LAST="$STATUS"
  fi
  case "$STATUS" in
    succeeded) break ;;
    failed|cancelled) echo "✗ $STATUS：${ERR:-见 $BASE/audio/speech/jobs/$JOB_ID}"; exit 1 ;;
  esac
  sleep "$POLL"
done

[ "$STATUS" = "succeeded" ] || die "等待超时。任务可能还在跑，手动查：curl $BASE/audio/speech/jobs/$JOB_ID"

echo "→ 下载"
CODE=$(curl -sS "$BASE/audio/speech/jobs/$JOB_ID/audio" \
  ${AUTH[@]+"${AUTH[@]}"} -o "$OUT" -D "$TMP.h" -w '%{http_code}')
CTYPE=$(grep -i '^content-type:' "$TMP.h" 2>/dev/null | tail -1 | cut -d: -f2- | tr -d '\r' | xargs)
rm -f "$TMP.h"

[ "$CODE" = "200" ] || die "下载失败 HTTP $CODE：$(head -c 400 "$OUT")"
case "$CTYPE" in
  audio/*) ;;
  *) die "返回的不是音频（Content-Type: $CTYPE），内容：$(head -c 400 "$OUT")" ;;
esac

printf '✓ 已保存 %s（%s 字节）\n' "$OUT" "$(wc -c < "$OUT" | tr -d ' ')"

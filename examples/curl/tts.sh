#!/usr/bin/env bash
# 一次最典型的 curl 调用：合成、存盘、打印计费凭据。
#
#   bash examples/curl/tts.sh "你好，世界"
#   bash examples/curl/tts.sh "测试" -o test.mp3 --speed 0.9
#   bash examples/curl/tts.sh "只看头" --headers-only
#   bash examples/curl/tts.sh --file 稿子.txt -o 成品.mp3
#
# 为什么不用一行 curl 了事：一行写法有三个必然会踩的坑，这个脚本都处理了
#   1) 失败时 -o 会把 JSON 错误体写进 .mp3 —— 文件几百字节、播放器打不开
#   2) 内联 -d '{"input":"..."}' 遇到引号/换行就 400 invalid JSON
#   3) PowerShell 里 curl 是 Invoke-WebRequest 的别名 —— 用 curl.exe 或本脚本
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/../.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

OUT="out.mp3"; FORMAT="${ADAPTER_FORMAT:-mp3}"; MODEL="${ADAPTER_MODEL:-eleven_v3}"
VOICE="${ADAPTER_VOICE:-yun}"; BASE="${ADAPTER_BASE_URL:-http://127.0.0.1:8123/v1}"
TOKEN="${ADAPTER_TOKEN:-}"; SPEED=""; FILE=""; TEXT=""; HEADERS_ONLY=0; TEXT_CLEAN=""

die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -o|--out)        OUT="$2"; shift 2 ;;
    --format)        FORMAT="$2"; shift 2 ;;
    --model)         MODEL="$2"; shift 2 ;;
    --voice)         VOICE="$2"; shift 2 ;;
    --speed)         SPEED="$2"; shift 2 ;;
    --text-clean)    TEXT_CLEAN="$2"; shift 2 ;;
    -b|--base-url)   BASE="$2"; shift 2 ;;
    -k|--token)      TOKEN="$2"; shift 2 ;;
    -f|--file)       FILE="$2"; shift 2 ;;
    --headers-only)  HEADERS_ONLY=1; shift ;;
    -h|--help)       sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)              die "不认识的参数：$1" ;;
    *)               TEXT="$1"; shift ;;
  esac
done

# 补齐 /v1 —— 漏了它，真实路径会变成 /audio/speech，直接 404
BASE="${BASE%/}"
case "$BASE" in
  */audio/speech) BASE="${BASE%/audio/speech}" ;;
esac
case "$BASE" in
  */v1) ;;
  *) printf '· base_url 未以 /v1 结尾，已自动补全为 %s/v1\n' "$BASE"; BASE="$BASE/v1" ;;
esac

if [ -n "$FILE" ]; then
  [ -f "$FILE" ] || die "文件不存在：$FILE"
  TEXT="$(cat "$FILE")"
fi
[ -n "${TEXT//[[:space:]]/}" ] || die "没有要合成的文本。用法：bash tts.sh \"你好，世界\"   或 --file 稿子.txt"

# JSON 转义：反斜杠 → 引号 → 换行。少了这一步，带引号或换行的稿子必然 400
# 顺序不能反：必须先 N 把所有行攒起来，再依次转义反斜杠 → 引号 → 换行。
# 先转义再攒行的话，第二行之后的内容不会被转义（曾经就是这么 400 的）。
json_escape() {
  printf '%s' "$1" | sed -e ':a;N;$!ba' -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\n/\\n/g'
}

PAYLOAD=$(printf '{"model":"%s","voice":"%s","input":"%s","response_format":"%s"%s%s}' \
  "$(json_escape "$MODEL")" "$(json_escape "$VOICE")" "$(json_escape "$TEXT")" "$FORMAT" \
  "${SPEED:+,\"speed\":$SPEED}" "${TEXT_CLEAN:+,\"text_clean\":\"$TEXT_CLEAN\"}")

# 空 token 时干脆不发这个头：`Authorization: Bearer `（尾部空）在部分网关会直接 401
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

TMP_H=$(mktemp); TMP_B=$(mktemp)
trap 'rm -f "$TMP_H" "$TMP_B"' EXIT

echo "→ POST $BASE/audio/speech"
echo "  model=$MODEL voice=$VOICE format=$FORMAT chars=${#TEXT}"

if [ "$HEADERS_ONLY" = 1 ]; then
  CODE=$(curl -sS -X POST "$BASE/audio/speech" \
    ${AUTH[@]+"${AUTH[@]}"} -H 'Content-Type: application/json' \
    -d "$PAYLOAD" -D "$TMP_H" -o /dev/null -w '%{http_code}')
else
  CODE=$(curl -sS -X POST "$BASE/audio/speech" \
    ${AUTH[@]+"${AUTH[@]}"} -H 'Content-Type: application/json' \
    -d "$PAYLOAD" -D "$TMP_H" -o "$TMP_B" -w '%{http_code}')
fi

CTYPE=$(grep -i '^content-type:' "$TMP_H" | tail -1 | cut -d: -f2- | tr -d '\r' | xargs)

if [ "$CODE" != "200" ]; then
  echo
  echo "✗ HTTP $CODE"
  BODY=""
  if [ -s "$TMP_B" ]; then
    BODY="$(cat "$TMP_B")"
    echo "$BODY"
  fi
  # 适配器的错误体一定是 {"error":{...}}。不是这个结构，说明响应来自反代 /
  # 云网关 / 占了同一端口的别的服务 —— 先怀疑地址，别去查上游。
  if ! printf '%s' "$BODY" | grep -q '"error"'; then
    echo "  → 这不是适配器的错误结构，多半来自反代/网关或别的服务。先确认端口上跑的是什么："
    echo "    curl -s ${BASE%/v1}/healthz"
  fi
  case "$CODE" in
    400) echo "  → 参数问题：response_format 只支持 mp3/opus/wav/pcm；看上面的 message 与 fix" ;;
    401) echo "  → 令牌不对：直连自建实例填上游 Key，经中转站填面板令牌，两者不能互换" ;;
    404) echo "  → 路径或模型不存在：检查 base 是否多了或少了一个 /v1" ;;
    429) echo "  → 限流：看响应里的 Retry-After" ;;
    501) echo "  → 本适配器不支持流式，去掉 stream=true" ;;
    502) echo "  → 上游异常或超时：长文本改用异步任务接口，或分段提交" ;;
  esac
  exit 1
fi

echo "✓ 200"

if [ "$HEADERS_ONLY" = 1 ]; then
  grep -i '^x-' "$TMP_H" | tr -d '\r' | sed 's/^/  /'
  exit 0
fi

# 用 Content-Type 而不是文件大小判断：错误体也可能是 200 以外的状态，
# 但反代偶尔会把错误包装成 200，只看状态码不够
case "$CTYPE" in
  audio/*) ;;
  *) die "服务端返回的不是音频（Content-Type: $CTYPE），内容：$(head -c 400 "$TMP_B")" ;;
esac

mkdir -p "$(dirname "$OUT")"
mv "$TMP_B" "$OUT"
grep -i '^x-' "$TMP_H" | tr -d '\r' | sed 's/^/  /'
printf '  已保存  %s（%s 字节）\n' "$OUT" "$(wc -c < "$OUT" | tr -d ' ')"

#!/usr/bin/env bash
# 五分钟包 —— 一条命令：建 .env → 起容器 → 等健康 → 跑自检 → 打印接入卡片
#
#   bash scripts/up.sh --key sk_你的ElevenLabsKey
#
# 参数
#   --key sk_xxx     上游 ElevenLabs Key（不填则提示输入；也可留空由调用方透传）
#   --port 8123      宿主机端口（默认取 .env 的 ADAPTER_PORT，再默认 8123）
#   --presets none   模型列举范围：none（默认，只列 5 个引擎）/ all / cn,tw
#   --db PATH        new-api 的 SQLite 路径（给了才做渠道/倍率/对账检查）
#   --dry-run        只打印将要执行的命令，不动系统
#   --no-build       不重新构建镜像（改过源码才需要构建）
#   --down           停止并移除容器
#   --logs           跟踪日志
#   --no-color       关闭颜色（写日志/CI 用）
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CONTAINER="elevenlabs-adapter"
KEY=""
PORT=""
PRESETS=""
DB_PATH=""
DRY=0
BUILD=1
ACTION="up"
NOCOLOR=0
[ -n "${NO_COLOR:-}" ] && NOCOLOR=1

ok()   { printf ' %s✓%s %s\n' "$C_OK" "$C_END" "$1"; }
warn() { printf ' %s!%s %s\n' "$C_WARN" "$C_END" "$1"; }
die()  { printf ' %s✗%s %s\n' "$C_ERR" "$C_END" "$1" >&2; exit 1; }
step() { printf '\n%s▸ %s%s\n' "$C_DIM" "$1" "$C_END"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --key)      KEY="${2:-}"; shift 2 ;;
    --port)     PORT="${2:-}"; shift 2 ;;
    --presets)  PRESETS="${2:-}"; shift 2 ;;
    --db)       DB_PATH="${2:-}"; shift 2 ;;
    --dry-run)  DRY=1; shift ;;
    --no-build) BUILD=0; shift ;;
    --down)     ACTION="down"; shift ;;
    --logs)     ACTION="logs"; shift ;;
    --no-color) NOCOLOR=1; shift ;;
    -h|--help)  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

# 颜色要在参数解析之后再定，否则 --no-color 无效
if [ -t 1 ] && [ "$NOCOLOR" -eq 0 ]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[90m'; C_END=$'\033[0m'
else
  C_OK=""; C_ERR=""; C_WARN=""; C_DIM=""; C_END=""
fi

# ---------------------------------------------------------------- 环境
detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    return 1
  fi
}

if [ "$DRY" -eq 0 ]; then
  command -v docker >/dev/null 2>&1 || die "没有 docker。先装 Docker：https://docs.docker.com/engine/install/"
  COMPOSE="$(detect_compose)" || die "没有 docker compose 插件（也没有 docker-compose）"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用：没有权限或没启动（试 sudo，或 systemctl start docker）"
else
  COMPOSE="docker compose"
fi

run() {                      # dry-run 时只打印
  if [ "$DRY" -eq 1 ]; then printf '   %s$ %s%s\n' "$C_DIM" "$*" "$C_END"; else "$@"; fi
}

if [ "$ACTION" = "down" ]; then
  step "停止容器"
  run $COMPOSE down
  ok "已停止。数据在镜像里，重启不会丢配置（.env 还在）"
  exit 0
fi

if [ "$ACTION" = "logs" ]; then
  exec $COMPOSE logs -f --tail=100
fi

# ---------------------------------------------------------------- .env
step "1/4 准备 .env"

if [ ! -f .env ]; then
  if [ "$DRY" -eq 1 ]; then
    printf '   %s$ cp .env.example .env && chmod 600 .env%s\n' "$C_DIM" "$C_END"
  else
    cp .env.example .env
    # .env 里有上游 Key：默认 644 意味着同机任何用户都能读走
    chmod 600 .env 2>/dev/null || true
    ok "已从 .env.example 生成 .env（权限 $(stat -c '%a' .env 2>/dev/null || echo 600)）"
  fi
else
  # 老部署可能停留在 644，这里顺带收紧
  [ "$DRY" -eq 0 ] && chmod 600 .env 2>/dev/null || true
  ok ".env 已存在，沿用（权限已收紧为 $(stat -c '%a' .env 2>/dev/null || echo 600)）"
fi

# 端口：命令行 > .env > 8123
PORT_ARG="$PORT"
if [ -z "$PORT" ] && [ -f .env ]; then
  PORT="$(grep -E '^ADAPTER_PORT=' .env | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
fi
PORT="${PORT:-8123}"

# 命令行指定的端口写回 .env，否则下次直接 docker compose up 会退回旧端口
if [ -n "$PORT_ARG" ] && [ -f .env ]; then
  if [ "$DRY" -eq 1 ]; then
    printf '   %s$ 写入 ADAPTER_PORT=%s 到 .env%s\n' "$C_DIM" "$PORT_ARG" "$C_END"
  elif grep -qE '^ADAPTER_PORT=' .env; then
    sed -i.bak "s|^ADAPTER_PORT=.*|ADAPTER_PORT=${PORT_ARG}|" .env && rm -f .env.bak
  else
    printf 'ADAPTER_PORT=%s\n' "$PORT_ARG" >> .env
  fi
fi

# Key：命令行 > .env > 交互询问
if [ -z "$KEY" ] && [ -f .env ]; then
  KEY="$(grep -E '^ELEVENLABS_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
fi
if [ -z "$KEY" ] && [ "$DRY" -eq 0 ] && [ -t 0 ]; then
  printf ' 请输入 ElevenLabs Key（sk_ 开头，直接回车跳过）：'
  read -r KEY || true
fi
if [ -n "$KEY" ]; then
  case "$KEY" in
    sk_*) ;;
    sk-*) warn "这看起来是 new-api 的令牌（sk-），不是 ElevenLabs 的 Key（sk_）。渠道里要填上游 Key。" ;;
    *)    warn "Key 不以 sk_ 开头，ElevenLabs 原生 Key 形如 sk_xxxxxxxx" ;;
  esac
  KEY_ESC="$(printf '%s' "$KEY" | sed -e 's/[&|\\]/\\&/g')"
  if [ "$DRY" -eq 1 ]; then
    printf '   %s$ 写入 ELEVENLABS_API_KEY 到 .env（已隐藏）%s\n' "$C_DIM" "$C_END"
  else
    if grep -qE '^ELEVENLABS_API_KEY=' .env; then
      sed -i.bak "s|^ELEVENLABS_API_KEY=.*|ELEVENLABS_API_KEY=${KEY_ESC}|" .env && rm -f .env.bak
    else
      printf 'ELEVENLABS_API_KEY=%s\n' "$KEY" >> .env
    fi
    ok "Key 已写入 .env（权限：$(stat -c '%a' .env 2>/dev/null || echo '-')）"
  fi
else
  warn "没有 Key：服务仍可启动，但调用时必须由调用方在 Authorization 头里透传"
fi

if [ -n "$PRESETS" ]; then
  if [ "$DRY" -eq 1 ]; then
    printf '   %s$ ADAPTER_PRESETS=%s%s\n' "$C_DIM" "$PRESETS" "$C_END"
  else
    sed -i.bak "s|^ADAPTER_PRESETS=.*|ADAPTER_PRESETS=${PRESETS}|" .env && rm -f .env.bak
    ok "模型列举范围：${PRESETS}"
  fi
fi

# ---------------------------------------------------------------- 起容器
step "2/4 启动容器（首次构建约 1–2 分钟）"

# 账本目录挂到宿主机。容器内进程是 uid 10001，宿主目录属主不对同样写不进去
if [ "$DRY" -eq 1 ]; then
  printf '   %s$ mkdir -p logs/billing && chown -R 10001:10001 logs%s\n' "$C_DIM" "$C_END"
else
  mkdir -p logs/billing
  if [ "$(id -u)" = "0" ]; then
    chown -R 10001:10001 logs 2>/dev/null || true
  else
    warn "非 root 运行：若 logs/ 属主不是 10001，账本会写不进去（sudo chown -R 10001:10001 logs）"
  fi
fi

if [ "$BUILD" -eq 1 ]; then
  run env ADAPTER_PORT="$PORT" $COMPOSE up -d --build
else
  run env ADAPTER_PORT="$PORT" $COMPOSE up -d
fi

# ---------------------------------------------------------------- 等健康
step "3/4 等待健康检查"
if [ "$DRY" -eq 1 ]; then
  printf '   %s$ docker inspect -f "{{.State.Health.Status}}" %s%s\n' "$C_DIM" "$CONTAINER" "$C_END"
else
  STATUS=""
  for ((i = 0; i < 60; i++)); do
    STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || echo missing)"
    case "$STATUS" in
      healthy) break ;;
      unhealthy) warn "容器不健康，最近日志："; docker logs --tail=30 "$CONTAINER" 2>&1 | sed 's/^/   /'; die "启动失败，按上面的日志修" ;;
      *) sleep 1 ;;
    esac
  done
  if [ "$STATUS" != "healthy" ]; then
    docker logs --tail=30 "$CONTAINER" 2>&1 | sed 's/^/   /'
    die "60 秒内未就绪（最后状态：$STATUS）。常见原因：端口 $PORT 被占用、Key 格式错误"
  fi
  ok "健康检查通过"
fi

# ---------------------------------------------------------------- 冒烟 + 自检
step "4/4 冒烟 + 自检"
ADAPTER="http://127.0.0.1:${PORT}"
PY=""
for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done

if [ "$DRY" -eq 1 ]; then
  printf '   %s$ GET %s/v1/models（用 python 读 JSON）%s\n' "$C_DIM" "$ADAPTER" "$C_END"
  printf '   %s$ %s scripts/preflight.py --adapter %s ...%s\n' "$C_DIM" "${PY:-python3}" "$ADAPTER" "$C_END"
elif [ -n "$PY" ]; then
  MODELS="$("$PY" - "$ADAPTER" <<'PYEOF'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/v1/models", timeout=5) as r:
        data = json.load(r).get("data", [])
    print(" ".join(m["id"] for m in data))
except Exception as e:
    print("ERROR:%s" % e)
PYEOF
)"
  case "$MODELS" in
    ERROR:*) warn "取模型列表失败：${MODELS#ERROR:}" ;;
    "")      warn "模型列表为空" ;;
    *)       ok "模型目录：$MODELS" ;;
  esac

  if [ -n "$DB_PATH" ] && [ -f "$ROOT/scripts/preflight.py" ]; then
    printf '\n'
    set +e
    if [ -n "$KEY" ]; then
      "$PY" "$ROOT/scripts/preflight.py" --adapter "$ADAPTER" --db "$DB_PATH" \
        --key "$KEY" --no-color
    else
      "$PY" "$ROOT/scripts/preflight.py" --adapter "$ADAPTER" --db "$DB_PATH" \
        --no-color
    fi
    PF=$?
    set -e
    [ "$PF" -ne 0 ] && warn "自检有未通过项（见上），不影响服务启动"
  elif [ -n "$DB_PATH" ]; then
    warn "找不到 scripts/preflight.py，跳过自检"
  fi
else
  warn "没有 python，跳过冒烟与自检；用 curl 手工验证："
  printf '   curl -s %s/v1/models\n' "$ADAPTER"
fi

# ---------------------------------------------------------------- 接入卡片
HOST_IP="$( (hostname -I 2>/dev/null | awk '{print $1}') || true )"
DOCKER_GW="$( (ip -4 addr show docker0 2>/dev/null | grep -oE 'inet ([0-9.]+)' | awk '{print $2}') || true )"
HOST_IP="${HOST_IP:-<本机IP>}"

printf '\n%s────────────────────────────────────────────────────────────%s\n' "$C_DIM" "$C_END"
printf ' 已就绪。把下面填进 new-api「添加渠道」：\n\n'
printf '   类型        OpenAI\n'
printf '   Base URL    http://%s:%s        ← 只到端口，不要加 /v1\n' "$HOST_IP" "$PORT"
printf '   密钥        你的 ElevenLabs Key（sk_ 开头，不是中转令牌）\n'
printf '   模型        eleven_v4,eleven_v3,eleven_multilingual_v2,eleven_flash_v2_5,eleven_turbo_v2_5\n'
printf '   分组        default\n'
if [ -n "$DOCKER_GW" ]; then
  printf '\n   %s若 new-api 跑在 Docker 里，Base URL 用 http://%s:%s（docker0 网关）%s\n' \
    "$C_WARN" "$DOCKER_GW" "$PORT" "$C_END"
fi
printf '\n 接着必做两件事（否则会算错钱）：\n'
printf '   ① 定价 → ModelRatio：v4 / v3 / multilingual_v2 = 40，flash / turbo = 20\n'
printf '   ② 同一面板 → AudioRatio：这 5 个模型全部设 0\n'
printf '\n 自检：%s scripts/preflight.py --adapter http://127.0.0.1:%s --db <new-api.db>%s\n' \
  "${PY:-python3}" "$PORT" "$C_END"
printf '%s────────────────────────────────────────────────────────────%s\n\n' "$C_DIM" "$C_END"

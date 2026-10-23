#!/usr/bin/env bash
# GitHub 上传环境：首次配置 + 后续更新
#   首次：先在 github.com 建空仓库 kimimei/elevenlabs-adapter（别勾 README/.gitignore/LICENSE）
#         再执行  bash scripts/setup_github.sh
#   更新：        bash scripts/setup_github.sh --update "feat: 说明"
set -euo pipefail

OWNER="kimimei"
REPO="elevenlabs-adapter"
KEY="$HOME/.ssh/id_ed25519_github"
SSH_URL="git@github.com:${OWNER}/${REPO}.git"

cd "$(dirname "$0")/.."

# 身份（已有则保留）
[ -n "$(git config user.name || true)" ]  || git config user.name  "$OWNER"
[ -n "$(git config user.email || true)" ] || git config user.email "${OWNER}@users.noreply.github.com"

# ---------- 后续更新模式 ----------
if [ "${1:-}" = "--update" ]; then
  git add -A
  git commit -m "${2:-chore: update $(date '+%Y-%m-%d %H:%M')}" || echo "nothing to commit"
  git push
  echo "已推送"
  exit 0
fi

# ---------- 首次：SSH key ----------
mkdir -p "$HOME/.ssh"
if [ ! -f "$KEY" ]; then
  ssh-keygen -t ed25519 -C "${OWNER}@github" -f "$KEY" -N ""
  chmod 600 "$KEY"
fi

echo ""
echo "===== 把下面整段公钥加到 https://github.com/settings/ssh/new ====="
cat "${KEY}.pub"
echo "=================================================================="
echo ""

# ---------- remote ----------
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$SSH_URL"
else
  git remote add origin "$SSH_URL"
fi
echo "remote -> $SSH_URL"

# ---------- ~/.ssh/config（免参数） ----------
touch "$HOME/.ssh/config"; chmod 600 "$HOME/.ssh/config" 2>/dev/null || true
grep -q "IdentityFile $KEY" "$HOME/.ssh/config" || cat >> "$HOME/.ssh/config" <<EOF

Host github.com
  HostName github.com
  User git
  IdentityFile $KEY
  IdentitiesOnly yes
EOF

# ---------- 首次提交 ----------
git add -A
git commit -m "feat: OpenAI-compatible ElevenLabs TTS adapter (input billing + gross margin)" || true
git branch -M main

# ---------- 验证 + 推送 ----------
if ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
  git push -u origin main
  echo "已推送：https://github.com/${OWNER}/${REPO}"
else
  echo "SSH 尚未认证：先把上面的公钥加到 GitHub，再执行  git push -u origin main"
fi

#!/usr/bin/env bash
# 一键发布：创建 GitHub 仓库并推送代码
# 前提：已执行 gh auth login
#
#   bash scripts/publish.sh [仓库名] [可见性]
#   例：bash scripts/publish.sh d-sync public

set -euo pipefail

REPO_NAME="${1:-d-sync}"
VISIBILITY="${2:-public}"
GH_BIN="$LOCALAPPDATA/Programs/GitHubCLI/bin/gh.exe"
REMOTE="git@github.com:gitsang/${REPO_NAME}.git"

cd "$(dirname "$0")/.."

echo "==> 检查 gh 登录状态"
if ! "$GH_BIN" auth status >/dev/null 2>&1; then
    echo "gh 尚未登录，请先运行："
    echo "  $GH_BIN auth login --hostname github.com --git-protocol ssh --web"
    exit 1
fi

echo "==> 创建仓库 ${REPO_NAME} (${VISIBILITY})"
if "$GH_BIN" repo view "gitsang/${REPO_NAME}" >/dev/null 2>&1; then
    echo "    仓库已存在，跳过创建"
else
    "$GH_BIN" repo create "gitsang/${REPO_NAME}" \
        --public --description "Windows 后台常驻的外置设备自动同步守护程序" \
        --source . --remote origin
fi

echo "==> 推送代码"
git push -u origin main

echo "==> 完成"
"$GH_BIN" repo view "gitsang/${REPO_NAME}" --web

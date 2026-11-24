#!/usr/bin/env bash
# One-shot publish: create the GitHub repo and push the code
# Prerequisite: gh auth login has been run
#
#   bash scripts/publish.sh [repo-name] [visibility]
#   e.g. bash scripts/publish.sh d-sync public

set -euo pipefail

REPO_NAME="${1:-d-sync}"
VISIBILITY="${2:-public}"
GH_BIN="$LOCALAPPDATA/Programs/GitHubCLI/bin/gh.exe"
REMOTE="git@github.com:gitsang/${REPO_NAME}.git"

cd "$(dirname "$0")/.."

echo "==> Checking gh login state"
if ! "$GH_BIN" auth status >/dev/null 2>&1; then
    echo "gh is not logged in; run this first:"
    echo "  $GH_BIN auth login --hostname github.com --git-protocol ssh --web"
    exit 1
fi

echo "==> Creating repo ${REPO_NAME} (${VISIBILITY})"
if "$GH_BIN" repo view "gitsang/${REPO_NAME}" >/dev/null 2>&1; then
    echo "    repo already exists, skipping creation"
else
    "$GH_BIN" repo create "gitsang/${REPO_NAME}" \
        --public --description "Resident Windows sync daemon for external devices" \
        --source . --remote origin
fi

echo "==> Pushing code"
git push -u origin main

echo "==> Done"
"$GH_BIN" repo view "gitsang/${REPO_NAME}" --web

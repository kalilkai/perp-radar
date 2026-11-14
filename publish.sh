#!/usr/bin/env bash
# 提交已產生的靜態頁與持倉快照，並推送目前分支。
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f site/index.html || ! -f data/oi-snapshot.json ]]; then
  echo "找不到頁面或快照。請先執行：python3 build.py" >&2
  exit 1
fi

email="$(git config --get user.email || true)"
name="$(git config --get user.name || true)"
if [[ -z "$name" || "$email" != *noreply.github.com ]]; then
  echo "請先把 git 作者設成 GitHub noreply 位址，再發布。" >&2
  exit 1
fi

git add -- data/oi-snapshot.json site
if git diff --cached --quiet; then
  echo "沒有需要提交的變更"
  exit 0
fi

git commit -m "更新每日合約數據"
git push origin HEAD

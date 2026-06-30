#!/usr/bin/env bash
# 収集 → data/latest.json を GitHub に push するラッパー（cron から実行）。
# クラウドルーチンはこの push された latest.json を読んで検証・通知する。
set -uo pipefail

cd /home/nuadmin/projects/DataFetch || exit 1

# 収集して latest.json を更新
/usr/bin/python3 collect_to_json.py

# 変更を push（latest.json は毎回 generated_at が変わるので毎日コミットされる＝死活監視も兼ねる）
git add data/latest.json
if ! git diff --cached --quiet; then
    git commit -q -m "data: $(date '+%Y-%m-%d %H:%M:%S')"
    git push -q origin master && echo "[$(date '+%F %T')] push 成功"
else
    echo "[$(date '+%F %T')] 変更なし（push スキップ）"
fi

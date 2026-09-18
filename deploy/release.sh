#!/usr/bin/env bash
# 一步发版：更新 VERSION + CHANGELOG 摘要 → 测试 → 同步服务器
# 用法：bash deploy/release.sh 0.4.1 "本次变更摘要"
set -euo pipefail
cd "$(dirname "$0")/.."
VER="${1:?用法: bash deploy/release.sh <version> <summary>}"
SUMMARY="${2:?需要变更摘要}"
DATE=$(date '+%Y-%m-%d')
echo "$VER" > VERSION
python3 - "$VER" "$DATE" "$SUMMARY" <<'PYEOF'
import sys
ver, date, summary = sys.argv[1:4]
path = "CHANGELOG.md"
src = open(path, encoding="utf-8").read()
entry = f"## {ver} — {date}\n\n**变更**：{summary}\n\n"
first = src.find("\n## ")
open(path, "w", encoding="utf-8").write(src[:first + 1] + entry + src[first + 1:]
                                       if first > 0 else src + "\n" + entry)
print("CHANGELOG 已更新")
PYEOF
bash deploy/sync.sh -y -m "release: v$VER — $SUMMARY" | tail -3
echo "已发版 v$VER"

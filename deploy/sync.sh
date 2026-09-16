#!/usr/bin/env bash
# UserLoop 一键同步：测试 → git 提交推送 → 服务器同步重启 → 线上验收 → 刷新 Cloudflare 缓存
# 用法：bash deploy/sync.sh [-m "提交信息"] [-y]
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=172.96.253.73
SSH_PORT=28766
APP_DIR=/www/wwwroot/userloop
SITE=https://nownexts.com
CF_CREDS="$HOME/OpenFlow Dev/data/cloudflare.json"
MSG="sync $(date '+%Y-%m-%d %H:%M')"
ASSUME_YES=0

while getopts "m:y" opt; do
  case "$opt" in
    m) MSG="$OPTARG" ;;
    y) ASSUME_YES=1 ;;
    *) echo "用法: bash deploy/sync.sh [-m 提交信息] [-y]" && exit 1 ;;
  esac
done

step() { printf "\n\033[1;36m═══ %s ═══\033[0m\n" "$*"; }

step "1/6 本地测试"
.venv/bin/python -m pytest tests/ -q --tb=line | tail -1

# ─── 2. git 提交推送 ───
step "2/6 git 提交推送"
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "$MSG" | head -2
  git push -u origin main 2>&1 | grep -E "main|up-to-date" | head -2
else
  echo "无未提交变更（跳过 commit，继续同步服务器）"
fi

# ─── 3. 打包同步服务器 ───
step "3/6 服务器同步（$HOST:$APP_DIR）"
TAR=/tmp/userloop-sync.tar.gz
tar czf "$TAR" \
  --exclude .venv --exclude data --exclude .git --exclude __pycache__ \
  --exclude .pytest_cache --exclude '*.pyc' --exclude .DS_Store \
  --exclude .gitignore --exclude .gitattributes .
scp -P "$SSH_PORT" -q "$TAR" "root@$HOST:/tmp/userloop-sync.tar.gz"

# ─── 4. 解压 + 重启 + 远端测试 ───
step "4/6 服务器重启 + 远端测试"
ssh -p "$SSH_PORT" "root@$HOST" bash -s <<'REMOTE'
set -euo pipefail
tar xzf /tmp/userloop-sync.tar.gz -C /www/wwwroot/userloop
cd /www/wwwroot/userloop
find . -path ./.venv -prune -o -name __pycache__ -print0 | xargs -0 rm -rf 2>/dev/null || true
systemctl restart userloop
sleep 2
systemctl is-active userloop
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -1
REMOTE

# ─── 5. 线上验收 ───
step "5/6 线上验收"
LOGIN_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$SITE/userloop/")
TRACK_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$SITE/userloop/track.js")
MAIN_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$SITE/")
MFLOW_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$SITE/mflow/")
INGEST=$(ssh -p "$SSH_PORT" "root@$HOST" 'TOKEN=$(python3 -c "import json;print(json.load(open(\"/www/wwwroot/userloop/data/config.json\"))[\"api_token\"])"); curl -s -X POST http://127.0.0.1:8600/userloop/api/v1/ingest -H "X-UserLoop-Token: $TOKEN" -H "Content-Type: application/json" -d "{\"distinct_id\":\"sync_healthcheck\",\"event\":\"page_view\"}" -o /dev/null -w "%{http_code}"')
echo "登录页:$LOGIN_CODE track.js:$TRACK_CODE ingest:$INGEST 主站:$MAIN_CODE mflow:$MFLOW_CODE"
if [ "$LOGIN_CODE" != "200" ] || [ "$INGEST" != "200" ]; then
  echo "❌ 线上验收未通过，中止（服务器代码未回滚，请人工检查 journalctl -u userloop）"
  exit 1
fi

# ─── 6. 刷新 Cloudflare（/userloop/* 相关 URL）───
step "6/6 刷新 Cloudflare 缓存"
PURGE_URLS=$(cat <<EOF
{"files":["$SITE/userloop/track.js","$SITE/userloop/","$SITE/userloop/static/index.html","$SITE/userloop/static/canvas.html","$SITE/userloop/static/login.html"]}
EOF
)
if [ -f "$CF_CREDS" ]; then
  ZONE_ID=$(python3 -c "import json;print(json.load(open('$CF_CREDS'))['zone_id'])")
  CF_TOKEN=$(python3 -c "import json;print(json.load(open('$CF_CREDS'))['token'])")
  CF_OUT=$(curl -s -X POST \
    "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/purge_cache" \
    -H "Authorization: Bearer $CF_TOKEN" \
    -H "Content-Type: application/json" \
    --data "$PURGE_URLS")
  echo "$CF_OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('CF 刷新:', '成功' if d.get('success') else d.get('errors'))"
else
  echo "（未找到 $CF_CREDS，跳过 CF 刷新）"
fi
echo "（CF 已按 URL 定向刷新；API 失败时可用 $CF_CREDS 凭据手动 purge_everything）"

step "同步完成 ✅  $SITE/userloop/"

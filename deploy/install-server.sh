#!/usr/bin/env bash
# UserLoop 服务器部署脚本（在服务器上以 root 执行）
set -euo pipefail

APP_DIR=/www/wwwroot/userloop
MFLOW_AUTH=/www/wwwroot/mflow/run/auth.json
CONF=/www/server/panel/vhost/apache/nownexts.com.conf

cd "$APP_DIR"

# 1. venv + 依赖
if [ ! -x "$APP_DIR/.venv/bin/userloop" ]; then
  "$HOME/.local/bin/uv" venv "$APP_DIR/.venv" --python 3.12
  "$HOME/.local/bin/uv" pip install -e "$APP_DIR" --python "$APP_DIR/.venv/bin/python"
fi

# 2. 数据目录 + config（幂等）
mkdir -p "$APP_DIR/data"
if [ ! -f "$APP_DIR/data/config.json" ]; then
  TOKEN=$("$APP_DIR/.venv/bin/python" -c "import secrets;print(secrets.token_urlsafe(24))")
  cat > "$APP_DIR/data/config.json" <<EOF
{
  "api_token": "$TOKEN",
  "default_webhook": null,
  "smtp": {"host": "", "port": 587, "user": "", "password": "", "from": "userloop@nownexts.com"},
  "integrations": {"openflow": {"base_url": null, "webhook_secret": ""}, "mflow": {"base_url": null, "password": ""}}
}
EOF
  chmod 600 "$APP_DIR/data/config.json"
  echo "api_token: $TOKEN"
fi

# 3. 登录账号迁移（复用 MFlow bcrypt 用户库，同名同密码）
if [ ! -f "$APP_DIR/data/auth.json" ] && [ -f "$MFLOW_AUTH" ]; then
  cp "$MFLOW_AUTH" "$APP_DIR/data/auth.json"
  chmod 600 "$APP_DIR/data/auth.json"
fi

# 4. systemd
cp -f "$APP_DIR/deploy/userloop.service" /etc/systemd/system/userloop.service
systemctl daemon-reload
systemctl enable --now userloop
sleep 2
systemctl is-active userloop

# 5. Apache 反代（幂等追加到两个 vhost 的 </VirtualHost> 前）
if ! grep -q "ProxyPass /userloop/" "$CONF"; then
  cp "$CONF" "$CONF.bak-before-userloop"
  "$APP_DIR/.venv/bin/python" - "$CONF" "$APP_DIR/deploy/apache-userloop-snippet.conf" <<'PYEOF'
import sys

path, snippet_path = sys.argv[1], sys.argv[2]
block = open(snippet_path, encoding="utf-8").read()
src = open(path, encoding="utf-8").read()
out = []
for line in src.splitlines(keepends=True):
    if "</VirtualHost>" in line:
        out.append(block)
    out.append(line)
open(path, "w", encoding="utf-8").write("".join(out))
print("apache vhost updated")
PYEOF
fi

apachectl configtest && systemctl reload httpd
echo "DEPLOY-DONE: https://nownexts.com/userloop/"

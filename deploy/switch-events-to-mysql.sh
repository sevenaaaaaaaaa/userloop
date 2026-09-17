#!/usr/bin/env bash
# 把 UserLoop 的 events 切到独立 MySQL 实例（:3307），并迁移 SQLite 存量
set -euo pipefail
APP=/www/wwwroot/userloop
PW=$(cat /tmp/ul_mysql_pw.txt)

python3 - "$PW" <<'PYEOF'
import json
import sys

pw = sys.argv[1]
p = "/www/wwwroot/userloop/data/config.json"
cfg = json.load(open(p))
cfg["storage"] = {"events": {"backend": "mysql", "mysql": {
    "enabled": True, "host": "127.0.0.1", "port": 3307,
    "user": "userloop", "password": pw, "database": "userloop", "pool_size": 5}}}
json.dump(cfg, open(p, "w"), indent=2, ensure_ascii=False)
print("config patched: storage.events -> mysql 127.0.0.1:3307")
PYEOF

cd "$APP"
uv pip install -q aiomysql --python .venv/bin/python
.venv/bin/python -c "import aiomysql; print('aiomysql installed')"

echo "--- migrate sqlite -> mysql ---"
.venv/bin/python -m userloop.cli db migrate-events 2>&1 | tail -3

echo "--- restart service ---"
systemctl restart userloop
sleep 3
systemctl is-active userloop

echo "--- backend status ---"
.venv/bin/userloop db status 2>&1 | tail -3

rm -f /tmp/ul_mysql_pw.txt
echo "DONE"

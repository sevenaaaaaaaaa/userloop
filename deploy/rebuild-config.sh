#!/usr/bin/env bash
# 重建 UserLoop config.json（从各处可信来源汇总，绝不凭空编造凭据）
set -euo pipefail
CFG=/www/wwwroot/userloop/data/config.json
MYSQL_CRED=/www/server/userloop-mysql/credentials.txt
OF_PLUGIN_CFG=/www/wwwroot/nownexts_com/data/plugins/userloop-tracker/config.json
BRIDGE_CFG=/www/wwwroot/nownexts_com/data/plugins/userloop-bridge/config.json
INBOUND=/www/wwwroot/nownexts_com/data/inbound.json

cp -f "$CFG" "$CFG.broken-$(date +%s)" || true

python3 - "$MYSQL_CRED" "$OF_PLUGIN_CFG" "$BRIDGE_CFG" "$INBOUND" <<'PYEOF'
import json
import sys

mysql_cred, of_plugin, bridge_cfg, inbound = sys.argv[1:5]

def load(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default

# 1) api_token / bridge_token / 抑制与入站密钥：从桥接方既有配置取，保证一致
of_track = load(of_plugin, {}) or {}
bridge = load(bridge_cfg, {}) or {}
conns = load(inbound, []) or []
of_conn = next((c for c in conns if c.get("id") == "userloop"), {}) or {}
mysql_line = [l for l in open(mysql_cred, encoding="utf-8").read().splitlines() if l.startswith("userloop=")]
mysql_pw = mysql_line[0].split("=", 1)[1] if mysql_line else ""

api_token = of_track.get("api_token", "")
bridge_token = bridge.get("bridge_token", "")

cfg = {
    "api_token": api_token,
    "default_webhook": None,
    "smtp": {"host": "127.0.0.1", "port": 25, "user": "", "password": "",
             "starttls": False, "from": "noreply@mail.nownexts.com"},
    "integrations": {
        "openflow": {"base_url": "https://nownexts.com",
                     "webhook_secret": of_conn.get("secret", ""),
                     "inbound_id": "userloop",
                     "bridge_token": bridge_token},
        "mflow": {"base_url": None, "password": ""},
    },
    "ai": {"base_url": "https://api.deepseek.com/v1",
           "api_key": "sk-1e09ba07c1cd42159d23182fa94dd9e2",
           "model": "deepseek-chat", "temperature": 0.8, "max_tokens": 300,
           "brain": {"enabled": True, "batch_size": 3, "min_gap_hours": 24, "weekly_cap": 3,
                     "daily_budget": 200, "auto_execute_medium": False,
                     "quiet_hours": [22, 8], "tz_offset_hours": 8}},
    "storage": {"events": {"backend": "mysql", "mysql": {
        "enabled": True, "host": "127.0.0.1", "port": 3307, "user": "userloop",
        "password": mysql_pw, "database": "userloop", "pool_size": 5}}},
    "touch": {"track_secret": api_token, "public_base": "https://nownexts.com/userloop",
              "brand": "芭乐派 · UserLoop", "signature": "— UserLoop 全域用户运营",
              "footer": "你收到这封邮件是因为你曾注册或订阅我们的服务。",
              "email": {"driver": "openflow", "bridge_url": "https://nownexts.com",
                        "bridge_token": bridge_token}},
}

with open("/www/wwwroot/userloop/data/config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("config rebuilt: api_token=%s..., bridge_token=%s..., mysql_pw=%s..., inbound_secret=%s..."
      % (api_token[:4] or "EMPTY", bridge_token[:4] or "EMPTY", (mysql_pw[:2] + "***") if mysql_pw else "EMPTY",
         (of_conn.get("secret", "")[:4] or "EMPTY")))
PYEOF

chmod 600 "$CFG"
python3 -c "import json; d=json.load(open('$CFG', encoding='utf-8')); print('JSON OK, keys:', sorted(d.keys()))"

systemctl restart userloop
for _ in $(seq 1 15); do systemctl is-active --quiet userloop && break; sleep 1; done
systemctl is-active userloop || { journalctl -u userloop -n 20 --no-pager; exit 1; }

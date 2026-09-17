#!/usr/bin/env bash
# 触点闭环验收：真实 HTML 邮件 → 打开/点击回流 → 退订 → 抑制名单生效
set -euo pipefail
APP=/www/wwwroot/userloop
cd "$APP"

echo "=== 1. 触发 touch.email（经 OpenFlow 桥：HTML + 追踪 + 退订）==="
.venv/bin/python - <<'PYEOF'
import asyncio
from userloop.actions.executors import ExecutorContext, execute_action
from userloop.config import load_config
from userloop.core.store import Store

async def main():
    cfg = load_config()
    s = Store(cfg["db_path"], cfg)
    await s.connect()
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    ctx.store = s
    user = await s.upsert_user("touch_demo", email="touch-demo@nownexts.com")
    await s.update_user(user["id"], stage="activated")
    user = await s.get_user(user["id"])
    action = {"type": "touch.email",
              "payload": {"subject": "你还有一步没完成",
                          "text": "上次你停在了创建第一个项目这一步。\n今天花 1 分钟就能搞定。",
                          "cta_text": "继续完成", "cta_url": "https://nownexts.com/product"}}
    res = await execute_action(ctx, action, {"id": "loop_touch_demo", "template_id": "ai.send_email"}, user)
    print("deliver:", {k: res.get(k) for k in ("ok", "channel", "ref", "degraded", "note", "via", "dry_run")})
    await s.close()

asyncio.run(main())
PYEOF

echo "=== 2. 邮件是否真发出（maillog）==="
grep -A2 "touch-demo@nownexts.com" /var/log/maillog 2>/dev/null | grep "status=sent" | tail -1 | head -c 200 || echo "（未找到投递记录）"

echo "=== 3. 模拟收件人打开 + 点击（走 UserLoop 自持追踪）==="
CJ=/tmp/tc.txt; rm -f $CJ
curl -s -c $CJ -X POST http://127.0.0.1:8600/userloop/api/login -H "Content-Type: application/json" \
  -d '{"username":"Seven","password":"ULkp7-mzx2-q94n"}' -o /dev/null
UID_=$(curl -s -b $CJ "http://127.0.0.1:8600/userloop/api/v1/users?limit=200" | \
  .venv/bin/python -c "import json,sys; print(next((u['id'] for u in json.load(sys.stdin)['users'] if u['distinct_id']=='touch_demo'), ''))")
TOK=$(.venv/bin/python - "$UID_" <<'PYEOF'
import sys
from userloop.config import load_config
from userloop.touch.base import make_token
cfg = load_config()
secret = str((cfg.get("touch") or {}).get("track_secret") or cfg.get("api_token"))
print(make_token(secret, sys.argv[1], "loop_touch_demo", {"t": "ai.send_email", "g": "activation"}))
PYEOF
)
curl -s -o /dev/null -w "open.gif: %{http_code}\n" "http://127.0.0.1:8600/userloop/t/e/open.gif?t=$TOK"
curl -s -o /dev/null -w "click: %{http_code} -> %{redirect_url}\n" "http://127.0.0.1:8600/userloop/t/e/click?t=$TOK&u=https://nownexts.com/product"
curl -s -o /dev/null -w "unsubscribe: %{http_code}\n" "http://127.0.0.1:8600/userloop/t/e/unsubscribe?t=$TOK"

echo "=== 4. 回执是否进入旅程事件 ==="
curl -s -b $CJ "http://127.0.0.1:8600/userloop/api/v1/events?limit=30" | \
  .venv/bin/python -c "import json,sys; ev=[e['event'] for e in json.load(sys.stdin)['events']]; print([x for x in ev if x.startswith('email_')])"

echo "=== 5. 抑制名单是否生效（OpenFlow 侧）==="
.venv/bin/python - <<'PYEOF'
import json
p = "/www/wwwroot/nownexts_com/data/email/suppression.json"
try:
    d = json.load(open(p, encoding="utf-8"))
    hit = "touch-demo@nownexts.com" in json.dumps(d, ensure_ascii=False)
    print("suppression.json 命中 touch-demo:", hit)
except FileNotFoundError:
    print("（OpenFlow 抑制名单文件尚未生成）")
PYEOF

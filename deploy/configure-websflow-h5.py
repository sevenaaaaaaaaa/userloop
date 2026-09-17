import json

PW = open("/tmp/wf_bot_pw.txt", encoding="utf-8").read().split(": ", 1)[1].strip()
p = "/www/wwwroot/userloop/data/config.json"
cfg = json.load(open(p, encoding="utf-8"))
cfg["touch"]["h5"] = {
    "provider": "websflow",                                  # websflow | builtin
    "api_base": "https://nownexts.com/webflow/api",          # 服务端内部也可用 http://127.0.0.1:3001/api
    "public_base": "https://nownexts.com/webflow/p",
    "project_mode": "h5",
    "email": "userloop-bot@nownexts.com",
    "password": PW,
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("touch.h5 -> websflow configured")

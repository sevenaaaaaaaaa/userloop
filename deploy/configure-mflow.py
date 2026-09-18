import json

PW = open("/tmp/mflow_bot_pw.txt", encoding="utf-8").read().strip()
p = "/www/wwwroot/userloop/data/config.json"
cfg = json.load(open(p, encoding="utf-8"))
cfg["integrations"]["mflow"] = {
    "base_url": "https://nownexts.com/mflow",     # 线上 API（不依赖本地服务）
    "auth_mode": "login",
    "username": "userloop-bot",
    "password": PW,
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("integrations.mflow -> https://nownexts.com/mflow (login/session)")

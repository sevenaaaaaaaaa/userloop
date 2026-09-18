import json
import secrets
import shutil
import subprocess
import sys

# 在 MFlow 多用户库中新增服务账号（保留原有账号，先备份）
AUTH = "/www/wwwroot/mflow/run/auth.json"
PY = "/www/wwwroot/userloop/.venv/bin/python"

shutil.copy(AUTH, AUTH + ".bak-userloop-bot")

users = json.load(open(AUTH, encoding="utf-8"))
if any(u.get("username") == "userloop-bot" for u in users):
    print("SKIP: userloop-bot already exists")
    sys.exit(0)

pw = secrets.token_urlsafe(12)
hashed = subprocess.run(
    [PY, "-c",
     "import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt(rounds=10)).decode())",
     pw],
    capture_output=True, text=True, check=True).stdout.strip()

users.append({"username": "userloop-bot", "hash": hashed,
              "name": "UserLoop 服务账号", "role": "operator"})
with open(AUTH, "w", encoding="utf-8") as f:
    json.dump(users, f, ensure_ascii=False, indent=1)
print("ADDED userloop-bot")
with open("/tmp/mflow_bot_pw.txt", "w", encoding="utf-8") as f:
    f.write(pw)

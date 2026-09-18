import asyncio
import json

import httpx

from userloop.config import load_config
from userloop.touch.websflow import track_back_snippet


async def main() -> None:
    cfg = load_config()
    h5 = cfg["touch"].get("h5") or {}
    base = "http://127.0.0.1:3001/api"
    ul = (cfg["touch"].get("public_base") or "https://nownexts.com/userloop").rstrip("/")
    async with httpx.AsyncClient(timeout=15) as client:
        login = await client.post(f"{base}/users/login",
                                  json={"email": h5["email"], "password": h5["password"]})
        token = (login.json() or {}).get("token")
        if not token:
            print("登录失败:", login.status_code, login.text[:120])
            return
        headers = {"Authorization": f"Bearer {token}"}
        projects = (await client.get(f"{base}/projects", headers=headers)).json().get("projects", [])
        for p in projects:
            if not p.get("published"):
                continue
            pid = p["id"]
            detail = (await client.get(f"{base}/projects/{pid}", headers=headers)).json().get("project", {})
            data = detail.get("data") or {}
            data.setdefault("global", {}).setdefault("tracking", {})["custom"] = track_back_snippet(ul)
            r = await client.put(f"{base}/projects/{pid}",
                                 json={"name": detail.get("name"), "data": data}, headers=headers)
            print("已注入:", p.get("name"), "->", r.status_code)


asyncio.run(main())

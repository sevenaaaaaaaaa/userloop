"""登录体系：auth.json bcrypt 多用户 + 内存 session —— 对齐 MFlow console 模式.

auth.json（用户库，迁移自 OpenFlow/MFlow 同名同密码）：
  [{"username": "...", "hash": "$2y$...", "name": "...", "role": "admin|marketing|sales|operator|viewer"}]

- 文件不存在/为空 → 免登录（本地开发模式）
- SESSIONS 内存态：进程重启即登出（与 MFlow 一致）
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

COOKIE_NAME = "userloop_session"

# 登录名专属后缀：Seven@userloop / Seven@ul 均可，便于密码管理器独立保存、不与主站混淆
SUFFIXES = ("@userloop", "@ul")


def normalize_username(username: str) -> str:
    """剥离 UserLoop 专属后缀，得到账号库中的规范用户名。"""
    name = (username or "").strip()
    low = name.lower()
    for suf in SUFFIXES:
        if low.endswith(suf):
            return name[: -len(suf)].strip()
    return name


def display_username(username: str, suffix: str = "@userloop") -> str:
    """展示/预填用：规范用户名 → 带后缀形式。"""
    base = normalize_username(username)
    return f"{base}{suffix}" if base else ""


def load_users(data_dir: str) -> list[dict]:
    path = os.path.join(data_dir, "auth.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            users = json.load(f)
        return users if isinstance(users, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def auth_enabled(data_dir: str) -> bool:
    return bool(load_users(data_dir))


def verify_user(data_dir: str, username: str, password: str) -> dict | None:
    wanted = normalize_username(username)
    for rec in load_users(data_dir):
        if str(rec.get("username", "")).lower() != wanted.lower():
            continue
        h = rec.get("hash", "")
        if not h:
            continue
        try:
            import bcrypt

            if bcrypt.checkpw(password.encode(), h.encode()):
                return {"username": rec["username"], "name": rec.get("name", rec["username"]),
                        "role": rec.get("role", "member"), "login": display_username(rec["username"])}
        except Exception:  # noqa: BLE001 —— bcrypt 环境异常按失败处理
            return None
    return None


class Sessions:
    """sid -> user dict（内存态）。"""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def create(self, user: dict) -> str:
        sid = secrets.token_urlsafe(32)
        self._store[sid] = user
        return sid

    def get(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        return self._store.get(sid)

    def drop(self, sid: str | None) -> None:
        if sid:
            self._store.pop(sid, None)


def sid_from_cookie(cookie_header: str) -> str | None:
    for part in (cookie_header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k == COOKIE_NAME:
                return v
    return None

"""MFlow 适配器 —— 走其**线上 API**（对齐 inFlow docs/04 §2/§4）.

MFlow 现实约束（已实测）：
- 无 API Token：auth.json（bcrypt 多用户）→ 必须 `POST {base}/api/login` 换 session cookie
- `/api/loop/create` **必须带 item_id**（正则 `[a-z0-9][a-z0-9-]{1,78}`）；入队后其 agent 异步产稿并自推进状态机
- 发布永远停在人工授权后：本适配器只创建 Loop/草稿，绝不触发发布

动作：
- mflow.create_content : 建 item + 入队产稿 loop（异步）
- mflow.register_topic : 登记进 MFlow 12 阶段状态机
- mflow.content_status : 查询 loop 状态（轮询用）
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext, render_text

# 进程内 session 缓存：base -> (cookie, expire_ts)
_SESSION: dict[str, tuple[str, float]] = {}


def _client(transport: Any = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, transport=transport)


def slugify_item_id(raw: str) -> str:
    """把任意字符串转成 MFlow 合法 item_id（[a-z0-9-]，首字符非 -）。"""
    s = re.sub(r"[^a-z0-9-]+", "-", str(raw).lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s or not re.match(r"^[a-z0-9]", s):
        s = "ul-" + s
    return s[:78] or "ul-item"


async def login(cfg: dict, transport: Any = None, force: bool = False) -> str | None:
    """登录取 session cookie（缓存 5 小时；失败返回 None）。"""
    base = str(cfg.get("base_url") or "").rstrip("/")
    user = str(cfg.get("username") or "")
    pw = str(cfg.get("password") or "")
    if not (base and user and pw):
        return None
    cached = _SESSION.get(base)
    if cached and not force and cached[1] > time.time():
        return cached[0]
    try:
        async with _client(transport) as client:
            resp = await client.post(f"{base}/api/login", json={"username": user, "password": pw})
        if resp.status_code == 200:
            cookie = resp.cookies.get("mflow_session") or resp.headers.get("set-cookie", "").split("mflow_session=")[-1].split(";")[0]
            if cookie:
                _SESSION[base] = (cookie, time.time() + 5 * 3600)
                return cookie
    except Exception:  # noqa: BLE001
        return None
    return None


async def _call(cfg: dict, method: str, path: str, body: dict | None = None,
                transport: Any = None) -> dict[str, Any]:
    """带 session 的调用；401/403 自动重登一次。"""
    base = str(cfg.get("base_url") or "").rstrip("/")
    for attempt in (0, 1):
        cookie = await login(cfg, transport=transport, force=attempt == 1)
        if not cookie:
            return {"ok": False, "error": "MFlow 登录失败（检查 username/password）"}
        try:
            async with _client(transport) as client:
                resp = await client.request(method, f"{base}{path}", json=body,
                                            headers={"Cookie": f"mflow_session={cookie}"})
            if resp.status_code in (401, 403) and attempt == 0:
                continue
            data = {}
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 200:
                return {"ok": True, **data}
            return {"ok": False, "error": data.get("error") or f"HTTP {resp.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"MFlow 调用失败：{exc}"}
    return {"ok": False, "error": "MFlow 鉴权失败"}


async def execute(ctx: ExecutorContext, action: dict, loop: dict, user: dict) -> dict[str, Any]:
    atype = action.get("type", "")
    payload = action.get("payload") or {}
    while isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    cfg = (ctx.config.get("integrations") or {}).get("mflow") or {}
    base = payload.get("base_url") or cfg.get("base_url")
    if not base:
        return {"ok": True, "dry_run": True, "note": "mflow.base_url 未配置；仅落 outbox"}
    cfg = {**cfg, "base_url": base}

    topic = render_text(payload.get("topic") or "UserLoop 旅程信号", user, loop)
    brief = render_text(payload.get("brief") or payload.get("text") or "", user, loop)
    transport = payload.get("_transport")
    result: dict[str, Any] = {"type": atype, "base_url": base}

    try:
        if atype == "mflow.create_content":
            # item_id 必填：优先显式传入，否则由 loop/user/主题派生（稳定可追溯）
            item_id = slugify_item_id(payload.get("item_id")
                                      or f"ul-{loop.get('template_id') or loop.get('id') or 'journey'}-"
                                         f"{user.get('id', '')[-6:]}-{int(time.time()) % 100000}")
            r1 = await _call(cfg, "POST", "/api/item/upsert",
                             {"id": item_id, "category": payload.get("type", "blog"),
                              "title": topic[:120]}, transport=transport)
            res = await _call(cfg, "POST", "/api/loop/create",
                              {"item_id": item_id, "topic": topic[:300], "brief": brief[:800],
                               "template_id": payload.get("template_id") or "",
                               "type": payload.get("type", "blog"), "lang": payload.get("lang", "zh")},
                              transport=transport)
            if res.get("ok"):
                # 不调用 item/advance：MFlow 自身会在产稿过程中推进状态机（S3→S4-qa），
                # 外部再推进会造成状态冲突。我们只登记 item + 入队 loop。
                result.update(ok=True, ref=res.get("id"), item_id=item_id,
                              queued=res.get("queued", True), register_ok=r1.get("ok", False))
            else:
                result.update(ok=False, error=res.get("error"))
        elif atype == "mflow.register_topic":
            item_id = slugify_item_id(payload.get("item_id") or f"ul-topic-{int(time.time())}")
            res = await _call(cfg, "POST", "/api/item/upsert",
                              {"id": item_id, "category": payload.get("type", "blog"), "title": topic[:120]},
                              transport=transport)
            result.update(ok=bool(res.get("ok")), ref=item_id, error=res.get("error"))
        elif atype == "mflow.content_status":
            lid = str(payload.get("loop_id") or loop.get("id") or "")
            data = await _call(cfg, "GET", f"/api/loop/detail?id={lid}", transport=transport)
            result.update(ok=bool(data.get("ok")), status=data.get("status"), detail=data)
        else:
            result.update(ok=False, error=f"unknown mflow action: {atype}")
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, error=str(exc))
    return result

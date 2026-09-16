"""MFlow 适配器 —— 对齐 inFlow docs/04 §2/§4：

- mflow.create_content : POST {base}/api/loop/create {topic, brief, template_id?, max_rounds:3}
- mflow.register_topic : POST {base}/api/item/upsert（登记进 MFlow 12 阶段状态机）

设计约束：发布永远停在人工授权后 —— 只创建 Loop/草稿，绝不触发发布。
MFlow 无 API Token 机制（密码换 session），默认用 X-MFlow-Password 头（与 inFlow
mflow-source 插件同款），config 可切换 login 模式。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext, render_text


def _client(transport: Any = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, transport=transport)


def _headers(cfg: dict, payload: dict) -> dict[str, str]:
    password = payload.get("password") or cfg.get("password") or ""
    if payload.get("auth_mode", cfg.get("auth_mode", "password_header")) == "login":
        return {}  # login 模式走 session，简化期仅支持 header 模式
    return {"X-MFlow-Password": password}


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
        return {"ok": True, "dry_run": True, "note": "mflow.base_url not configured; outbox only"}

    topic = render_text(payload.get("topic") or "UserLoop 旅程信号", user, loop)
    brief = render_text(payload.get("brief") or payload.get("text") or "", user, loop)
    transport = payload.get("_transport")
    headers = _headers(cfg, payload)
    result: dict[str, Any] = {"type": atype}

    try:
        if atype == "mflow.create_content":
            body = {
                "topic": topic,
                "brief": brief,
                "template_id": payload.get("template_id"),
                "max_rounds": int(payload.get("max_rounds", 3)),
                "meta": {"source": "userloop", "loop_id": loop.get("id"), "user_stage": user.get("stage")},
            }
            async with _client(transport) as client:
                resp = await client.post(f"{base.rstrip('/')}/api/loop/create", json=body, headers=headers)
            ok = 200 <= resp.status_code < 300
            data = {}
            if ok:
                try:
                    data = resp.json()
                except ValueError:
                    pass
            result.update(ok=ok, status_code=resp.status_code, ref=data.get("id"))
        elif atype == "mflow.register_topic":
            body = {"item_id": payload.get("item_id"), "topic": topic, "brief": brief,
                    "source_ref": loop.get("id")}
            async with _client(transport) as client:
                resp = await client.post(f"{base.rstrip('/')}/api/item/upsert", json=body, headers=headers)
            result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code)
        else:
            result.update(ok=False, error=f"unknown mflow action: {atype}")
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, error=str(exc))
    return result

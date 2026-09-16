"""MA（营销自动化）对接适配器 —— 对接客户现有 MA，不绑定平台.

动作族：
  ma.hubspot.contact_upsert : HubSpot contacts upsert（email 幂等）+ 旅程阶段 → lifecycle_stage
  ma.hubspot.note           : 给联系人写入旅程触达备注（自定义属性 userloop_last_touch）
  ma.webhook                : 任意客户 MA 的 webhook 入口（n8n/Zapier/自研 MA 均可，可选 HMAC）

生命周期映射（可在 config integrations.ma.hubspot.lifecycle_map 覆盖）：
  visitor→subscriber, signup→lead, activated→marketingqualifiedlead,
  paying→customer, retained→customer, advocate→promoter
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext, render_text

DEFAULT_LIFECYCLE = {
    "visitor": "subscriber", "signup": "lead", "activated": "marketingqualifiedlead",
    "paying": "customer", "retained": "customer", "advocate": "promoter",
}


def _parse_payload(action: dict) -> dict:
    payload = action.get("payload") or {}
    while isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    return payload


def _client(hub: dict, transport: Any = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, base_url=hub.get("base_url", "https://api.hubapi.com"),
                             transport=transport)


def _headers(hub: dict, payload: dict) -> dict[str, str]:
    token = payload.get("token") or hub.get("token") or ""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _lifecycle(hub: dict, stage: str) -> str:
    mapping = hub.get("lifecycle_map") or {}
    return mapping.get(stage) or DEFAULT_LIFECYCLE.get(stage, "subscriber")


async def execute(ctx: ExecutorContext, action: dict, loop: dict, user: dict) -> dict[str, Any]:
    atype = action.get("type", "")
    payload = _parse_payload(action)
    cfg = (ctx.config.get("integrations") or {}).get("ma") or {}
    hub = cfg.get("hubspot") or {}
    result: dict[str, Any] = {"type": atype}
    transport = payload.get("_transport")
    email = user.get("email") or ""

    try:
        if atype.startswith("ma.hubspot"):
            if not (hub.get("token") or payload.get("token")):
                return {"ok": True, "dry_run": True, "note": "ma.hubspot token not configured; outbox only"}
            if not email:
                return {"ok": False, "error": "no recipient email for hubspot sync"}
            headers = _headers(hub, payload)

            if atype == "ma.hubspot.contact_upsert":
                body = {"properties": {
                    "email": email,
                    "lifecycle_stage": _lifecycle(hub, user.get("stage", "visitor")),
                    "userloop_stage": user.get("stage", ""),
                    "userloop_loop_id": loop.get("id") or "",
                    "userloop_last_touch": render_text(payload.get("note") or payload.get("subject") or "旅程触达", user, loop),
                }}
                async with _client(hub, transport) as client:
                    resp = await client.post("/crm/v3/objects/contacts", params={"idProperty": "email"},
                                             json=body, headers=headers)
                ok = 200 <= resp.status_code < 300
                ref = (resp.json() or {}).get("id") if ok else None
                result.update(ok=ok, status_code=resp.status_code, ref=ref)
            else:  # ma.hubspot.note
                body = {"properties": {
                    "userloop_last_touch": render_text(
                        payload.get("text") or payload.get("subject") or "旅程触达", user, loop)},
                }
                async with _client(hub, transport) as client:
                    resp = await client.patch(f"/crm/v3/objects/contacts/{email}",
                                              params={"idProperty": "email"},
                                              json=body, headers=headers)
                result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code)

        elif atype == "ma.webhook":
            url = payload.get("url") or cfg.get("webhook_url")
            if not url:
                return {"ok": True, "dry_run": True, "note": "ma.webhook url not configured; outbox only"}
            body = {"event": payload.get("event") or "userloop_journey",
                    "user": {"id": user.get("id"), "email": email, "stage": user.get("stage")},
                    "stage": user.get("stage"), "loop_id": loop.get("id"),
                    "template_id": loop.get("template_id"),
                    "message": render_text(payload.get("text") or payload.get("subject") or "旅程触达", user, loop)}
            raw = json.dumps(body, ensure_ascii=False).encode()
            headers = {"Content-Type": "application/json"}
            secret = payload.get("secret") or cfg.get("webhook_secret") or ""
            if secret:
                headers["X-MA-Signature"] = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            async with httpx.AsyncClient(timeout=10, transport=transport) as client:
                resp = await client.post(url, content=raw, headers=headers)
            result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code)
        else:
            result.update(ok=False, error=f"unknown ma action: {atype}")
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, error=str(exc))
    return result

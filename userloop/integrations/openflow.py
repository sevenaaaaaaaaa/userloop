"""OpenFlow 适配器 —— 对齐 inFlow docs/04 §3/§4：

- openflow.webhook_insight : POST {base}/api/webhook.php（InboundReceiver，X-Inbound-Signature=HMAC-SHA256(rawBody, secret)）
- openflow.automation      : POST {base}/{path}（连接器式调用，Bearer 鉴权）
- openflow.plugin_api      : POST {base}/api/plugin/userloop/{method}

总原则：零改 OpenFlow 内核，全部走官方扩展点；失败不阻塞 UserLoop 闭环。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext, render_text


def _client(transport: Any = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10, transport=transport)


def _insight_body(action: dict, loop: dict, user: dict, text: str, subject: str) -> dict:
    """扁平 body（对齐 InboundReceiver 映射：cdp_event 取 event/visitor_id，其余透传为 props）"""
    payload = action.get("_payload") or {}
    trigger = loop.get("trigger") or {}
    if isinstance(trigger, str):
        try:
            trigger = json.loads(trigger)
        except json.JSONDecodeError:
            trigger = {}
    return {
        "event": payload.get("event") or trigger.get("event") or "userloop_signal",
        "visitor_id": payload.get("visitor_id") or user.get("distinct_id") or user.get("id"),
        "message": text,
        "subject": subject,
        "loop_id": loop.get("id"),
        "template_id": loop.get("template_id"),
        "user_stage": user.get("stage"),
        "email": user.get("email") or "",
        "source_system": "userloop",
    }


async def execute(ctx: ExecutorContext, action: dict, loop: dict, user: dict) -> dict[str, Any]:
    atype = action.get("type", "")
    payload = action.get("payload") or {}
    while isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    cfg = (ctx.config.get("integrations") or {}).get("openflow") or {}
    base = payload.get("base_url") or cfg.get("base_url")
    if not base:
        return {"ok": True, "dry_run": True, "note": "openflow.base_url not configured; outbox only"}

    subject = render_text(payload.get("subject") or "UserLoop 信号", user, loop)
    text = render_text(payload.get("text") or payload.get("body") or "UserLoop 信号", user, loop)
    transport = payload.get("_transport")
    result: dict[str, Any] = {"type": atype}

    try:
        if atype == "openflow.webhook_insight":
            action = {**action, "_payload": payload}
            body = _insight_body(action, loop, user, text, subject)
            raw = json.dumps(body, ensure_ascii=False).encode()
            secret = payload.get("webhook_secret") or cfg.get("webhook_secret") or ""
            sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            inbound_id = payload.get("inbound_id") or cfg.get("inbound_id") or ""
            headers = {"Content-Type": "application/json", "X-Inbound-Signature": sig}
            if inbound_id:
                headers["X-Inbound-Id"] = inbound_id
            async with _client(transport) as client:
                resp = await client.post(f"{base.rstrip('/')}/api/webhook.php", content=raw, headers=headers)
            result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code,
                          ref=f"inbound:{inbound_id or 'default'}")
        elif atype == "openflow.automation":
            # 默认走桥接插件的 /automation（内部调 flow_handle：CDP + 自动化 + 画布）
            path = payload.get("path", "/api/plugin/userloop-bridge/automation")
            token = payload.get("token") or cfg.get("token") or ""
            body = {
                "event": payload.get("event") or "userloop_journey",
                "visitor_id": user.get("distinct_id") or user.get("id"),
                "email": user.get("email") or "",
                "loop_id": loop.get("id"), "template_id": loop.get("template_id"),
                "message": text, "extra": payload.get("data", {}),
            }
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with _client(transport) as client:
                resp = await client.post(f"{base.rstrip('/')}{path}", json=body, headers=headers)
            result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code)
        elif atype == "openflow.plugin_api":
            method = payload.get("method", "insights")
            token = payload.get("token") or cfg.get("token") or ""
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with _client(transport) as client:
                resp = await client.post(
                    f"{base.rstrip('/')}/api/plugin/userloop/{method.lstrip('/')}",
                    json={"loop_id": loop.get("id"), "user_id": user.get("id"), "message": text},
                    headers=headers,
                )
            result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code)
        else:
            result.update(ok=False, error=f"unknown openflow action: {atype}")
    except Exception as exc:  # noqa: BLE001
        import traceback

        tb = traceback.extract_tb(__import__("sys").exc_info()[2])
        result.update(ok=False, error=f"{exc} @ {tb[-1].filename.split('/')[-1]}:{tb[-1].lineno}")
    return result

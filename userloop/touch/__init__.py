"""触点调度：把 `touch.<channel>` 动作交给驱动注册表执行（统一结果结构）."""

from __future__ import annotations

from typing import Any

from userloop.touch import drivers as _drivers  # noqa: F401  （导入即注册）
from userloop.touch.base import TouchSpec, get


async def dispatch(ctx: Any, action: dict, loop: dict, user: dict, store: Any = None) -> dict[str, Any]:
    """action.type = touch.<channel>；payload 承载内容槽位与追踪参数。"""
    atype = str(action.get("type", ""))
    channel = atype.split(".", 1)[1] if "." in atype else "email"
    payload = action.get("payload") or {}
    while isinstance(payload, str):
        try:
            import json

            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}

    driver = get(channel)
    if driver is None:
        return {"ok": False, "type": atype, "channel": channel,
                "error": f"未注册的触点渠道：{channel}（可用：{', '.join(c.channel for c in __import__('userloop.touch.base', fromlist=['_REGISTRY'])._REGISTRY.values())}）"}

    spec = TouchSpec.from_payload(payload, channel, loop,
                                  template_id=str(payload.get("template_id") or loop.get("template_id") or ""))
    if store is not None and not hasattr(ctx, "store"):
        ctx.store = store  # 驱动里按身份挑渠道标识
    result = await driver.deliver(spec, user, ctx)
    out = {"type": atype, "channel": channel, "spec": {"title": spec.title, "cta": spec.cta_text}}
    out.update(result.as_dict())
    return out

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

    # A/B 版式实验：稳定分桶 → 覆盖内容槽位（闭环：已 promote 则只发 winner）
    ab: dict[str, Any] | None = None
    if store is not None:
        try:
            from userloop.experiments import engine as ab_engine

            picked = await ab_engine.pick(store, channel, spec.template_id, user["id"], spec.loop_id)
            if picked:
                exp, variant = picked
                variant = {**variant, "_experiment": exp["id"]}
                spec = ab_engine.apply_variant(spec, variant)
                spec.loop_id = spec.loop_id or loop.get("id")
                ab = {"experiment": exp["id"], "variant": variant["id"], "variant_name": variant.get("name", "")}
        except Exception:  # noqa: BLE001 —— 实验异常不得影响正常交付
            ab = None

    result = await driver.deliver(spec, user, ctx)
    out = {"type": atype, "channel": channel, "spec": {"title": spec.title, "cta": spec.cta_text}}
    if ab:
        out["ab"] = ab
    out.update(result.as_dict())
    return out

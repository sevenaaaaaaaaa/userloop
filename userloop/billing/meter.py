"""用量计量与配额门（默认只记账不拦截；billing.enforce=true 才 429）。"""

from __future__ import annotations

from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store

METERS = ("events", "ai_calls", "touches", "agent_campaigns", "agent_tasks")

DEFAULT_QUOTAS = {
    "events": 100_000,
    "ai_calls": 500,
    "touches": 2_000,
    "agent_campaigns": 50,
    "agent_tasks": 200,
}


def quotas_of(ctx: ExecutorContext) -> dict[str, int]:
    raw = ((ctx.config.get("billing") or {}).get("quotas")) or {}
    out = dict(DEFAULT_QUOTAS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out:
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    pass
    return out


def enforced(ctx: ExecutorContext) -> bool:
    return bool((ctx.config.get("billing") or {}).get("enforce"))


async def record(store: Store, meter: str, quantity: int = 1, amount: int | None = None) -> int:
    """记用量。`amount` 为 `quantity` 的兼容别名（历史调用混用过两种命名）。"""
    qty = int(amount if amount is not None else quantity)
    if meter not in METERS:
        return 0
    return await store.add_usage(meter, qty)


async def snapshot(store: Store, ctx: ExecutorContext) -> dict[str, Any]:
    used = await store.usage_today()
    limits = quotas_of(ctx)
    rows = []
    for m in METERS:
        u = int(used.get(m, 0))
        lim = limits[m]
        rows.append({"meter": m, "used": u, "quota": lim,
                     "remaining": max(0, lim - u), "pct": round(u / lim * 100, 1) if lim else 0})
    return {"day": __import__("datetime").datetime.utcnow().date().isoformat(),
            "enforce": enforced(ctx), "meters": rows}


async def check(store: Store, ctx: ExecutorContext, meter: str, quantity: int = 1) -> dict[str, Any]:
    """返回 {ok, used, quota, reason}。未开启 enforce 永远 ok。"""
    used = int((await store.usage_today(meter)).get(meter, 0))
    quota = quotas_of(ctx).get(meter, 0)
    if not enforced(ctx):
        return {"ok": True, "used": used, "quota": quota, "enforced": False}
    if used + quantity > quota:
        return {"ok": False, "used": used, "quota": quota, "enforced": True,
                "reason": f"今日 {meter} 配额已用尽（{used}/{quota}）"}
    return {"ok": True, "used": used, "quota": quota, "enforced": True}

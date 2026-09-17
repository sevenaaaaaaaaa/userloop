"""AI 护栏：频控 / 静默期 / 日预算 / 阶段策略（服务端强制，AI 不可绕过）.

对齐 OpenFlow 教训：限额与门禁在服务端执行，AI 只是提案方。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store

DEFAULTS = {
    "min_gap_hours": 24,        # 同用户两次 AI 触达最小间隔
    "margin_hours": 4,          # 频控余量（避免临界抖动）
    "weekly_cap": 3,            # 每用户每周 AI 触达上限
    "daily_budget": 200,        # 全局每日 AI 决策预算（成本上限）
    "quiet_hours": [22, 8],     # 静默期（本地时区小时，[起,止)）
    "tz_offset_hours": 8,       # 默认 Asia/Shanghai
}


def cfg_of(ctx: ExecutorContext) -> dict[str, Any]:
    brain = ((ctx.config.get("ai") or {}).get("brain") or {})
    out = dict(DEFAULTS)
    out.update({k: v for k, v in brain.items() if k in DEFAULTS})
    return out


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    """静默期判断：quiet_hours=[起,止)（本地时区，按 tz_offset_hours 从 UTC 换算）。

    - `[]` 或 `[0, 0]` → 关闭静默期（测试/内部通知场景）
    - 支持跨零点（如 [22, 8]）
    """
    qh = cfg.get("quiet_hours") or []
    if len(qh) != 2 or int(qh[0]) == int(qh[1]):
        return False
    start, end = int(qh[0]), int(qh[1])
    hour = ((now or datetime.utcnow()) + timedelta(hours=int(cfg["tz_offset_hours"]))).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


async def precheck(store: Store, ctx: ExecutorContext, user: dict, force: bool = False) -> dict[str, Any]:
    """返回 {ok, reason}；force=True 时跳过频控与静默期（人工触发）。"""
    cfg = cfg_of(ctx)
    if not force:
        if in_quiet_hours(cfg):
            return {"ok": False, "reason": f"静默期 {cfg['quiet_hours'][0]}:00-{cfg['quiet_hours'][1]}:00"}
        gap = await store.touches_since(user["id"], hours=int(cfg["min_gap_hours"]))
        if gap:
            return {"ok": False, "reason": f"{cfg['min_gap_hours']}h 内已触达 {gap} 次（频控）"}
        week = await store.touches_since(user["id"], hours=24 * 7)
        if week >= int(cfg["weekly_cap"]):
            return {"ok": False, "reason": f"周触达已达上限 {cfg['weekly_cap']} 次"}
    today = await store.ai_decisions_today()
    if today >= int(cfg["daily_budget"]):
        return {"ok": False, "reason": f"每日 AI 决策预算已用尽（{cfg['daily_budget']}）"}
    return {"ok": True, "reason": "ok"}

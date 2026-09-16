"""调度器：到期动作执行 + 滞留扫描（下行旅程）+ 验证窗口检查.

三种职责，皆可独立调用（CLI demo / 测试），也可由 APScheduler 常驻驱动（server 模式）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.core.entities import (
    ActionStatus,
    LoopStatus,
    Stage,
    iso,
)
from userloop.core.journey import stall_breakpoint
from userloop.core.loops import create_loop_from_template
from userloop.core.store import Store, iso_now, pj
from userloop.core.verify import verify_loop
from userloop.core.verify import verify_loop


def _utcnow() -> datetime:
    return datetime.utcnow()


def _ts(dt: datetime) -> str:
    return iso(dt)


async def process_due_actions(store: Store, ctx: ExecutorContext, limit: int = 50) -> list[dict]:
    """执行所有到期 pending 动作；每条 Loop 收敛到 verifying/verified。"""
    due = await store.due_actions(iso(_utcnow()), limit=limit)
    results: list[dict] = []
    for action in due:
        loop = await store.get_loop(action["loop_id"])
        if not loop or loop["status"] not in (LoopStatus.RUNNING, LoopStatus.PENDING):
            continue
        user = await store.get_user(loop["user_id"])
        if not user:
            continue
        result = await execute_action(ctx, action, loop, user)
        await store.update_action(
            action["id"],
            status=ActionStatus.DONE if result.get("ok") else ActionStatus.FAILED,
            executed_at=iso(), result=json.dumps(result, ensure_ascii=False),
        )
        await _mark_loop(store, loop["id"])
        results.append({"action_id": action["id"], "loop_id": action["loop_id"], **result})
    return results


async def _mark_loop(store: Store, loop_id: str) -> None:
    actions = await store.actions_for_loop(loop_id)
    if any(a["status"] == ActionStatus.PENDING for a in actions):
        return
    loop = await store.get_loop(loop_id)
    if not loop or loop["status"] != LoopStatus.RUNNING:
        return
    templates = {t["id"]: t for t in await store.get_templates(enabled_only=False)}
    template = templates.get(loop["template_id"])
    if template and template.get("goal_event"):
        await store.update_loop(loop_id, status=LoopStatus.VERIFYING)
    else:
        await store.update_loop(loop_id, status=LoopStatus.VERIFIED)


async def sweep_inactivity(store: Store) -> list[dict]:
    """下行旅程扫描：滞留用户产生 stall 断点 + Loop（churn 挽回）。"""
    templates = [t for t in await store.get_templates(enabled_only=True)
                 if (t.get("trigger") or {}).get("type") == "inactivity"]
    created: list[dict] = []
    for template in templates:
        trigger = template["trigger"]
        stage = trigger.get("stage") or Stage.ACTIVATED
        days = int(trigger.get("days", 14))
        cutoff = _ts(_utcnow() - timedelta(days=days))
        # 该阶段所有滞留用户
        cur = store.db  # type: ignore[assignment]
        async with cur.execute("SELECT * FROM users WHERE stage=? AND last_seen<?", (stage, cutoff)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for user in rows:
            bp = stall_breakpoint(user, days)
            loop = await create_loop_from_template(
                store, template, user, {"type": "inactivity", "days": days}, {"breakpoint": bp["detail"]}
            )
            if loop:
                created.append(loop)
    return created


async def verify_due_loops(store: Store, limit: int = 50) -> list[dict]:
    """验证窗口到期的 verifying Loop → verdict → feedback。"""
    now_iso = iso(_utcnow())
    cur = store.db  # type: ignore[assignment]
    async with cur.execute(
        "SELECT * FROM loops WHERE status=? AND verify_before<=? LIMIT ?", (LoopStatus.VERIFYING, now_iso, limit)
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    feedbacks: list[dict] = []
    for loop in rows:
        fb = await verify_loop(store, loop)
        if fb:
            feedbacks.append(fb)
    return feedbacks


async def resume_canvas_waits(store: Store, ctx: ExecutorContext) -> list[dict]:
    """Canvas delay 节点到点恢复。"""
    from userloop.core import canvas

    return await canvas.resume_waits(store, ctx)


def build_scheduler(store: Store, ctx: ExecutorContext):
    """APScheduler 常驻编排（server 模式使用）。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(process_due_actions, "interval", seconds=30, args=[store, ctx], id="actions")
    sched.add_job(sweep_inactivity, "interval", minutes=30, args=[store], id="inactivity")
    sched.add_job(verify_due_loops, "interval", minutes=5, args=[store], id="verify")
    sched.add_job(resume_canvas_waits, "interval", seconds=30, args=[store, ctx], id="canvas_waits")
    return sched

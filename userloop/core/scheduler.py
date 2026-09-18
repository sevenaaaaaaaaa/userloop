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
    ctx.store = store          # 让频控台账/回执/硬规则在调度路径同样生效
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


async def prune_data(store: Store, retention_days: int = 180, min_interval_hours: int = 24) -> dict:
    """保留策略（对齐 OpenFlow 教训：事件表不得无限增长）。

    默认每 24h 最多跑一次；事件保留 180 天，噪音类（heartbeat）仅 7 天。
    """
    now = _utcnow()
    last = getattr(store, "_last_prune", None)
    if last and (now - last).total_seconds() < min_interval_hours * 3600:
        return {"skipped": True, "next_in_h": round(min_interval_hours - (now - last).total_seconds() / 3600, 1)}
    result = await store.prune_events(retention_days=retention_days)
    store._last_prune = now  # type: ignore[attr-defined]
    return result


async def recompute_predictions(store: Store, ctx: ExecutorContext) -> int:
    """批量重算预测分数（优先近期活跃的实名用户，成本有界）。"""
    ctx.store = store
    from userloop.predict import engine as predict

    return await predict.run_batch(store, limit=int(((ctx.config.get("predict") or {}).get("batch_size")) or 50))


async def weekly_report(store: Store, ctx: ExecutorContext) -> dict:
    """每周运营周报：生成 + 落盘（配置了 send 则推送）。"""
    ctx.store = store
    from userloop.ai import reporter

    rep = await reporter.build(store, ctx)
    path = await reporter.save(store, ctx, rep)
    sent = await reporter.send(ctx, rep) if (ctx.config.get("report") or {}).get("send") else {}
    return {"name": rep["name"], "path": path, "degraded": rep["degraded"], "sent": sent}


async def run_ai_brain(store: Store, ctx: ExecutorContext) -> list[dict]:
    """AI 大脑批次任务（频次受限：默认每 30 分钟一批，每批成本有界）。"""
    ctx.store = store
    from userloop.ai import brain as brain_mod

    brain_cfg = ((ctx.config.get("ai") or {}).get("brain") or {})
    if not brain_cfg.get("enabled", False):
        return []
    limit = int(brain_cfg.get("batch_size", 5))
    return await brain_mod.run_batch(store, ctx, limit=limit)


def build_scheduler(store: Store, ctx: ExecutorContext):
    """APScheduler 常驻编排（server 模式使用）。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(process_due_actions, "interval", seconds=30, args=[store, ctx], id="actions",
                  max_instances=1, coalesce=True)
    sched.add_job(sweep_inactivity, "interval", minutes=30, args=[store], id="inactivity",
                  max_instances=1, coalesce=True)
    sched.add_job(verify_due_loops, "interval", minutes=5, args=[store], id="verify",
                  max_instances=1, coalesce=True)
    sched.add_job(resume_canvas_waits, "interval", seconds=30, args=[store, ctx], id="canvas_waits",
                  max_instances=1, coalesce=True)
    sched.add_job(prune_data, "interval", hours=6, args=[store], id="prune",
                  max_instances=1, coalesce=True)
    sched.add_job(run_ai_brain, "interval", minutes=30, args=[store, ctx], id="ai_brain",
                  max_instances=1, coalesce=True)
    sched.add_job(recompute_predictions, "interval", minutes=60, args=[store, ctx],
                  id="predictions", max_instances=1, coalesce=True)
    # 每周一 09:00（CST = UTC 01:00）生成运营周报
    from apscheduler.triggers.cron import CronTrigger

    sched.add_job(weekly_report, CronTrigger(day_of_week="mon", hour=1, minute=0, timezone="UTC"),
                  args=[store, ctx], id="weekly_report", max_instances=1, coalesce=True)
    return sched

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
        if result.get("deferred_by_sto"):
            # STO 已把动作重排回 pending：不要覆盖状态（等待最佳时段再发）
            results.append({"action_id": action["id"], "loop_id": action["loop_id"], **result})
            continue
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
    # 被门禁拦下的（频控/退订/STO）不算完成：Loop 置 skipped，避免误判为已验证
    blocked = any(a["status"] == ActionStatus.FAILED for a in actions)
    if blocked:
        await store.update_loop(loop_id, status=LoopStatus.SKIPPED,
                                error="action blocked by gate (frequency/suppression)")
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

    pc = ctx.config.get("predict") or {}
    tz = int(((ctx.config.get("touch") or {}).get("frequency") or {}).get("tz_offset_hours", 8))
    return await predict.run_batch(store, limit=int(pc.get("batch_size") or 50), tz_offset=tz,
                                   data_dir=ctx.config.get("data_dir"))


async def train_predict_models(store: Store, ctx: ExecutorContext) -> dict:
    """每日训练预测模型（样本不足自动跳过，退回规则版）。"""
    ctx.store = store
    from userloop.predict import model as mdl

    out = {}
    for kind in ("churn", "propensity"):
        out[kind] = await mdl.train(store, ctx.config.get("data_dir") or "data", kind)
    return out


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


async def run_all_tenants(ctx: ExecutorContext, job: str) -> dict:
    """按租户遍历执行调度任务（每租户独立数据，互不影响；单租户失败不阻塞其他）。"""
    from userloop.core.tenants import DEFAULT_TENANT, TenantStores, tenant_config

    out: dict[str, Any] = {}
    try:
        tenants = TenantStores(ctx.config)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    for rec in tenants.registry.list():
        tid = rec["id"]
        if not rec.get("enabled", True):
            continue
        try:
            st = await tenants.get(tid)
            tcfg = tenant_config(ctx.config, rec)
            tctx = ExecutorContext(tcfg["data_dir"], tcfg)
            tctx.store = st
            if job == "actions":
                r = await process_due_actions(st, tctx)
                out[tid] = {"actions": len(r)}
            elif job == "canvas":
                out[tid] = {"resumed": len(await resume_canvas_waits(st, tctx))}
            elif job == "inactivity":
                out[tid] = {"loops": len(await sweep_inactivity(st))}
            elif job == "verify":
                out[tid] = {"feedbacks": len(await verify_due_loops(st))}
            elif job == "prune":
                out[tid] = await prune_data(st)
            elif job == "ai":
                out[tid] = {"decisions": len(await run_ai_brain(st, tctx))}
            elif job == "predict":
                from userloop.predict import engine as predict

                pc = tcfg.get("predict") or {}
                out[tid] = {"computed": await predict.run_batch(
                    st, limit=int(pc.get("batch_size") or 50),
                    tz_offset=int(tcfg.get("timezone_offset") or 8), data_dir=tcfg.get("data_dir"))}
            elif job == "train":
                from userloop.predict import model as mdl

                out[tid] = {k: await mdl.train(st, tcfg["data_dir"], k) for k in ("churn", "propensity")}
            elif job == "report":
                out[tid] = await weekly_report(st, tctx)
            elif job == "evolve_daily":
                from userloop.evolve import engine as evo

                tel = await evo.collect_telemetry(st, tctx)
                dg = await evo.diagnose(st, tctx)
                out[tid] = {"identified_rate": tel["identified_rate"],
                            "issues": dg["counts"], "ok": dg["ok"]}
            elif job == "evolve_propose":
                from userloop.evolve import engine as evo

                r = await evo.propose(st, tctx)
                out[tid] = {"proposals": r.get("count", 0), "ok": r.get("ok")}
        except Exception as exc:  # noqa: BLE001
            out[tid] = {"error": str(exc)[:160]}
    await tenants.close_all()
    return out


def build_scheduler(store: Store, ctx: ExecutorContext):
    """APScheduler 常驻编排（server 模式使用）。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler(timezone="UTC")
    # 全部任务按租户遍历（多租户隔离；单租户异常不影响其他）
    sched.add_job(run_all_tenants, "interval", seconds=30, args=[ctx, "actions"], id="actions",
                  max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, "interval", minutes=30, args=[ctx, "inactivity"], id="inactivity",
                  max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, "interval", minutes=5, args=[ctx, "verify"], id="verify",
                  max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, "interval", seconds=30, args=[ctx, "canvas"], id="canvas_waits",
                  max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, "interval", hours=6, args=[ctx, "prune"], id="prune",
                  max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, "interval", minutes=30, args=[ctx, "ai"], id="ai_brain",
                  max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, "interval", minutes=60, args=[ctx, "predict"], id="predictions",
                  max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, "interval", hours=24, args=[ctx, "train"], id="train_models",
                  max_instances=1, coalesce=True)
    # 每周一 09:00（CST = UTC 01:00）生成运营周报
    from apscheduler.triggers.cron import CronTrigger

    sched.add_job(run_all_tenants, CronTrigger(day_of_week="mon", hour=1, minute=0, timezone="UTC"),
                  args=[ctx, "report"], id="weekly_report", max_instances=1, coalesce=True)
    # 自进化：每日遥测+诊断；每周一 02:00 生成改进提案
    sched.add_job(run_all_tenants, CronTrigger(hour=2, minute=0, timezone="UTC"),
                  args=[ctx, "evolve_daily"], id="evolve_daily", max_instances=1, coalesce=True)
    sched.add_job(run_all_tenants, CronTrigger(day_of_week="mon", hour=2, minute=30, timezone="UTC"),
                  args=[ctx, "evolve_propose"], id="evolve_propose", max_instances=1, coalesce=True)
    return sched

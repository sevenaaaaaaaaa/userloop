"""战役编排：目标 → 拆解 → 审批门 → 按角色执行。人只审批，Agent 在白名单内动手。"""

from __future__ import annotations

from typing import Any

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.agents import planner
from userloop.agents.roster import tool_allowed
from userloop.core.store import Store, iso_now, new_id


DEFAULT_BUDGET = {"ai_calls": 20, "touches": 80, "tasks": 20}


def _budget(raw: Any) -> dict[str, int]:
    out = dict(DEFAULT_BUDGET)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out:
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    pass
    return out


async def create_campaign(store: Store, ctx: ExecutorContext, goal: str,
                          target: float | None = None, budget: dict | None = None,
                          recipe: str | None = None) -> dict[str, Any]:
    """拆解目标并落库。含中风险任务 → pending_approval；低风险先跑，中风险等人审。"""
    from userloop.billing import meter as billing

    plan = planner.plan(goal, target, recipe=recipe)
    cid = new_id("camp")
    status = "pending_approval" if plan["needs_approval"] else "running"
    camp = await store.put_campaign({
        "id": cid, "goal": goal, "metric": plan["metric"], "target": plan["target"],
        "status": status, "budget": _budget(budget), "plan": {k: plan[k] for k in
                                                              ("recipe", "name", "roles", "needs_approval")},
    })
    tasks = []
    for spec in plan["tasks"]:
        tid = new_id("tsk")
        row = {"id": tid, "campaign_id": cid, **spec, "status": "pending"}
        await store.put_agent_task(row)
        tasks.append(row)
    await billing.record(store, "agent_campaigns", 1)
    # 低风险（分析/通知）立刻跑；中风险等人审后才动手
    await run_ready(store, ctx, cid)
    camp = await store.get_campaign(cid)
    tasks = await store.list_agent_tasks(cid)
    return {"ok": True, "campaign": camp, "tasks": tasks,
            "needs_approval": plan["needs_approval"]}


async def approve_campaign(store: Store, ctx: ExecutorContext, campaign_id: str) -> dict[str, Any]:
    camp = await store.get_campaign(campaign_id)
    if not camp:
        return {"ok": False, "error": "战役不存在"}
    if camp["status"] not in ("pending_approval", "running"):
        return {"ok": False, "error": f"当前状态 {camp['status']} 不可批准"}
    camp["status"] = "running"
    await store.put_campaign(camp)
    ran = await run_ready(store, ctx, campaign_id)
    return {"ok": True, "campaign": await store.get_campaign(campaign_id), "executed": ran}


async def reject_campaign(store: Store, campaign_id: str, reason: str = "") -> dict[str, Any]:
    camp = await store.get_campaign(campaign_id)
    if not camp:
        return {"ok": False, "error": "战役不存在"}
    camp["status"] = "rejected"
    if reason:
        plan = dict(camp.get("plan") or {})
        plan["reject_reason"] = reason[:200]
        camp["plan"] = plan
    await store.put_campaign(camp)
    for t in await store.list_agent_tasks(campaign_id):
        if t["status"] == "pending":
            t["status"] = "rejected"
            await store.put_agent_task(t)
    return {"ok": True, "campaign": camp}


async def run_ready(store: Store, ctx: ExecutorContext, campaign_id: str) -> list[dict]:
    """执行当前战役中允许跑的任务（running 全跑；pending_approval/planning 只跑 low）。"""
    camp = await store.get_campaign(campaign_id)
    if not camp or camp["status"] not in ("running", "planning", "pending_approval"):
        return []
    budget = _budget(camp.get("budget"))
    spent = (camp.get("plan") or {}).get("spent") or {"ai_calls": 0, "touches": 0, "tasks": 0}
    out: list[dict] = []
    for task in await store.list_agent_tasks(campaign_id):
        if task["status"] != "pending":
            continue
        if camp["status"] != "running" and task["risk"] != "low":
            continue
        if not tool_allowed(task["role"], task["tool"]):
            task["status"] = "skipped"
            task["result"] = {"error": f"角色 {task['role']} 无权使用 {task['tool']}"}
            await store.put_agent_task(task)
            out.append(task)
            continue
        if spent["tasks"] >= budget["tasks"]:
            task["status"] = "skipped"
            task["result"] = {"error": "战役任务预算用尽"}
            await store.put_agent_task(task)
            out.append(task)
            continue
        result = await _execute_task(store, ctx, camp, task)
        task["result"] = result
        task["status"] = "done" if result.get("ok") else "failed"
        await store.put_agent_task(task)
        spent["tasks"] += 1
        if task["tool"] in ("ai.email", "ai.compose", "draft_loop"):
            spent["ai_calls"] += 1
        if task["tool"] in ("touch.email", "ai.email"):
            spent["touches"] += 1
        try:
            from userloop.billing import meter as billing

            await billing.record(store, "agent_tasks", 1)
        except Exception:  # noqa: BLE001
            pass
        out.append(task)
    plan = dict(camp.get("plan") or {})
    plan["spent"] = spent
    camp["plan"] = plan
    tasks = await store.list_agent_tasks(campaign_id)
    if tasks and all(t["status"] in ("done", "skipped", "rejected", "failed") for t in tasks):
        camp["status"] = "done"
    await store.put_campaign(camp)
    return out


async def _execute_task(store: Store, ctx: ExecutorContext, camp: dict, task: dict) -> dict[str, Any]:
    tool = task["tool"]
    payload = task.get("payload") or {}
    try:
        if tool == "analyze":
            stages = await store.count_users_by_stage()
            wanted = payload.get("stages") or []
            count = sum(stages.get(s, 0) for s in wanted) if wanted else sum(stages.values())
            return {"ok": True, "baseline_users": count, "stages": stages, "filter": wanted}
        if tool == "verify_plan":
            return {"ok": True, "metric": payload.get("metric") or camp.get("metric"),
                    "target": payload.get("target_lift") or camp.get("target"),
                    "window_days": payload.get("window_days", 30),
                    "note": "验收在验证窗口到期后对照 feedback"}
        if tool == "draft_loop":
            from userloop.ai.copilot import validate

            draft, warnings = validate({
                "name": payload.get("name") or task["title"],
                "trigger": payload.get("trigger") or {"type": "inactivity", "stage": "paying", "days": 30},
                "actions": payload.get("actions") or [{"type": "ai.compose",
                                                       "payload": {"topic": camp["goal"], "brief": task["title"]}}],
                "goal_event": payload.get("goal_event"),
                "verify_window_hours": payload.get("verify_window_hours", 168),
                "cooldown_hours": payload.get("cooldown_hours", 336),
            })
            draft["id"] = payload.get("template_id") or new_id("agt")
            draft["enabled"] = False
            draft["source_campaign"] = camp["id"]
            await store.put_template(draft)
            return {"ok": True, "template_id": draft["id"], "enabled": False, "warnings": warnings}
        if tool == "apply_pack":
            from userloop.assets import packs as ap

            pack = ap.get_pack(ctx.config.get("data_dir") or "data", str(payload.get("pack_id") or ""))
            if not pack:
                return {"ok": True, "skipped": True, "note": "资产包不存在，已跳过"}
            res = await ap.import_assets(store, pack, dry_run=False, require_approval=True)
            return {"ok": True, "imported": res.get("created") or res.get("summary") or res,
                    "require_approval": True}
        if tool in ("notify_team", "feishu"):
            action = {"type": "feishu", "payload": {
                "subject": payload.get("subject") or camp["goal"],
                "text": payload.get("text") or task["title"]}}
            dummy_user = {"id": "agent", "email": "", "stage": "advocate", "distinct_id": "agent"}
            r = await execute_action(ctx, action, {"id": camp["id"], "template_id": "agent.team",
                                                   "trigger": {}}, dummy_user)
            return {"ok": True, **{k: r.get(k) for k in ("ok", "dry_run", "note", "error") if k in r}}
        if tool in ("mflow.create_content", "ai.compose", "ai.email", "touch.email"):
            dummy_user = {"id": "agent", "email": "", "stage": "paying", "distinct_id": "agent"}
            r = await execute_action(ctx, {"type": tool, "payload": payload},
                                     {"id": camp["id"], "template_id": "agent.team", "trigger": {}},
                                     dummy_user)
            return {"ok": bool(r.get("ok", True)), "detail": r}
        return {"ok": False, "error": f"未实现的工具：{tool}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


async def run_ready_all(store: Store, ctx: ExecutorContext) -> list[dict]:
    """调度器用：扫所有可跑战役。"""
    out: list[dict] = []
    for camp in await store.list_campaigns(limit=80):
        if camp["status"] in ("running", "planning", "pending_approval"):
            out.extend(await run_ready(store, ctx, camp["id"]))
    return out


async def snapshot(store: Store, campaign_id: str) -> dict[str, Any]:
    from userloop.agents.roster import ROLES

    camp = await store.get_campaign(campaign_id)
    if not camp:
        return {"ok": False, "error": "战役不存在"}
    tasks = await store.list_agent_tasks(campaign_id)
    for t in tasks:
        t["role_name"] = (ROLES.get(t["role"]) or {}).get("name") or t["role"]
    return {"ok": True, "campaign": camp, "tasks": tasks}


approve = approve_campaign
reject = reject_campaign


async def list_team() -> list[dict[str, Any]]:
    from userloop.agents.roster import ROLES

    return [{"id": k, **{kk: vv if kk != "tools" else sorted(vv) for kk, vv in v.items()}}
            for k, v in ROLES.items()]

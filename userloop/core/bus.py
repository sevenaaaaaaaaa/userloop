"""事件总线：全域事件的唯一入口 —— 对齐 OpenFlow flow_handle(event, ctx).

流程：去重 → CDP 建档 → 统计更新 → 旅程判定 → Loop 生成 → 即期动作执行。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.core import journey
from userloop.core.entities import ActionStatus, iso
from userloop.core.journey import PURCHASE_EVENTS
from userloop.core.loops import evaluate as loops_evaluate, mark_actions_done
from userloop.core.store import Store, pj
from userloop.touch.identity import EVENT_IDENT_MAP, lift_contacts


def _norm_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容多种埋点信封：distinct_id/user_id/anonymous_id 皆可。"""
    distinct_id = (
        payload.get("distinct_id")
        or payload.get("user_id")
        or payload.get("anonymous_id")
    )
    event = payload.get("event") or payload.get("event_name")
    if not distinct_id or not event:
        raise ValueError("ingest payload requires distinct_id and event")
    return {
        "distinct_id": str(distinct_id),
        "event": str(event),
        "email": payload.get("email"),
        "name": payload.get("name"),
        "props": payload.get("props") or {},
        "source": payload.get("source") or "api",
        "event_id": payload.get("event_id"),
        "ts": payload.get("ts") or payload.get("timestamp") or iso(),
    }


async def handle(store: Store, ctx: ExecutorContext, payload: dict[str, Any]) -> dict[str, Any]:
    p = _norm_payload(payload)
    now_iso = iso()
    ctx.store = store          # 让适配器（MFlow 选题绑定等）在总线路径也能写身份

    # 顺着 payload/props 已有字段抬邮箱，不另造采集点
    lifted = lift_contacts(payload, p, p.get("props") or {})
    if lifted.get("email") and not p.get("email"):
        p["email"] = lifted["email"]

    # ── 身份识别（识别优先级：匿名标识已知归属 > distinct_id 命中 > 新建）──
    merged_info: dict | None = None
    known = await store.resolve_identity("anonymous_id", p["distinct_id"])
    if known:
        user = await store.get_user(known) or await store.upsert_user(
            p["distinct_id"], email=p["email"], name=p["name"], props=p["props"])
    else:
        user = await store.upsert_user(p["distinct_id"], email=p["email"], name=p["name"], props=p["props"])

    # 事件里带实名标识（email/phone/openid/跨系统 id）→ 绑定；已属他人则合并
    ident_props = {**(p["props"] or {}), **lifted}
    if p.get("email"):
        ident_props.setdefault("email", p["email"])
    for key, type_ in EVENT_IDENT_MAP.items():
        val = ident_props.get(key)
        if not val:
            continue
        owner = await store.resolve_identity(type_, str(val))
        if owner and owner != user["id"]:
            # 以已有实名档案为主档案（保留其历史），把当前（通常匿名）档案并进去
            merged_info = await store.merge_users(owner, user["id"])
            user = await store.get_user(owner) or user
        else:
            await store.bind_identity(user["id"], type_, str(val), source="event")
    # 记住匿名来源，后续同源事件继续归并到实名档案
    if p["distinct_id"] and not await store.resolve_identity("anonymous_id", p["distinct_id"]):
        await store.bind_identity(user["id"], "anonymous_id", p["distinct_id"], source="bus")

    # 1. 事件落库（event_id 幂等去重）
    event_record = {
        "user_id": user["id"], "distinct_id": p["distinct_id"], "event": p["event"],
        "props": p["props"], "source": p["source"], "event_id": p["event_id"], "created_at": p["ts"],
    }
    event_id = await store.insert_event(event_record)
    if event_id is None:
        return {"status": "deduped", "event": p["event"], "user_id": user["id"]}

    # 2. 用户统计更新（旅程判定依据）
    stats = pj(user.get("stats"), {}) or {}
    if p["event"] in PURCHASE_EVENTS:
        stats["purchases"] = int(stats.get("purchases", 0)) + 1
        stats["last_purchase_at"] = p["ts"]
    stats["event_count"] = int(stats.get("event_count", 0)) + 1
    stats["last_event"] = p["event"]
    stats["last_event_at"] = p["ts"]
    await store.update_user(user["id"], stats=json.dumps(stats, ensure_ascii=False), last_seen=p["ts"])
    user["stats"] = stats

    # 3. 旅程引擎：阶段判定 + 迁移记录 + 断点
    new_stage = journey.evaluate_stage(user["stage"], p["event"], stats)
    transition: dict | None = None
    breakpoints: list[dict[str, Any]] = []
    if new_stage:
        transition = {
            "user_id": user["id"], "from_stage": user["stage"], "to_stage": new_stage,
            "reason": journey.stage_reason(p["event"], stats), "created_at": p["ts"],
        }
        await store.insert_transition(transition)
        await store.update_user(user["id"], stage=new_stage)
        user["stage"] = new_stage
        breakpoints.append(journey.stage_enter_breakpoint(user, new_stage))

    # 4. Loop 引擎：模板匹配 → LoopRun + 动作
    templates = await store.get_templates(enabled_only=True)
    loops = await loops_evaluate(store, user, p["event"], p["props"], transition, templates)

    # 4b. Canvas 编排：触发节点匹配 → 图执行
    from userloop.core import canvas

    canvas_runs = await canvas.evaluate(store, ctx, user, p["event"], p["props"], transition)

    # 5. 即期动作（delay=0）立即执行；延迟动作交由调度器
    executed = 0
    for loop in loops:
        for action in await store.actions_for_loop(loop["id"]):
            if action["status"] == ActionStatus.PENDING and action["scheduled_at"] <= now_iso:
                result = await execute_action(ctx, action, loop, user)
                await store.update_action(
                    action["id"],
                    status=ActionStatus.DONE if result.get("ok") else ActionStatus.FAILED,
                    executed_at=iso(), result=json.dumps(result, ensure_ascii=False),
                )
                executed += 1
        await mark_actions_done(store, loop["id"])

    _note_metrics(p["source"], loops, canvas_runs)
    return {
        "status": "ok",
        "event": p["event"],
        "user_id": user["id"],
        "identified": bool(user.get("email") or user.get("stage") not in (None, "visitor")),
        "merged": bool(merged_info and merged_info.get("merged")),
        "stage": user["stage"],
        "stage_changed": transition is not None,
        "breakpoints": breakpoints,
        "loops_created": [loop["id"] for loop in loops],
        "canvas_runs": [run["id"] for run in canvas_runs],
        "actions_executed": executed,
    }


def _note_metrics(source: str, loops: list[dict], canvas_runs: list[dict]) -> None:
    """入库/生成的指标（失败静默，绝不影响主链路）。"""
    try:
        from userloop.ops.metrics import METRICS

        METRICS.inc("userloop_events_ingested_total", {"source": source or "unknown"})
        if loops:
            METRICS.inc("userloop_loops_created_total", value=float(len(loops)))
        if canvas_runs:
            METRICS.inc("userloop_canvas_runs_total", value=float(len(canvas_runs)))
    except Exception:  # noqa: BLE001
        pass


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)

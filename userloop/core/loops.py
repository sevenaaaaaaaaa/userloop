"""Loop 引擎：断点 × 模板 → LoopRun + 动作序列（含冷却控制）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from userloop.core.entities import (
    ActionStatus,
    LoopStatus,
    iso,
)
from userloop.core.store import Store, new_id, pj


def iso_dt(dt: datetime) -> str:
    return iso(dt)


def match_event_trigger(trigger: dict, event: str, props: dict) -> bool:
    """trigger: {"type":"event","name":"purchase","props":{"channel":"app"}}"""
    if trigger.get("type") != "event":
        return False
    if trigger.get("name") != event:
        return False
    want = trigger.get("props") or {}
    return all(props.get(k) == v for k, v in want.items())


def match_stage_trigger(trigger: dict, to_stage: str | None) -> bool:
    return trigger.get("type") == "stage_enter" and to_stage == trigger.get("stage")


def cooldown_until(template: dict) -> str:
    return iso_dt(datetime.utcnow() - timedelta(hours=int(template.get("cooldown_hours", 168))))


async def create_loop_from_template(
    store: Store,
    template: dict,
    user: dict,
    trigger: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict | None:
    """生成 LoopRun + 动作（按 delay_minutes 排期）；返回 loop dict 或 None（冷却中）。"""
    if await store.has_recent_loop(template["id"], user["id"], cooldown_until(template)):
        return None

    loop_id = new_id("loop")
    created = iso()
    verify_before = iso_dt(datetime.utcnow() + timedelta(hours=int(template.get("verify_window_hours", 72))))
    loop = {
        "id": loop_id,
        "template_id": template["id"],
        "user_id": user["id"],
        "status": LoopStatus.RUNNING,
        "trigger": trigger,
        "context": {**(context or {}), "user_stage": user.get("stage"), "user_email": user.get("email")},
        "verify_after": created,
        "verify_before": verify_before,
        "created_at": created,
        "updated_at": created,
    }
    await store.insert_loop(loop)

    for seq, spec in enumerate(template.get("actions", []), start=1):
        delay = int(spec.get("delay_minutes", 0))
        scheduled = iso_dt(datetime.utcnow() + timedelta(minutes=delay))
        await store.insert_action(
            {
                "id": new_id("act"),
                "loop_id": loop_id,
                "seq": seq,
                "type": spec.get("type", "generic"),
                "payload": spec.get("payload", {}),
                "delay_minutes": delay,
                "status": ActionStatus.PENDING,
                "scheduled_at": scheduled,
            }
        )
    return loop


async def evaluate(
    store: Store,
    user: dict,
    event: str | None,
    props: dict,
    transition: dict | None,
    templates: list[dict],
) -> list[dict]:
    """事件总线调用：匹配 event / stage_enter 触发器，创建 Loop。"""
    created: list[dict] = []
    for template in sorted(templates, key=lambda t: -int(t.get("priority", 50))):
        trigger = template.get("trigger") or {}
        hit: dict[str, Any] | None = None
        if event and match_event_trigger(trigger, event, props):
            hit = {"type": "event", "event": event}
        elif transition and match_stage_trigger(trigger, transition["to_stage"]):
            hit = {"type": "stage_enter", "stage": transition["to_stage"]}
        if not hit:
            continue
        loop = await create_loop_from_template(store, template, user, hit)
        if loop:
            created.append(loop)
    return created


async def mark_actions_done(store: Store, loop_id: str) -> None:
    """全部动作终态后，Loop 进入 verifying（待验证）或直接 verified（无目标）。"""
    actions = await store.actions_for_loop(loop_id)
    pending = [a for a in actions if a["status"] == ActionStatus.PENDING]
    if pending:
        return
    loop = await store.get_loop(loop_id)
    if not loop or loop["status"] not in (LoopStatus.RUNNING,):
        return
    template = await find_template(store, loop["template_id"])
    if template and template.get("goal_event"):
        await store.update_loop(loop_id, status=LoopStatus.VERIFYING)
    else:
        await store.update_loop(loop_id, status=LoopStatus.VERIFIED)


async def find_template(store: Store, template_id: str) -> dict | None:
    templates = await store.get_templates(enabled_only=False)
    for t in templates:
        if t.get("id") == template_id:
            return t
    return None


def loop_to_dict(loop: dict, actions: list[dict], template: dict | None) -> dict[str, Any]:
    out = dict(loop)
    out["trigger"] = pj(loop.get("trigger"), {}) or {}
    out["context"] = pj(loop.get("context"), {}) or {}
    out["actions"] = [{**a, "payload": pj(a.get("payload"), {}) or {}} for a in actions]
    out["template"] = template
    return out

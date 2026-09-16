"""验证与回流：验证窗口到期 → 目标事件比对 → verdict → feedback 落库."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from userloop.core.entities import (
    LoopStatus,
    Verdict,
    iso,
)
from userloop.core.loops import find_template
from userloop.core.store import Store


async def verify_loop(store: Store, loop: dict) -> dict[str, Any] | None:
    """返回 feedback dict；无 goal_event 的 Loop 不产生 feedback。"""
    template = await find_template(store, loop["template_id"])
    if not template or not template.get("goal_event"):
        await store.update_loop(loop["id"], status=LoopStatus.VERIFIED)
        return None

    since = loop.get("verify_after") or loop["created_at"]
    before = loop.get("verify_before")
    goal = template["goal_event"]
    hit = await store.has_event_since(loop["user_id"], goal, since=since, before=before)
    verdict = (Verdict.EFFECTIVE if hit else Verdict.NEUTRAL).value
    evidence = {"goal_event": goal, "hit_event": hit["event"] if hit else None,
                "window": [since, before]}
    feedback = {
        "loop_id": loop["id"],
        "template_id": loop["template_id"],
        "verdict": verdict,
        "goal_event": goal,
        "evidence": evidence,
        "created_at": iso(),
    }
    await store.insert_feedback(feedback)
    await store.update_loop(loop["id"], status=LoopStatus.VERIFIED, error=None)
    return feedback


def verify_after_hours(hours: int) -> tuple[str, str]:
    now = datetime.utcnow()
    return iso(now), iso(now + timedelta(hours=hours))

"""旅程引擎：全域事件 → 生命周期阶段判定 + 阶段迁移 + 断点信号.

旅程单调上行（visitor→signup→activated→paying→retained→advocate），
下行风险（churn_risk/churned）由调度器周期扫描发现 —— 对齐 OpenFlow CDP 评分 + MFlow 状态机。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from userloop.core.entities import STAGE_ORDER, Stage

SIGNUP_EVENTS = frozenset({"signup", "register", "member_register"})
ACTIVATION_EVENTS = frozenset({"activation", "project_created", "first_content", "course_start", "first_tool_use"})
PURCHASE_EVENTS = frozenset({"purchase", "order.paid", "payment_success"})
REFERRAL_EVENTS = frozenset({"referral", "invite_friend", "share_success"})


@dataclass
class JourneyResult:
    """单次事件后的旅程评估结果。"""

    new_stage: str | None = None
    transition: dict[str, Any] | None = None
    breakpoints: list[dict[str, Any]] = field(default_factory=list)


def evaluate_stage(current_stage: str, event: str, stats: dict[str, Any]) -> str | None:
    """根据事件与累计统计判定新阶段（只升不降，返回 None 表示不变）。"""
    candidate: str | None = None

    if event in REFERRAL_EVENTS:
        candidate = Stage.ADVOCATE.value
    elif event in PURCHASE_EVENTS:
        purchases = int(stats.get("purchases", 1))
        if purchases >= 3:
            candidate = Stage.ADVOCATE.value
        elif purchases >= 2:
            candidate = Stage.RETAINED.value
        else:
            candidate = Stage.PAYING.value
    elif event in ACTIVATION_EVENTS:
        candidate = Stage.ACTIVATED.value
    elif event in SIGNUP_EVENTS:
        candidate = Stage.SIGNUP.value

    if candidate is None:
        return None
    if STAGE_ORDER.get(current_stage, 0) >= STAGE_ORDER.get(candidate, 99):
        return None
    return candidate


def stage_reason(event: str, stats: dict[str, Any]) -> str:
    if event in PURCHASE_EVENTS:
        return f"purchase #{int(stats.get('purchases', 1))}"
    return f"event:{event}"


def stage_enter_breakpoint(user: dict, new_stage: str) -> dict[str, Any]:
    """断点：用户进入新阶段 —— 契机型断点（onboarding/复购/推荐引导）。"""
    return {
        "kind": "stage_enter",
        "user_id": user["id"],
        "stage": new_stage,
        "detail": f"{user['stage']} -> {new_stage}",
    }


def stall_breakpoint(user: dict, days: int) -> dict[str, Any]:
    """断点：用户滞留（无事件超过 N 天）—— 风险型断点，由调度器扫描产生。"""
    return {
        "kind": "inactivity",
        "user_id": user["id"],
        "stage": user["stage"],
        "detail": f"no event for {days}d",
    }

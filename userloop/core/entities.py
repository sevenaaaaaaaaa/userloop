"""核心实体：用户 / 事件 / 旅程 / Loop / 动作 / 回流.

借鉴 OpenFlow DomainContract（领域契约 + 状态机）与 inFlow entities（Pydantic 一等公民对象）。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.utcnow()


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds") + "Z"


class Stage(str, Enum):
    """生命周期阶段（旅程骨架，可经 data/journey.json 调整权重）。"""

    VISITOR = "visitor"
    SIGNUP = "signup"
    ACTIVATED = "activated"
    PAYING = "paying"
    RETAINED = "retained"
    ADVOCATE = "advocate"
    CHURN_RISK = "churn_risk"
    CHURNED = "churned"


STAGE_ORDER: dict[str, int] = {
    Stage.VISITOR: 0,
    Stage.SIGNUP: 1,
    Stage.ACTIVATED: 2,
    Stage.PAYING: 3,
    Stage.RETAINED: 4,
    Stage.ADVOCATE: 5,
}


class User(BaseModel):
    """CDP 档案：匿名/实名身份合一（对齐 OpenFlow cdp_customers）。"""

    id: str
    distinct_id: str
    email: str | None = None
    name: str | None = None
    stage: str = Stage.VISITOR
    props: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)  # purchases / views / last_event_at ...
    first_seen: str = Field(default_factory=lambda: iso())
    last_seen: str = Field(default_factory=lambda: iso())


class Event(BaseModel):
    """全域行为事件（幂等由 event_id 保证，对齐 OpenFlow events.message_id）。"""

    id: str | None = None
    user_id: str
    distinct_id: str
    event: str
    props: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"
    event_id: str | None = None  # 幂等键
    created_at: str = Field(default_factory=lambda: iso())


class StageTransition(BaseModel):
    """旅程阶段迁移（旅程重建的 SSOT，对齐 MFlow 状态机审计）。"""

    id: str | None = None
    user_id: str
    from_stage: str
    to_stage: str
    reason: str = ""
    created_at: str = Field(default_factory=lambda: iso())


class LoopStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


class LoopTemplate(BaseModel):
    """Loop 模板：触发 + 动作 + 目标 + 验证窗口 + 冷却。

    trigger:
      type=event         -> name + optional props 匹配
      type=stage_enter   -> stage
      type=inactivity    -> stage + days 无事件
    """

    id: str
    name: str
    enabled: bool = True
    trigger: dict[str, Any]
    actions: list[dict[str, Any]] = Field(default_factory=list)
    goal_event: str | None = None          # 验证目标事件；None 表示任意事件
    verify_window_hours: int = 72
    cooldown_hours: int = 168              # 同用户同模板冷却
    priority: int = 50
    description: str = ""


class LoopRun(BaseModel):
    """Loop 运行实例（状态机：pending→running→verifying→verified/failed/skipped）。"""

    id: str
    template_id: str
    user_id: str
    status: str = LoopStatus.PENDING
    trigger: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    verify_after: str | None = None
    verify_before: str | None = None
    created_at: str = Field(default_factory=lambda: iso())
    updated_at: str = Field(default_factory=lambda: iso())
    error: str | None = None


class ActionStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    DONE = "done"
    FAILED = "failed"


class LoopAction(BaseModel):
    """Loop 内单个动作（可延迟：delay_minutes）。"""

    id: str
    loop_id: str
    seq: int
    type: str  # webhook | email | feishu | generic
    payload: dict[str, Any] = Field(default_factory=dict)
    delay_minutes: int = 0
    status: str = ActionStatus.PENDING
    scheduled_at: str = Field(default_factory=lambda: iso())
    executed_at: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class Verdict(str, Enum):
    EFFECTIVE = "effective"
    NEUTRAL = "neutral"


class Feedback(BaseModel):
    """动作效果回流（闭环最后一公里，对齐 inFlow Feedback）。"""

    id: str | None = None
    loop_id: str
    template_id: str
    verdict: str
    goal_event: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: iso())

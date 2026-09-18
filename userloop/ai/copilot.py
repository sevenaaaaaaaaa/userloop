"""Copilot：自然语言 → Loop / Canvas 草稿（AI Native 编排入口）.

设计纪律（对齐"人给目标、AI 出方案、人审后启用"）：
- AI 只产出**草稿**（enabled=False），必须经人确认才落库启用
- 服务端强校验：触发器类型、动作白名单、数值范围、必填字段；越权/非法项被裁剪并记 warning
- 不发明动作：只允许现有执行器支持的 action type（与 AI 大脑同一份白名单）
"""

from __future__ import annotations

import json
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store, new_id

TRIGGER_TYPES = ("event", "stage_enter", "inactivity")

# 可用动作（与执行器实现一一对应；AI 不得发明）
ALLOWED_ACTIONS = {
    "touch.email", "touch.sms", "touch.h5", "touch.im", "touch.wechat_mp", "touch.wecom",
    "ai.email", "ai.compose",
    "feishu", "webhook", "generic", "email",
    "openflow.webhook_insight", "openflow.automation",
    "mflow.create_content", "mflow.register_topic",
    "ma.hubspot.contact_upsert", "ma.hubspot.note", "ma.webhook",
}

STAGES = ("visitor", "signup", "activated", "paying", "retained", "advocate", "churn_risk", "churned")

SYSTEM = """你是 UserLoop 的运营编排 Copilot。把用户的自然语言需求翻译成**可执行的运营 Loop**。

输出严格 JSON（不要解释、不要 markdown 围栏）：
{
  "name": "流程名称（中文，简洁）",
  "description": "一句话说明触发条件与目标",
  "trigger": {"type": "event|stage_enter|inactivity", ...},
  "actions": [{"type": "<动作类型>", "payload": {...}, "delay_minutes": 0}],
  "goal_event": "用于验证效果的目标事件（无则 null）",
  "verify_window_hours": 72,
  "cooldown_hours": 168
}

规则：
1. trigger 三选一：
   - {"type":"event","name":"<事件名>"}            用户做了某事
   - {"type":"stage_enter","stage":"<stage>"}     进入某旅程阶段
   - {"type":"inactivity","stage":"<stage>","days":N} 某阶段 N 天无事件
2. actions 只能从这些类型里选（不得发明）：
   touch.email / touch.sms / touch.h5 / touch.wechat_mp / touch.wecom / touch.im /
   ai.email / ai.compose / feishu / webhook /
   openflow.webhook_insight / mflow.create_content / ma.hubspot.contact_upsert / ma.webhook
   其中 touch.email / ai.email / touch.h5 最常用于用户触达；payload 写 subject/text/cta_text/cta_url/topic/brief。
3. 频次纪律：同一流程 cooldown_hours 建议 ≥ 48；不要设计高频骚扰。
4. 若需求信息不足，做出最合理假设并在 description 里说明。
5. goal_event 要能反映业务目标（如 activation / purchase / h5_click / referral）。"""


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def validate(draft: dict) -> tuple[dict, list[str]]:
    """服务端强校验与裁剪；返回 (规范化草稿, warnings)。"""
    warnings: list[str] = []
    out: dict[str, Any] = {
        "id": str(draft.get("id") or new_id("flow_ai")),
        "name": str(draft.get("name") or "AI 生成的运营流程")[:60],
        "description": str(draft.get("description") or "")[:300],
        "enabled": False,                       # 草稿态：必须人工启用
        "priority": 60,
        "goal_event": (str(draft["goal_event"]) if draft.get("goal_event") else None),
        "verify_window_hours": _clamp(draft.get("verify_window_hours"), 0, 24 * 90, 72),
        "cooldown_hours": _clamp(draft.get("cooldown_hours"), 1, 24 * 365, 168),
    }

    # 触发器
    trig = draft.get("trigger") or {}
    ttype = str(trig.get("type") or "event")
    if ttype not in TRIGGER_TYPES:
        warnings.append(f"触发器类型 {ttype} 不支持，已改为 event")
        ttype = "event"
    if ttype == "event":
        name = str(trig.get("name") or "")
        if not name:
            warnings.append("event 触发器缺少事件名，已用 page_view 占位")
            name = "page_view"
        out["trigger"] = {"type": "event", "name": name}
    elif ttype == "stage_enter":
        stage = str(trig.get("stage") or "signup")
        if stage not in STAGES:
            warnings.append(f"阶段 {stage} 不存在，已改为 signup")
            stage = "signup"
        out["trigger"] = {"type": "stage_enter", "stage": stage}
    else:
        stage = str(trig.get("stage") or "activated")
        if stage not in STAGES:
            warnings.append(f"阶段 {stage} 不存在，已改为 activated")
            stage = "activated"
        out["trigger"] = {"type": "inactivity", "stage": stage,
                          "days": _clamp(trig.get("days"), 1, 365, 7)}

    # 动作白名单
    actions: list[dict[str, Any]] = []
    for spec in (draft.get("actions") or [])[:5]:
        atype = str((spec or {}).get("type") or "")
        if atype not in ALLOWED_ACTIONS:
            warnings.append(f"动作 {atype or '(空)'} 不在白名单，已丢弃")
            continue
        payload = spec.get("payload") if isinstance(spec.get("payload"), dict) else {}
        actions.append({"type": atype, "payload": payload,
                        "delay_minutes": _clamp(spec.get("delay_minutes"), 0, 60 * 24 * 30, 0)})
    if not actions:
        warnings.append("无合法动作，已补一条 compose 草稿（不外发）")
        actions = [{"type": "ai.compose", "payload": {"topic": out["name"], "brief": out["description"]},
                    "delay_minutes": 0}]
    out["actions"] = actions
    return out, warnings


async def draft_loop(ctx: ExecutorContext, prompt: str, transport: Any = None) -> dict[str, Any]:
    """自然语言 → Loop 草稿（LLM + 强校验）。"""
    from userloop.integrations.ai import chat

    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"需求：{prompt}"}]
    res = await chat(ctx, msgs, transport=transport)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "AI 不可用", "degraded": True}
    try:
        raw = json.loads(res.get("content") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "AI 输出不是合法 JSON（可重试或换更明确的描述）"}
    draft, warnings = validate(raw)
    return {"ok": True, "draft": draft, "warnings": warnings, "model": res.get("model")}


async def apply_loop(store: Store, draft: dict, enable: bool = False) -> dict[str, Any]:
    """落库为 Loop 模板（默认草稿态；enable=True 则立即生效）。"""
    normalized, warnings = validate(draft)
    normalized["enabled"] = bool(enable)
    await store.put_template(normalized)
    return {"ok": True, "id": normalized["id"], "enabled": normalized["enabled"], "warnings": warnings,
            "draft": normalized}

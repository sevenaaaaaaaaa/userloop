"""AI 大脑（全域全生命周期用户运营 AI）.

职责：在护栏内为每个用户决定「下一步最佳动作」（Next Best Action），而不是套固定模板。
- 决策依据：阶段 + 统计 + 最近行为 + 历史 Loop 效果（验证回流）+ 频控状态
- 决策输出：白名单动作 + 文案 + 理由/置信度/预期效果（JSON）
- 执行纪律：低风险自动执行；中风险进审批门；高风险阻断；noop 允许"不打扰"
- 全程审计：ai_decisions 表留痕（模型/理由/结果），可回溯可评估

对齐 OpenFlow AgentRuntime 的教训：白名单 + 风险门 + 审计，AI 不能绕过规则。
"""

from __future__ import annotations

import json
from typing import Any

from userloop.ai import guardrails as G
from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store, new_id, pj

# 决策白名单：intent → 动作模板（复用现有执行器，AI 不可发明新动作）
INTENTS: dict[str, dict[str, Any]] = {
    "send_email": {
        "risk": "medium", "channel": "email",
        "action": {"type": "ai.email", "payload": {"topic": "{topic}", "brief": "{brief}", "tone": "{tone}"}},
        "goal_event": "activation", "verify_window_hours": 72, "cooldown_hours": 72,
    },
    "compose_email": {
        "risk": "low", "channel": "email",
        "action": {"type": "ai.compose", "payload": {"topic": "{topic}", "brief": "{brief}", "tone": "{tone}"}},
        "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 24,
    },
    "notify_team": {
        "risk": "low", "channel": "feishu",
        "action": {"type": "feishu", "payload": {"subject": "{topic}", "text": "{brief}"}},
        "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 12,
    },
    "sync_crm": {
        "risk": "low", "channel": "hubspot",
        "action": {"type": "ma.hubspot.contact_upsert", "payload": {"note": "{topic}"}},
        "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 168,
    },
    "signal_openflow": {
        "risk": "low", "channel": "openflow",
        "action": {"type": "openflow.webhook_insight", "payload": {"subject": "{topic}", "text": "{brief}"}},
        "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 168,
    },
    "push_content": {
        "risk": "medium", "channel": "mflow",
        "action": {"type": "mflow.create_content", "payload": {"topic": "{topic}", "brief": "{brief}"}},
        "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 168,
    },
    "noop": {
        "risk": "low", "channel": "none", "action": None,
        "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 0,
    },
}

SYSTEM_PROMPT = """你是"全域全生命周期用户运营 AI"，为一个具体用户决定「下一步最佳动作」。

硬性纪律（必须遵守）：
1. 只在给定 intent 白名单里选；没有合适动作时必须选 noop（不打扰用户也是一种正确决策）。
2. 尊重用户所处生命周期阶段：访客阶段不外发邮件；付费用户重留存与增值；流失风险用户重召回。
3. 考虑历史效果：只参考历史数据中 effective 的正向经验，避免重复无效动作。
4. 触达上限：单个用户 24 小时内不超过 1 次外发触达；宁少勿滥。
5. 只输出 JSON，字段：intent, topic, brief, reasoning, confidence(0-1), urgency(low|medium|high), expected_effect。"""


def stage_policy(stage: str) -> list[str]:
    """阶段 → 允许的 intent 列表（AI 越权也在服务端被裁剪）。"""
    return {
        "visitor": ["noop", "notify_team", "compose_email", "signal_openflow"],
        "signup": ["noop", "send_email", "compose_email", "sync_crm", "notify_team"],
        "activated": ["noop", "send_email", "compose_email", "sync_crm", "signal_openflow", "notify_team"],
        "paying": ["noop", "send_email", "compose_email", "sync_crm", "signal_openflow", "notify_team"],
        "retained": ["noop", "send_email", "compose_email", "sync_crm", "notify_team", "push_content"],
        "advocate": ["noop", "send_email", "compose_email", "sync_crm", "push_content", "notify_team"],
        "churn_risk": ["noop", "send_email", "compose_email", "notify_team"],
        "churned": ["noop", "send_email", "notify_team"],
    }.get(stage, ["noop"])


async def build_context(store: Store, user: dict) -> dict[str, Any]:
    """全生命周期上下文（供 LLM 决策）。"""
    uid = user["id"]
    events = await store.recent_events_for_user(uid, limit=6)
    transitions = await store.list_transitions(user_id=uid, limit=4)
    loops = [l for l in await store.list_loops(limit=50) if l["user_id"] == uid][:4]
    feedback = await store.feedback_stats()
    touches = await store.touches_since(uid, hours=24)
    stats = user.get("stats") if isinstance(user.get("stats"), dict) else pj(user.get("stats"), {}) or {}
    return {
        "user": {"stage": user.get("stage"), "email": user.get("email") or None,
                 "stats": stats, "first_seen": user.get("first_seen"), "last_seen": user.get("last_seen")},
        "recent_events": [{"event": e["event"], "at": e["created_at"]} for e in events],
        "journey": [f"{t['from_stage']}→{t['to_stage']}" for t in reversed(transitions)],
        "recent_loops": [{"template": l["template_id"], "status": l["status"]} for l in loops],
        "touches_last_24h": touches,
        "effect_stats": {k: v for k, v in list(feedback.items())[:8]},
        "allowed_intents": stage_policy(user.get("stage", "visitor")),
    }


async def decide(ctx: ExecutorContext, context: dict, transport: Any = None) -> dict[str, Any]:
    """调用 LLM 产出决策（失败返回 noop，AI 不可用时不阻塞业务）。"""
    from userloop.integrations.ai import _parse_copy, chat

    cfg = ctx.config.get("ai") or {}
    brain = cfg.get("brain") or {}
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
    out = await chat(ctx, msgs, model=brain.get("model"), transport=transport)
    if not out.get("ok"):
        return {"intent": "noop", "reasoning": f"ai unavailable: {out.get('error')}", "confidence": 0,
                "degraded": True, "model": None}
    try:
        data = json.loads(out.get("content") or "{}")
    except json.JSONDecodeError:
        data = _parse_copy(out.get("content") or "")
        data = {"intent": "noop", "reasoning": "unparsable decision", "confidence": 0}
    return {
        "intent": str(data.get("intent") or "noop"),
        "topic": str(data.get("topic") or "")[:120],
        "brief": str(data.get("brief") or "")[:400],
        "reasoning": str(data.get("reasoning") or "")[:400],
        "confidence": float(data.get("confidence") or 0),
        "urgency": str(data.get("urgency") or "low"),
        "expected_effect": str(data.get("expected_effect") or "")[:200],
        "model": out.get("model"),
    }


def _render_intent(intent: str, decision: dict, user: dict) -> dict[str, Any] | None:
    """intent → 动作模板（占位符替换 + 稳健兜底）。"""
    spec = INTENTS.get(intent)
    if not spec or not spec.get("action"):
        return None
    mapping = {"topic": decision.get("topic") or f"{user.get('stage', '')} 阶段运营触达",
               "brief": decision.get("brief") or decision.get("reasoning") or "",
               "tone": (decision.get("tone") or "简洁友好")}
    raw = json.dumps(spec["action"], ensure_ascii=False)
    for k, v in mapping.items():
        raw = raw.replace("{" + k + "}", str(v).replace('"', "'"))
    return json.loads(raw)


async def run_for_user(
    store: Store,
    ctx: ExecutorContext,
    user: dict,
    transport: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """对单个用户执行一次 AI 决策（含护栏与审计）。返回决策记录。"""
    cfg = ctx.config.get("ai") or {}
    brain = cfg.get("brain") or {}
    stage = user.get("stage", "visitor")

    # 1) 前置护栏：频控 / 静默期 / 预算
    gate = await G.precheck(store, ctx, user, force=force)
    if not gate["ok"]:
        return await _audit(store, user, {
            "intent": "noop", "reasoning": gate["reason"], "confidence": 1.0,
            "status": "skipped", "risk": "low", "channel": "none",
        })

    # 2) LLM 决策
    context = await build_context(store, user)
    decision = await decide(ctx, context, transport=transport)

    # 3) 服务端裁剪：白名单 + 阶段策略（AI 越权在此被纠正）
    allowed = set(context["allowed_intents"]) & set(INTENTS)
    intent = decision["intent"] if decision["intent"] in allowed else "noop"
    if intent != decision["intent"]:
        decision["reasoning"] = f"[策略裁剪 {decision['intent']}→noop] " + decision["reasoning"]
    spec = INTENTS[intent]

    if intent == "noop":
        return await _audit(store, user, {**decision, "intent": "noop", "status": "noop",
                                          "risk": "low", "channel": "none"})

    # 4) 风险门：low 自动执行 / medium 待审批 / high 阻断
    payload = _render_intent(intent, decision, user)
    risk = spec["risk"]
    if risk == "high":
        return await _audit(store, user, {**decision, "intent": intent, "status": "blocked",
                                          "risk": risk, "channel": spec["channel"], "payload": payload})
    auto = bool(brain.get("auto_execute_medium", False))
    if risk == "medium" and not (auto or force):
        return await _audit(store, user, {**decision, "intent": intent, "status": "pending_approval",
                                          "risk": risk, "channel": spec["channel"], "payload": payload})

    loop = await execute_intent(store, ctx, user, intent, decision, payload)
    return await _audit(store, user, {**decision, "intent": intent, "status": "auto_executed",
                                      "risk": risk, "channel": spec["channel"], "payload": payload,
                                      "loop_id": loop["id"] if loop else None})


async def execute_intent(
    store: Store,
    ctx: ExecutorContext,
    user: dict,
    intent: str,
    decision: dict,
    payload: dict | None = None,
) -> dict | None:
    """把决策落成 LoopRun 并执行到期动作（复用 Loop 引擎的冷却/验证状态机）。"""
    from userloop.core.loops import create_loop_from_template
    from userloop.core.scheduler import process_due_actions

    spec = INTENTS.get(intent)
    if not spec or not spec.get("action"):
        return None
    action = payload if payload is not None else _render_intent(intent, decision, user)
    template = {
        "id": f"ai.{intent}", "name": f"AI 决策 · {intent}",
        "trigger": {"type": "ai_brain", "stage": user.get("stage")},
        "actions": [action],
        "goal_event": spec.get("goal_event"),
        "verify_window_hours": spec.get("verify_window_hours", 0),
        "cooldown_hours": spec.get("cooldown_hours", 24),
        "priority": 100,
    }
    loop = await create_loop_from_template(
        store, template, user, {"type": "ai_brain", "intent": intent, "confidence": decision.get("confidence")},
        {"reasoning": decision.get("reasoning"), "expected_effect": decision.get("expected_effect")})
    if loop:
        await process_due_actions(store, ctx)  # 即期动作执行
    return loop


async def _audit(store: Store, user: dict, record: dict) -> dict:
    rec = {
        "id": new_id("ai"), "user_id": user["id"], "stage": user.get("stage"),
        "intent": record.get("intent", "noop"),
        "channel": record.get("channel", "none"),
        "risk": record.get("risk", "low"),
        "status": record.get("status", "noop"),
        "reasoning": record.get("reasoning", ""),
        "confidence": float(record.get("confidence") or 0),
        "expected_effect": record.get("expected_effect", ""),
        "payload": record.get("payload"),
        "loop_id": record.get("loop_id"),
        "model": record.get("model"),
        "topic": record.get("topic", ""),
    }
    await store.insert_ai_decision(rec)
    return rec


async def run_batch(store: Store, ctx: ExecutorContext, limit: int = 5, force: bool = False) -> list[dict]:
    """批量：优先最近活跃、尚未被频繁触达的用户（成本有界）。"""
    cfg = ctx.config.get("ai") or {}
    brain = cfg.get("brain") or {}
    if not brain.get("enabled", False) and not force:
        return []
    candidates = await store.ai_candidates(limit=limit)
    out = []
    for user in candidates:
        out.append(await run_for_user(store, ctx, user, force=force))
    return out

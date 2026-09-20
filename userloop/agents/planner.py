"""把运营目标拆成跨角色任务。先走确定性配方（可演示、可验收），LLM 只作可选增强。"""

from __future__ import annotations

from typing import Any

from userloop.agents.roster import ROLES

# 配方：验收标准「本月复购率 +10%」必须有一条可执行路径
RECIPES: dict[str, dict[str, Any]] = {
    "repurchase": {
        "metric": "repurchase_rate",
        "name": "复购提升",
        "tasks": [
            {"role": "analyst", "tool": "analyze", "risk": "low",
             "title": "盘点付费沉默用户基线",
             "payload": {"stages": ["paying", "retained"], "silence_days": 30}},
            {"role": "content", "tool": "draft_loop", "risk": "medium",
             "title": "起草复购召回 Loop（停用待批）",
             "payload": {"template_id": "agent_repurchase",
                         "name": "智能体·复购召回",
                         "trigger": {"type": "inactivity", "stage": "paying", "days": 30},
                         "actions": [{"type": "ai.email",
                                      "payload": {"topic": "复购提醒",
                                                  "brief": "付费用户沉默，给出新品/权益理由"}}],
                         "goal_event": "purchase", "verify_window_hours": 168, "cooldown_hours": 336}},
            {"role": "outreach", "tool": "apply_pack", "risk": "medium",
             "title": "套用电商复购资产（若已有则跳过）",
             "payload": {"pack_id": "ecommerce-growth", "template_id": "ec_repurchase_nudge"}},
            {"role": "support", "tool": "notify_team", "risk": "low",
             "title": "通知团队：复购战役已拆解，等人审",
             "payload": {"subject": "复购战役待审批", "text": "分析/内容/触达任务已排队，请在控制台批准。"}},
            {"role": "analyst", "tool": "verify_plan", "risk": "low",
             "title": "写验收口径：窗口内购买事件相对基线 +10%",
             "payload": {"metric": "repurchase_rate", "target_lift": 0.10, "window_days": 30}},
        ],
    },
    "activation": {
        "metric": "activation_rate",
        "name": "激活提升",
        "tasks": [
            {"role": "analyst", "tool": "analyze", "risk": "low",
             "title": "盘点注册未激活用户",
             "payload": {"stages": ["signup"], "silence_days": 1}},
            {"role": "content", "tool": "draft_loop", "risk": "medium",
             "title": "起草激活引导 Loop（停用待批）",
             "payload": {"template_id": "agent_activation",
                         "name": "智能体·激活引导",
                         "trigger": {"type": "inactivity", "stage": "signup", "days": 1},
                         "actions": [{"type": "ai.email",
                                      "payload": {"topic": "完成首次激活",
                                                  "brief": "注册后未激活，引导完成第一个关键动作"}}],
                         "goal_event": "activation", "verify_window_hours": 72, "cooldown_hours": 168}},
            {"role": "outreach", "tool": "apply_pack", "risk": "medium",
             "title": "套用 SaaS 激活资产",
             "payload": {"pack_id": "saas-onboarding", "template_id": "saas_activation"}},
            {"role": "support", "tool": "notify_team", "risk": "low",
             "title": "通知团队：激活战役待审批",
             "payload": {"subject": "激活战役待审批", "text": "任务已拆解，请审批后执行。"}},
            {"role": "analyst", "tool": "verify_plan", "risk": "low",
             "title": "写验收口径：72h 内 activation",
             "payload": {"metric": "activation_rate", "target_lift": 0.10, "window_days": 7}},
        ],
    },
    "churn": {
        "metric": "churn_rescue_rate",
        "name": "流失挽回",
        "tasks": [
            {"role": "analyst", "tool": "analyze", "risk": "low",
             "title": "盘点流失风险用户",
             "payload": {"stages": ["churn_risk", "churned"], "silence_days": 14}},
            {"role": "content", "tool": "draft_loop", "risk": "medium",
             "title": "起草挽回 Loop（停用待批）",
             "payload": {"template_id": "agent_churn_rescue",
                         "name": "智能体·流失挽回",
                         "trigger": {"type": "inactivity", "stage": "churn_risk", "days": 14},
                         "actions": [{"type": "ai.email",
                                      "payload": {"topic": "专属回归礼",
                                                  "brief": "流失风险用户，给出回归理由"}}],
                         "goal_event": "activation", "verify_window_hours": 168, "cooldown_hours": 336}},
            {"role": "support", "tool": "notify_team", "risk": "low",
             "title": "通知团队关注高风险名单",
             "payload": {"subject": "流失挽回待审批", "text": "高风险用户名单已盘点。"}},
            {"role": "analyst", "tool": "verify_plan", "risk": "low",
             "title": "写验收口径：7 日内回流事件",
             "payload": {"metric": "churn_rescue_rate", "target_lift": 0.10, "window_days": 14}},
        ],
    },
}


def pick_recipe(goal: str) -> str:
    text = (goal or "").lower()
    if any(k in text for k in ("复购", "回购", "repurchase", "repeat")):
        return "repurchase"
    if any(k in text for k in ("激活", "activation", "onboard")):
        return "activation"
    if any(k in text for k in ("流失", "挽回", "churn", "rescue")):
        return "churn"
    return "repurchase"   # 默认走验收主路径


def list_recipes() -> list[dict[str, Any]]:
    return [{"id": k, "name": v["name"], "metric": v["metric"], "tasks": len(v["tasks"])}
            for k, v in RECIPES.items()]


def plan(goal: str, target: float | None = None, recipe: str | None = None) -> dict[str, Any]:
    key = recipe if recipe in RECIPES else pick_recipe(goal)
    spec_recipe = RECIPES[key]
    tasks = []
    for i, spec in enumerate(spec_recipe["tasks"]):
        role = spec["role"]
        tasks.append({
            "seq": i,
            "role": role,
            "role_name": ROLES[role]["name"],
            "tool": spec["tool"],
            "title": spec["title"],
            "risk": spec["risk"],
            "payload": spec.get("payload") or {},
        })
    return {
        "recipe": key,
        "name": spec_recipe["name"],
        "metric": spec_recipe["metric"],
        "target": target if target is not None else 0.10,
        "goal": goal,
        "roles": list({t["role"] for t in tasks}),
        "tasks": tasks,
        "needs_approval": any(t["risk"] != "low" for t in tasks),
    }

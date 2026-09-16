"""Loop 模板内置种子：内置模板落库，用户可经 data/loop-templates.json 覆盖/扩展."""

from __future__ import annotations

import json
import os

from userloop.core.store import Store

BUILTIN_TEMPLATES: list[dict] = [
    {
        "id": "signup_no_activate",
        "name": "注册未激活引导",
        "enabled": True,
        "trigger": {"type": "inactivity", "stage": "signup", "days": 1},
        "actions": [
            {"type": "ai.email", "payload": {"topic": "注册后 24 小时未完成首次激活的引导邮件",
                                             "brief": "用户刚注册一天还没体验核心功能，引导完成第一个项目，语气轻松不催促"}}
        ],
        "goal_event": "activation",
        "verify_window_hours": 72,
        "cooldown_hours": 168,
        "priority": 60,
        "description": "注册后 24h 未激活 → AI 个性化引导邮件（DeepSeek 生成），目标 72h 内 activation",
    },
    {
        "id": "cart_abandon",
        "name": "购物车放弃挽回",
        "enabled": True,
        "trigger": {"type": "event", "name": "add_to_cart"},
        "actions": [
            {"type": "email", "delay_minutes": 120, "payload": {"subject": "购物车还等着你", "text": "{name}，你加购的商品即将恢复原价 →"}}
        ],
        "goal_event": "purchase",
        "verify_window_hours": 48,
        "cooldown_hours": 72,
        "priority": 70,
        "description": "加购 2h 未购买 → 邮件提醒，目标 48h 内购买",
    },
    {
        "id": "first_purchase_celebrate",
        "name": "首单激活欢迎",
        "enabled": True,
        "trigger": {"type": "stage_enter", "stage": "paying"},
        "actions": [
            {"type": "webhook", "payload": {"subject": "新客欢迎", "text": "欢迎 {name} 成为付费用户"}},
            {"type": "openflow.webhook_insight",
             "payload": {"subject": "高价值客户信号", "text": "{name} 进入付费阶段，OpenFlow 已同步建档"}}
        ],
        "goal_event": None,
        "verify_window_hours": 0,
        "cooldown_hours": 720,
        "priority": 80,
        "description": "进入 paying 阶段 → 新客欢迎 + 回推 OpenFlow CDP 建档",
    },
    {
        "id": "churn_risk_rescue",
        "name": "流失风险挽回",
        "enabled": True,
        "trigger": {"type": "inactivity", "stage": "activated", "days": 14},
        "actions": [
            {"type": "feishu", "payload": {"subject": "流失风险预警", "text": "用户 {email} 已 14 天无行为，建议人工跟进"}},
            {"type": "ai.email", "payload": {"topic": "14 天未活跃老用户的召回邮件",
                                             "brief": "用户曾是活跃用户，两周没回来。给一个回访理由，不硬推销"}}
        ],
        "goal_event": None,  # 任意事件即算回流
        "verify_window_hours": 168,
        "cooldown_hours": 336,
        "priority": 50,
        "description": "激活用户 14 天无事件 → 飞书预警 + 邮件召回",
    },
    {
        "id": "advocate_prompt",
        "name": "推荐官培育",
        "enabled": True,
        "trigger": {"type": "stage_enter", "stage": "advocate"},
        "actions": [
            {"type": "email", "payload": {"subject": "邀请好友赢奖励", "text": "{name}，邀请好友双方各得优惠 →"}}
        ],
        "goal_event": "referral",
        "verify_window_hours": 336,
        "cooldown_hours": 720,
        "priority": 40,
        "description": "进入 advocate 阶段 → 推荐邀请邮件，目标 14 天内 referral",
    },
]


async def seed_templates(store: Store, data_dir: str) -> None:
    for t in BUILTIN_TEMPLATES:
        await store.put_template(t)
    ext_path = os.path.join(data_dir, "loop-templates.json")
    if os.path.exists(ext_path):
        try:
            with open(ext_path, encoding="utf-8") as f:
                extras = json.load(f)
            if isinstance(extras, list):
                for t in extras:
                    if isinstance(t, dict) and t.get("id"):
                        await store.put_template(t)
        except (json.JSONDecodeError, OSError):
            pass


CANVAS_BUILTINS: list[dict] = [
    {
        "id": "welcome_canvas",
        "name": "新手欢迎旅程",
        "enabled": True,
        "nodes": [
            {"id": "t1", "type": "trigger", "event": "signup"},
            {"id": "c1", "type": "condition",
             "rules": [{"field": "user.email", "op": "exists", "value": None}], "logic": "and"},
            {"id": "a1", "type": "action",
             "action": {"type": "email", "payload": {"subject": "欢迎加入", "text": "{name}，欢迎！先完成你的第一个项目 →"}}},
            {"id": "d1", "type": "delay", "minutes": 1440},
            {"id": "a2", "type": "action",
             "action": {"type": "feishu", "payload": {"subject": "次日未激活提醒", "text": "用户 {email} 注册 1 天，检查激活情况"}}},
            {"id": "x1", "type": "exit"},
        ],
        "edges": [
            {"from": "t1", "to": "c1"},
            {"from": "c1", "to": "a1", "label": "true"},
            {"from": "c1", "to": "x1", "label": "false"},
            {"from": "a1", "to": "d1"},
            {"from": "d1", "to": "a2", "label": "true"},
            {"from": "a2", "to": "x1"},
        ],
        "description": "signup → 有邮箱发欢迎邮件 → 等 1 天 → 飞书提醒跟进",
    },
    {
        "id": "vip_canvas",
        "name": "高价值客户识别",
        "enabled": False,
        "nodes": [
            {"id": "t1", "type": "trigger", "stage": "paying"},
            {"id": "c1", "type": "condition",
             "rules": [{"field": "stats.purchases", "op": "gte", "value": 1}], "logic": "and"},
            {"id": "a1", "type": "action",
             "action": {"type": "openflow.webhook_insight",
                        "payload": {"subject": "高价值客户", "text": "{name} 进入付费阶段，建议 OpenFlow 建档跟进"}}},
            {"id": "x1", "type": "exit"},
        ],
        "edges": [
            {"from": "t1", "to": "c1"},
            {"from": "c1", "to": "a1", "label": "true"},
            {"from": "a1", "to": "x1"},
        ],
        "description": "paying 阶段 → 推送 OpenFlow InboundReceiver（示例 disabled）",
    },
]


async def seed_canvas(store: Store, data_dir: str) -> None:
    for f in CANVAS_BUILTINS:
        await store.put_canvas(f)
    ext_path = os.path.join(data_dir, "canvas-flows.json")
    if os.path.exists(ext_path):
        try:
            with open(ext_path, encoding="utf-8") as fh:
                extras = json.load(fh)
            if isinstance(extras, list):
                for f in extras:
                    if isinstance(f, dict) and f.get("id") and f.get("nodes"):
                        await store.put_canvas(f)
        except (json.JSONDecodeError, OSError):
            pass

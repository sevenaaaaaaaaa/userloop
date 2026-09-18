"""运营资产市场（N2）：行业资产包的注册、导出、导入、应用与版本化审批.

设计原则：
- **只流转"定义"，不流转"数据"**：资产包只含 Loop 模板/画布/实验/分群规则，
  绝不含用户、事件、会话等任何租户数据（跨租户复用的前提是数据不共享）
- **导入幂等 + 可预览**：同 id 资产默认跳过；`dry_run` 先给出"将创建/跳过"清单
- **版本化**：每次落库快照一个版本（可查看/回滚）
- **审批**：`assets.require_approval=true` 时，导入/应用的资产以**停用态**落地，
  需管理员逐条批准后才生效（沿用家族的"人工授权"纪律）
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from userloop.core.store import Store, iso_now, new_id

ASSET_KINDS = ("loop_templates", "canvas_flows", "experiments", "segments")
PACKS_DIR = "asset-packs"

# ── 内置行业资产包（可经 data/asset-packs/*.json 覆盖或扩展）──

_BUILTIN_PACKS: list[dict[str, Any]] = [
    {
        "id": "ecommerce-growth", "name": "电商增长包", "industry": "电商", "version": "1.0.0",
        "description": "加购挽回 / 首单欢迎 / 复购提醒 / 流失挽回，含分群与 A/B 实验",
        "requires": ["email"],
        "loop_templates": [
            {"id": "ec_cart_abandon", "name": "加购未付挽回", "enabled": True, "priority": 70,
             "trigger": {"type": "event", "name": "add_to_cart"},
             "actions": [{"type": "ai.email", "delay_minutes": 120,
                          "payload": {"topic": "购物车挽回", "brief": "用户加购未付款，给出限时理由"}}],
             "goal_event": "purchase", "verify_window_hours": 48, "cooldown_hours": 72},
            {"id": "ec_first_order_welcome", "name": "首单欢迎", "enabled": True, "priority": 80,
             "trigger": {"type": "stage_enter", "stage": "paying"},
             "actions": [{"type": "touch.email",
                          "payload": {"subject": "欢迎成为会员", "text": "{name}，首单权益已到账",
                                      "cta_text": "查看订单", "cta_url": "https://nownexts.com/"}}],
             "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 720},
            {"id": "ec_repurchase_nudge", "name": "复购提醒", "enabled": True, "priority": 60,
             "trigger": {"type": "inactivity", "stage": "paying", "days": 30},
             "actions": [{"type": "ai.email", "payload": {"topic": "复购提醒",
                                                          "brief": "付费用户 30 天未复购，给出新品/权益理由"}}],
             "goal_event": "purchase", "verify_window_hours": 168, "cooldown_hours": 336},
        ],
        "experiments": [
            {"id": "ec_cta_urgency", "name": "挽回邮件 CTA A/B", "enabled": True, "channel": "email",
             "match_templates": ["ec_cart_abandon"], "metric": "click_rate", "goal_event": "purchase",
             "min_samples": 20, "confidence": 0.9, "window_days": 14,
             "variants": [
                 {"id": "A", "weight": 50, "name": "温和", "overrides": {"cta_text": "回到购物车"}},
                 {"id": "B", "weight": 50, "name": "紧迫", "overrides": {"cta_text": "立即完成（库存告急）"}}]},
        ],
        "segments": [
            {"id": "seg_ec_high_value", "name": "高价值客户", "logic": "and",
             "rules": [{"field": "purchases", "op": "gte", "value": 2}, {"field": "total_amount", "op": "gte", "value": 500}]},
            {"id": "seg_ec_churn_risk", "name": "流失风险付费用户", "logic": "and",
             "rules": [{"field": "stage", "op": "in", "value": ["paying", "retained"]},
                       {"field": "silence_days", "op": "gte", "value": 30}]},
        ],
    },
    {
        "id": "saas-onboarding", "name": "SaaS 增长包", "industry": "SaaS", "version": "1.0.0",
        "description": "注册激活 / 试用到期 / 流失召回，适合 PLG 产品",
        "requires": ["email"],
        "loop_templates": [
            {"id": "saas_activation", "name": "注册未激活引导", "enabled": True, "priority": 65,
             "trigger": {"type": "inactivity", "stage": "signup", "days": 1},
             "actions": [{"type": "ai.email", "payload": {"topic": "完成首个关键动作",
                                                          "brief": "注册一天未激活，引导完成第一个项目"}}],
             "goal_event": "activation", "verify_window_hours": 72, "cooldown_hours": 168},
            {"id": "saas_trial_expiry", "name": "试用到期提醒", "enabled": True, "priority": 75,
             "trigger": {"type": "inactivity", "stage": "activated", "days": 7},
             "actions": [{"type": "touch.email",
                          "payload": {"subject": "你的权益还在", "text": "继续使用并解锁高级能力",
                                      "cta_text": "查看方案", "cta_url": "https://nownexts.com/pricing"}}],
             "goal_event": "purchase", "verify_window_hours": 168, "cooldown_hours": 168},
            {"id": "saas_churn_rescue", "name": "流失召回", "enabled": True, "priority": 50,
             "trigger": {"type": "inactivity", "stage": "paying", "days": 21},
             "actions": [{"type": "feishu", "payload": {"subject": "流失预警", "text": "付费用户 {email} 已 21 天无行为"}},
                         {"type": "ai.email", "payload": {"topic": "召回邮件", "brief": "付费用户沉默 21 天，重述价值并给回访理由"}}],
             "goal_event": None, "verify_window_hours": 168, "cooldown_hours": 336},
        ],
        "experiments": [
            {"id": "saas_onboarding_subject", "name": "激活邮件主题 A/B", "enabled": True, "channel": "email",
             "match_templates": ["saas_activation"], "metric": "click_rate", "goal_event": "activation",
             "min_samples": 20, "confidence": 0.9, "window_days": 14,
             "variants": [
                 {"id": "A", "weight": 50, "name": "收益导向", "overrides": {"subject_suffix": ""}},
                 {"id": "B", "weight": 50, "name": "损失导向", "overrides": {"subject_suffix": "（别浪费试用期）"}}]},
        ],
        "segments": [
            {"id": "seg_saas_pql", "name": "高意向试用用户", "logic": "and",
             "rules": [{"field": "stage", "op": "eq", "value": "activated"},
                       {"field": "has_event", "op": "eq", "value": "view_pricing"}]},
        ],
    },
    {
        "id": "education-enroll", "name": "教育转化包", "industry": "教育", "version": "1.0.0",
        "description": "试听转化 / 完课激励 / 续费提醒",
        "requires": ["email"],
        "loop_templates": [
            {"id": "edu_trial_followup", "name": "试听后跟进", "enabled": True, "priority": 70,
             "trigger": {"type": "event", "name": "trial_lesson_finished"},
             "actions": [{"type": "ai.email", "delay_minutes": 60,
                          "payload": {"topic": "试听跟进", "brief": "用户刚完成试听，解答疑问并引导报名"}}],
             "goal_event": "purchase", "verify_window_hours": 72, "cooldown_hours": 168},
            {"id": "edu_lesson_progress", "name": "完课激励", "enabled": True, "priority": 60,
             "trigger": {"type": "event", "name": "lesson_completed"},
             "actions": [{"type": "touch.email", "payload": {"subject": "继续保持！", "text": "你已完成本节，下一节更有意思",
                                                              "cta_text": "继续学习", "cta_url": "https://nownexts.com/"}}],
             "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 24},
        ],
        "segments": [
            {"id": "seg_edu_active_learner", "name": "活跃学员", "logic": "and",
             "rules": [{"field": "stage", "op": "in", "value": ["paying", "retained"]},
                       {"field": "silence_days", "op": "lte", "value": 3}]},
        ],
    },
    {
        "id": "local-service", "name": "本地服务包", "industry": "本地服务", "version": "1.0.0",
        "description": "预约提醒 / 到店回访 / 会员复购（短信为主）",
        "requires": ["sms"],
        "loop_templates": [
            {"id": "ls_appointment_remind", "name": "预约提醒", "enabled": True, "priority": 80,
             "trigger": {"type": "event", "name": "appointment_booked"},
             "actions": [{"type": "touch.sms", "delay_minutes": 1440,
                          "payload": {"text": "你的预约在明天，记得按时到店哦"}}],
             "goal_event": None, "verify_window_hours": 0, "cooldown_hours": 12},
            {"id": "ls_post_visit", "name": "到店后回访", "enabled": True, "priority": 60,
             "trigger": {"type": "event", "name": "visit_completed"},
             "actions": [{"type": "touch.sms", "delay_minutes": 120,
                          "payload": {"text": "感谢到店，有问题随时联系我们"}}],
             "goal_event": "purchase", "verify_window_hours": 336, "cooldown_hours": 336},
        ],
    },
]


def builtin_packs() -> list[dict[str, Any]]:
    return [dict(p) for p in _BUILTIN_PACKS]


def load_packs(data_dir: str) -> list[dict[str, Any]]:
    """内置包 + data/asset-packs/*.json（同 id 覆盖）。"""
    packs: dict[str, dict] = {p["id"]: dict(p) for p in builtin_packs()}
    path = os.path.join(data_dir, PACKS_DIR)
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(path, name), encoding="utf-8") as f:
                    pack = json.load(f)
                if isinstance(pack, dict) and pack.get("id"):
                    packs[pack["id"]] = pack
            except (json.JSONDecodeError, OSError):
                continue
    return list(packs.values())


def get_pack(data_dir: str, pack_id: str) -> dict | None:
    return next((p for p in load_packs(data_dir) if p["id"] == pack_id), None)


def summarize(pack: dict) -> dict[str, Any]:
    return {"id": pack.get("id"), "name": pack.get("name"), "industry": pack.get("industry"),
            "version": pack.get("version"), "description": pack.get("description"),
            "requires": pack.get("requires") or [],
            "counts": {k: len(pack.get(k) or []) for k in ASSET_KINDS}}


async def export_assets(store: Store, name: str = "租户资产导出") -> dict[str, Any]:
    """导出：Loop 模板 + 画布 + 实验 + 分群（仅定义，绝不含数据）。"""
    templates = await store.get_templates(enabled_only=False)
    canvas = await store.list_canvas(enabled_only=False)
    experiments = await store.list_experiments(enabled_only=False)
    segments = await store.list_segments()
    return {
        "id": f"export-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        "name": name, "industry": "custom", "version": "1.0.0",
        "description": "由租户导出的运营资产（仅定义，不含用户数据）", "requires": [],
        "loop_templates": templates, "canvas_flows": canvas,
        "experiments": experiments, "segments": segments,
        "exported_at": iso_now(),
    }


async def import_assets(store: Store, pack: dict, dry_run: bool = False,
                        require_approval: bool = False, prefix: str = "") -> dict[str, Any]:
    """导入资产包到当前租户：幂等（同 id 跳过）、可预览、可挂审批.

    prefix: 给资产 id 加前缀，便于同租户多套包共存（如 "ec_"）。
    """
    created: dict[str, list[str]] = {k: [] for k in ASSET_KINDS}
    skipped: dict[str, list[str]] = {k: [] for k in ASSET_KINDS}
    pending: list[str] = []

    async def _id_conflict(kind: str, item_id: str) -> bool:
        if kind == "loop_templates":
            return any(t["id"] == item_id for t in await store.get_templates(enabled_only=False))
        if kind == "canvas_flows":
            return any(c["id"] == item_id for c in await store.list_canvas(enabled_only=False))
        if kind == "experiments":
            return any(e["id"] == item_id for e in await store.list_experiments(enabled_only=False))
        return any(s["id"] == item_id for s in await store.list_segments())

    for kind in ASSET_KINDS:
        for raw in (pack.get(kind) or []):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            item = json.loads(json.dumps(raw))          # 深拷贝，避免污染包定义
            if prefix and kind != "experiments":        # 实验 id 关联模板名，前缀会断链，故不加前缀
                item["id"] = f"{prefix}{item['id']}"
            elif prefix and kind == "experiments":
                item["id"] = f"{prefix}{item['id']}"
                item["match_templates"] = [f"{prefix}{t}" for t in (item.get("match_templates") or [])]
            if await _id_conflict(kind, item["id"]):
                skipped[kind].append(item["id"])
                continue
            if dry_run:
                created[kind].append(item["id"])
                continue
            if kind == "loop_templates":
                if require_approval:
                    item["enabled"] = False              # 审批前不生效
                await store.put_template(item)
                await store.snapshot_asset("loop_template", item["id"], item, note=f"import:{pack.get('id')}")
                if require_approval:
                    pending.append(item["id"])
            elif kind == "canvas_flows":
                if require_approval:
                    item["enabled"] = False
                await store.put_canvas(item)
                await store.snapshot_asset("canvas_flow", item["id"], item, note=f"import:{pack.get('id')}")
                if require_approval:
                    pending.append(item["id"])
            elif kind == "experiments":
                if require_approval:
                    item["enabled"] = False
                await store.put_experiment(item)
                await store.snapshot_asset("experiment", item["id"], item, note=f"import:{pack.get('id')}")
                if require_approval:
                    pending.append(item["id"])
            else:
                await store.put_segment(item)
            created[kind].append(item["id"])

    if not dry_run:
        await store.put_asset_pack({"id": pack.get("id"), "name": pack.get("name"),
                                    "version": pack.get("version"), "industry": pack.get("industry"),
                                    "imported_at": iso_now(), "created": created, "skipped": skipped,
                                    "pending_approval": pending})
    return {"ok": True, "pack": pack.get("id"), "dry_run": dry_run,
            "created": created, "skipped": skipped, "pending_approval": pending,
            "counts": {k: len(v) for k, v in created.items()},
            "requires": pack.get("requires") or []}

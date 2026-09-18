"""inFlow（增长情报 OS）→ UserLoop：把"洞察"翻译成"可执行的运营 Loop".

方向：**inFlow → UserLoop（洞察流入）**，以及 **UserLoop → inFlow（回执 ack，标记已行动）**。
这是生态里"情报 → 动作"的最短闭环：竞品异动/流量骤降/转化偏低 自动变成本租户的 Loop 草稿（默认 disabled，人工启用）。

纪律（与家族一致）：
- 只调线上 API，不碰对方代码/数据
- AI/规则产出的 Loop 一律**草稿态**（enabled=False），启用由人确认
- 动作必须落在 UserLoop 的白名单内（复用 copilot.validate 强校验）
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store, iso_now, new_id

# 洞察类型 → Loop 配方（可被租户配置覆盖）
INSIGHT_RECIPES: dict[str, dict[str, Any]] = {
    "traffic_anomaly": {
        "name": "流量异动 → 沉默用户召回",
        "trigger": {"type": "inactivity", "stage": "activated", "days": 7},
        "actions": [
            {"type": "touch.email", "payload": {"subject": "最近还好吗？有新内容想给你看",
                                                "text": "我们注意到你有一阵没来了，这里有一些更新值得一看。",
                                                "cta_text": "回来看看", "cta_url": "https://nownexts.com/"}},
        ],
        "goal_event": "activation",
    },
    "conversion_low": {
        "name": "转化偏低 → 注册未激活引导",
        "trigger": {"type": "inactivity", "stage": "signup", "days": 1},
        "actions": [
            {"type": "touch.h5", "payload": {"title": "完成这一步就绪", "text": "只差一步就能开始使用全部功能。",
                                             "cta_text": "立即完成", "cta_url": "https://nownexts.com/"}},
        ],
        "goal_event": "activation",
    },
    "keyword_opportunity": {
        "name": "关键词机会 → 内容选题（MFlow）",
        "trigger": {"type": "stage_enter", "stage": "advocate"},
        "actions": [
            {"type": "mflow.create_content", "payload": {"topic": "{insight_title}",
                                                         "brief": "{insight_summary}"}},
            {"type": "feishu", "payload": {"subject": "情报选题已入队", "text": "{insight_title}"}},
        ],
        "goal_event": None,
    },
    "competitor_move": {
        "name": "竞品异动 → 内容对抗 + 团队预警",
        "trigger": {"type": "stage_enter", "stage": "churn_risk"},
        "actions": [
            {"type": "feishu", "payload": {"subject": "竞品异动", "text": "{insight_summary}"}},
            {"type": "mflow.create_content", "payload": {"topic": "对比 | {insight_title}",
                                                         "brief": "{insight_summary}"}},
        ],
        "goal_event": None,
    },
    "competitor_pricing": {
        "name": "竞品定价 → 高价值用户挽留",
        "trigger": {"type": "stage_enter", "stage": "paying"},
        "actions": [
            {"type": "feishu", "payload": {"subject": "竞品定价变动", "text": "{insight_summary}"}},
            {"type": "touch.email", "payload": {"subject": "你的权益升级了", "text": "我们为你准备了新的权益。",
                                                "cta_text": "查看权益", "cta_url": "https://nownexts.com/"}},
        ],
        "goal_event": "purchase",
    },
    # ── inFlow 真实产出的洞察类型（按需扩展）──
    "rfm_at_risk": {
        "name": "RFM 风险客户 → 挽回",
        "trigger": {"type": "stage_enter", "stage": "churn_risk"},
        "actions": [
            {"type": "touch.email", "payload": {"subject": "专属回归礼已备好",
                                                "text": "你有一份专属权益待领取，回来即可使用。",
                                                "cta_text": "领取权益", "cta_url": "https://nownexts.com/"}},
            {"type": "feishu", "payload": {"subject": "RFM 风险客户", "text": "{insight_summary}"}},
        ],
        "goal_event": "purchase",
    },
    "retention_decline": {
        "name": "留存下降 → 沉默召回",
        "trigger": {"type": "inactivity", "stage": "retained", "days": 14},
        "actions": [{"type": "touch.email", "payload": {"subject": "好久不见，有新内容",
                                                        "text": "我们更新了不少能力，值得你回来看看。",
                                                        "cta_text": "回来看看", "cta_url": "https://nownexts.com/"}}],
        "goal_event": "activation",
    },
    "journey_gap": {
        "name": "旅程断点 → 激活引导",
        "trigger": {"type": "inactivity", "stage": "signup", "days": 2},
        "actions": [{"type": "touch.h5", "payload": {"title": "继续未完成的旅程",
                                                     "text": "只差一步就能解锁全部功能。",
                                                     "cta_text": "立即继续", "cta_url": "https://nownexts.com/"}}],
        "goal_event": "activation",
    },
    "journey_content_gap": {
        "name": "旅程内容缺口 → 内容生产 + 引导",
        "trigger": {"type": "inactivity", "stage": "activated", "days": 7},
        "actions": [
            {"type": "mflow.create_content", "payload": {"topic": "补齐旅程内容：{insight_title}",
                                                         "brief": "{insight_summary}"}},
            {"type": "touch.h5", "payload": {"title": "为你补上这一步", "text": "{insight_summary}",
                                             "cta_text": "查看", "cta_url": "https://nownexts.com/"}},
        ],
        "goal_event": "activation",
    },
    "topic_negative_alert": {
        "name": "舆情负面 → 危机响应（预警 + 内容反击）",
        "trigger": {"type": "stage_enter", "stage": "advocate"},
        "actions": [
            {"type": "feishu", "payload": {"subject": "⚠ 舆情负面预警", "text": "{insight_summary}"}},
            {"type": "mflow.create_content", "payload": {"topic": "澄清/正面内容：{insight_title}",
                                                         "brief": "{insight_summary}"}},
        ],
        "goal_event": None,
    },
    "ltv_cac_unhealthy": {
        "name": "LTV:CAC 不健康 → 高价值运营",
        "trigger": {"type": "stage_enter", "stage": "paying"},
        "actions": [
            {"type": "feishu", "payload": {"subject": "单位经济预警", "text": "{insight_summary}"}},
            {"type": "openflow.webhook_insight", "payload": {"subject": "高价值客户信号",
                                                             "text": "{insight_summary}"}},
        ],
        "goal_event": "purchase",
    },
    "keyword_gap": {
        "name": "关键词缺口 → 内容补位",
        "trigger": {"type": "stage_enter", "stage": "advocate"},
        "actions": [{"type": "mflow.create_content", "payload": {"topic": "内容补位：{insight_title}",
                                                                 "brief": "{insight_summary}"}}],
        "goal_event": None,
    },
    "nps_shift": {
        "name": "NPS 变化 → 团队预警",
        "trigger": {"type": "event", "name": "nps_submit"},
        "actions": [{"type": "feishu", "payload": {"subject": "NPS 变动", "text": "{insight_summary}"}}],
        "goal_event": None,
    },
    "site_change": {
        "name": "站点变更 → 团队预警",
        "trigger": {"type": "event", "name": "site_changed"},
        "actions": [{"type": "feishu", "payload": {"subject": "站点变更", "text": "{insight_summary}"}}],
        "goal_event": None,
    },
    "topic_digest": {
        "name": "舆情摘要 → 团队日报",
        "trigger": {"type": "event", "name": "topic_digest_ready"},
        "actions": [{"type": "feishu", "payload": {"subject": "舆情摘要", "text": "{insight_summary}"}}],
        "goal_event": None,
    },
}
DEFAULT_RECIPE = {
    "name": "情报跟进",
    "trigger": {"type": "stage_enter", "stage": "activated"},
    "actions": [{"type": "feishu", "payload": {"subject": "新情报", "text": "{insight_title}"}}],
    "goal_event": None,
}


def cfg_of(ctx: ExecutorContext) -> dict[str, Any]:
    raw = ((ctx.config.get("integrations") or {}).get("inflow") or {})
    return {"base_url": raw.get("base_url"), "workspace_id": raw.get("workspace_id"),
            "token": raw.get("token"), "status": raw.get("status", "new"),
            "min_severity": raw.get("min_severity", "medium"),
            "auto_enable_low_risk": bool(raw.get("auto_enable_low_risk", False)),
            "ack_back": bool(raw.get("ack_back", True))}


_SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


async def fetch_insights(cfg: dict, limit: int = 20, transport: Any = None) -> list[dict]:
    base = str(cfg.get("base_url") or "").rstrip("/")
    if not (base and cfg.get("workspace_id")):
        return []
    params = {"workspace_id": cfg["workspace_id"], "limit": limit}
    if cfg.get("status"):
        params["status"] = cfg["status"]
    headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
    try:
        async with httpx.AsyncClient(timeout=12, transport=transport) as client:
            resp = await client.get(f"{base}/api/v1/insights", params=params, headers=headers)
        data = resp.json() if resp.status_code == 200 else {}
        return data.get("insights") or []
    except Exception:  # noqa: BLE001
        return []


async def ack_insight(cfg: dict, insight_id: str, transport: Any = None) -> bool:
    base = str(cfg.get("base_url") or "").rstrip("/")
    if not base:
        return False
    headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.post(f"{base}/api/v1/insights/{insight_id}/ack", headers=headers)
        return 200 <= resp.status_code < 300
    except Exception:  # noqa: BLE001
        return False


def recipe_for(insight_type: str) -> dict[str, Any]:
    return INSIGHT_RECIPES.get(insight_type) or DEFAULT_RECIPE


def _render(template: str, insight: dict) -> str:
    return (template.replace("{insight_title}", str(insight.get("title") or "")[:80])
            .replace("{insight_summary}", str(insight.get("summary") or "")[:200]))


def draft_from_insight(insight: dict) -> dict[str, Any]:
    """洞察 → Loop 草稿（白名单强校验；默认 disabled，人工启用）。"""
    from userloop.ai.copilot import validate

    recipe = recipe_for(str(insight.get("type") or ""))
    actions = []
    for spec in recipe.get("actions", []):
        payload = {k: (_render(v, insight) if isinstance(v, str) else v)
                   for k, v in (spec.get("payload") or {}).items()}
        actions.append({"type": spec.get("type"), "payload": payload,
                        "delay_minutes": spec.get("delay_minutes", 0)})
    draft, warnings = validate({
        "name": recipe["name"],
        "description": f"来源 inFlow 洞察：{insight.get('title', '')}（{insight.get('type', '')}）",
        "trigger": recipe["trigger"],
        "actions": actions,
        "goal_event": recipe.get("goal_event"),
        "cooldown_hours": 168,
    })
    draft["source_insight"] = insight.get("id")
    return {"draft": draft, "warnings": warnings, "recipe": recipe["name"]}


async def sync(store: Store, ctx: ExecutorContext, limit: int = 20, transport: Any = None,
               auto_enable: bool | None = None) -> dict[str, Any]:
    """拉取 inFlow 新洞察 → 镜像入库 + 生成 Loop 草稿（+ 可选回执 ack）。"""
    cfg = cfg_of(ctx)
    if not (cfg.get("base_url") and cfg.get("workspace_id")):
        return {"ok": False, "error": "未配置 integrations.inflow（base_url/workspace_id）"}
    insights = await fetch_insights(cfg, limit=limit, transport=transport)
    min_rank = _SEV_RANK.get(str(cfg.get("min_severity") or "medium"), 2)
    enable = cfg["auto_enable_low_risk"] if auto_enable is None else auto_enable

    created, skipped, drafts = [], [], []
    for ins in insights:
        iid = str(ins.get("id") or "")
        if not iid or await store.get_external_insight("inflow", iid):
            skipped.append(iid)
            continue
        if _SEV_RANK.get(str(ins.get("severity") or "medium"), 2) < min_rank:
            skipped.append(iid)
            continue
        out = draft_from_insight(ins)
        draft = out["draft"]
        # 低风险（不含用户外发动作）可配置为自动启用，否则保持草稿待人工
        # 只要包含用户外发或内容产出动作，就不自动启用（一律人工确认）
        has_outbound = any(a["type"].startswith(("touch.", "ai.", "email", "feishu", "mflow.", "openflow."))
                           for a in draft["actions"])
        enabled = bool(enable and not has_outbound)
        await store.put_template({**draft, "enabled": enabled})
        await store.put_external_insight({
            "id": new_id("ins"), "source": "inflow", "external_id": iid,
            "type": ins.get("type"), "severity": ins.get("severity"),
            "confidence": ins.get("confidence"), "title": ins.get("title"),
            "summary": ins.get("summary"), "payload": ins,
            "loop_id": draft["id"], "enabled": enabled, "status": "actioned",
        })
        created.append(iid)
        drafts.append({"insight": iid, "loop_id": draft["id"], "recipe": out["recipe"],
                       "enabled": enabled, "warnings": out["warnings"]})
        if cfg.get("ack_back"):
            await ack_insight(cfg, iid, transport=transport)
    return {"ok": True, "fetched": len(insights), "created": len(created),
            "skipped": len(skipped), "drafts": drafts}

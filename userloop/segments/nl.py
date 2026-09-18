"""自然语言分群：一句话 → 可执行人群规则（与 Copilot 组成"对话即运营"）.

设计纪律与 Copilot 一致：
- AI 只产**规则 JSON**，服务端强校验字段/操作符/取值（越权即裁剪并 warning）
- 支持字段（白名单）：stage / stats.*（purchases/total_amount/event_count）/ props.* /
  score.*（churn/ltv/propensity/tier）/ silence_days / first_seen_days / event（含某事件）
- 求值：拉取候选用户（默认仅实名，避免匿名噪音），Python 侧批量判定（有界）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store, new_id

FIELDS = ("stage", "silence_days", "first_seen_days", "purchases", "total_amount", "event_count",
          "churn", "ltv", "propensity", "tier", "has_event", "props")
OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "in", "contains", "exists", "empty")

SYSTEM = """你是 UserLoop 的人群分群器。把自然语言需求翻译成**人群规则 JSON**。

输出严格 JSON（不要解释、不要 markdown 围栏）：
{"name": "分群名称（中文，简洁）", "logic": "and|or", "rules": [
  {"field": "<字段>", "op": "<操作符>", "value": <值>}
]}

可用字段（只能从这里选）：
- stage            生命周期阶段：visitor/signup/activated/paying/retained/advocate/churn_risk/churned
- silence_days     沉默天数（数字）
- first_seen_days  注册天数（数字）
- purchases        累计付费次数（数字）
- total_amount     累计金额（数字）
- event_count      事件总数（数字）
- churn / ltv / propensity  预测分数 0-1（数字）
- tier             LTV 分层：low/mid/high/vip
- has_event        是否发生过某事件（value 写事件名，如 purchase / view_pricing / add_to_cart）
操作符：eq / neq / gt / gte / lt / lte / in / contains / exists / empty

规则：
1. 一律用上述字段表达，不要发明字段；"最近 N 天"用 silence_days ≤ N 表达
2. 组合条件用 logic=and；互斥场景用 or
3. 最多 5 条规则"""


def _clamp_rules(raw: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    rules: list[dict[str, Any]] = []
    for r in (raw.get("rules") or [])[:5]:
        if not isinstance(r, dict):
            continue
        field, op = str(r.get("field") or ""), str(r.get("op") or "eq")
        if field not in FIELDS:
            warnings.append(f"字段 {field or '(空)'} 不支持，已丢弃")
            continue
        if op not in OPS:
            warnings.append(f"操作符 {op} 不支持，已改为 eq")
            op = "eq"
        rules.append({"field": field, "op": op, "value": r.get("value")})
    if not rules:
        warnings.append("无合法规则，已回退为「付费用户」")
        rules = [{"field": "stage", "op": "in", "value": ["paying", "retained", "advocate"]}]
    logic = "or" if str(raw.get("logic") or "and").lower() == "or" else "and"
    return {"id": str(raw.get("id") or new_id("seg")), "name": str(raw.get("name") or "AI 分群")[:40],
            "logic": logic, "rules": rules}, warnings


async def draft(ctx: ExecutorContext, prompt: str, transport: Any = None) -> dict[str, Any]:
    from userloop.integrations.ai import chat

    from userloop.i18n import t as _t

    lang = _t("ai.language", ctx.config.get("locale") or "zh-CN")
    res = await chat(ctx, [{"role": "system", "content": SYSTEM + f"\n\n输出语言要求：{lang}"},
                           {"role": "user", "content": f"需求：{prompt}"}],
                     transport=transport, max_tokens=600)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "AI 不可用", "degraded": True}
    try:
        raw = json.loads(res.get("content") or "{}")
    except json.JSONDecodeError:
        raw = {}
    seg, warnings = _clamp_rules(raw if isinstance(raw, dict) else {})
    return {"ok": True, "segment": seg, "warnings": warnings, "model": res.get("model")}


def _user_facts(user: dict, score: dict | None, events: list[dict], now: datetime) -> dict[str, Any]:
    stats = user.get("stats")
    if isinstance(stats, str):
        try:
            stats = json.loads(stats or "{}")
        except json.JSONDecodeError:
            stats = {}
    stats = stats if isinstance(stats, dict) else {}
    props = user.get("props")
    if isinstance(props, str):
        try:
            props = json.loads(props or "{}")
        except json.JSONDecodeError:
            props = {}
    props = props if isinstance(props, dict) else {}

    def days(ts: str | None) -> float:
        if not ts:
            return 9999.0
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return 9999.0
        return (now - t).total_seconds() / 86400

    names = {e["event"] for e in events}
    return {
        "stage": user.get("stage", "visitor"),
        "silence_days": days(user.get("last_seen")),
        "first_seen_days": days(user.get("first_seen")),
        "purchases": stats.get("purchases", 0),
        "total_amount": stats.get("total_amount", stats.get("amount", 0)),
        "event_count": stats.get("event_count", len(events)),
        "_events": names,
        "churn": (score or {}).get("churn", 0),
        "ltv": (score or {}).get("ltv", 0),
        "propensity": (score or {}).get("propensity", 0),
        "tier": (score or {}).get("tier", ""),
        "props": props,
    }


def match_rule(facts: dict, rule: dict) -> bool:
    field, op, val = rule["field"], rule["op"], rule.get("value")
    if field == "has_event":
        evs = facts.get("_events") or set()
        return str(val) in evs if op in ("eq", "exists", "contains") else str(val) not in evs
    actual = facts.get(field)
    if isinstance(actual, dict) and field == "props" and isinstance(val, dict):
        return all(actual.get(k) == v for k, v in val.items())
    try:
        if op == "eq":
            return actual == val
        if op == "neq":
            return actual != val
        if op == "gt":
            return float(actual or 0) > float(val)
        if op == "gte":
            return float(actual or 0) >= float(val)
        if op == "lt":
            return float(actual or 0) < float(val)
        if op == "lte":
            return float(actual or 0) <= float(val)
        if op == "in":
            return actual in (val if isinstance(val, (list, tuple)) else [val])
        if op == "contains":
            return isinstance(actual, str) and str(val) in actual
        if op == "exists":
            return actual not in (None, "", [], {})
        if op == "empty":
            return actual in (None, "", [], {})
    except (TypeError, ValueError):
        return False
    return False


async def preview(store: Store, segment: dict, limit: int = 2000) -> dict[str, Any]:
    """求值：返回命中用户数与样例（默认只扫实名用户，避免匿名噪音）。"""
    cur = await store.db.execute(
        "SELECT * FROM users WHERE stage != 'visitor' ORDER BY last_seen DESC LIMIT ?", (limit,))
    users = [dict(r) for r in await cur.fetchall()]
    now = datetime.utcnow()
    matched: list[dict] = []
    for u in users:
        score = await store.get_user_score(u["id"])
        events = await store.recent_events_for_user(u["id"], limit=200)
        facts = _user_facts(u, score, events, now)
        results = [match_rule(facts, r) for r in segment["rules"]]
        hit = all(results) if segment.get("logic") == "and" else any(results)
        if hit:
            matched.append({"user_id": u["id"], "distinct_id": u["distinct_id"], "email": u.get("email"),
                            "stage": u.get("stage"), "churn": facts["churn"], "ltv": facts["ltv"]})
    return {"count": len(matched), "scanned": len(users), "sample": matched[:20],
            "segment": segment}


async def save(store: Store, segment: dict) -> str:
    await store.put_segment(segment)
    return segment["id"]

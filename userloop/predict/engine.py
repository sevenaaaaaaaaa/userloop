"""预测层（v1，可解释规则模型）：churn 风险 / LTV 分层 / 购买倾向.

为什么先用规则而不是训练模型：
- 观众（运营）需要**可解释**的分数（"为什么这个用户被认为是高风险"），黑盒分数无法指导动作
- 当前样本量（实名用户数十级）不足以训练稳定模型；规则版可先用起来，并把**特征与标签沉淀**下来
  （events + user_scores + 后续真实转化），为将来拟合模型准备好训练数据

三类分数（0-1，越大越强）：
- **churn**：流失风险（沉默天数 vs 个人历史节奏、阶段、活跃趋势）
- **ltv**：价值分层（付费次数/金额、关系时长、近期活跃）→ tier: low/mid/high/vip
- **propensity**：购买倾向（关键行为信号：看定价、加购未付、近期激活、邮件点击）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from userloop.core.store import Store, iso_now

PURCHASE_EVENTS = {"purchase", "order.paid", "payment_success"}
INTENT_EVENTS = {"view_pricing", "pricing_view", "add_to_cart", "cart_added", "plan_compare", "upgrade_click"}
ENGAGE_EVENTS = {"email_click", "h5_click", "element_click", "page_view"}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _days_since(ts: datetime | None) -> float:
    if not ts:
        return 9999.0
    return (datetime.utcnow() - ts).total_seconds() / 86400


def churn_risk(user: dict, events: list[dict], stats: dict) -> dict[str, Any]:
    """流失风险 0-1 + 归因（可解释）。"""
    reasons: list[str] = []
    score = 0.0
    stage = user.get("stage", "visitor")

    times = sorted(t for t in (_parse(e["created_at"]) for e in events) if t)
    last = times[-1] if times else _parse(user.get("last_seen"))
    silence = _days_since(last)

    # 1) 个人历史节奏：沉默天数 / 历史中位间隔
    gaps = [(times[i + 1] - times[i]).total_seconds() / 86400 for i in range(len(times) - 1)]
    gaps = [g for g in gaps if g > 0]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 7.0
    ratio = silence / max(median_gap, 0.5)
    if median_gap < 9999 and ratio >= 3:
        score += min(0.5, 0.15 * ratio)
        reasons.append(f"沉默 {silence:.1f} 天，约为其历史活跃间隔的 {ratio:.1f} 倍")

    # 2) 活跃趋势：近 7 天 vs 前 7 天
    now = datetime.utcnow()
    recent = sum(1 for t in times if (now - t).days < 7)
    prior = sum(1 for t in times if 7 <= (now - t).days < 14)
    if prior > 0 and recent < prior:
        drop = 1 - recent / prior
        score += 0.2 * drop
        reasons.append(f"近 7 天活跃较前 7 天下降 {drop*100:.0f}%")
    if recent == 0 and times:
        score += 0.15
        reasons.append("近 7 天零活跃")

    # 3) 阶段权重（付费用户流失代价更高）
    if stage in ("paying", "retained", "advocate") and silence >= 7:
        score += 0.2
        reasons.append(f"付费/留存阶段已沉默 {silence:.0f} 天")

    # 4) 从未激活 / 注册后停滞
    if stage == "signup" and _days_since(_parse(user.get("first_seen"))) >= 2:
        score += 0.15
        reasons.append("注册后 2 天仍未激活")

    score = max(0.0, min(1.0, score))
    if not reasons:
        reasons.append("活跃节奏正常，暂无流失信号")
    return {"score": round(score, 3), "reasons": reasons, "silence_days": round(silence, 1),
            "median_gap_days": round(median_gap, 1)}


def ltv(user: dict, stats: dict) -> dict[str, Any]:
    """价值分层 0-1 + tier（付费次数/金额/关系时长）。"""
    purchases = int(stats.get("purchases", 0) or 0)
    amount = float(stats.get("total_amount", 0) or stats.get("amount", 0) or 0)
    tenure = _days_since(_parse(user.get("first_seen")))
    reasons: list[str] = []

    score = 0.0
    score += min(0.5, purchases * 0.18)
    if purchases:
        reasons.append(f"已付费 {purchases} 次")
    if amount > 0:
        score += min(0.3, amount / 3000.0)
        reasons.append(f"累计金额约 {amount:.0f}")
    if tenure >= 30:
        score += 0.15
        reasons.append(f"关系时长 {tenure:.0f} 天")
    score = max(0.0, min(1.0, score))

    tier = ("vip" if score >= 0.75 else "high" if score >= 0.5
            else "mid" if score >= 0.25 else "low")
    if not reasons:
        reasons.append("尚无付费/长期关系信号")
    return {"score": round(score, 3), "tier": tier, "reasons": reasons, "purchases": purchases}


def propensity(user: dict, events: list[dict], target: str = "purchase") -> dict[str, Any]:
    """购买倾向 0-1（行为意图信号）。"""
    names = [e["event"] for e in events]
    reasons: list[str] = []
    score = 0.0

    intent_hits = sum(1 for n in names if n in INTENT_EVENTS)
    if intent_hits:
        score += min(0.45, 0.15 * intent_hits)
        reasons.append(f"出现 {intent_hits} 次购买意向行为（看定价/加购/对比）")
    if "add_to_cart" in names or "cart_added" in names:
        if not any(n in PURCHASE_EVENTS for n in names):
            score += 0.25
            reasons.append("加购后未付款（高意向未转化）")
    clicks = sum(1 for n in names if n in {"email_click", "h5_click"})
    if clicks >= 2:
        score += 0.15
        reasons.append(f"近期内容点击 {clicks} 次（互动度高）")
    if user.get("stage") == "activated":
        score += 0.1
        reasons.append("已激活但未付费")
    score = max(0.0, min(1.0, score))
    if not reasons:
        reasons.append("暂无明确购买意向信号")
    return {"score": round(score, 3), "reasons": reasons, "intent_hits": intent_hits}


async def compute(store: Store, user: dict, events: list[dict] | None = None) -> dict[str, Any]:
    """计算并落库某用户的三个分数。"""
    if events is None:
        events = await store.recent_events_for_user(user["id"], limit=300)
    stats = user.get("stats")
    if isinstance(stats, str):
        import json

        try:
            stats = json.loads(stats or "{}")
        except json.JSONDecodeError:
            stats = {}
    stats = stats if isinstance(stats, dict) else {}

    ch = churn_risk(user, events, stats)
    lt = ltv(user, stats)
    pr = propensity(user, events)
    row = {"user_id": user["id"], "churn": ch["score"], "ltv": lt["score"],
           "propensity": pr["score"], "tier": lt["tier"],
           "reasons": {"churn": ch["reasons"], "ltv": lt["reasons"], "propensity": pr["reasons"]},
           "computed_at": iso_now()}
    await store.upsert_user_score(row)
    return row


async def run_batch(store: Store, limit: int = 50, only_identified: bool = True) -> int:
    """批量重算（优先近期活跃；只算实名用户，避免给匿名噪音打分）。"""
    cur = await store.db.execute(
        "SELECT * FROM users WHERE stage != 'visitor' ORDER BY last_seen DESC LIMIT ?" if only_identified
        else "SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in await cur.fetchall()]
    for u in rows:
        await compute(store, u)
    return len(rows)


async def top(store: Store, kind: str = "churn", limit: int = 20) -> list[dict]:
    """按分数排序取头部（供控制台/周报）。"""
    col = {"churn": "churn", "ltv": "ltv", "propensity": "propensity"}.get(kind, "churn")
    cur = await store.db.execute(
        f"SELECT s.*, u.distinct_id, u.email, u.stage FROM user_scores s "
        f"JOIN users u ON u.id = s.user_id ORDER BY s.{col} DESC LIMIT ?", (limit,))
    import json

    out = []
    for r in await cur.fetchall():
        d = dict(r)
        try:
            d["reasons"] = json.loads(d.get("reasons") or "{}")
        except json.JSONDecodeError:
            d["reasons"] = {}
        out.append(d)
    return out

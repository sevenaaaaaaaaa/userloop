"""Next Best Channel：同一运营意图，自动挑"最可能被响应"的渠道.

打分依据（可解释，不引入黑盒）：
- **可得性**：用户是否有该渠道标识（email / phone / openid / wecom userid）
- **历史效果**：近 N 天全局渠道打开/点击率（无个体数据时的先验）
- **个体效果**：该用户自身在该渠道的历史响应（有则覆盖先验，样本足够才用）
- **渠道成本/打扰权重**：短信成本高、打扰强 → 权重下调（可配）
- **渠道间隔**：由 frequency 门禁二次把关（这里只做排序提示）

输出：{channel, rationale, scores}；无可用渠道时返回 None，调用方降级为 noop/skip。
"""

from __future__ import annotations

from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store
from userloop.touch import frequency

DEFAULTS: dict[str, Any] = {
    "order": ["email", "im", "wechat_mp", "wecom", "sms"],   # 默认兜底顺序
    "weights": {"email": 1.0, "im": 0.9, "wechat_mp": 1.0, "wecom": 1.0, "sms": 0.7},
    "prior_days": 30,
    "min_individual_samples": 3,      # 个体样本达此数量才覆盖全局先验
    "engagement_events": {
        "email": ("email_open", "email_click"),
        "h5": ("h5_view", "h5_click"),
        "sms": ("sms_click",),
        "im": ("im_click",),
    },
}
# 意图 → 允许的渠道（触达内容形态决定可用渠道）
# 注意：im（飞书）是**团队通知**渠道，不属于用户触达；仅 notify_team 意图使用
INTENT_CHANNELS: dict[str, list[str]] = {
    "reengage": ["email", "wechat_mp", "wecom", "sms"],
    "onboarding": ["email", "wechat_mp", "wecom"],
    "notify_team": ["im"],
    "upsell": ["email", "wechat_mp", "sms"],
    "cart_abandon": ["email", "sms", "wechat_mp"],
    "default": ["email", "wechat_mp", "wecom", "sms"],
}


def cfg_of(ctx: ExecutorContext) -> dict[str, Any]:
    raw = ((ctx.config.get("touch") or {}).get("selector") or {})
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    if isinstance(raw.get("weights"), dict):
        out["weights"] = {**DEFAULTS["weights"], **raw["weights"]}
    return out


def available_channels(user: dict, identities: dict[str, str]) -> list[str]:
    """可得渠道：有邮箱→email；有手机→sms；有微信/企微标识→对应渠道；IM 由配置决定。"""
    chans: list[str] = []
    if user.get("email") or identities.get("email"):
        chans.append("email")
    if identities.get("phone"):
        chans.append("sms")
    if identities.get("wechat_openid") or identities.get("wechat_unionid"):
        chans.append("wechat_mp")
    if identities.get("wecom_userid"):
        chans.append("wecom")
    return chans


async def score(store: Store, user: dict, identities: dict[str, str], ctx: ExecutorContext,
                intent: str = "default") -> dict[str, Any]:
    """给候选渠道打分，返回排序与理由。"""
    cfg = cfg_of(ctx)
    allowed = INTENT_CHANNELS.get(intent, INTENT_CHANNELS["default"])
    if intent == "notify_team":
        avail = ["im"]                       # 团队通道不依赖用户标识
    else:
        avail = [c for c in available_channels(user, identities) if c in allowed]

    engagement = await store.channel_engagement(days=int(cfg["prior_days"]))
    weights = cfg.get("weights") or {}
    scores: list[dict[str, Any]] = []
    for ch in avail:
        ev = cfg["engagement_events"].get(ch, ())
        # 先验：点击率 > 打开率（无送达数据时退化用计数比）
        clicks = sum(engagement.get(ch, {}).get(e, 0) for e in ev if e.endswith("click"))
        opens = sum(engagement.get(ch, {}).get(e, 0) for e in ev if not e.endswith("click"))
        if ch == "email":
            delivered = max(1, engagement.get("email", {}).get("delivered", 0))
            prior = (opens / delivered) * 0.3 + (clicks / delivered) * 0.7
        else:
            base = max(1, opens + clicks)
            prior = clicks / base if (opens or clicks) else 0.25      # 无数据 → 中性先验
        # 个体效果：该用户是否有该渠道响应事件
        individual = None
        try:
            rows = await store.recent_events_for_user(user["id"], limit=200)
            names = {r["event"] for r in rows}
            hits = sum(1 for e in ev if e in names)
            if hits >= int(cfg["min_individual_samples"]):
                individual = min(1.0, 0.4 + 0.2 * hits)
        except Exception:  # noqa: BLE001
            individual = None
        base_score = individual if individual is not None else prior
        final = base_score * float(weights.get(ch, 1.0))
        scores.append({"channel": ch, "score": round(final, 4),
                       "basis": "individual" if individual is not None else "global_prior",
                       "prior": round(prior, 4), "weight": weights.get(ch, 1.0)})

    scores.sort(key=lambda x: x["score"], reverse=True)
    if not scores:
        return {"channel": None, "scores": [], "rationale": "无可用渠道（缺邮箱/手机/微信标识）"}
    top = scores[0]
    rationale = (f"选 {top['channel']}：{'个体历史响应' if top['basis'] == 'individual' else '全局先验'}"
                 f" {top['score']}（权重 {top['weight']}）")
    if len(scores) > 1:
        rationale += f"；次选 {scores[1]['channel']} {scores[1]['score']}"
    return {"channel": top["channel"], "scores": scores, "rationale": rationale}

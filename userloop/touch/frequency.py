"""跨渠道全局频控：不管消息从哪条链路发出，同一个人只被打扰有限次.

为什么需要：AI 护栏只管 AI 决策，模板 Loop 会绕过它 —— 结果就是"邮件刚发完，短信又来"。
本门禁作用在**触点分发层**（template Loop 与 AI 决策共用），按**渠道组**统一计数：

- 全局：同一用户任意营销触达之间的最小间隔 + 每周上限
- 分渠道：单渠道独立上限（如短信更严，避免成本与打扰）
- 静默期：默认 22:00–08:00（按 tz_offset_hours 换算）
- 事务类豁免：收据/验证码等（template_id 在白名单或 priority=transactional）

被拦下来的触达都会返回明确 reason（可观测、可统计），不会静默丢失。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "global_gap_hours": 24,          # 任意两次营销触达的最小间隔
    "weekly_cap": 3,                 # 每用户每周上限（跨渠道合计）
    "quiet_hours": [22, 8],          # 静默期（本地时区）
    "tz_offset_hours": 8,
    "channel_gap_hours": {"sms": 168, "email": 24, "im": 12, "wechat_mp": 48, "wecom": 48},
    "channel_weekly_cap": {"sms": 1, "email": 3, "im": 7, "wechat_mp": 2, "wecom": 3},
    "transactional_templates": ["receipt", "verify", "verification", "password_reset", "order_confirmed"],
}

# 动作类型 → 渠道（覆盖 touch.* 与历史动作类型）
CHANNEL_OF: dict[str, str] = {
    "email": "email", "ai.email": "email", "touch.email": "email",
    "touch.sms": "sms", "sms": "sms",
    "feishu": "im", "touch.im": "im", "im": "im",
    "touch.wechat_mp": "wechat_mp", "touch.wecom": "wecom",
    "touch.auto": "auto",
}
MARKETING_CHANNELS = ("email", "sms", "im", "wechat_mp", "wecom")


def cfg_of(ctx: ExecutorContext) -> dict[str, Any]:
    raw = ((ctx.config.get("touch") or {}).get("frequency") or {})
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k in ("channel_gap_hours", "channel_weekly_cap") and isinstance(v, dict):
            out[k] = {**DEFAULTS[k], **v}
        elif k in DEFAULTS:
            out[k] = v
    return out


def channel_of(action_type: str) -> str:
    return CHANNEL_OF.get(str(action_type), "other")


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    qh = cfg.get("quiet_hours") or []
    if len(qh) != 2 or int(qh[0]) == int(qh[1]):
        return False
    start, end = int(qh[0]), int(qh[1])
    hour = ((now or datetime.utcnow()) + timedelta(hours=int(cfg.get("tz_offset_hours", 0)))).hour
    return (start <= hour < end) if start <= end else (hour >= start or hour < end)


def _is_transactional(template_id: str | None, cfg: dict) -> bool:
    if not template_id:
        return False
    t = str(template_id).lower()
    return any(k in t for k in cfg.get("transactional_templates", []))


async def check(store: Store | None, ctx: ExecutorContext, user: dict, channel: str,
                template_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """返回 {ok, reason, counts}; force=True 跳过营销频控（审批通过的人工触达）。"""
    cfg = cfg_of(ctx)
    if not cfg.get("enabled") or force:
        return {"ok": True, "reason": "ok", "counts": {}}
    if channel not in MARKETING_CHANNELS:
        return {"ok": True, "reason": "非营销渠道", "counts": {}}
    if _is_transactional(template_id, cfg):
        return {"ok": True, "reason": "事务类豁免", "counts": {}}
    if store is None:
        return {"ok": True, "reason": "无存储（跳过频控）", "counts": {}}

    if in_quiet_hours(cfg):
        return {"ok": False, "reason": f"静默期 {cfg['quiet_hours'][0]}:00-{cfg['quiet_hours'][1]}:00",
                "counts": {}}

    rows = await store.recent_touches(user["id"], hours=24 * 7)
    per_channel: dict[str, int] = {}
    last_by_channel: dict[str, str] = {}
    for r in rows:
        ch = r.get("channel") or channel_of(r["type"])
        per_channel[ch] = per_channel.get(ch, 0) + 1
        if ch not in last_by_channel:
            last_by_channel[ch] = r["executed_at"]

    # 1) 全局间隔
    week_total = sum(per_channel.values())
    if rows:
        last_at = rows[0]["executed_at"]
        gap = cfg["global_gap_hours"]
        try:
            last_dt = datetime.fromisoformat(str(last_at).replace("Z", "+00:00")).replace(tzinfo=None)
            hours_since = (datetime.utcnow() - last_dt).total_seconds() / 3600
            if hours_since < gap:
                return {"ok": False, "reason": f"距上次触达仅 {hours_since:.1f}h（全局间隔需 {gap}h）",
                        "counts": {"week_total": week_total, "hours_since_last": round(hours_since, 1)}}
        except ValueError:
            pass

    # 2) 全局周上限
    if week_total >= int(cfg["weekly_cap"]):
        return {"ok": False, "reason": f"周触达已达上限 {cfg['weekly_cap']} 次（跨渠道合计）",
                "counts": {"week_total": week_total}}

    # 3) 分渠道间隔与上限
    ch_gap = (cfg.get("channel_gap_hours") or {}).get(channel)
    ch_cap = (cfg.get("channel_weekly_cap") or {}).get(channel)
    if ch_cap is not None and per_channel.get(channel, 0) >= int(ch_cap):
        return {"ok": False, "reason": f"{channel} 渠道周上限 {ch_cap} 次已用完",
                "counts": {"channel_count": per_channel.get(channel, 0)}}
    if ch_gap and last_by_channel.get(channel):
        try:
            last_dt = datetime.fromisoformat(last_by_channel[channel].replace("Z", "+00:00")).replace(tzinfo=None)
            hours_since = (datetime.utcnow() - last_dt).total_seconds() / 3600
            if hours_since < int(ch_gap):
                return {"ok": False, "reason": f"{channel} 渠道间隔不足（{hours_since:.1f}h < {ch_gap}h）",
                        "counts": {"channel_hours_since": round(hours_since, 1)}}
        except ValueError:
            pass

    return {"ok": True, "reason": "ok",
            "counts": {"week_total": week_total, "per_channel": per_channel,
                       "remaining_week": max(0, int(cfg["weekly_cap"]) - week_total)}}


async def record(store: Store | None, user: dict, channel: str, action_type: str,
                 template_id: str | None = None, loop_id: str | None = None) -> None:
    """投递成功后记台账（营销渠道才记；事务类也记但不占营销配额由 check 判断）。"""
    if store is None or channel not in MARKETING_CHANNELS:
        return
    await store.log_touch(user["id"], channel, action_type, template_id=template_id, loop_id=loop_id)

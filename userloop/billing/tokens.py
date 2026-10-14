"""Token 计量、日预算门禁与消耗大户限制（成本可观测 + 成本有界）。

- 记账：integrations/ai.chat 每次调用成功后落 token_usage（日/小时/调用方三维）
- 预算：billing.daily_budget_cny（默认 5 元/天），超支 → 邮件告知管理员（每日最多一次）+ 后续 AI 调用降级
- 临时加预算：管理员可单日追加（budget_override 按日存储，第二天自然回落）
- 大户限制：ai.caller_caps 按调用方设每日 token 上限，超限降级（防单个功能烧穿预算）

成本按「元」计：ai.price_per_1m_input / ai.price_per_1m_output（默认 DeepSeek 官方价 2/8 元每百万 token）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from userloop.core.store import Store

# 默认调用方每日 token 上限（成本护栏：谁多花谁受限，互不拖累）
DEFAULT_CALLER_CAPS: dict[str, int] = {
    "brain": 300_000,        # AI 大脑批次（量最大，单独看紧）
    "copilot": 100_000,
    "conversation": 200_000,
    "segments": 50_000,
    "report": 50_000,
    "evolve": 50_000,
    "ai_email": 100_000,
}


def ai_cfg(cfg: dict) -> dict[str, Any]:
    return cfg.get("ai") or {}


def daily_budget(cfg: dict, extra_cny: float = 0.0) -> float:
    b = (cfg.get("billing") or {}).get("daily_budget_cny")
    try:
        base = float(b) if b is not None else 5.0
    except (TypeError, ValueError):
        base = 5.0
    return max(0.0, base) + max(0.0, float(extra_cny))


def token_cost(prompt_tokens: int, completion_tokens: int, cfg: dict) -> float:
    """按配置单价（元/百万 token）估算成本。"""
    c = ai_cfg(cfg)
    pin = float(c.get("price_per_1m_input", 2.0))
    pout = float(c.get("price_per_1m_output", 8.0))
    return (int(prompt_tokens) * pin + int(completion_tokens) * pout) / 1_000_000


def caller_cap(cfg: dict, caller: str) -> int:
    caps = (ai_cfg(cfg).get("caller_caps") or {})
    raw = caps.get(caller, DEFAULT_CALLER_CAPS.get(caller, 100_000))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 100_000


async def caller_tokens_today(store: Store, caller: str) -> int:
    rows = await store.token_usage_top_callers(limit=100)
    for r in rows:
        if r["caller"] == caller:
            return int(r["tokens"])
    return 0


async def budget_status(store: Store, cfg: dict) -> dict[str, Any]:
    """今日预算状态：{limit, used, remaining, exceeded, extra}。"""
    day = datetime.utcnow().date().isoformat()
    extra = await store.get_budget_override(day)
    limit = daily_budget(cfg, extra)
    used = await store.token_cost_today()
    return {"day": day, "limit": round(limit, 4), "base_limit": round(daily_budget(cfg), 4),
            "extra": round(extra, 4), "used": round(used, 4),
            "remaining": round(max(0.0, limit - used), 4),
            "exceeded": used >= limit and limit > 0,
            "pct": round(used / limit * 100, 1) if limit else 0}


async def gate(store: Store, cfg: dict, caller: str = "") -> dict[str, Any]:
    """AI 调用前置门：日预算 + 调用方上限。超限返回 {ok: False, reason}。"""
    st = await budget_status(store, cfg)
    if st["exceeded"]:
        return {"ok": False, "reason": f"今日 AI 预算已用尽（{st['used']}/{st['limit']} 元），"
                                       f"管理员可在控制台单日追加预算",
                "budget_exceeded": True}
    if caller:
        cap = caller_cap(cfg, caller)
        used = await caller_tokens_today(store, caller)
        if used >= cap:
            return {"ok": False, "reason": f"调用方 {caller} 今日 token 已达上限（{used}/{cap}），降级处理",
                    "cap_exceeded": True, "caller": caller, "used_tokens": used, "cap": cap}
    return {"ok": True, "budget": st}


async def record(store: Store, cfg: dict, caller: str, model: str,
                 prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    """记账一次 AI 调用（成功后调用；永不抛错）。返回 {cost, budget}。"""
    out: dict[str, Any] = {"cost": 0.0}
    try:
        cost = token_cost(prompt_tokens, completion_tokens, cfg)
        now = datetime.utcnow()
        await store.add_token_usage(
            caller=caller or "unknown", model=model or "", prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens), cost_cny=cost,
            ts=now.isoformat(timespec="seconds") + "Z",
            day=now.date().isoformat(), hour=now.hour)
        out["cost"] = round(cost, 6)
        st = await budget_status(store, cfg)
        out["budget"] = st
        # 超支 → 邮件告知管理员（每自然日最多一封，避免刷屏）
        if st["exceeded"]:
            out["alerted"] = await _alert_over_budget(store, cfg, st)
    except Exception as exc:  # noqa: BLE001 —— 记账绝不影响主流程
        out["error"] = str(exc)[:160]
    return out


def _state_path(cfg: dict) -> str:
    return os.path.join(cfg.get("data_dir") or "data", "ops", "budget_alert_state.json")


async def _alert_over_budget(store: Store, cfg: dict, st: dict[str, Any]) -> dict[str, Any]:
    """超预算邮件告警（冷却：每自然日一封）。"""
    p = _state_path(cfg)
    today = st["day"]
    try:
        state = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                state = json.load(f)
        if state.get("last_alert_day") == today:
            return {"sent": False, "reason": "今日已提醒过"}
        recipients = _recipients(cfg)
        sent: dict[str, Any] = {}
        if recipients:
            from userloop.actions.executors import ExecutorContext, send_email

            ctx = ExecutorContext(cfg.get("data_dir") or "data", cfg)
            subject = f"[UserLoop] AI 预算告警：今日已花 {st['used']} 元（上限 {st['limit']} 元）"
            lines = [f"今日 AI token 成本已达 {st['used']} 元，超过日预算 {st['limit']} 元。", ""]
            top = await store.token_usage_top_callers(limit=5)
            if top:
                lines.append("今日消耗大户：")
                lines += [f"- {r['caller']}：{r['tokens']} tokens · {r['cost']} 元 · {r['calls']} 次调用" for r in top]
            lines += ["", f"处理：控制台「成本与任务」面板可单日追加预算（第二天自动回落），"
                          f"或在 config 调整 billing.daily_budget_cny / ai.caller_caps。"]
            for to in recipients:
                sent[to] = send_email(ctx, to, subject, "\n".join(lines)).get("ok", False)
        state["last_alert_day"] = today
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return {"sent": True, "recipients": sent}
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "error": str(exc)[:160]}


def _recipients(cfg: dict) -> list[str]:
    """管理员收件箱：digest.recipients 优先，回落 billing.alert_email。"""
    d = cfg.get("digest") or {}
    rec = [r for r in (d.get("recipients") or []) if r]
    if rec:
        return rec
    b = (cfg.get("billing") or {}).get("alert_email")
    return [b] if b else []

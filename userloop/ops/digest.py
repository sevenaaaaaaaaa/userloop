"""每日两次运营简报（清晨 / 傍晚）：过去 12 小时执行/流量/转化 + 当日任务与 token 消耗。

- 执行情况：Loop 新建、动作完成/失败、验证回流
- 流量情况：事件量、活跃用户、渠道打开/点击
- 转化情况：旅程迁移、付费转化（purchase 事件）
- 当日：任务执行汇总 + token 消耗（总量/分小时/消耗大户）+ 预算水位

收件人：config.digest.recipients（默认空则跳过发送，简报仍会返回数据）。
时区：config.ops.calendar.tz_offset_hours（默认 +8）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store


def local_hour(cfg: dict) -> int:
    from userloop.ops import workdays

    return workdays.local_now(cfg).hour


def _recipients(cfg: dict) -> list[str]:
    return [r for r in ((cfg.get("digest") or {}).get("recipients") or []) if r]


async def collect(store: Store, cfg: dict, hours: int = 12) -> dict[str, Any]:
    """聚合简报数据（只读，纯 SQL 聚合，不打内存）。"""
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat(timespec="seconds") + "Z"
    today = datetime.utcnow().date().isoformat()
    since_day = f"{today}T00:00:00Z"

    async def q(sql: str, args: tuple) -> list[dict]:
        cur = await store.db.execute(sql, args)  # type: ignore[union-attr]
        return [dict(r) for r in await cur.fetchall()]

    # ---- 过去 12 小时：执行 ----
    loops_12h = (await q("SELECT COUNT(*) c FROM loops WHERE created_at>=?", (since,)))[0]["c"]
    acts = await q("SELECT status, COUNT(*) c FROM actions WHERE executed_at>=? GROUP BY status", (since,))
    act_map = {r["status"]: r["c"] for r in acts}
    feedback_12h = (await q("SELECT COUNT(*) c FROM feedback WHERE created_at>=?", (since,)))[0]["c"]

    # ---- 过去 12 小时：流量 ----
    events_12h = (await q("SELECT COUNT(*) c FROM events WHERE created_at>=?", (since,)))[0]["c"]
    users_12h = (await q("SELECT COUNT(DISTINCT user_id) c FROM events WHERE created_at>=?", (since,)))[0]["c"]
    top_events = await q("SELECT event, COUNT(*) c FROM events WHERE created_at>=? "
                         "GROUP BY event ORDER BY c DESC LIMIT 5", (since,))
    channel_rows = await q("SELECT source, COUNT(*) c FROM events WHERE created_at>=? GROUP BY source", (since,))
    touches = {r["event"]: r["c"] for r in await q(
        "SELECT event, COUNT(*) c FROM events WHERE created_at>=? AND event IN "
        "('delivered','email_open','email_click','h5_view','h5_click','sms_click','im_click') "
        "GROUP BY event", (since,))}

    # ---- 过去 12 小时：转化 ----
    transitions = await q("SELECT from_stage, to_stage, COUNT(*) c FROM stage_transitions "
                          "WHERE created_at>=? GROUP BY from_stage, to_stage ORDER BY c DESC LIMIT 8", (since,))
    purchases = (await q("SELECT COUNT(*) c, COALESCE(SUM(CAST(json_extract(props,'$.amount') AS REAL)),0) amt "
                         "FROM events WHERE created_at>=? AND event='purchase'", (since,)))[0]

    # ---- 当日任务执行 ----
    loops_today = (await q("SELECT COUNT(*) c FROM loops WHERE created_at>=?", (since_day,)))[0]["c"]
    acts_today = await q("SELECT status, COUNT(*) c FROM actions WHERE executed_at>=? GROUP BY status", (since_day,))
    act_today_map = {r["status"]: r["c"] for r in acts_today}

    # ---- 当日 token 消耗 ----
    hourly = await store.token_usage_hourly(today)
    daily = await store.token_usage_daily(days=7)
    top_callers = await store.token_usage_top_callers(limit=5)

    from userloop.billing import tokens as token_billing

    budget = await token_billing.budget_status(store, cfg)

    return {"generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "window_hours": hours, "day": today,
            "exec_12h": {"loops_created": loops_12h, "actions_done": act_map.get("done", 0),
                         "actions_failed": act_map.get("failed", 0), "feedbacks": feedback_12h},
            "traffic_12h": {"events": events_12h, "active_users": users_12h,
                            "top_events": top_events, "sources": channel_rows, "touches": touches},
            "conversion_12h": {"transitions": transitions, "purchases": purchases["c"],
                               "purchase_amount": round(float(purchases["amt"] or 0), 2)},
            "today": {"loops_created": loops_today,
                      "actions_done": act_today_map.get("done", 0),
                      "actions_failed": act_today_map.get("failed", 0)},
            "tokens": {"budget": budget, "hourly": hourly, "daily": daily, "top_callers": top_callers}}


def _render(d: dict[str, Any], variant: str) -> str:
    title = "清晨简报" if variant == "morning" else "傍晚简报"
    ex, tr, cv, td, tk = d["exec_12h"], d["traffic_12h"], d["conversion_12h"], d["today"], d["tokens"]
    b = tk["budget"]
    lines = [f"# UserLoop {title}（过去 {d['window_hours']} 小时）",
             f"生成时间：{d['generated_at']}", "",
             "## 执行情况（过去 12 小时）",
             f"- Loop 新建 {ex['loops_created']} · 动作完成 {ex['actions_done']} / 失败 {ex['actions_failed']} · 验证回流 {ex['feedbacks']}",
             "",
             "## 流量情况（过去 12 小时）",
             f"- 事件 {tr['events']} · 活跃用户 {tr['active_users']}",
             ]
    if tr["top_events"]:
        lines.append("- 热门事件：" + "、".join(f"{r['event']} {r['c']}" for r in tr["top_events"]))
    if tr["touches"]:
        lines.append("- 触点互动：" + "、".join(f"{k} {v}" for k, v in tr["touches"].items()))
    lines += ["", "## 转化情况（过去 12 小时）",
              f"- purchase {cv['purchases']} 笔 · 金额 {cv['purchase_amount']} 元"]
    for t in cv["transitions"][:5]:
        lines.append(f"- 旅程迁移 {t['from_stage']} → {t['to_stage']}：{t['c']} 人")
    lines += ["", "## 今日任务执行",
              f"- Loop 新建 {td['loops_created']} · 动作完成 {td['actions_done']} / 失败 {td['actions_failed']}",
              "",
              "## 今日 token 消耗",
              f"- 成本 {b['used']} / 预算 {b['limit']} 元（水位 {b['pct']}%）"
              + ("⚠️ 已超支" if b["exceeded"] else "")]
    if tk["hourly"]:
        lines.append("- 分小时：" + "、".join(f"{r['hour']}时 {r['tokens']}tk/{r['cost']}元" for r in tk["hourly"]))
    if tk["top_callers"]:
        lines.append("")
        lines.append("### 消耗大户 Top")
        lines += [f"- {r['caller']}：{r['tokens']} tokens · {r['cost']} 元 · {r['calls']} 次"
                  for r in tk["top_callers"]]
    if tk["daily"]:
        lines += ["", "### 近 7 天每日消耗",
                  * [f"- {r['day']}：{r['tokens']} tokens · {r['cost']} 元 · {r['calls']} 次" for r in tk["daily"]]]
    return "\n".join(lines) + "\n"


async def send_digest(store: Store, ctx: ExecutorContext, variant: str = "morning") -> dict[str, Any]:
    """生成 + 邮件发送简报；未配置收件人时只返回数据（控制台/CLI 可用）。"""
    cfg = ctx.config
    d = await collect(store, cfg, hours=12)
    md = _render(d, variant)
    label = "清晨简报" if variant == "morning" else "傍晚简报"
    out: dict[str, Any] = {"ok": True, "variant": variant, "markdown": md, "sent": {}}
    recipients = _recipients(cfg)
    if not recipients:
        out["sent"] = {"skipped": "未配置 digest.recipients"}
        return out
    from userloop.actions.executors import ExecutorContext as _Ctx, send_email

    ctx2 = ctx if getattr(ctx, "smtp", None) else _Ctx(cfg.get("data_dir") or "data", cfg)
    for to in recipients:
        try:
            out["sent"][to] = send_email(ctx2, to, f"[UserLoop] {label}（过去 12 小时 + 今日消耗）", md).get("ok", False)
        except Exception as exc:  # noqa: BLE001
            out["sent"][to] = f"error: {exc}"[:160]
    # 留档：data/digests/
    import os

    dig_dir = os.path.join(cfg.get("data_dir") or "data", "digests")
    os.makedirs(dig_dir, exist_ok=True)
    name = f"{d['day']}-{variant}.md"
    with open(os.path.join(dig_dir, name), "w", encoding="utf-8") as f:
        f.write(md)
    out["path"] = os.path.join(dig_dir, name)
    return out

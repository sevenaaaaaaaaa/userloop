"""AI 运营周报：把"数据"翻译成"决策建议"（AI Native 的复盘闭环）.

组成：
- **硬指标**：用户/事件/Loop/回流、漏斗与转化、模板效果、A/B 结论、渠道效果、
  AI 决策分布（自动/待批/驳回）、**被门禁少打扰的次数**（体验指标）
- **AI 叙事 + 建议**：LLM 基于硬指标写"本周发生了什么 / 最大漏损在哪 / 下周建议做什么"
- **积压提醒**：待批决策数超阈值时在报告顶部高亮（可执行）

产出：`data/reports/YYYY-Www.md`（可读）+ 可推送（飞书/邮件），控制台可查看。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store

DEFAULTS = {
    "days": 7,
    "pending_alert_threshold": 20,     # 待批决策超过此数在报告顶部提醒
    "send": False,                     # 是否推送（飞书/邮件）
    "recipients": [],                  # 邮件收件人
}


def cfg_of(ctx: ExecutorContext) -> dict[str, Any]:
    raw = ((ctx.config.get("report") or {}))
    return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}


async def collect(store: Store, ctx: ExecutorContext, days: int = 7) -> dict[str, Any]:
    """聚合周报所需的全部硬指标（只读）。"""
    from userloop.experiments import engine as ab
    from userloop.touch import frequency

    since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds") + "Z"
    stages = await store.count_users_by_stage()
    total_users = sum(stages.values()) or 1
    counts = await store.counts()

    # 漏斗与转化（区分匿名访客与实名用户：避免"访客多、注册少"被误读为转化差）
    order = ("visitor", "signup", "activated", "paying", "retained", "advocate", "churn_risk", "churned")
    funnel = [{"stage": s, "users": stages.get(s, 0),
               "share": round(stages.get(s, 0) / total_users * 100, 1)} for s in order]
    identified = sum(v for k, v in stages.items() if k != "visitor")
    audience = {"total": total_users, "anonymous_visitor": stages.get("visitor", 0), "identified": identified,
                "identified_share": round(identified / total_users * 100, 1) if total_users else 0}

    # Loop 与回流
    loops = await store.list_loops(limit=500)
    loop_status = await store.count_loops_by_status()
    feedback = await store.feedback_stats()
    template_effect = []
    for tid, v in feedback.items():
        e, n = v.get("effective", 0), v.get("neutral", 0)
        template_effect.append({"template": tid, "effective": e, "neutral": n,
                                "rate": round(e / (e + n) * 100, 1) if (e + n) else 0})
    template_effect.sort(key=lambda x: x["rate"], reverse=True)

    # A/B 结论
    experiments = []
    for exp in await store.list_experiments(enabled_only=False):
        v = await ab.evaluate(store, exp["id"])
        experiments.append({"id": exp["id"], "name": exp.get("name"), "verdict": v.get("verdict"),
                            "leader": v.get("leader"), "confidence": v.get("confidence"),
                            "promoted": exp.get("promoted_variant")})

    # AI 决策分布（窗口内）
    decisions = await store.list_ai_decisions(limit=500)
    recent = [d for d in decisions if str(d.get("created_at") or "") >= since]
    ai_counts: dict[str, int] = {}
    for d in recent:
        ai_counts[d["status"]] = ai_counts.get(d["status"], 0) + 1

    # 渠道效果 + 被拦次数（体验指标）
    channels = await store.channel_engagement(days=days)
    blocks = await store.block_stats(days=days)

    return {
        "window_days": days,
        "audience": audience,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "counts": counts,
        "funnel": funnel,
        "loops": {"status": loop_status, "total": len(loops),
                  "running": sum(1 for x in loops if x["status"] == "running")},
        "template_effect": template_effect[:8],
        "experiments": experiments,
        "ai": {"counts": ai_counts, "total": len(recent),
               "pending": ai_counts.get("pending_approval", 0)},
        "channels": channels,
        "blocks": blocks,
        "pending_threshold": int(cfg_of(ctx)["pending_alert_threshold"]),
    }


def _stats_brief(stats: dict[str, Any]) -> str:
    """给 LLM 的精简事实（避免 token 浪费）。"""
    return json.dumps({
        "窗口天数": stats["window_days"],
        "用户总数": stats["counts"].get("users"),
        "事件总数": stats["counts"].get("events"),
        "受众": stats["audience"],
        "漏斗（流量口径，含匿名访客）": [{"阶段": f["stage"], "人数": f["users"], "占比": f["share"]}
                                        for f in stats["funnel"]],
        "Loop": stats["loops"],
        "模板效果": stats["template_effect"],
        "A/B结论": [{"实验": e["id"], "结论": e["verdict"], "领先": e["leader"], "置信": e["confidence"]}
                    for e in stats["experiments"]],
        "AI决策": stats["ai"],
        "渠道效果": stats["channels"],
        "被门禁少打扰次数": stats["blocks"],
    }, ensure_ascii=False)


SYSTEM = """你是 UserLoop 的用户运营分析师。基于给定事实写一份**周报**，要求：

1. 结构（Markdown，二级标题）：## 本周概览 / ## 关键发现 / ## 最大漏损 / ## 下周建议（3 条，具体可执行）
2. 只依据给定数据，不编造数字；数据缺失就明说"数据不足"
3. 建议必须落到"动作"（例如：给 X 阶段用户上 Y 流程、把 Z 实验的 winner 推广、清空待批队列）
4. 语气克制、结论明确；全文 ≤ 500 字
5. 若"待批决策"较多，在"本周概览"里单独提醒需要人工审批"""


async def build(store: Store, ctx: ExecutorContext, days: int | None = None,
                transport: Any = None) -> dict[str, Any]:
    """生成周报：硬指标 + AI 叙事与建议。"""
    cfg = cfg_of(ctx)
    days = int(days or cfg["days"])
    stats = await collect(store, ctx, days)

    narrative = ""
    degraded = False
    try:
        from userloop.integrations.ai import chat

        res = await chat(ctx, [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": _stats_brief(stats)}],
                         transport=transport, max_tokens=900, json_mode=False)
        if res.get("ok"):
            narrative = str(res.get("content") or "").strip()
        else:
            degraded = True
            narrative = f"（AI 叙事不可用：{res.get('error')}；以下为硬指标）"
    except Exception as exc:  # noqa: BLE001
        degraded = True
        narrative = f"（AI 叙事不可用：{exc}；以下为硬指标）"

    md = _render_markdown(stats, narrative)
    name = f"{datetime.utcnow().strftime('%Y')}-W{datetime.utcnow().isocalendar().week:02d}.md"
    return {"ok": True, "name": name, "markdown": md, "stats": stats,
            "degraded": degraded, "narrative": narrative}


def _render_markdown(stats: dict[str, Any], narrative: str) -> str:
    lines = [f"# UserLoop 运营周报（近 {stats['window_days']} 天）",
             f"生成时间：{stats['generated_at']}", ""]
    if stats["ai"]["pending"] >= stats.get("pending_threshold", 20):
        lines += [f"> ⚠️ **待人工审批的 AI 决策有 {stats['ai']['pending']} 条**，"
                  f"建议先批量处理，避免错过触达窗口。", ""]
    lines += [narrative or "（无 AI 叙事）", ""]
    lines += ["---", "## 硬指标附录", ""]
    lines += [f"- 用户 {stats['counts'].get('users')} · 事件 {stats['counts'].get('events')} · "
              f"Loop {stats['loops']['total']}（运行中 {stats['loops']['running']}） · "
              f"回流 {stats['counts'].get('feedback')}"]
    au = stats.get("audience") or {}
    lines += [f"- 受众：实名 {au.get('identified', '—')}（{au.get('identified_share', '—')}%）· "
              f"匿名访客 {au.get('anonymous_visitor', '—')}"]
    lines += ["- 漏斗（流量口径）：" + " / ".join(f"{f['stage']} {f['users']}({f['share']}%)" for f in stats["funnel"])]
    if stats["template_effect"]:
        lines += ["- 模板效果（effective 率）：" +
                  "；".join(f"{t['template']} {t['rate']}%" for t in stats["template_effect"][:5])]
    if stats["experiments"]:
        lines += ["- A/B：" + "；".join(f"{e['id']}={e['verdict']}"
                                        + (f"(领先 {e['leader']})" if e.get("leader") else "")
                                        for e in stats["experiments"])]
    if stats["ai"]["counts"]:
        lines += ["- AI 决策：" + "；".join(f"{k} {v}" for k, v in stats["ai"]["counts"].items())]
    ch = stats["channels"].get("email", {})
    if ch:
        lines += [f"- 渠道（邮件）：送达 {ch.get('delivered', 0)} · 打开 {ch.get('email_open', 0)} · "
                  f"点击 {ch.get('email_click', 0)}"]
    lines += [f"- **被门禁少打扰 {stats['blocks']['total']} 次**"
              + (f"（按渠道 {stats['blocks']['by_channel']}）" if stats["blocks"]["by_channel"] else "")]
    return "\n".join(lines) + "\n"


async def save(store: Store, ctx: ExecutorContext, report: dict[str, Any]) -> str:
    """落盘到 data/reports/<name>.md；返回路径。"""
    import os

    out_dir = os.path.join(ctx.config.get("data_dir") or "data", "reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, report["name"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(report["markdown"])
    return path


async def send(ctx: ExecutorContext, report: dict[str, Any]) -> dict[str, Any]:
    """推送周报（飞书 IM webhook / 邮件），失败不抛。"""
    cfg = cfg_of(ctx)
    out: dict[str, Any] = {}
    im_url = ((ctx.config.get("touch") or {}).get("im") or {}).get("webhook_url")
    if im_url:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(im_url, json={"msg_type": "text",
                                                    "content": {"text": report["markdown"][:2500]}})
            out["im"] = 200 <= r.status_code < 300
        except Exception as exc:  # noqa: BLE001
            out["im"] = f"error: {exc}"
    recipients = cfg.get("recipients") or []
    if recipients:
        try:
            from userloop.actions.executors import send_email

            smtp_cfg = ctx.config.get("touch") or {}
            r = send_email(ctx, recipients[0], "UserLoop 运营周报", report["markdown"])
            out["email"] = r.get("ok")
        except Exception as exc:  # noqa: BLE001
            out["email"] = f"error: {exc}"
    return out

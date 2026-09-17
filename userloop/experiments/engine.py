"""A/B 版式实验引擎（P2）：稳定分桶 → 版式覆盖 → 指标归因 → 显著性判定 → 提升 winner.

设计要点：
- **稳定分桶**：hash(experiment_id + user_id) 决定变体，同一用户始终同一版式（避免体验抖动）
- **版式覆盖**：变体只覆盖"内容槽位"（标题/正文/CTA 文案/语气），版式由渲染层保证不崩
- **指标归因**：送达数（分桶人数）、打开/浏览、点击、转化（现有验证回流的目标事件），全部按变体聚合
- **显著性**：双比例 z 检验（正态近似），返回置信度；达阈值+样本量充足才判 winner
- **闭环**：winner 可提升（promote）—— 之后只发 winner；实验结果进 feedback 供 AI 大脑参考
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from typing import Any

from userloop.core.store import Store

DEFAULTS = {
    "min_samples": 30,          # 每个变体最少样本（不足不下结论）
    "confidence": 0.95,         # 判 winner 的置信阈值
    "window_days": 14,          # 归因窗口
    "metric": "click_rate",     # click_rate | open_rate | conversion_rate
}

# 内置实验示例（enabled=数据驱动，可在 data/experiments.json 覆盖/扩展）
BUILTIN: list[dict] = [
    {
        "id": "email_cta_urgency",
        "name": "邮件 CTA 紧迫感 A/B",
        "enabled": True,
        "channel": "email",
        "match_templates": ["signup_no_activate", "churn_risk_rescue", "ai.send_email"],
        "variants": [
            {"id": "A", "weight": 50, "name": "温和版",
             "overrides": {"cta_text": "了解详情"}},
            {"id": "B", "weight": 50, "name": "紧迫版",
             "overrides": {"cta_text": "立即完成（限今天）"}},
        ],
        "goal_event": "activation",
        "min_samples": 20,
        "confidence": 0.9,
        "window_days": 14,
        "metric": "click_rate",
    },
    {
        "id": "h5_hero_style",
        "name": "H5 标题风格 A/B",
        "enabled": True,
        "channel": "h5",
        "match_templates": ["*"],
        "variants": [
            {"id": "A", "weight": 50, "name": "权益导向",
             "overrides": {"title_suffix": "", "cta_text": "领取权益"}},
            {"id": "B", "weight": 50, "name": "行动导向",
             "overrides": {"title_suffix": "｜现在就能完成", "cta_text": "马上开始"}},
        ],
        "goal_event": "activation",
        "min_samples": 20,
        "confidence": 0.9,
        "window_days": 14,
        "metric": "click_rate",
    },
]


def cfg_of(exp: dict) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in exp.items() if k in DEFAULTS})
    return out


async def seed_experiments(store: Store, data_dir: str) -> None:
    import json
    import os

    for exp in BUILTIN:
        await store.put_experiment(exp)
    path = os.path.join(data_dir, "experiments.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                extra = json.load(f)
            if isinstance(extra, list):
                for exp in extra:
                    if isinstance(exp, dict) and exp.get("id") and exp.get("variants"):
                        await store.put_experiment(exp)
        except (json.JSONDecodeError, OSError):
            pass


def _matches(exp: dict, channel: str, template_id: str | None) -> bool:
    if exp.get("channel") != channel:
        return False
    patterns = exp.get("match_templates") or ["*"]
    tpl = template_id or ""
    return any(p == "*" or p == tpl for p in patterns)


async def pick(
    store: Store,
    channel: str,
    template_id: str | None,
    user_id: str,
    loop_id: str | None,
) -> tuple[dict, dict] | None:
    """挑实验并稳定分桶；返回 (experiment, variant) 或 None。"""
    for exp in await store.list_experiments(enabled_only=True):
        if not _matches(exp, channel, template_id):
            continue
        variants = [v for v in (exp.get("variants") or []) if int(v.get("weight", 0)) > 0]
        if len(variants) < 2:
            continue
        if exp.get("promoted_variant"):        # 已判定 winner → 只发 winner（闭环）
            winner = next((v for v in variants if v["id"] == exp["promoted_variant"]), variants[0])
            return exp, winner
        bucket = int(hashlib.md5(f"{exp['id']}:{user_id}".encode()).hexdigest()[:8], 16) % 100
        acc = 0
        for v in variants:
            acc += int(v.get("weight", 0))
            if bucket < acc:
                await store.record_assignment({"experiment_id": exp["id"], "variant": v["id"],
                                               "user_id": user_id, "loop_id": loop_id, "channel": channel})
                return exp, v
        await store.record_assignment({"experiment_id": exp["id"], "variant": variants[-1]["id"],
                                       "user_id": user_id, "loop_id": loop_id, "channel": channel})
        return exp, variants[-1]
    return None


def apply_variant(spec: Any, variant: dict) -> Any:
    """把变体覆盖应用到内容槽位（不碰版式）。"""
    ov = variant.get("overrides") or {}
    if ov.get("cta_text"):
        spec.cta_text = str(ov["cta_text"])
    if ov.get("title"):
        spec.title = str(ov["title"])
    elif ov.get("title_suffix"):
        spec.title = f"{spec.title}{ov['title_suffix']}"
    if ov.get("body_prefix"):
        spec.body = f"{ov['body_prefix']}{spec.body}"
    if ov.get("body_suffix"):
        spec.body = f"{spec.body}{ov['body_suffix']}"
    spec.vars = {**(spec.vars or {}), "_variant": variant["id"], "_experiment": variant.get("_experiment", "")}
    return spec


# ── 指标与显著性 ──

def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_proportion_confidence(x1: int, n1: int, x2: int, n2: int) -> float:
    """双比例 z 检验：返回"两个比例不同"的置信度（0-1）。样本不足返回 0。"""
    if n1 < 5 or n2 < 5:
        return 0.0
    p1, p2 = x1 / n1, x2 / n2
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    z = abs(p2 - p1) / se
    return round(2 * (_norm_cdf(z) - 0.5), 4)   # 双侧


async def metrics(store: Store, exp: dict, variant_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """按变体聚合指标：分母=分桶人数，分子=窗口内事件去重人数。"""
    cfg = cfg_of(exp)
    since = (datetime.utcnow() - timedelta(days=int(cfg["window_days"]))).isoformat(timespec="seconds") + "Z"
    counts = await store.assignment_counts(exp["id"], since=since)
    out: dict[str, dict[str, Any]] = {}
    goal = exp.get("goal_event")
    for v in (exp.get("variants") or []):
        vid = v["id"]
        if variant_ids and vid not in variant_ids:
            continue
        users = await store.users_by_variant(exp["id"], vid)
        n = len(users)
        ops = clicks = convs = 0
        for uid in users:
            ev_names = {e["event"] for e in await store.recent_events_for_user(uid, limit=200)}
            if ev_names & {"email_open", "h5_view"}:
                ops += 1
            if ev_names & {"email_click", "h5_click"}:
                clicks += 1
            if goal and goal in ev_names:
                convs += 1
        out[vid] = {
            "variant": vid, "name": v.get("name", vid), "weight": v.get("weight", 0),
            "assigned": counts.get(vid, n), "delivered": n,
            "opened": ops, "clicked": clicks, "converted": convs,
            "open_rate": round(ops / n, 4) if n else 0,
            "click_rate": round(clicks / n, 4) if n else 0,
            "conversion_rate": round(convs / n, 4) if n else 0,
        }
    return out


async def evaluate(store: Store, exp_id: str) -> dict[str, Any]:
    """判定实验结论：winner / running / insufficient。"""
    exp = await store.get_experiment(exp_id)
    if not exp:
        return {"error": f"experiment not found: {exp_id}"}
    cfg = cfg_of(exp)
    metric = str(cfg["metric"])
    m = await metrics(store, exp)
    if len(m) < 2:
        return {"experiment": exp_id, "verdict": "insufficient", "reason": "变体不足", "metrics": m}

    ordered = sorted(m.values(), key=lambda r: r.get(metric, 0), reverse=True)
    top, second = ordered[0], ordered[1]
    n_top, n_second = top["delivered"], second["delivered"]
    x_top = int(round(top.get(metric, 0) * n_top))
    x_second = int(round(second.get(metric, 0) * n_second))

    if min(n_top, n_second) < int(cfg["min_samples"]):
        return {"experiment": exp_id, "verdict": "insufficient", "metric": metric,
                "reason": f"样本不足（需各 {cfg['min_samples']}，当前 {n_top}/{n_second}）", "metrics": m}
    if x_top == x_second:
        return {"experiment": exp_id, "verdict": "running", "metric": metric,
                "reason": "暂无明显差异", "metrics": m}

    conf = two_proportion_confidence(x_second, n_second, x_top, n_top)
    verdict = "winner" if conf >= float(cfg["confidence"]) else "running"
    return {"experiment": exp_id, "verdict": verdict, "metric": metric,
            "leader": top["variant"], "confidence": conf,
            "threshold": cfg["confidence"],
            "reason": f"{top['variant']} 领先（{metric} {top.get(metric)} vs {second.get(metric)}，置信 {conf}）",
            "metrics": m}


async def promote(store: Store, exp_id: str, variant: str | None = None) -> dict[str, Any]:
    """提升 winner：此后该实验只发该版式（同时写 feedback 供 AI 参考）。"""
    exp = await store.get_experiment(exp_id)
    if not exp:
        return {"ok": False, "error": "experiment not found"}
    if not variant:
        r = await evaluate(store, exp_id)
        if r.get("verdict") != "winner":
            return {"ok": False, "error": f"尚无显著 winner（{r.get('verdict')}）", "detail": r}
        variant = r["leader"]
    exp["promoted_variant"] = variant
    exp["promoted_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    await store.put_experiment(exp)
    await store.insert_feedback({
        "loop_id": f"exp:{exp_id}", "template_id": f"experiment.{exp_id}",
        "verdict": "effective", "goal_event": exp.get("goal_event"),
        "evidence": {"promoted_variant": variant, "metric": cfg_of(exp)["metric"]},
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
    return {"ok": True, "experiment": exp_id, "promoted": variant}

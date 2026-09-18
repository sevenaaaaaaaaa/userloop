"""预测模型化：从自身事件历史重建"时点快照 + 未来结局"，训练逻辑回归（纯 Python，无重依赖）.

为什么这样做：
- 规则版 v1 可解释但权重靠拍；有数据后应让权重由数据决定
- 我们没有现成标签，但**事件带时间戳** → 可以回放：取参考时点 T（如 14 天前），
  用 T 之前的窗口算特征，用 (T, now] 是否发生目标事件作标签 —— 这是标准的时序特征工程
- 样本不足时**自动退回规则版**（并在 reasons 里说明），避免用不可靠模型做决策

产物：
- `data/models/churn.json` / `propensity.json`：特征均值/方差 + 权重 + 指标（accuracy/auc/samples/version）
- `predict()` 输出 sigmoid 概率；与规则分**加权融合**（模型权重随样本量自适应）
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timedelta
from typing import Any

from userloop.core.store import Store

FEATURES = [
    "events_7d", "events_30d", "purchases", "intent_events", "clicks",
    "silence_days_at_T", "active_days_30d", "stage_rank", "email_known",
]
STAGE_RANK = {"visitor": 0, "signup": 1, "activated": 2, "paying": 3, "retained": 4,
              "advocate": 5, "churn_risk": 0, "churned": 0}
MODELS_DIR = "models"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _features_at(events: list[dict], user: dict, T: datetime) -> list[float]:
    """参考时点 T 的特征（只用 T 之前的事件，避免未来信息泄漏）。"""
    before = [e for e in events if (t := _parse(e.get("created_at"))) and t <= T]
    within = lambda d: sum(1 for e in before if (_parse(e.get("created_at")) or T) > T - timedelta(days=d))
    names = [e["event"] for e in before]
    last = max((_parse(e["created_at"]) for e in before), default=None)
    active_days = len({(_parse(e["created_at"]).date()) for e in before if _parse(e.get("created_at"))})
    return [
        float(within(7)),                                  # events_7d
        float(within(30)),                                 # events_30d
        float(sum(1 for n in names if n in ("purchase", "order.paid", "payment_success"))),
        float(sum(1 for n in names if n in ("view_pricing", "add_to_cart", "cart_added", "plan_compare"))),
        float(sum(1 for n in names if n in ("email_click", "h5_click"))),
        float((T - last).total_seconds() / 86400) if last else 999.0,   # silence_days_at_T
        float(active_days),                                # active_days_30d
        float(STAGE_RANK.get(user.get("stage", "visitor"), 0)),
        1.0 if user.get("email") else 0.0,
    ]


def _build_dataset(users: list[dict], events_by_user: dict[str, list[dict]], label_event: str | None,
                   reference_days: int = 14, horizon_days: int = 14, min_history: int = 2) -> tuple[list[list[float]], list[int]]:
    """回放式构造数据集：T = now - reference_days；标签取 (T, T+horizon] 的目标事件（churn 则为"无事件"）。"""
    now = datetime.utcnow()
    T = now - timedelta(days=reference_days)
    end = min(now, T + timedelta(days=horizon_days))
    X: list[list[float]] = []
    y: list[int] = []
    for u in users:
        events = events_by_user.get(u["id"], [])
        before = [e for e in events if (t := _parse(e.get("created_at"))) and t <= T]
        if len(before) < min_history:
            continue
        after = [e for e in events if (t := _parse(e.get("created_at"))) and T < t <= end]
        if label_event is None:                    # churn：窗口内无任何事件 → 1
            label = 0 if after else 1
        else:                                      # 目标事件是否发生
            label = 1 if any(e["event"] == label_event or
                             (label_event == "purchase" and e["event"] in ("order.paid", "payment_success"))
                             for e in after) else 0
        X.append(_features_at(events, u, T))
        y.append(label)
    return X, y


# ── 纯 Python 逻辑回归（SGD + L2，够小样本用）──

def train_logreg(X: list[list[float]], y: list[int], epochs: int = 400, lr: float = 0.15,
                 l2: float = 0.01, seed: int = 42) -> dict[str, Any]:
    n, d = len(X), len(X[0]) if X else 0
    mean = [sum(row[j] for row in X) / n for j in range(d)]
    std = [math.sqrt(sum((row[j] - mean[j]) ** 2 for row in X) / n) or 1.0 for j in range(d)]
    Xn = [[(row[j] - mean[j]) / std[j] for j in range(d)] for row in X]
    w = [0.0] * d
    b = 0.0
    rng = random.Random(seed)
    idx = list(range(n))
    for _ in range(epochs):
        rng.shuffle(idx)
        for i in idx:
            z = b + sum(w[j] * Xn[i][j] for j in range(d))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            g = p - y[i]
            for j in range(d):
                w[j] -= lr * (g * Xn[i][j] + l2 * w[j])
            b -= lr * g
    return {"w": w, "b": b, "mean": mean, "std": std, "features": FEATURES}


def _auc(scores: list[float], labels: list[int]) -> float:
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return 0.5
    wins = sum(1 for a in pos for b in neg if a > b) + 0.5 * sum(1 for a in pos for b in neg if a == b)
    return round(wins / (len(pos) * len(neg)), 3)


def evaluate(model: dict[str, Any], X: list[list[float]], y: list[int]) -> dict[str, Any]:
    scores = [predict_score(model, row) for row in X]
    acc = sum(1 for s, l in zip(scores, y) if (s >= 0.5) == bool(l)) / max(1, len(y))
    return {"samples": len(y), "positives": sum(y), "accuracy": round(acc, 3), "auc": _auc(scores, y)}


def predict_score(model: dict[str, Any], row: list[float]) -> float:
    z = model["b"] + sum(model["w"][j] * ((row[j] - model["mean"][j]) / model["std"][j])
                         for j in range(len(model["w"])))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def model_path(data_dir: str, kind: str) -> str:
    return os.path.join(data_dir, MODELS_DIR, f"{kind}.json")


def load(data_dir: str, kind: str) -> dict | None:
    try:
        with open(model_path(data_dir, kind), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


async def train(store: Store, data_dir: str, kind: str = "churn", limit: int = 1500,
                reference_days: int = 14, horizon_days: int = 14) -> dict[str, Any]:
    """训练并落盘；返回指标。kind: churn | propensity"""
    cur = await store.db.execute(
        "SELECT * FROM users WHERE stage != 'visitor' ORDER BY last_seen DESC LIMIT ?", (limit,))
    users = [dict(r) for r in await cur.fetchall()]
    events_by_user: dict[str, list[dict]] = {}
    for u in users:
        events_by_user[u["id"]] = await store.recent_events_for_user(u["id"], limit=500)

    label_event = None if kind == "churn" else "purchase"
    X, y = _build_dataset(users, events_by_user, label_event,
                          reference_days=reference_days, horizon_days=horizon_days)
    if len(X) < 20 or len(set(y)) < 2:
        return {"ok": False, "reason": f"样本不足（{len(X)} 条，正例 {sum(y)}）", "samples": len(X)}

    split = max(1, int(len(X) * 0.8))
    model = train_logreg(X[:split], y[:split])
    metrics = evaluate(model, X[split:], y[split:]) if split < len(X) else evaluate(model, X, y)
    model.update({"kind": kind, "version": f"{kind}-v2-{datetime.utcnow().strftime('%Y%m%d')}",
                  "trained_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                  "metrics": metrics,
                  "params": {"reference_days": reference_days, "horizon_days": horizon_days}})
    os.makedirs(os.path.join(data_dir, MODELS_DIR), exist_ok=True)
    with open(model_path(data_dir, kind), "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False)
    return {"ok": True, **metrics, "version": model["version"], "samples": len(X)}


def blend(rule_score: float, model_score: float | None, samples: int, full_weight_samples: int = 200) -> tuple[float, float]:
    """规则分与模型分加权融合；样本越少模型权重越低（自适应）。"""
    if model_score is None:
        return rule_score, 0.0
    w = min(0.7, max(0.0, samples / max(1, full_weight_samples) * 0.7))
    return round((1 - w) * rule_score + w * model_score, 3), round(w, 2)

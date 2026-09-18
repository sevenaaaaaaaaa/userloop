"""预测模型化测试：回放式数据集 / 逻辑回归 / 融合权重 / 样本不足回退 / API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.predict import model as mdl


def _ts(days_ago: float) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).isoformat(timespec="seconds") + "Z"


def test_logreg_learns_separable_signal() -> None:
    """正例特征更强 → 训练后可分开（AUC 高）。"""
    X, y = [], []
    for i in range(60):
        strong = i % 2 == 0
        X.append([5.0 if strong else 0.0, 1.0 if strong else 0.0, 0, 0, 0, 1.0, 3.0, 3.0, 1.0])
        y.append(1 if strong else 0)
    m = mdl.train_logreg(X, y)
    metrics = mdl.evaluate(m, X, y)
    assert metrics["auc"] >= 0.95 and metrics["accuracy"] >= 0.9


def test_build_dataset_uses_history_and_future(tmp_path) -> None:
    """回放式数据集：标签来自参考时点之后的窗口（不泄漏未来信息到特征）。"""
    now = datetime.utcnow()
    T = now - timedelta(days=14)
    active_user = {"id": "u1", "stage": "activated", "email": "a@x.com",
                   "events": [{"event": "page_view", "created_at": (T - timedelta(days=d)).isoformat(timespec="seconds") + "Z"}
                              for d in (1, 3, 5, 8)] + [{"event": "page_view", "created_at": _ts(2)}]}
    churned_user = {"id": "u2", "stage": "activated", "email": "b@x.com",
                    "events": [{"event": "page_view", "created_at": (T - timedelta(days=d)).isoformat(timespec="seconds") + "Z"}
                               for d in (1, 3, 5, 8)]}
    events_by_user = {"u1": active_user["events"], "u2": churned_user["events"]}
    X, y = mdl._build_dataset([{k: v for k, v in active_user.items() if k != "events"},
                               {k: v for k, v in churned_user.items() if k != "events"}],
                              events_by_user, label_event=None, reference_days=14, horizon_days=14)
    assert len(X) == 2 and y == [0, 1]           # 活跃 → 未流失；沉默 → 流失


def test_blend_weight_adapts_to_samples() -> None:
    s0, w0 = mdl.blend(0.8, 0.2, samples=0)
    assert s0 == 0.8 and w0 == 0.0               # 无样本 → 纯规则
    s1, w1 = mdl.blend(0.8, 0.2, samples=100, full_weight_samples=200)
    assert 0 < w1 < 0.7 and s1 < 0.8             # 部分权重
    s2, w2 = mdl.blend(0.8, 0.2, samples=1000)
    assert w2 == 0.7 and round(s2, 3) == round(0.3 * 0.8 + 0.7 * 0.2, 3)


async def test_train_persists_and_falls_back(tmp_path) -> None:
    s = Store(str(tmp_path / "m.db"), {})
    await s.connect()
    try:
        # 样本不足 → 明确回退（不产出模型文件）
        out = await mdl.train(s, str(tmp_path), "churn")
        assert out["ok"] is False and "样本不足" in out["reason"]
        assert mdl.load(str(tmp_path), "churn") is None

        # 造 40 个用户：一半在参考点后仍活跃，一半沉默
        T_days = 14
        for i in range(40):
            u = await s.upsert_user(f"mu{i}", email=f"mu{i}@x.com")
            await s.update_user(u["id"], stage="activated")
            for d in (16, 18, 20, 22):            # 参考点之前的历史
                await s.insert_event({"user_id": u["id"], "distinct_id": f"mu{i}", "event": "page_view",
                                      "props": {}, "source": "t", "event_id": f"{u['id']}-h{d}-{i}",
                                      "created_at": _ts(d)})
            if i % 2 == 0:                        # 一半在窗口内继续活跃
                await s.insert_event({"user_id": u["id"], "distinct_id": f"mu{i}", "event": "email_click",
                                      "props": {}, "source": "t", "event_id": f"{u['id']}-f{i}",
                                      "created_at": _ts(3)})
        out = await mdl.train(s, str(tmp_path), "churn")
        assert out["ok"] and out["samples"] >= 20
        m = mdl.load(str(tmp_path), "churn")
        assert m and m["version"].startswith("churn-v2") and "w" in m
    finally:
        await s.close()


async def test_engine_uses_model_when_available(tmp_path) -> None:
    """有模型时 engine.compute 融合模型分并在归因里说明。"""
    from userloop.predict import engine

    s = Store(str(tmp_path / "e.db"), {})
    await s.connect()
    try:
        u = await s.upsert_user("eu1", email="eu@x.com")
        await s.update_user(u["id"], stage="activated")
        # 手写一个模型（特征全 0 均值/1 方差，偏置高 → 高分）
        import os

        os.makedirs(os.path.join(str(tmp_path), "models"), exist_ok=True)
        fake = {"w": [1.0] + [0.0] * (len(mdl.FEATURES) - 1), "b": 0.0,
                "mean": [0.0] * len(mdl.FEATURES), "std": [1.0] * len(mdl.FEATURES),
                "features": mdl.FEATURES, "version": "churn-v2-test",
                "metrics": {"samples": 300, "auc": 0.8}}
        with open(os.path.join(str(tmp_path), "models", "churn.json"), "w", encoding="utf-8") as f:
            json.dump(fake, f)
        row = await engine.compute(s, await s.get_user(u["id"]), data_dir=str(tmp_path))
        assert any("模型" in r for r in row["reasons"]["churn"])
        assert row["reasons"]["model"].get("churn", {}).get("version") == "churn-v2-test"
    finally:
        await s.close()


def test_model_api(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        st = client.get("/api/v1/predictions/model").json()
        assert "churn" in st["models"]
        r = client.post("/api/v1/predictions/train", json={"kinds": ["churn"]}).json()
        assert r["ok"] and "churn" in r["results"]


def test_adaptive_window_shrinks_for_young_data() -> None:
    assert mdl.adaptive_window(0) == (1, 1)
    assert mdl.adaptive_window(3) == (1, 1)          # 3 天 → 1 天窗口
    assert mdl.adaptive_window(30) == (10, 10)
    assert mdl.adaptive_window(365) == (14, 14)      # 上限 14 天

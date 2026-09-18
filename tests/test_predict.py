"""阶段二：预测层测试（churn / LTV / propensity 的确定性与归因）."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.predict import engine


def _days_ago(n: float) -> str:
    return (datetime.utcnow() - timedelta(days=n)).isoformat(timespec="seconds") + "Z"


async def _seed_events(store: Store, user: dict, events: list[tuple[str, float]]) -> None:
    for i, (name, days) in enumerate(events):
        await store.insert_event({"user_id": user["id"], "distinct_id": user["distinct_id"], "event": name,
                                  "props": {}, "source": "t", "event_id": f"{user['id']}-{i}",
                                  "created_at": _days_ago(days)})


async def test_churn_high_for_silent_paying_user(tmp_path) -> None:
    s = Store(str(tmp_path / "c.db"), {})
    await s.connect()
    try:
        u = await s.upsert_user("ch1", email="ch1@x.com")
        await s.update_user(u["id"], stage="paying", stats='{"purchases": 2, "total_amount": 599}')
        # 历史节奏：3~4 天一次 → 现已沉默 20 天
        await _seed_events(s, u, [("page_view", 30), ("page_view", 26), ("page_view", 22), ("page_view", 20)])
        row = await engine.compute(s, await s.get_user(u["id"]))
        assert row["churn"] >= 0.5
        assert any("沉默" in r or "下降" in r or "零活跃" in r for r in row["reasons"]["churn"])
        assert row["tier"] in ("high", "vip")
    finally:
        await s.close()


async def test_churn_low_for_active_user(tmp_path) -> None:
    s = Store(str(tmp_path / "c2.db"), {})
    await s.connect()
    try:
        u = await s.upsert_user("ch2", email="ch2@x.com")
        await s.update_user(u["id"], stage="activated")
        await _seed_events(s, u, [("page_view", 1), ("page_view", 2), ("page_view", 3),
                                  ("email_click", 1), ("page_view", 0.2)])
        row = await engine.compute(s, await s.get_user(u["id"]))
        assert row["churn"] < 0.35
    finally:
        await s.close()


async def test_propensity_signals(tmp_path) -> None:
    s = Store(str(tmp_path / "p.db"), {})
    await s.connect()
    try:
        u = await s.upsert_user("pr1", email="pr1@x.com")
        await s.update_user(u["id"], stage="activated")
        await _seed_events(s, u, [("view_pricing", 2), ("add_to_cart", 1), ("email_click", 1)])
        row = await engine.compute(s, await s.get_user(u["id"]))
        assert row["propensity"] >= 0.5
        reasons = row["reasons"]["propensity"]
        assert any("意向" in r for r in reasons) and any("加购" in r for r in reasons)

        # 已购买 → 加购未付信号消失
        u2 = await s.upsert_user("pr2", email="pr2@x.com")
        await _seed_events(s, u2, [("add_to_cart", 1), ("purchase", 0.5)])
        row2 = await engine.compute(s, await s.get_user(u2["id"]))
        assert not any("加购后未付款" in r for r in row2["reasons"]["propensity"])
    finally:
        await s.close()


async def test_ltv_tiers(tmp_path) -> None:
    s = Store(str(tmp_path / "l.db"), {})
    await s.connect()
    try:
        u = await s.upsert_user("l1", email="l1@x.com")
        await s.update_user(u["id"], stage="advocate", stats='{"purchases": 5, "total_amount": 5000}')
        row = await engine.compute(s, await s.get_user(u["id"]))
        assert row["tier"] == "vip" and row["ltv"] >= 0.75

        u2 = await s.upsert_user("l2", email="l2@x.com")
        row2 = await engine.compute(s, await s.get_user(u2["id"]))
        assert row2["tier"] == "low"
    finally:
        await s.close()


async def test_run_batch_and_top(tmp_path) -> None:
    s = Store(str(tmp_path / "b.db"), {})
    await s.connect()
    try:
        for i in range(3):
            u = await s.upsert_user(f"b{i}", email=f"b{i}@x.com")
            await s.update_user(u["id"], stage="paying")
            await _seed_events(s, u, [("page_view", 20 + i)])
        n = await engine.run_batch(s, limit=10)
        assert n == 3
        rows = await engine.top(s, "churn", limit=2)
        assert len(rows) == 2 and rows[0]["churn"] >= rows[1]["churn"]
    finally:
        await s.close()


def test_predictions_api(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "api1", "email": "api1@x.com",
                                            "event": "purchase"})
        r = client.post("/api/v1/predictions/recompute", json={"limit": 10}).json()
        assert r["ok"] and r["computed"] >= 1
        top = client.get("/api/v1/predictions?sort=churn&limit=5").json()
        assert top["count"] >= 1 and "churn" in top["items"][0]
        uid = top["items"][0]["user_id"]
        detail = client.get(f"/api/v1/predictions/{uid}").json()
        assert detail["user_id"] == uid and "reasons" in detail

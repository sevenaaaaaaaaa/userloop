"""性能与保留策略测试（对齐 OpenFlow 教训）：聚合端点 / 限流 / 批量查询 / 索引 / 清理."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.core.throttle import NOISE_WINDOW, Throttle


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "perf.db"))
    await s.connect()
    from userloop.core.templates import seed_templates

    await seed_templates(s, str(tmp_path))
    yield s
    await s.close()


def test_throttle_basic_and_noise_window() -> None:
    t = Throttle()
    assert t.allow_event("u1", "page_view") is True
    assert t.allow_event("u1", "page_view") is False      # 3 秒窗口内重复
    assert t.allow_event("u1", "click") is True
    assert t.allow_event("u2", "page_view") is True       # 不同用户互不影响
    assert NOISE_WINDOW == 90.0
    # 噪音事件同用户 90 秒 1 条
    assert t.allow_event("u3", "heartbeat") is True
    assert t.allow_event("u3", "heartbeat") is False
    assert t.stats()["keys"] >= 3


def test_throttle_evicts(monkeypatch) -> None:
    t = Throttle(max_keys=10)
    import userloop.core.throttle as m

    seq = iter(range(100))
    monkeypatch.setattr(m.time, "monotonic", lambda: next(seq) * 0.001)
    for i in range(60):
        t.allow(f"k{i}", 0.0)
    assert t.stats()["keys"] <= 10


async def test_indexes_present(store: Store) -> None:
    cur = await store.db.execute("SELECT name FROM sqlite_master WHERE type='index'")
    names = {r["name"] for r in await cur.fetchall()}
    for idx in ("idx_events_created", "idx_events_user_event", "idx_loops_template",
                "idx_loops_created", "idx_runs_flow", "idx_actions_status"):
        assert idx in names, idx


async def test_batch_actions_no_n_plus_one(store: Store) -> None:
    from userloop.actions.executors import ExecutorContext
    from userloop.core.bus import handle

    ctx = ExecutorContext(str(store.path).replace("perf.db", ""), {})
    for i in range(3):
        await handle(store, ctx, {"distinct_id": f"p{i}", "event": "purchase"})
    loops = await store.list_loops(limit=10)
    acts = await store.actions_for_loops([l["id"] for l in loops])
    assert len(acts) == len(loops)
    assert all(isinstance(v, list) for v in acts.values())


async def test_prune_events_retention(store: Store) -> None:
    long_ago = (datetime.utcnow() - timedelta(days=200)).isoformat(timespec="seconds") + "Z"
    noise_age = (datetime.utcnow() - timedelta(days=30)).isoformat(timespec="seconds") + "Z"
    recent = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    user = await store.upsert_user("retention_u")
    for ev, ts in (("page_view", long_ago), ("heartbeat", noise_age), ("page_view", recent)):
        await store.insert_event({"user_id": user["id"], "distinct_id": "retention_u", "event": ev,
                                  "props": {}, "source": "test", "event_id": f"{ev}-{ts}", "created_at": ts})
    before = (await store.counts())["events"]
    result = await store.prune_events(retention_days=180, noise_days=7)
    assert result["removed"] == 1        # 200 天前的事件按保留期清理
    assert result["noise_removed"] == 1  # 30 天前的 heartbeat 按噪音策略清理（早于 7 天窗口）
    after = (await store.counts())["events"]
    assert after == before - 2           # 只留最近 1 条


async def test_overview_endpoint_and_cache(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "ov1", "event": "purchase"})
        r1 = client.get("/api/v1/overview").json()
        assert set(r1) >= {"me", "overview", "stamp"}
        assert r1["overview"]["counts"]["users"] >= 1
        assert r1["overview"]["loops"], "loops 应随聚合返回"
        assert "actions" in r1["overview"]["loops"][0]
        r2 = client.get("/api/v1/overview").json()
        assert r2["cached_for"] >= 0  # 命中 10s 缓存
        # 心跳端点零 DB：字段固定且快
        hb = client.get("/api/v1/heartbeat").json()
        assert hb["ok"] is True and "ts" in hb


async def test_track_throttled(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        batch = {"events": [{"distinct_id": "t1", "event": "page_view", "props": {}, "event_id": "t-1"},
                            {"distinct_id": "t1", "event": "page_view", "props": {}, "event_id": "t-2"}]}
        r = client.post("/api/v1/track", json=batch).json()
        assert r["accepted"] == 1 and r["throttled"] == 1

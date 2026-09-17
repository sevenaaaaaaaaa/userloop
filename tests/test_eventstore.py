"""分层存储测试：EventStore 抽象（SQLite 默认 / MySQL 配置 / 降级 / 委托）."""

from __future__ import annotations

import pytest

from userloop.core.eventstore import FallbackEventStore, SqliteEventStore, build_event_store
from userloop.core.store import Store


async def test_default_backend_is_sqlite(tmp_path) -> None:
    s = Store(str(tmp_path / "a.db"), {})
    await s.connect()
    try:
        assert isinstance(s.events, SqliteEventStore)
        assert s.events.backend == "sqlite"
    finally:
        await s.close()


async def test_mysql_config_unreachable_falls_back(tmp_path) -> None:
    """MySQL 配了但连不上 → 自动降级 SQLite，闭环不中断（可用性优先）。"""
    cfg = {"storage": {"events": {"backend": "mysql", "mysql": {
        "enabled": True, "host": "127.0.0.1", "port": 33306, "user": "x", "password": "y", "database": "z"}}}}
    s = Store(str(tmp_path / "b.db"), cfg)
    await s.connect()
    try:
        assert isinstance(s.events, FallbackEventStore)
        assert s.events.backend.startswith("sqlite")
        assert s.events.reason  # 记录降级原因
        # 降级后功能照常
        u = await s.upsert_user("fb1")
        assert await s.insert_event({"user_id": u["id"], "distinct_id": "fb1", "event": "page_view",
                                     "props": {}, "source": "t", "event_id": "fb-1",
                                     "created_at": "2026-09-17T00:00:00Z"})
        assert (await s.counts())["events"] == 1
    finally:
        await s.close()


async def test_events_delegation_full_surface(tmp_path) -> None:
    """Store 的 events 全部走 EventStore（写/查/用户维度/最新时间/计数/清理）。"""
    s = Store(str(tmp_path / "c.db"), {})
    await s.connect()
    try:
        u = await s.upsert_user("dv1", email="dv1@x.com")
        for i, ev in enumerate(("page_view", "signup", "purchase")):
            await s.insert_event({"user_id": u["id"], "distinct_id": "dv1", "event": ev, "props": {"i": i},
                                  "source": "test", "event_id": f"dv-{i}",
                                  "created_at": f"2026-09-1{i}T00:00:00Z"})
        assert (await s.counts())["events"] == 3
        assert await s.count_events(u["id"], "page_view") == 1
        assert len(await s.recent_events_for_user(u["id"], limit=2)) == 2
        assert await s.newest_event_at() == "2026-09-12T00:00:00Z"
        assert await s.has_event_since(u["id"], "purchase", "2026-09-01T00:00:00Z")
        assert not await s.has_event_since(u["id"], "refund", "2026-09-01T00:00:00Z")
        # 幂等
        assert await s.insert_event({"user_id": u["id"], "distinct_id": "dv1", "event": "page_view",
                                     "props": {}, "source": "test", "event_id": "dv-0",
                                     "created_at": "2026-09-10T00:00:00Z"}) is None
        pruned = await s.prune_events(retention_days=3650, noise_days=3650)
        assert "removed" in pruned
    finally:
        await s.close()


async def test_build_event_store_auto_without_mysql(tmp_path) -> None:
    s = Store(str(tmp_path / "d.db"), {"storage": {"events": {"backend": "auto"}}})
    await s.connect()
    try:
        assert s.events.backend == "sqlite"
    finally:
        await s.close()

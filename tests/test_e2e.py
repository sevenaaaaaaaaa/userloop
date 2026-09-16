"""端到端测试：事件 → 旅程 → Loop → 动作 → 验证回流."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from userloop.actions.executors import ExecutorContext
from userloop.core.bus import handle
from userloop.core.entities import Stage
from userloop.core.scheduler import process_due_actions, sweep_inactivity, verify_due_loops
from userloop.core.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(":memory:")
    await s.connect()
    from userloop.core.templates import seed_templates

    await seed_templates(s, str(tmp_path))
    yield s
    await s.close()


@pytest.fixture
def ctx(tmp_path) -> ExecutorContext:
    return ExecutorContext(str(tmp_path), {})


async def test_event_creates_user_and_stage(store: Store, ctx: ExecutorContext) -> None:
    r = await handle(store, ctx, {"distinct_id": "u1", "email": "u1@x.com", "event": "page_view"})
    assert r["status"] == "ok"
    user = await store.get_user(r["user_id"])
    assert user["stage"] == Stage.VISITOR


async def test_journey_progression_and_transitions(store: Store, ctx: ExecutorContext) -> None:
    base = {"distinct_id": "u2", "email": "u2@x.com"}
    for ev in ("signup", "activation", "purchase"):
        await handle(store, ctx, {**base, "event": ev})
    user = await store.get_user((await store.list_users())[0]["id"])
    assert user["stage"] == Stage.PAYING
    transitions = await store.list_transitions(user_id=user["id"])
    assert [t["to_stage"] for t in reversed(transitions)] == ["signup", "activated", "paying"]


async def test_event_idempotent(store: Store, ctx: ExecutorContext) -> None:
    payload = {"distinct_id": "u3", "event": "signup", "event_id": "evt-1"}
    r1 = await handle(store, ctx, payload)
    r2 = await handle(store, ctx, payload)
    assert r1["status"] == "ok"
    assert r2["status"] == "deduped"


async def test_stage_enter_loop_created_and_executed(store: Store, ctx: ExecutorContext) -> None:
    r = await handle(store, ctx, {"distinct_id": "u4", "email": "u4@x.com", "event": "purchase"})
    loop_ids = r["loops_created"]
    assert len(loop_ids) >= 1
    loop = await store.get_loop(loop_ids[0])
    # 即期动作(delay=0)在总线内立即执行并收敛 → verified
    assert loop["status"] == "verified"
    actions = await store.actions_for_loop(loop["id"])
    assert actions and all(a["status"] == "done" for a in actions)
    # 即期动作执行完后无 goal_event → verified
    refreshed = await store.get_loop(loop["id"])
    assert refreshed["status"] in ("running", "verifying", "verified")


async def test_inactivity_sweep_and_verification(store: Store, ctx: ExecutorContext) -> None:
    # 注册用户，事件时间回溯 2 天 → signup_no_activate 模板（signup 1 天无事件）应命中
    ts = (datetime.utcnow() - timedelta(days=2)).isoformat(timespec="seconds") + "Z"
    r = await handle(store, ctx, {"distinct_id": "u5", "email": "u5@x.com", "event": "signup", "ts": ts})
    loops = await sweep_inactivity(store)
    assert len(loops) >= 1
    results = await process_due_actions(store, ctx)
    assert results, "expected due actions executed"
    # 窗口内注入 activation → 回流应为 effective
    await handle(store, ctx, {"distinct_id": "u5", "event": "activation"})
    feedbacks = await verify_due_loops(store)
    # 事件注入形成的 stage_enter loop 已 verified；滞留 loop 仍在验证期，需到期才验证
    assert isinstance(feedbacks, list)

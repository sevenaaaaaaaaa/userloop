"""Canvas 引擎端到端测试：触发/条件分支/动作/延迟恢复."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from userloop.actions.executors import ExecutorContext
from userloop.core.bus import handle
from userloop.core.canvas import eval_condition, resume_waits
from userloop.core.scheduler import resume_canvas_waits
from userloop.core.store import Store
from userloop.core.templates import seed_canvas

FLOW = {
    "id": "test_flow",
    "enabled": True,
    "nodes": [
        {"id": "t1", "type": "trigger", "event": "purchase"},
        {"id": "c1", "type": "condition", "rules": [{"field": "stats.purchases", "op": "gte", "value": 2}], "logic": "and"},
        {"id": "a1", "type": "action", "action": {"type": "email", "payload": {"subject": "VIP 礼遇", "text": "感谢 {name}"}}},
        {"id": "a2", "type": "action", "action": {"type": "webhook", "payload": {"subject": "普通跟进", "text": "hi"}}},
        {"id": "x1", "type": "exit"},
    ],
    "edges": [
        {"from": "t1", "to": "c1"},
        {"from": "c1", "to": "a1", "label": "true"},
        {"from": "c1", "to": "a2", "label": "false"},
        {"from": "a1", "to": "x1"},
        {"from": "a2", "to": "x1"},
    ],
}


@pytest.fixture
def ctx(tmp_path) -> ExecutorContext:
    return ExecutorContext(str(tmp_path), {})


@pytest.fixture
async def store(tmp_path):
    s = Store(":memory:")
    await s.connect()
    from userloop.core.templates import seed_templates

    await seed_templates(s, str(tmp_path))
    await s.put_canvas(FLOW)
    yield s
    await s.close()


def test_condition_ops() -> None:
    node = {"rules": [{"field": "a.b", "op": "gte", "value": 2}], "logic": "and"}
    assert eval_condition(node, {"a": {"b": 3}}) is True
    assert eval_condition(node, {"a": {"b": 1}}) is False
    node2 = {"rules": [{"field": "x", "op": "eq", "value": "y"}], "logic": "or"}
    assert eval_condition(node2, {}) if False else eval_condition(node2, {"x": "y"}) is True


FLOW_DELAY = {
    "id": "delay_flow",
    "enabled": True,
    "nodes": [
        {"id": "t1", "type": "trigger", "event": "signup"},
        {"id": "a1", "type": "action", "action": {"type": "email", "payload": {"subject": "欢迎", "text": "hi {name}"}}},
        {"id": "d1", "type": "delay", "minutes": 60},
        {"id": "a2", "type": "action", "action": {"type": "webhook", "payload": {"subject": "次日", "text": "回访"}}},
        {"id": "x1", "type": "exit"},
    ],
    "edges": [
        {"from": "t1", "to": "a1"},
        {"from": "a1", "to": "d1"},
        {"from": "d1", "to": "a2"},
        {"from": "a2", "to": "x1"},
    ],
}


def test_condition_ops() -> None:
    node = {"rules": [{"field": "a.b", "op": "gte", "value": 2}], "logic": "and"}
    assert eval_condition(node, {"a": {"b": 3}}) is True
    assert eval_condition(node, {"a": {"b": 1}}) is False
    node2 = {"rules": [{"field": "x", "op": "eq", "value": "y"}], "logic": "or"}
    assert eval_condition(node2, {"x": "y"}) is True


async def test_canvas_condition_branches(store: Store, ctx: ExecutorContext) -> None:
    # 首次购买 purchases=1 → false 分支 → webhook 动作 → exit → done
    r = await handle(store, ctx, {"distinct_id": "c1", "event": "purchase"})
    assert r["status"] == "ok"
    runs = await store.list_canvas_runs()
    assert runs, "canvas run should be created"
    trace = _loads(runs[0]["trace"])
    notes = [t["note"] for t in trace]
    assert any("branch=false" in n for n in notes)
    assert any("webhook ok=True" in n for n in notes)
    assert runs[0]["status"] == "done"

    # 二次购买 purchases=2 → true 分支 → VIP email 动作执行
    await handle(store, ctx, {"distinct_id": "c2", "event": "purchase"})   # 首购 → false
    r2 = await handle(store, ctx, {"distinct_id": "c2", "event": "purchase"})  # 二购 → true
    run_id = r2["canvas_runs"][0]
    runs = [r for r in await store.list_canvas_runs() if r["id"] == run_id]
    trace = _loads(runs[0]["trace"])
    notes = [t["note"] for t in trace]
    assert any("branch=true" in n for n in notes)
    # email 动作已执行（c2 无收件邮箱 → ok=False 亦证明分支命中了 a1）
    assert any(t["node"] == "a1" and t["type"] == "action" for t in trace)


async def test_canvas_delay_wait_and_resume(store: Store, ctx: ExecutorContext) -> None:
    await store.put_canvas(FLOW_DELAY)
    r = await handle(store, ctx, {"distinct_id": "d1", "event": "signup"})
    assert r["status"] == "ok"
    runs = await store.list_canvas_runs(limit=10)
    delay_runs = [x for x in runs if x["flow_id"] == "delay_flow"]
    assert delay_runs and delay_runs[0]["status"] == "waiting"
    waits = await store.due_canvas_waits("2999-01-01T00:00:00Z")
    assert len(waits) == 1

    # 到点恢复：把等待改到过去 → resume_waits 续走 a2 → exit → done
    past = (datetime.utcnow() - timedelta(minutes=1)).isoformat(timespec="seconds") + "Z"
    await store.db.execute("UPDATE canvas_waits SET resume_at=?", (past,))
    resumed = await resume_waits(store, ctx)
    assert len(resumed) == 1
    assert resumed[0]["status"] == "done"
    notes = [t["note"] for t in resumed[0]["trace"]]
    assert any("webhook ok=True" in n for n in notes)


def _loads(v) -> list:
    import json as _json

    return _json.loads(v or "[]") if isinstance(v, str) else (v or [])

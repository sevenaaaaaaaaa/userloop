"""N3：角色化 Agent 拆解目标 → 审批门 → 白名单执行。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from userloop.actions.executors import ExecutorContext
from userloop.agents import engine as agents
from userloop.agents import planner, roster
from userloop.core.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "agents.db"), {})
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def ctx(tmp_path) -> ExecutorContext:
    c = ExecutorContext(str(tmp_path), {"data_dir": str(tmp_path)})
    return c


def test_repurchase_goal_picks_recipe() -> None:
    p = planner.plan("本月复购率 +10%")
    assert p["recipe"] == "repurchase" and p["needs_approval"]
    tools = {t["tool"] for t in p["tasks"]}
    assert {"analyze", "draft_loop", "apply_pack", "notify_team", "verify_plan"} <= tools
    assert all(roster.tool_allowed(t["role"], t["tool"]) for t in p["tasks"])
    assert not roster.tool_allowed("analyst", "draft_loop")
    assert not roster.tool_allowed("content", "analyze")


async def test_campaign_waits_for_human_then_drafts_disabled_loop(store: Store, ctx: ExecutorContext) -> None:
    ctx.store = store
    out = await agents.create_campaign(store, ctx, "本月复购率 +10%")
    camp = out["campaign"]
    assert out["ok"] and out["needs_approval"]
    assert camp["status"] == "pending_approval"
    by_tool = {t["tool"]: t for t in out["tasks"]}
    assert by_tool["analyze"]["status"] == "done"
    assert by_tool["analyze"]["result"].get("ok")
    assert by_tool["draft_loop"]["status"] == "pending"
    assert by_tool["apply_pack"]["status"] == "pending"

    ap = await agents.approve_campaign(store, ctx, camp["id"])
    assert ap["ok"]
    snap = await agents.snapshot(store, camp["id"])
    done = {t["tool"]: t for t in snap["tasks"]}
    assert done["draft_loop"]["status"] == "done"
    tpl_id = done["draft_loop"]["result"]["template_id"]
    tpls = {t["id"]: t for t in await store.get_templates(enabled_only=False)}
    assert tpl_id in tpls and tpls[tpl_id]["enabled"] is False
    assert done["verify_plan"]["status"] == "done"
    assert snap["campaign"]["status"] in ("running", "done")


async def test_reject_cancels_pending(store: Store, ctx: ExecutorContext) -> None:
    ctx.store = store
    out = await agents.create_campaign(store, ctx, "激活提升", recipe="activation")
    cid = out["campaign"]["id"]
    rej = await agents.reject_campaign(store, cid, reason="预算不够")
    assert rej["ok"] and rej["campaign"]["status"] == "rejected"
    tasks = await store.list_agent_tasks(cid)
    assert all(t["status"] != "pending" for t in tasks)


def test_agents_api(tmp_path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"storage": {}}), encoding="utf-8")
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        roles = client.get("/api/v1/agents/roles").json()
        assert {r["id"] for r in roles["roles"]} >= {"analyst", "content", "outreach", "support"}
        created = client.post("/api/v1/agents/campaigns",
                              json={"goal": "本月复购率 +10%"}).json()
        assert created["ok"] and created["needs_approval"]
        cid = created["campaign"]["id"]
        assert created["campaign"]["status"] == "pending_approval"
        approved = client.post(f"/api/v1/agents/campaigns/{cid}/approve").json()
        assert approved["ok"]
        snap = client.get(f"/api/v1/agents/campaigns/{cid}").json()
        draft = next(t for t in snap["tasks"] if t["tool"] == "draft_loop")
        assert draft["status"] == "done" and draft["result"].get("enabled") is False

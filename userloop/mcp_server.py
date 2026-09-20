"""UserLoop MCP Server（stdio）—— 双向：读旅程 + 写操作走状态机/审批门.

供 OpenFlow AgentRuntime（mcp:* 工具通道）、Claude/Cursor 等 MCP 客户端消费：
  只读：userloop_dashboard / journey / loops / feedback / plugins / usage
  写入：userloop_create_campaign / userloop_approve_campaign
        （只创建草稿战役或走审批，不直接改模板、不直接发触达）

运行：userloop mcp
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("userloop")


def _load_cfg() -> dict:
    from userloop.config import load_config

    return load_config()


async def _with_store(fn: Any, with_ctx: bool = False) -> Any:
    from userloop.actions.executors import ExecutorContext
    from userloop.core.store import Store

    cfg = _load_cfg()
    store = Store(cfg["db_path"], cfg)
    await store.connect()
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    ctx.store = store
    try:
        return await fn(store, ctx) if with_ctx else await fn(store)
    finally:
        await store.close()


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


@mcp.tool()
async def userloop_dashboard() -> str:
    """全局看板：用户/事件/Loop 计数、旅程漏斗、Loop 状态分布、模板验证回流统计."""
    async def run(store: Any) -> dict:
        stages = await store.count_users_by_stage()
        from userloop.core.entities import STAGE_ORDER, Stage

        funnel = [{"stage": s.value, "users": stages.get(s.value, 0)}
                  for s in sorted(STAGE_ORDER, key=STAGE_ORDER.get)]
        return {"counts": await store.counts(), "funnel": funnel,
                "loop_status": await store.count_loops_by_status(),
                "template_stats": await store.feedback_stats()}

    return _j(await _with_store(run))


@mcp.tool()
async def userloop_journey(distinct_id: str) -> str:
    """单用户旅程档案：CDP 画像、阶段迁移历史、最近 10 条行为事件."""
    async def run(store: Any) -> dict:
        user = await store.find_user(distinct_id)
        if not user:
            return {"error": f"user not found: {distinct_id}"}
        uid = user["id"]
        return {
            "user": {"stage": user["stage"], "email": user["email"],
                     "stats": json.loads(user["stats"] or "{}"),
                     "first_seen": user["first_seen"], "last_seen": user["last_seen"]},
            "transitions": [{"from": t["from_stage"], "to": t["to_stage"], "reason": t["reason"],
                             "at": t["created_at"]} for t in reversed(await store.list_transitions(user_id=uid, limit=20))],
            "recent_events": [{"event": e["event"], "at": e["created_at"]}
                              for e in await store.recent_events_for_user(uid, limit=10)],
            "loops": [{"id": l["id"], "template": l["template_id"], "status": l["status"]}
                      for l in await store.list_loops(limit=50) if l["user_id"] == uid],
        }

    return _j(await _with_store(run))


@mcp.tool()
async def userloop_loops(status: str = "") -> str:
    """Loop 运行列表（可选 status 过滤：running/verifying/verified/failed），含动作状态."""
    async def run(store: Any) -> list:
        rows = await store.list_loops(status=status or None, limit=20)
        out = []
        for loop in rows:
            actions = await store.actions_for_loop(loop["id"])
            out.append({"id": loop["id"], "template": loop["template_id"], "status": loop["status"],
                        "created": loop["created_at"],
                        "actions": [{"type": a["type"], "status": a["status"]} for a in actions]})
        return out

    return _j(await _with_store(run))


@mcp.tool()
async def userloop_feedback(template_id: str = "") -> str:
    """模板级验证回流统计：effective/neutral 分布（哪类运营 Loop 真的有用）."""
    async def run(store: Any) -> dict:
        stats = await store.feedback_stats()
        if template_id:
            return {template_id: stats.get(template_id, {})}
        return stats

    return _j(await _with_store(run))


@mcp.tool()
async def userloop_list_campaigns() -> str:
    """N3 战役列表（目标拆解后的状态：pending_approval / running / done）。"""
    async def run(store: Any) -> list:
        return await store.list_campaigns(limit=30)

    return _j(await _with_store(run))


@mcp.tool()
async def userloop_create_campaign(goal: str) -> str:
    """把运营目标拆成跨角色任务。含中风险动作时进入待审批，不会直接启用 Loop 或对外触达。"""
    async def run(store: Any, ctx: Any) -> dict:
        from userloop.agents import engine as agents

        return await agents.create_campaign(store, ctx, goal)

    return _j(await _with_store(run, with_ctx=True))


@mcp.tool()
async def userloop_approve_campaign(campaign_id: str) -> str:
    """批准战役：仅通过 UserLoop 状态机执行白名单任务（草稿 Loop 默认停用）。"""
    async def run(store: Any, ctx: Any) -> dict:
        from userloop.agents import engine as agents

        return await agents.approve_campaign(store, ctx, campaign_id)

    return _j(await _with_store(run, with_ctx=True))


@mcp.tool()
async def userloop_list_plugins() -> str:
    """N4 插件市场：source / action / model / template。"""
    async def run(store: Any) -> list:
        from userloop.plugins import registry as plug

        return plug.list_plugins(_load_cfg())

    return _j(await _with_store(run))


@mcp.tool()
async def userloop_usage() -> str:
    """当前租户今日用量与配额（默认只记账；billing.enforce 才拦截）。"""
    async def run(store: Any, ctx: Any) -> dict:
        from userloop.billing import meter as billing

        return await billing.snapshot(store, ctx)

    return _j(await _with_store(run, with_ctx=True))


def main() -> None:
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()

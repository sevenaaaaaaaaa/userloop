"""UserLoop MCP Server（stdio）—— 联动通道③：只读回读旅程数据.

供 OpenFlow AgentRuntime（mcp:* 工具通道）、Claude/Cursor 等 MCP 客户端消费：
  userloop_dashboard  全局看板（漏斗/Loop 状态/模板效果）
  userloop_journey    单用户旅程（档案 + 阶段迁移 + 最近事件）
  userloop_loops      Loop 运行列表（可按状态过滤）
  userloop_feedback   模板级验证回流统计

运行：userloop mcp   （stdio，只读，不暴露写操作）
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


async def _with_store(fn: Any) -> Any:
    from userloop.core.store import Store

    cfg = _load_cfg()
    store = Store(cfg["db_path"], cfg)
    await store.connect()
    try:
        return await fn(store)
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


def main() -> None:
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()

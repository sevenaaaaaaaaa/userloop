"""Canvas 引擎：nodes/edges 图执行 —— 对齐 OpenFlow CanvasSystem（canvas_trigger→canvas_walk）.

节点类型：
  trigger   {"event": "purchase"} 或 {"stage": "paying"}（stage_enter 匹配）
  condition {"rules": [{"field":"stats.purchases","op":"gte","value":2}], "logic":"and|or"}
            → 按 true/false 边分支
  action    {"action": {"type":"email","payload":{...}}} → 复用动作执行器
  delay     {"minutes": 60} → 写 canvas_waits，调度器到点恢复
  exit      终止

执行契约：深度限 32 防环；每步写 trace；delay 停走写等待队列，scheduler 恢复。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.core.entities import iso
from userloop.core.store import Store, new_id, pj

MAX_DEPTH = 32


def _hydrated_user(user: dict) -> dict:
    """stats 可能是 JSON 字符串（来自 DB），条件求值需要 dict."""
    out = dict(user)
    if isinstance(out.get("stats"), str):
        out["stats"] = pj(out["stats"], {}) or {}
    return out


def _dt(minutes: int) -> datetime:
    return datetime.utcnow() + timedelta(minutes=minutes)


# ---- 条件求值（支持 user./stats./props. 点路径）----

def _resolve(field: str, wctx: dict) -> Any:
    cur: Any = wctx
    for p in field.split("."):
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("-inf")


_OPS = {
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
    "gt": lambda a, b: _num(a) > _num(b),
    "gte": lambda a, b: _num(a) >= _num(b),
    "lt": lambda a, b: _num(a) < _num(b),
    "lte": lambda a, b: _num(a) <= _num(b),
    "contains": lambda a, b: b in a if isinstance(a, (str, list, dict)) else False,
    "exists": lambda a, b: a is not None,
    "empty": lambda a, b: a in (None, "", [], {}),
}


def eval_condition(node: dict, wctx: dict) -> bool:
    rules = [r for r in (node.get("rules") or []) if r.get("field")]
    if not rules:
        return True
    logic = (node.get("logic") or "and").lower()
    results = []
    for rule in rules:
        actual = _resolve(rule["field"], wctx)
        op = _OPS.get(rule.get("op", "eq"))
        results.append(bool(op(actual, rule.get("value"))) if op else False)
    return all(results) if logic == "and" else any(results)


# ---- 图执行 ----

class CanvasContext:
    """一次 walk 的可变状态：trace 记录每步节点执行."""

    def __init__(self, store: Store, ctx: ExecutorContext, flow: dict, run: dict, user: dict) -> None:
        self.store = store
        self.ctx = ctx
        self.flow = flow
        self.run = run
        self.user = user
        self.edges: dict[str, list[dict]] = {}
        for e in flow.get("edges", []):
            self.edges.setdefault(e.get("from", ""), []).append(e)

    def log(self, node_id: str, node_type: str, note: str) -> None:
        self.run.setdefault("trace", []).append({"node": node_id, "type": node_type, "note": note, "at": iso()})

    async def flush(self) -> None:
        await self.store.update_canvas_run(
            self.run["id"],
            trace=json.dumps(self.run.get("trace", []), ensure_ascii=False),
            status=self.run["status"],
        )


def _outgoing(cc: CanvasContext, node: dict) -> list[dict]:
    edges = cc.edges.get(node["id"], [])
    if node.get("type") == "condition":
        branch = "true" if cc.run.get("_branch") else "false"
        labeled = [e for e in edges if e.get("label")]
        if labeled:
            matched = [e for e in labeled if (e.get("label") or "true") == branch]
            return matched or [e for e in edges if not e.get("label")]
    return edges


async def walk(cc: CanvasContext, node_id: str, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        cc.log(node_id, "guard", "max depth exceeded")
        cc.run["status"] = "done"
        return

    nodes = {n.get("id"): n for n in cc.flow.get("nodes", [])}
    node = nodes.get(node_id)
    if not node:
        cc.run["status"] = "done"
        return

    ntype = node.get("type", "")

    if ntype == "action":
        spec = dict(node.get("action") or {})
        spec.setdefault("type", "generic")
        pseudo_loop = {"id": f"canvas_{cc.run['id']}", "template_id": cc.flow["id"],
                       "trigger": {"type": "canvas", "flow": cc.flow["id"]}}
        result = await execute_action(cc.ctx, spec, pseudo_loop, cc.user)
        note = f"{spec['type']} ok={result.get('ok')}"
        if result.get("dry_run"):
            note += " dry_run"
        cc.log(node_id, "action", note)

    elif ntype == "condition":
        wctx = {
            "user": cc.user,
            "stats": cc.user.get("stats") or {},
            "props": (cc.run.get("context") or {}).get("props", {}),
        }
        cc.run["_branch"] = eval_condition(node, wctx)
        cc.log(node_id, "condition", f"branch={'true' if cc.run['_branch'] else 'false'}")

    elif ntype == "delay":
        minutes = int(node.get("minutes", 0))
        resume_at = iso(_dt(minutes))
        await cc.store.insert_canvas_wait({
            "id": new_id("cw"), "run_id": cc.run["id"], "flow_id": cc.flow["id"],
            "user_id": cc.user["id"], "node_id": node_id, "resume_at": resume_at,
        })
        cc.run["status"] = "waiting"
        cc.log(node_id, "delay", f"wait {minutes}min until {resume_at}")
        await cc.flush()
        return

    elif ntype == "trigger":
        cc.log(node_id, "trigger", "entered")

    elif ntype == "exit":
        cc.log(node_id, "exit", "flow end")
        cc.run["status"] = "done"
        await cc.flush()
        return

    else:
        cc.log(node_id, "unknown", f"type={ntype}")

    outgoing = _outgoing(cc, node)
    if not outgoing:
        cc.run["status"] = "done"
        await cc.flush()
        return

    for edge in outgoing:
        if cc.run["status"] == "waiting":
            break
        await walk(cc, edge["to"], depth + 1)

    if cc.run["status"] != "waiting":
        await cc.flush()


# ---- 触发入口 ----

def _node_matches(node: dict, event: str | None, transition: dict | None) -> bool:
    if node.get("type") != "trigger":
        return False
    if event and node.get("event") == event:
        return True
    if transition and node.get("stage") == transition.get("to_stage"):
        return True
    return False


async def evaluate(
    store: Store,
    ctx: ExecutorContext,
    user: dict,
    event: str | None,
    props: dict,
    transition: dict | None,
) -> list[dict]:
    """事件总线调用：匹配 trigger 节点 → 每个 flow 生成一个 run 并启动 walk."""
    started: list[dict] = []
    for flow in await store.list_canvas(enabled_only=True):
        entries = [n for n in flow.get("nodes", []) if _node_matches(n, event, transition)]
        if not entries:
            continue
        run_id = new_id("cr")
        run = {
            "id": run_id, "flow_id": flow["id"], "user_id": user["id"],
            "status": "running",
            "context": {"props": props},
            "trace": [], "created_at": iso(), "updated_at": iso(),
        }
        await store.insert_canvas_run(run)
        cc = CanvasContext(store, ctx, flow, run, _hydrated_user(user))
        for entry in entries:
            await walk(cc, entry["id"])
        await cc.flush()
        started.append(run)
    return started


async def resume_waits(store: Store, ctx: ExecutorContext) -> list[dict]:
    """调度器：到点的 delay 恢复 walk."""
    now_iso = iso()
    resumed: list[dict] = []
    for w in await store.due_canvas_waits(now_iso):
        flow = await store.get_canvas(w["flow_id"])
        run_row = await store.get_canvas_run(w["run_id"])
        user = await store.get_user(w["user_id"])
        await store.update_canvas_wait(w["id"], status="resumed")
        if not flow or not run_row or not user:
            continue
        run = {
            "id": run_row["id"], "flow_id": run_row["flow_id"], "user_id": run_row["user_id"],
            "status": "running", "context": {},
            "trace": pj_list(run_row.get("trace")),
            "created_at": run_row["created_at"], "updated_at": run_row["updated_at"],
        }
        cc = CanvasContext(store, ctx, flow, run, _hydrated_user(user))
        for edge in cc.edges.get(w["node_id"], []):
            if cc.run["status"] == "waiting":
                break
            await walk(cc, edge["to"])
        await cc.flush()
        resumed.append(run)
    return resumed


def pj_list(s: Any) -> list:
    from userloop.core.store import pj

    return pj(s, []) or []

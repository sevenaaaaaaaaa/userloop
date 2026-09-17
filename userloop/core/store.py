"""SQLite 存储层（aiosqlite, WAL）—— 对齐 inFlow store.py 风格，单文件库。"""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    distinct_id TEXT UNIQUE NOT NULL,
    email TEXT,
    name TEXT,
    stage TEXT NOT NULL DEFAULT 'visitor',
    props TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_stage ON users(stage);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    distinct_id TEXT NOT NULL,
    event TEXT NOT NULL,
    props TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'api',
    event_id TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(event, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_user_event ON events(user_id, event);

CREATE TABLE IF NOT EXISTS stage_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    from_stage TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_user ON stage_transitions(user_id, created_at);

CREATE TABLE IF NOT EXISTS loop_templates (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loops (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    trigger TEXT NOT NULL DEFAULT '{}',
    context TEXT NOT NULL DEFAULT '{}',
    verify_after TEXT,
    verify_before TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_loops_user ON loops(user_id, template_id);
CREATE INDEX IF NOT EXISTS idx_loops_status ON loops(status);
CREATE INDEX IF NOT EXISTS idx_loops_template ON loops(template_id, created_at);
CREATE INDEX IF NOT EXISTS idx_loops_created ON loops(created_at);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    loop_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    delay_minutes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_at TEXT NOT NULL,
    executed_at TEXT,
    result TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, scheduled_at);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    goal_event TEXT,
    evidence TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canvas_flows (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canvas_runs (
    id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    trace TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON canvas_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_flow ON canvas_runs(flow_id, created_at);

CREATE TABLE IF NOT EXISTS canvas_waits (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    resume_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_waits_due ON canvas_waits(status, resume_at);
"""


def j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def pj(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        # 性能与并发（对齐 OpenFlow 教训）：WAL + NORMAL 同步 + 忙等，避免读写互堵
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA cache_size=-8000")  # 8MB page cache
        await self.db.execute("PRAGMA wal_autocheckpoint=512")
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        try:
            await self.db.execute("PRAGMA optimize")
        except aiosqlite.Error:
            pass

    async def close(self) -> None:
        if self.db:
            try:
                await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 收尾压缩 WAL，防文件膨胀
            except aiosqlite.Error:
                pass
            await self.db.close()
            self.db = None

    async def commit(self) -> None:
        assert self.db
        await self.db.commit()

    # ---- users (CDP find-or-create) ----

    async def find_user(self, distinct_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM users WHERE distinct_id=?", (distinct_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_user(
        self,
        distinct_id: str,
        email: str | None = None,
        name: str | None = None,
        props: dict | None = None,
    ) -> dict:
        assert self.db
        existing = await self.find_user(distinct_id)
        if existing:
            merged_props = {**(pj(existing["props"], {}) or {}), **(props or {})}
            await self.db.execute(
                "UPDATE users SET email=COALESCE(?, email), name=COALESCE(?, name), "
                "props=?, last_seen=? WHERE id=?",
                (email, name, j(merged_props), iso_now(), existing["id"]),
            )
            return await self.find_user(distinct_id)  # type: ignore[return-value]
        uid = new_id("u")
        await self.db.execute(
            "INSERT INTO users (id, distinct_id, email, name, stage, props, stats, first_seen, last_seen) "
            "VALUES (?,?,?,?,'visitor',?,'{}',?,?)",
            (uid, distinct_id, email, name, j(props or {}), iso_now(), iso_now()),
        )
        return await self.find_user(distinct_id)  # type: ignore[return-value]

    async def update_user(self, user_id: str, **fields: Any) -> None:
        assert self.db
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), user_id))

    async def get_user(self, user_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_users(self, limit: int = 100) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def count_users_by_stage(self) -> dict[str, int]:
        assert self.db
        cur = await self.db.execute("SELECT stage, COUNT(*) c FROM users GROUP BY stage")
        return {r["stage"]: r["c"] for r in await cur.fetchall()}

    # ---- events ----

    async def insert_event(self, e: dict) -> int | None:
        assert self.db
        try:
            cur = await self.db.execute(
                "INSERT INTO events (user_id, distinct_id, event, props, source, event_id, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (e["user_id"], e["distinct_id"], e["event"], j(e.get("props", {})),
                 e.get("source", "api"), e.get("event_id"), e["created_at"]),
            )
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None  # 幂等去重

    async def has_event_since(self, user_id: str, event: str | None, since: str, before: str | None = None) -> dict | None:
        """查询窗口 [since, before) 内是否出现过目标事件（before=None 表示至今）。"""
        assert self.db
        if event:
            sql = "SELECT * FROM events WHERE user_id=? AND event=? AND created_at>=?"
            args: list[Any] = [user_id, event, since]
        else:
            sql = "SELECT * FROM events WHERE user_id=? AND created_at>=?"
            args = [user_id, since]
        if before:
            sql += " AND created_at<?"
            args.append(before)
        sql += " LIMIT 1"
        cur = await self.db.execute(sql, args)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def count_events(self, user_id: str, event: str) -> int:
        assert self.db
        cur = await self.db.execute("SELECT COUNT(*) c FROM events WHERE user_id=? AND event=?", (user_id, event))
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def list_events(self, limit: int = 100) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def recent_events_for_user(self, user_id: str, limit: int = 10) -> list[dict]:
        """单用户最近事件（走 idx_events_user，不在内存里全表过滤）。"""
        assert self.db
        cur = await self.db.execute(
            "SELECT * FROM events WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [dict(r) for r in await cur.fetchall()]

    async def newest_event_at(self) -> str | None:
        """最新事件时间（极轻量，用于查询条件缓存/心跳判断）。"""
        assert self.db
        cur = await self.db.execute("SELECT created_at FROM events ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return row["created_at"] if row else None

    async def prune_events(self, retention_days: int = 180, noise_days: int = 7,
                           noise_events: tuple[str, ...] = ("heartbeat",)) -> dict:
        """保留策略（对齐 OpenFlow 教训：事件表不得无限增长）。

        普通事件保留 retention_days 天；噪音类事件（心跳等）只留 noise_days 天。
        """
        from datetime import datetime, timedelta

        assert self.db
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat(timespec="seconds") + "Z"
        noise_cut = (datetime.utcnow() - timedelta(days=noise_days)).isoformat(timespec="seconds") + "Z"
        cur = await self.db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        removed = cur.rowcount or 0
        noise_removed = 0
        if noise_events:
            marks = ",".join("?" * len(noise_events))
            cur = await self.db.execute(
                f"DELETE FROM events WHERE event IN ({marks}) AND created_at < ?",
                (*noise_events, noise_cut))
            noise_removed = cur.rowcount or 0
        try:
            await self.db.execute("PRAGMA optimize")
            await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 清理后压缩 WAL（DB 使用教训）
        except aiosqlite.Error:
            pass
        return {"removed": removed, "noise_removed": noise_removed, "cutoff": cutoff}

    # ---- stage transitions ----

    async def insert_transition(self, t: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO stage_transitions (user_id, from_stage, to_stage, reason, created_at) VALUES (?,?,?,?,?)",
            (t["user_id"], t["from_stage"], t["to_stage"], t.get("reason", ""), t["created_at"]),
        )

    async def list_transitions(self, user_id: str | None = None, limit: int = 100) -> list[dict]:
        assert self.db
        if user_id:
            cur = await self.db.execute(
                "SELECT * FROM stage_transitions WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
            )
        else:
            cur = await self.db.execute("SELECT * FROM stage_transitions ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    # ---- loop templates ----

    async def put_template(self, t: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO loop_templates (id, data) VALUES (?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (t["id"], j(t)),
        )

    async def get_templates(self, enabled_only: bool = True) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT data FROM loop_templates")
        rows = [pj(r["data"], {}) for r in await cur.fetchall()]
        return [r for r in rows if r and (not enabled_only or r.get("enabled", True))]

    # ---- loops ----

    async def insert_loop(self, loop: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO loops (id, template_id, user_id, status, trigger, context, verify_after, verify_before, "
            "created_at, updated_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (loop["id"], loop["template_id"], loop["user_id"], loop["status"], j(loop.get("trigger", {})),
             j(loop.get("context", {})), loop.get("verify_after"), loop.get("verify_before"),
             loop["created_at"], loop["updated_at"], loop.get("error")),
        )

    async def update_loop(self, loop_id: str, **fields: Any) -> None:
        assert self.db
        fields.setdefault("updated_at", iso_now())
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE loops SET {cols} WHERE id=?", (*fields.values(), loop_id))

    async def get_loop(self, loop_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM loops WHERE id=?", (loop_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_loops(self, status: str | None = None, limit: int = 100) -> list[dict]:
        assert self.db
        if status:
            cur = await self.db.execute(
                "SELECT * FROM loops WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            cur = await self.db.execute("SELECT * FROM loops ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def has_recent_loop(self, template_id: str, user_id: str, since: str) -> bool:
        assert self.db
        cur = await self.db.execute(
            "SELECT 1 FROM loops WHERE template_id=? AND user_id=? AND created_at>=? LIMIT 1",
            (template_id, user_id, since),
        )
        return await cur.fetchone() is not None

    async def count_loops_by_status(self) -> dict[str, int]:
        assert self.db
        cur = await self.db.execute("SELECT status, COUNT(*) c FROM loops GROUP BY status")
        return {r["status"]: r["c"] for r in await cur.fetchall()}

    # ---- actions ----

    async def insert_action(self, a: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO actions (id, loop_id, seq, type, payload, delay_minutes, status, scheduled_at, executed_at, "
            "result) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (a["id"], a["loop_id"], a["seq"], a["type"], j(a.get("payload", {})), a.get("delay_minutes", 0),
             a["status"], a["scheduled_at"], a.get("executed_at"), j(a.get("result", {}))),
        )

    async def due_actions(self, now_iso: str, limit: int = 50) -> list[dict]:
        """到期的 pending 动作（调度器消费）。"""
        assert self.db
        cur = await self.db.execute(
            "SELECT * FROM actions WHERE status='pending' AND scheduled_at<=? ORDER BY scheduled_at LIMIT ?",
            (now_iso, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def update_action(self, action_id: str, **fields: Any) -> None:
        assert self.db
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE actions SET {cols} WHERE id=?", (*fields.values(), action_id))

    async def actions_for_loop(self, loop_id: str) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT * FROM actions WHERE loop_id=? ORDER BY seq", (loop_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def actions_for_loops(self, loop_ids: list[str]) -> dict[str, list[dict]]:
        """批量取动作（消除 N+1：列表接口一次 WHERE IN 查完）。"""
        assert self.db
        out: dict[str, list[dict]] = {i: [] for i in loop_ids}
        if not loop_ids:
            return out
        marks = ",".join("?" * len(loop_ids))
        cur = await self.db.execute(
            f"SELECT * FROM actions WHERE loop_id IN ({marks}) ORDER BY loop_id, seq", tuple(loop_ids))
        for r in await cur.fetchall():
            out.setdefault(r["loop_id"], []).append(dict(r))
        return out

    # ---- canvas ----

    async def put_canvas(self, flow: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO canvas_flows (id, data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (flow["id"], j(flow)),
        )

    async def get_canvas(self, flow_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT data FROM canvas_flows WHERE id=?", (flow_id,))
        row = await cur.fetchone()
        return pj(row["data"], {}) if row else None

    async def list_canvas(self, enabled_only: bool = True) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT data FROM canvas_flows")
        rows = [pj(r["data"], {}) for r in await cur.fetchall()]
        return [r for r in rows if r and (not enabled_only or r.get("enabled", True))]

    async def insert_canvas_run(self, run: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO canvas_runs (id, flow_id, user_id, status, trace, created_at, updated_at, error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run["id"], run["flow_id"], run["user_id"], run["status"], j(run.get("trace", [])),
             run["created_at"], run["updated_at"], run.get("error")),
        )

    async def update_canvas_run(self, run_id: str, **fields: Any) -> None:
        assert self.db
        fields.setdefault("updated_at", iso_now())
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE canvas_runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    async def list_canvas_runs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        assert self.db
        if status:
            cur = await self.db.execute(
                "SELECT * FROM canvas_runs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            cur = await self.db.execute("SELECT * FROM canvas_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def get_canvas_run(self, run_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM canvas_runs WHERE id=?", (run_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def insert_canvas_wait(self, w: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO canvas_waits (id, run_id, flow_id, user_id, node_id, resume_at, status) VALUES (?,?,?,?,?,?,?)",
            (w["id"], w["run_id"], w["flow_id"], w["user_id"], w["node_id"], w["resume_at"], "pending"),
        )

    async def due_canvas_waits(self, now_iso: str, limit: int = 50) -> list[dict]:
        assert self.db
        cur = await self.db.execute(
            "SELECT * FROM canvas_waits WHERE status='pending' AND resume_at<=? ORDER BY resume_at LIMIT ?",
            (now_iso, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def update_canvas_wait(self, wait_id: str, **fields: Any) -> None:
        assert self.db
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE canvas_waits SET {cols} WHERE id=?", (*fields.values(), wait_id))

    # ---- feedback ----

    async def insert_feedback(self, f: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO feedback (loop_id, template_id, verdict, goal_event, evidence, created_at) VALUES (?,?,?,?,?,?)",
            (f["loop_id"], f["template_id"], f["verdict"], f.get("goal_event"), j(f.get("evidence", {})), f["created_at"]),
        )

    async def feedback_stats(self) -> dict[str, dict[str, int]]:
        """按模板统计 effective/neutral —— 模板级效果看板。"""
        assert self.db
        cur = await self.db.execute(
            "SELECT template_id, verdict, COUNT(*) c FROM feedback GROUP BY template_id, verdict"
        )
        out: dict[str, dict[str, int]] = {}
        for r in await cur.fetchall():
            out.setdefault(r["template_id"], {})[r["verdict"]] = r["c"]
        return out

    async def counts(self) -> dict:
        assert self.db
        out = {}
        for table in ("users", "events", "loops", "actions", "feedback"):
            cur = await self.db.execute(f"SELECT COUNT(*) c FROM {table}")
            row = await cur.fetchone()
            out[table] = row["c"] if row else 0
        return out


def iso_now() -> str:
    from userloop.core.entities import iso

    return iso()

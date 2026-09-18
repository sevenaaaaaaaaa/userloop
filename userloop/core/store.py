"""SQLite 存储层（aiosqlite, WAL）—— 对齐 inFlow store.py 风格，单文件库。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
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

CREATE TABLE IF NOT EXISTS ab_experiments (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ab_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    user_id TEXT NOT NULL,
    loop_id TEXT,
    channel TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ab_exp ON ab_assignments(experiment_id, variant, created_at);
CREATE INDEX IF NOT EXISTS idx_ab_user ON ab_assignments(user_id, experiment_id);

CREATE TABLE IF NOT EXISTS h5_campaigns (
    campaign_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    share_token TEXT NOT NULL,
    url TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    granted INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, purpose)
);
CREATE INDEX IF NOT EXISTS idx_consents_user ON consents(user_id);

CREATE TABLE IF NOT EXISTS user_scores (
    user_id TEXT PRIMARY KEY,
    churn REAL NOT NULL DEFAULT 0,
    ltv REAL NOT NULL DEFAULT 0,
    propensity REAL NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT '',
    best_hour INTEGER,
    reasons TEXT NOT NULL DEFAULT '{}',
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS touch_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_created ON touch_blocks(created_at);

CREATE TABLE IF NOT EXISTS touch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    action_type TEXT NOT NULL,
    template_id TEXT,
    loop_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_touchlog_user ON touch_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_touchlog_channel ON touch_log(channel, created_at);

CREATE TABLE IF NOT EXISTS touch_pages (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    loop_id TEXT,
    template_id TEXT,
    goal_event TEXT,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    cta_text TEXT NOT NULL DEFAULT '',
    cta_url TEXT NOT NULL DEFAULT '',
    views INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_user ON touch_pages(user_id, created_at);

CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(type, value)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id, type);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    stage TEXT,
    intent TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'none',
    risk TEXT NOT NULL DEFAULT 'low',
    status TEXT NOT NULL DEFAULT 'noop',
    reasoning TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    expected_effect TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    payload TEXT,
    loop_id TEXT,
    model TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_user ON ai_decisions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_status ON ai_decisions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_created ON ai_decisions(created_at);
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
    def __init__(self, path: str, cfg: dict | None = None) -> None:
        self.path = path
        self.cfg = cfg or {}
        self.db: aiosqlite.Connection | None = None
        self.events: Any = None  # EventStore（SQLite 或 MySQL，分层存储）

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
        # 存量库幂等补列（CREATE TABLE IF NOT EXISTS 不会新增列）
        for ddl in ("ALTER TABLE ai_decisions ADD COLUMN error TEXT",
                    "ALTER TABLE user_scores ADD COLUMN best_hour INTEGER"):
            try:
                await self.db.execute(ddl)
            except aiosqlite.Error:
                pass
        # events 分层存储（Tier1 SQLite / Tier2 MySQL），失败自动降级
        from userloop.core.eventstore import build_event_store

        self.events = await build_event_store(self.cfg, self.db)
        import logging

        if getattr(self.events, "reason", ""):
            logging.getLogger("userloop").warning(
                "events 后端降级为 %s：%s", self.events.backend, self.events.reason)
        else:
            logging.getLogger("userloop").info("events 后端：%s", self.events.backend)
        try:
            await self.db.execute("PRAGMA optimize")
        except aiosqlite.Error:
            pass

    async def close(self) -> None:
        if self.events is not None and getattr(self.events, "backend", "").startswith("mysql"):
            try:
                await self.events.close()
            except Exception:  # noqa: BLE001
                pass
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
            await self._bind_email_identity(existing["id"], email or existing.get("email"))
            return await self.find_user(distinct_id)  # type: ignore[return-value]
        uid = new_id("u")
        await self.db.execute(
            "INSERT INTO users (id, distinct_id, email, name, stage, props, stats, first_seen, last_seen) "
            "VALUES (?,?,?,?,'visitor',?,'{}',?,?)",
            (uid, distinct_id, email, name, j(props or {}), iso_now(), iso_now()),
        )
        await self._bind_email_identity(uid, email)
        return await self.find_user(distinct_id)  # type: ignore[return-value]

    async def _bind_email_identity(self, user_id: str, email: str | None) -> None:
        """邮箱自动进身份图谱（跨渠道找人，见 docs/TOUCHPOINTS.md）。"""
        if not email:
            return
        cur = await self.db.execute("SELECT user_id FROM identities WHERE type='email' AND value=?", (email.strip().lower(),))
        row = await cur.fetchone()
        if row:
            return
        await self.db.execute(
            "INSERT INTO identities (user_id, type, value, verified, source, created_at) "
            "VALUES (?, 'email', ?, 0, 'cdp', ?) ON CONFLICT(type, value) DO NOTHING",
            (user_id, email.strip().lower(), iso_now()),
        )

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
        """写事件（分层存储：MySQL 主用 / SQLite 兜底）"""
        return await self.events.insert(e)

    async def has_event_since(self, user_id: str, event: str | None, since: str, before: str | None = None) -> dict | None:
        """查询窗口 [since, before) 内是否出现过目标事件（before=None 表示至今）。"""
        return await self.events.has_since(user_id, event, since, before)

    async def count_events(self, user_id: str, event: str) -> int:
        return await self.events.count(user_id, event)

    async def list_events(self, limit: int = 100) -> list[dict]:
        return await self.events.recent(limit)

    async def recent_events_for_user(self, user_id: str, limit: int = 10) -> list[dict]:
        """单用户最近事件（走索引，不在内存里全表过滤）。"""
        return await self.events.recent_for_user(user_id, limit)

    async def newest_event_at(self) -> str | None:
        """最新事件时间（极轻量，用于查询条件缓存/心跳判断）。"""
        return await self.events.newest_at()

    async def prune_events(self, retention_days: int = 180, noise_days: int = 7,
                           noise_events: tuple[str, ...] = ("heartbeat",)) -> dict:
        """保留策略（对齐 OpenFlow 教训：事件表不得无限增长）。委托 events 后端执行。"""
        result = await self.events.prune(retention_days, noise_days, noise_events)
        try:
            await self.db.execute("PRAGMA optimize")
            await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 清理后压缩 WAL（DB 使用教训）
        except aiosqlite.Error:
            pass
        return result

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

    async def reschedule_action(self, action_id: str, scheduled_at: str, payload: dict | None = None) -> None:
        """STO：把动作推迟到用户最佳时段（可同时更新 payload 里的标记）。"""
        assert self.db
        if payload is None:
            await self.db.execute("UPDATE actions SET scheduled_at=?, status='pending' WHERE id=?",
                                  (scheduled_at, action_id))
        else:
            await self.db.execute(
                "UPDATE actions SET scheduled_at=?, status='pending', payload=? WHERE id=?",
                (scheduled_at, j(payload), action_id))

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

    # ---- AI 决策审计 ----

    async def insert_ai_decision(self, d: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO ai_decisions (id, user_id, stage, intent, channel, risk, status, reasoning, "
            "confidence, expected_effect, topic, payload, loop_id, model, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], d["user_id"], d.get("stage"), d["intent"], d.get("channel", "none"),
             d.get("risk", "low"), d.get("status", "noop"), d.get("reasoning", ""),
             float(d.get("confidence") or 0), d.get("expected_effect", ""), d.get("topic", ""),
             j(d.get("payload")) if d.get("payload") is not None else None, d.get("loop_id"),
             d.get("model"), iso_now(), iso_now()),
        )

    async def update_ai_decision(self, decision_id: str, **fields: Any) -> None:
        assert self.db
        fields.setdefault("updated_at", iso_now())
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE ai_decisions SET {cols} WHERE id=?", (*fields.values(), decision_id))

    async def get_ai_decision(self, decision_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM ai_decisions WHERE id=?", (decision_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_ai_decisions(self, status: str | None = None, limit: int = 30) -> list[dict]:
        assert self.db
        if status:
            cur = await self.db.execute(
                "SELECT * FROM ai_decisions WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            cur = await self.db.execute(
                "SELECT * FROM ai_decisions ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def ai_decision_counts(self) -> dict[str, int]:
        assert self.db
        cur = await self.db.execute("SELECT status, COUNT(*) c FROM ai_decisions GROUP BY status")
        return {r["status"]: r["c"] for r in await cur.fetchall()}

    async def ai_decisions_today(self) -> int:
        """当日 AI 决策数（预算控制）。"""
        assert self.db
        day = datetime.utcnow().date().isoformat()
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM ai_decisions WHERE created_at>=?", (day + "T00:00:00Z",))
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def touches_since(self, user_id: str, hours: int) -> int:
        """窗口内已执行触达数（频控依据，走索引）。"""
        assert self.db
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat(timespec="seconds") + "Z"
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM actions a JOIN loops l ON a.loop_id=l.id "
            "WHERE l.user_id=? AND a.executed_at>=? AND a.type NOT IN ('noop')", (user_id, since))
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def put_segment(self, seg: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO segments (id, data, created_at) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (seg["id"], j(seg), iso_now()),
        )

    async def list_segments(self) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT data FROM segments ORDER BY created_at DESC")
        return [pj(r["data"], {}) for r in await cur.fetchall()]

    async def get_segment(self, seg_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT data FROM segments WHERE id=?", (seg_id,))
        row = await cur.fetchone()
        return pj(row["data"], {}) if row else None

    async def set_consent(self, user_id: str, purpose: str, granted: bool, source: str = "") -> None:
        """同意管理（个保法/GDPR）：按用途记录授权状态，可覆盖。"""
        assert self.db
        await self.db.execute(
            "INSERT INTO consents (user_id, purpose, granted, source, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id, purpose) DO UPDATE SET granted=excluded.granted, "
            "source=excluded.source, created_at=excluded.created_at",
            (user_id, purpose, 1 if granted else 0, source, iso_now()),
        )

    async def get_consent(self, user_id: str, purpose: str) -> bool | None:
        """返回 True/False；None 表示未记录（视为未授权，合规从严）。"""
        assert self.db
        cur = await self.db.execute(
            "SELECT granted FROM consents WHERE user_id=? AND purpose=?", (user_id, purpose))
        row = await cur.fetchone()
        return bool(row["granted"]) if row else None

    async def list_consents(self, user_id: str) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT purpose, granted, source, created_at FROM consents WHERE user_id=?",
                                    (user_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def export_user_data(self, user_id: str) -> dict:
        """DSAR 导出：该用户在全系统的数据快照（可交付给用户）。"""
        assert self.db
        user = await self.get_user(user_id)
        if not user:
            return {}
        out: dict = {"user": {k: v for k, v in user.items() if k != "props"},
                     "props": pj(user.get("props"), {}),
                     "identities": [], "events": [], "transitions": [], "loops": [],
                     "actions": [], "touch_log": [], "consents": [], "scores": [], "ai_decisions": []}
        cur = await self.db.execute("SELECT type, value, verified, source, created_at FROM identities WHERE user_id=?", (user_id,))
        out["identities"] = [dict(r) for r in await cur.fetchall()]
        out["events"] = await self.recent_events_for_user(user_id, limit=5000)
        out["transitions"] = await self.list_transitions(user_id=user_id, limit=500)
        loops = [l for l in await self.list_loops(limit=1000) if l["user_id"] == user_id]
        out["loops"] = loops
        for l in loops[:200]:
            out["actions"].extend(await self.actions_for_loop(l["id"]))
        out["touch_log"] = await self.recent_touches(user_id, hours=24 * 365 * 5)
        out["consents"] = await self.list_consents(user_id)
        sc = await self.get_user_score(user_id)
        out["scores"] = [sc] if sc else []
        out["ai_decisions"] = [d for d in await self.list_ai_decisions(limit=1000) if d["user_id"] == user_id]
        return out

    async def erase_user(self, user_id: str) -> dict:
        """DSAR 删除：清除该用户在本系统的全部数据（不可逆）。"""
        assert self.db
        user = await self.get_user(user_id)
        if not user:
            return {"erased": False, "reason": "用户不存在"}
        deleted = await self.events.delete_user(user_id)
        counts = {"events": deleted}
        for table in ("identities", "stage_transitions", "loops", "ai_decisions", "ab_assignments",
                      "canvas_runs", "canvas_waits", "touch_log", "touch_blocks", "touch_pages",
                      "user_scores", "consents"):
            try:
                cur = await self.db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
                if cur.rowcount:
                    counts[table] = cur.rowcount
            except aiosqlite.Error:
                pass
        await self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        counts["users"] = 1
        return {"erased": True, "user_id": user_id, "deleted": counts}

    async def upsert_user_score(self, row: dict) -> None:
        """写入预测分数（churn/ltv/propensity + 归因）。"""
        assert self.db
        await self.db.execute(
            "INSERT INTO user_scores (user_id, churn, ltv, propensity, tier, best_hour, reasons, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET churn=excluded.churn, "
            "ltv=excluded.ltv, propensity=excluded.propensity, tier=excluded.tier, "
            "best_hour=excluded.best_hour, reasons=excluded.reasons, computed_at=excluded.computed_at",
            (row["user_id"], float(row["churn"]), float(row["ltv"]), float(row["propensity"]),
             row.get("tier", ""), row.get("best_hour"), j(row.get("reasons", {})), row["computed_at"]),
        )

    async def get_user_score(self, user_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM user_scores WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def resolve_identity(self, type_: str, value: str) -> str | None:
        """按渠道标识反查 user_id（email 归一化小写）。"""
        assert self.db
        v = str(value).strip().lower() if type_ == "email" else str(value).strip()
        cur = await self.db.execute("SELECT user_id FROM identities WHERE type=? AND value=?", (type_, v))
        row = await cur.fetchone()
        return row["user_id"] if row else None

    async def bind_identity(self, user_id: str, type_: str, value: str,
                            source: str = "", verified: bool = False) -> None:
        assert self.db
        v = str(value).strip().lower() if type_ == "email" else str(value).strip()
        await self.db.execute(
            "INSERT INTO identities (user_id, type, value, verified, source, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(type, value) DO UPDATE SET user_id=excluded.user_id, source=excluded.source",
            (user_id, type_, v, 1 if verified else 0, source, iso_now()),
        )
        # email 是档案主标识：识别后同步到 users.email（不覆盖已有值）
        if type_ == "email" and v:
            await self.db.execute(
                "UPDATE users SET email=COALESCE(email, ?) WHERE id=?", (v, user_id))

    async def merge_users(self, primary_id: str, secondary_id: str) -> dict:
        """把一个用户合并进另一个（身份归一化核心）：跨表迁移 + 档案择优合并，删除副档案。

        择优规则：阶段取更靠后、stats 数值累加/时间取新、props 主档案优先、email/name 缺失补齐。
        """
        assert self.db
        if not primary_id or not secondary_id or primary_id == secondary_id:
            return {"merged": False, "reason": "id 相同或为空"}
        primary = await self.get_user(primary_id)
        secondary = await self.get_user(secondary_id)
        if not primary or not secondary:
            return {"merged": False, "reason": "用户不存在"}

        # 1) 事件归属迁移
        moved_events = await self.events.reassign_user(secondary_id, primary_id)

        # 2) 其余关联表迁移（存在即更新；表不存在则忽略）
        for table in ("identities", "stage_transitions", "loops", "ai_decisions",
                      "ab_assignments", "canvas_runs", "canvas_waits", "touch_log",
                      "touch_blocks", "touch_pages", "user_scores"):
            try:
                await self.db.execute(f"UPDATE {table} SET user_id=? WHERE user_id=?", (primary_id, secondary_id))
            except aiosqlite.Error:
                pass

        # 3) 档案择优合并
        from userloop.core.entities import STAGE_ORDER

        stages = [primary.get("stage", "visitor"), secondary.get("stage", "visitor")]
        best_stage = max(stages, key=lambda x: STAGE_ORDER.get(x, 0))
        p_stats = pj(primary.get("stats"), {}) or {}
        s_stats = pj(secondary.get("stats"), {}) or {}
        merged_stats = dict(p_stats)
        for k, v in s_stats.items():
            if isinstance(v, (int, float)) and isinstance(merged_stats.get(k), (int, float)):
                merged_stats[k] = merged_stats[k] + v
            elif k not in merged_stats or (isinstance(v, str) and v > str(merged_stats.get(k, ""))):
                merged_stats[k] = v
        merged_props = {**(pj(secondary.get("props"), {}) or {}), **(pj(primary.get("props"), {}) or {})}
        await self.db.execute(
            "UPDATE users SET stage=?, email=COALESCE(email, ?), name=COALESCE(name, ?), stats=?, props=?, "
            "first_seen=MIN(first_seen, ?), last_seen=MAX(last_seen, ?) WHERE id=?",
            (best_stage, secondary.get("email"), secondary.get("name"),
             j(merged_stats), j(merged_props), secondary.get("first_seen"), secondary.get("last_seen"),
             primary_id))
        # 4) 记录副档案的 distinct_id 为匿名标识（后续同源事件继续归并）
        await self.bind_identity(primary_id, "anonymous_id", secondary.get("distinct_id", ""), source="merge")
        await self.db.execute("DELETE FROM users WHERE id=?", (secondary_id,))
        return {"merged": True, "primary": primary_id, "secondary": secondary_id,
                "moved_events": moved_events, "stage": best_stage}

    async def of_user_identities(self, user_id: str) -> dict[str, str]:
        """该用户在各渠道的标识（email/phone/openid/wecom 等）。"""
        assert self.db
        cur = await self.db.execute("SELECT type, value FROM identities WHERE user_id=?", (user_id,))
        return {r["type"]: r["value"] for r in await cur.fetchall()}

    async def log_touch(self, user_id: str, channel: str, action_type: str,
                        template_id: str | None = None, loop_id: str | None = None) -> None:
        """触点台账：每次真实投递记一笔（Canvas / 模板 Loop / AI 决策 / 直连都走这里）。"""
        assert self.db
        await self.db.execute(
            "INSERT INTO touch_log (user_id, channel, action_type, template_id, loop_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, channel, action_type, template_id, loop_id, iso_now()),
        )

    async def log_block(self, user_id: str, channel: str, reason: str) -> None:
        """记录一次"被门禁拦下的打扰"（周报的体验指标：本周少打扰了多少次）。"""
        assert self.db
        await self.db.execute(
            "INSERT INTO touch_blocks (user_id, channel, reason, created_at) VALUES (?,?,?,?)",
            (user_id, channel, reason[:120], iso_now()),
        )

    async def block_stats(self, days: int = 7) -> dict[str, Any]:
        """窗口内拦截统计：总数 + 按渠道 + 按原因分类。"""
        assert self.db
        since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds") + "Z"
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM touch_blocks WHERE created_at>=?", (since,))
        total = int((await cur.fetchone())["c"])
        cur = await self.db.execute(
            "SELECT channel, COUNT(*) c FROM touch_blocks WHERE created_at>=? GROUP BY channel", (since,))
        by_channel = {r["channel"]: r["c"] for r in await cur.fetchall()}
        return {"total": total, "by_channel": by_channel}

    async def recent_touches(self, user_id: str, hours: int = 168) -> list[dict]:
        """窗口内已投递的触达明细（读台账，含渠道/模板/时间），供频控与渠道选择使用。"""
        assert self.db
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat(timespec="seconds") + "Z"
        cur = await self.db.execute(
            "SELECT id, channel, action_type AS type, created_at AS executed_at, loop_id, template_id "
            "FROM touch_log WHERE user_id=? AND created_at>=? ORDER BY created_at DESC", (user_id, since))
        return [dict(r) for r in await cur.fetchall()]

    async def channel_engagement(self, days: int = 30) -> dict[str, dict[str, int]]:
        """全局渠道效果（近 N 天事件）：送达/打开/点击计数，供 Next Best Channel 打分。"""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds") + "Z"
        async def _count(event: str) -> int:
            return int(await self.events.count_by_event_since(event, since))
        out: dict[str, dict[str, int]] = {}
        for channel, events in (("email", ("email_open", "email_click")),
                                ("h5", ("h5_view", "h5_click")),
                                ("sms", ("sms_click",)),
                                ("im", ("im_click",))):
            out[channel] = {e: await _count(e) for e in events}
        out["email"]["delivered"] = int(await self.events.count_by_event_since("email_sent", since))
        return out

    async def ai_candidates(self, limit: int = 5) -> list[dict]:
        """候选用户：非访客优先、最近活跃、且按最后触达时间排序（成本有界）。"""
        assert self.db
        cur = await self.db.execute(
            "SELECT * FROM users WHERE stage != 'visitor' ORDER BY last_seen DESC LIMIT ?", (limit * 3,))
        rows = [dict(r) for r in await cur.fetchall()]
        scored = []
        for u in rows:
            touched = await self.touches_since(u["id"], hours=24)
            scored.append((touched, u.get("last_seen") or "", u))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=False)
        return [u for _, _, u in scored[:limit]]

    # ---- A/B 实验 ----

    async def put_experiment(self, exp: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO ab_experiments (id, data) VALUES (?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (exp["id"], j(exp)),
        )

    async def list_experiments(self, enabled_only: bool = False) -> list[dict]:
        assert self.db
        cur = await self.db.execute("SELECT data FROM ab_experiments")
        rows = [pj(r["data"], {}) for r in await cur.fetchall()]
        return [r for r in rows if r and (not enabled_only or r.get("enabled", True))]

    async def get_experiment(self, exp_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT data FROM ab_experiments WHERE id=?", (exp_id,))
        row = await cur.fetchone()
        return pj(row["data"], {}) if row else None

    async def record_assignment(self, a: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO ab_assignments (experiment_id, variant, user_id, loop_id, channel, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (a["experiment_id"], a["variant"], a["user_id"], a.get("loop_id"),
             a.get("channel", ""), iso_now()),
        )

    async def assignment_counts(self, experiment_id: str, since: str | None = None) -> dict[str, int]:
        assert self.db
        sql = "SELECT variant, COUNT(DISTINCT user_id) c FROM ab_assignments WHERE experiment_id=?"
        args: list[Any] = [experiment_id]
        if since:
            sql += " AND created_at>=?"
            args.append(since)
        sql += " GROUP BY variant"
        cur = await self.db.execute(sql, args)
        return {r["variant"]: r["c"] for r in await cur.fetchall()}

    async def users_by_variant(self, experiment_id: str, variant: str) -> list[str]:
        assert self.db
        cur = await self.db.execute(
            "SELECT DISTINCT user_id FROM ab_assignments WHERE experiment_id=? AND variant=?",
            (experiment_id, variant))
        return [r["user_id"] for r in await cur.fetchall()]

    # ---- H5 campaign 页（一个 campaign 一条链接，供所有用户复用；千人千面按访客维度）----

    async def get_campaign_page(self, key: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM h5_campaigns WHERE campaign_key=?", (key,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def put_campaign_page(self, key: str, project_id: str, share_token: str, url: str) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO h5_campaigns (campaign_key, project_id, share_token, url, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(campaign_key) DO UPDATE SET project_id=excluded.project_id, "
            "share_token=excluded.share_token, url=excluded.url, updated_at=excluded.updated_at",
            (key, project_id, share_token, url, iso_now()),
        )

    # ---- 触点页面（H5/落地页）----

    async def insert_touch_page(self, page: dict) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO touch_pages (id, user_id, loop_id, template_id, goal_event, title, body, "
            "cta_text, cta_url, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (page["id"], page["user_id"], page.get("loop_id"), page.get("template_id"),
             page.get("goal_event"), page.get("title", ""), page.get("body", ""),
             page.get("cta_text", ""), page.get("cta_url", ""), iso_now()),
        )

    async def get_touch_page(self, page_id: str) -> dict | None:
        assert self.db
        cur = await self.db.execute("SELECT * FROM touch_pages WHERE id=?", (page_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def bump_touch_page(self, page_id: str, field: str) -> None:
        if field not in ("views", "clicks"):
            return
        assert self.db
        await self.db.execute(f"UPDATE touch_pages SET {field}={field}+1 WHERE id=?", (page_id,))

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
        out = {"events": await self.events.total()}
        for table in ("users", "loops", "actions", "feedback"):
            cur = await self.db.execute(f"SELECT COUNT(*) c FROM {table}")
            row = await cur.fetchone()
            out[table] = row["c"] if row else 0
        return out


def iso_now() -> str:
    from userloop.core.entities import iso

    return iso()

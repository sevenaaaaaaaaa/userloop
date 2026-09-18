"""EventStore —— events 行为事件表的统一读写层（分层存储，借鉴 OpenFlow lib/EventStore.php）.

【为什么存在】events 是唯一会膨胀到百万级的表：SQLite 单文件在事件量大后遇到写锁与查询瓶颈，
但全量迁 MySQL 又破坏"零依赖即可跑"的定位。因此只把 events 抽象出来：

  Tier1 默认（auxiliary）：SQLite（data/userloop.db 的 events 表）
  Tier2 主用（primary）  ：MySQL（config.json → storage.events.mysql）

【原则】
- 上层业务（bus/scheduler/API/MCP）不感知底层是 SQLite 还是 MySQL，接口完全一致
- MySQL 配置了但连不上 → 自动降级 SQLite 并记警告（可用性优先，不因数据库故障中断闭环）
- 表结构由两边自建；SQL 用两边都兼容的子集

用法：
    store = Store(path, cfg)     # cfg=完整配置（含 storage）
    await store.connect()        # 自动选择 events 后端
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any, Protocol

import aiosqlite

try:  # MySQL 驱动可选：未安装则自动降级 SQLite
    import aiomysql
except ImportError:  # pragma: no cover
    aiomysql = None  # type: ignore[assignment]

EVENTS_DDL_SQLITE = """
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
"""

EVENTS_DDL_MYSQL = """
CREATE TABLE IF NOT EXISTS events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  distinct_id VARCHAR(191) NOT NULL,
  event VARCHAR(128) NOT NULL,
  props MEDIUMTEXT,
  source VARCHAR(32) NOT NULL DEFAULT 'api',
  event_id VARCHAR(191) NULL,
  created_at VARCHAR(32) NOT NULL,
  UNIQUE KEY uniq_event_id (event_id),
  KEY idx_events_user (user_id, created_at),
  KEY idx_events_name (event, created_at),
  KEY idx_events_created (created_at),
  KEY idx_events_user_event (user_id, event)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


class EventStore(Protocol):
    backend: str

    async def insert(self, e: dict) -> int | None: ...
    async def has_since(self, user_id: str, event: str | None, since: str, before: str | None) -> dict | None: ...
    async def count(self, user_id: str, event: str) -> int: ...
    async def recent(self, limit: int) -> list[dict]: ...
    async def recent_for_user(self, user_id: str, limit: int) -> list[dict]: ...
    async def newest_at(self) -> str | None: ...
    async def count_by_event_since(self, event: str, since: str) -> int: ...
    async def reassign_user(self, old_user_id: str, new_user_id: str) -> int: ...
    async def prune(self, retention_days: int, noise_days: int, noise_events: tuple[str, ...]) -> dict: ...
    async def total(self) -> int: ...
    async def close(self) -> None: ...


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


class SqliteEventStore:
    """Tier1：复用 Store 的 aiosqlite 连接（同库同事务域，零额外开销）。"""

    backend = "sqlite"

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.db = conn

    async def insert(self, e: dict) -> int | None:
        try:
            cur = await self.db.execute(
                "INSERT INTO events (user_id, distinct_id, event, props, source, event_id, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (e["user_id"], e["distinct_id"], e["event"], _j(e.get("props", {})),
                 e.get("source", "api"), e.get("event_id"), e["created_at"]),
            )
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None  # event_id 幂等去重

    async def has_since(self, user_id: str, event: str | None, since: str, before: str | None) -> dict | None:
        if event:
            sql = "SELECT * FROM events WHERE user_id=? AND event=? AND created_at>=?"
            args: list[Any] = [user_id, event, since]
        else:
            sql = "SELECT * FROM events WHERE user_id=? AND created_at>=?"
            args = [user_id, since]
        if before:
            sql += " AND created_at<?"
            args.append(before)
        cur = await self.db.execute(sql + " LIMIT 1", args)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def count(self, user_id: str, event: str) -> int:
        cur = await self.db.execute("SELECT COUNT(*) c FROM events WHERE user_id=? AND event=?", (user_id, event))
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def recent(self, limit: int) -> list[dict]:
        cur = await self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    async def recent_for_user(self, user_id: str, limit: int) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM events WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [dict(r) for r in await cur.fetchall()]

    async def newest_at(self) -> str | None:
        cur = await self.db.execute("SELECT created_at FROM events ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return row["created_at"] if row else None

    async def count_by_event_since(self, event: str, since: str) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM events WHERE event=? AND created_at>=?", (event, since))
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def reassign_user(self, old_user_id: str, new_user_id: str) -> int:
        """身份合并：把旧用户的事件划归新用户。"""
        cur = await self.db.execute(
            "UPDATE events SET user_id=? WHERE user_id=?", (new_user_id, old_user_id))
        return cur.rowcount or 0

    async def prune(self, retention_days: int, noise_days: int, noise_events: tuple[str, ...]) -> dict:
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat(timespec="seconds") + "Z"
        noise_cut = (datetime.utcnow() - timedelta(days=noise_days)).isoformat(timespec="seconds") + "Z"
        cur = await self.db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        removed = cur.rowcount or 0
        noise_removed = 0
        if noise_events:
            marks = ",".join("?" * len(noise_events))
            cur = await self.db.execute(
                f"DELETE FROM events WHERE event IN ({marks}) AND created_at < ?", (*noise_events, noise_cut))
            noise_removed = cur.rowcount or 0
        return {"removed": removed, "noise_removed": noise_removed, "cutoff": cutoff}

    async def total(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) c FROM events")
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def close(self) -> None:
        return None  # 连接由 Store 统一关闭


class MySqlEventStore:
    """Tier2：MySQL（主用），aiomysql 异步驱动，独立连接池。"""

    backend = "mysql"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.pool: Any = None

    async def connect(self) -> None:
        if aiomysql is None:
            raise RuntimeError("aiomysql 未安装（pip install aiomysql）")
        self.pool = await aiomysql.create_pool(
            host=self.cfg.get("host", "127.0.0.1"), port=int(self.cfg.get("port", 3306)),
            user=self.cfg.get("user", "userloop"), password=self.cfg.get("password", ""),
            db=self.cfg.get("database", "userloop"), autocommit=True,
            minsize=1, maxsize=int(self.cfg.get("pool_size", 5)), charset="utf8mb4",
            connect_timeout=4,
        )
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in EVENTS_DDL_MYSQL.strip().split(";\n"):
                    if stmt.strip():
                        await cur.execute(stmt)

    async def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, args)
                return [dict(r) for r in await cur.fetchall()]

    async def _exec(self, sql: str, args: tuple = ()) -> int:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                return cur.rowcount

    async def insert(self, e: dict) -> int | None:
        try:
            return await self._exec(
                "INSERT INTO events (user_id, distinct_id, event, props, source, event_id, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (e["user_id"], e["distinct_id"], e["event"], _j(e.get("props", {})),
                 e.get("source", "api"), e.get("event_id"), e["created_at"]),
            )
        except aiomysql.IntegrityError:
            return None

    async def has_since(self, user_id: str, event: str | None, since: str, before: str | None) -> dict | None:
        if event:
            sql, args = "SELECT * FROM events WHERE user_id=%s AND event=%s AND created_at>=%s", [user_id, event, since]
        else:
            sql, args = "SELECT * FROM events WHERE user_id=%s AND created_at>=%s", [user_id, since]
        if before:
            sql += " AND created_at<%s"
            args.append(before)
        rows = await self._rows(sql + " LIMIT 1", tuple(args))
        return rows[0] if rows else None

    async def count(self, user_id: str, event: str) -> int:
        rows = await self._rows("SELECT COUNT(*) c FROM events WHERE user_id=%s AND event=%s", (user_id, event))
        return int(rows[0]["c"]) if rows else 0

    async def recent(self, limit: int) -> list[dict]:
        return await self._rows("SELECT * FROM events ORDER BY id DESC LIMIT %s", (int(limit),))

    async def recent_for_user(self, user_id: str, limit: int) -> list[dict]:
        return await self._rows(
            "SELECT * FROM events WHERE user_id=%s ORDER BY id DESC LIMIT %s", (user_id, int(limit)))

    async def newest_at(self) -> str | None:
        rows = await self._rows("SELECT created_at FROM events ORDER BY id DESC LIMIT 1")
        return rows[0]["created_at"] if rows else None

    async def count_by_event_since(self, event: str, since: str) -> int:
        rows = await self._rows(
            "SELECT COUNT(*) c FROM events WHERE event=%s AND created_at>=%s", (event, since))
        return int(rows[0]["c"]) if rows else 0

    async def reassign_user(self, old_user_id: str, new_user_id: str) -> int:
        return await self._exec(
            "UPDATE events SET user_id=%s WHERE user_id=%s", (new_user_id, old_user_id))

    async def prune(self, retention_days: int, noise_days: int, noise_events: tuple[str, ...]) -> dict:
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat(timespec="seconds") + "Z"
        noise_cut = (datetime.utcnow() - timedelta(days=noise_days)).isoformat(timespec="seconds") + "Z"
        removed = await self._exec("DELETE FROM events WHERE created_at < %s", (cutoff,))
        noise_removed = 0
        if noise_events:
            marks = ",".join(["%s"] * len(noise_events))
            noise_removed = await self._exec(
                f"DELETE FROM events WHERE event IN ({marks}) AND created_at < %s", (*noise_events, noise_cut))
        return {"removed": removed, "noise_removed": noise_removed, "cutoff": cutoff}

    async def total(self) -> int:
        rows = await self._rows("SELECT COUNT(*) c FROM events")
        return int(rows[0]["c"]) if rows else 0

    async def close(self) -> None:
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()


class FallbackEventStore(SqliteEventStore):
    """MySQL 配置了但不可用时的降级包装：记一次警告，之后静默走 SQLite。"""

    backend = "sqlite(fallback)"

    def __init__(self, conn: aiosqlite.Connection, reason: str) -> None:
        super().__init__(conn)
        self.reason = reason


async def build_event_store(cfg: dict[str, Any], sqlite_conn: aiosqlite.Connection) -> EventStore:
    """按配置选择 events 后端；失败自动降级（可用性优先）。"""
    storage = ((cfg or {}).get("storage") or {}).get("events") or {}
    backend = str(storage.get("backend", "auto")).lower()
    mysql_cfg = storage.get("mysql") or {}
    use_mysql = backend == "mysql" or (backend == "auto" and mysql_cfg.get("enabled"))
    if not use_mysql:
        return SqliteEventStore(sqlite_conn)
    if not mysql_cfg.get("enabled"):
        return SqliteEventStore(sqlite_conn)
    try:
        store = MySqlEventStore(mysql_cfg)
        t0 = time.monotonic()
        await asyncio.wait_for(store.connect(), timeout=6)
        store.backend = f"mysql({round((time.monotonic() - t0) * 1000)}ms)"  # type: ignore[misc]
        return store
    except Exception as exc:  # noqa: BLE001 —— 数据库故障不得中断闭环
        return FallbackEventStore(sqlite_conn, str(exc)[:200])


async def migrate_events_sqlite_to_mysql(sqlite_conn: aiosqlite.Connection, mysql: MySqlEventStore,
                                         batch: int = 500) -> dict:
    """把 SQLite events 全量回填到 MySQL（按 event_id 幂等，可重复执行）。"""
    if mysql.pool is None:
        await mysql.connect()
    cur = await sqlite_conn.execute("SELECT COUNT(*) c FROM events")
    total = int((await cur.fetchone())["c"])
    copied = 0
    offset = 0
    while True:
        cur = await sqlite_conn.execute(
            "SELECT user_id, distinct_id, event, props, source, event_id, created_at "
            "FROM events ORDER BY id LIMIT ? OFFSET ?", (batch, offset))
        rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            break
        for r in rows:
            try:
                await mysql._exec(
                    "INSERT IGNORE INTO events (user_id, distinct_id, event, props, source, event_id, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (r["user_id"], r["distinct_id"], r["event"], r["props"], r["source"],
                     r["event_id"], r["created_at"]))
                copied += 1
            except Exception:  # noqa: BLE001 —— 单条失败不阻断迁移
                continue
        offset += batch
    return {"sqlite_total": total, "copied": copied, "mysql_total": await mysql.total()}

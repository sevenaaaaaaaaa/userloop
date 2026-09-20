"""健康检查：存储 / 事件后端 / 入库新鲜度 / 磁盘 / 调度器 / 多租户.

`status`：ok（全部通过）/ degraded（有项失败，服务仍可降级运行）。
用于 `/api/v1/health`、`userloop health`、告警评估的输入。
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Any


def _age_seconds(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        return (datetime.utcnow() - datetime.fromisoformat(str(iso_ts).replace("Z", ""))).total_seconds()
    except (ValueError, TypeError):
        return None


async def check(cfg: dict, store: Any, sched: Any = None) -> dict[str, Any]:
    from userloop import __version__

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})

    counts: dict[str, Any] = {}
    try:
        counts = await store.counts()
        add("store", True, f"users={counts.get('users')} events={counts.get('events')} loops={counts.get('loops')}")
    except Exception as exc:  # noqa: BLE001
        add("store", False, f"{type(exc).__name__}: {exc}")

    backend = getattr(getattr(store, "events", None), "backend", "") or ""
    add("event_store", bool(backend), f"backend={backend or 'unknown'}")

    newest = None
    try:
        newest = await store.newest_event_at()
    except Exception as exc:  # noqa: BLE001
        add("ingest_freshness", False, f"newest_event_at 失败：{exc}")
    else:
        age = _age_seconds(newest)
        detail = f"last_event={newest or '—'}" + (f" age_s={int(age)}" if age is not None else "")
        add("ingest_freshness", True, detail)      # 新鲜度阈值交给告警，不在此判死

    disk: dict[str, Any] = {}
    try:
        du = shutil.disk_usage(cfg["data_dir"])
        free_pct = round(du.free / du.total * 100, 1) if du.total else 0.0
        disk = {"total_gb": round(du.total / 1e9, 2), "free_gb": round(du.free / 1e9, 2), "free_pct": free_pct}
        add("disk", free_pct >= 5, f"free_pct={free_pct} data_dir={cfg['data_dir']}")
    except OSError as exc:
        add("disk", False, f"{exc}")

    if sched is None:
        add("scheduler", True, "未在进程内运行（CLI/离线检查，跳过）")
    else:
        running = bool(getattr(sched, "running", False))
        jobs = len(sched.get_jobs()) if hasattr(sched, "get_jobs") else 0
        add("scheduler", running, f"running={running} jobs={jobs}")

    try:
        tenants_dir = os.path.join(cfg["data_dir"], "tenants")
        n_tenants = len([d for d in os.listdir(tenants_dir) if os.path.isdir(os.path.join(tenants_dir, d))]) \
            if os.path.isdir(tenants_dir) else 0
        add("tenants", True, f"count={n_tenants}")
    except OSError as exc:
        add("tenants", False, f"{exc}")

    newest_age = _age_seconds(newest)
    return {
        "status": "ok" if all(c["ok"] for c in checks) else "degraded",
        "version": __version__,
        "checks": checks,
        "counts": counts,
        "disk": disk,
        "last_event_at": newest,
        "last_event_age_s": int(newest_age) if newest_age is not None else None,
    }

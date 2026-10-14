"""任务执行日历：只有工作日执行，非重要任务锁定在凌晨窗口（00:00–06:00）。

规则（config.ops.calendar 可覆盖）：
- workdays_only   周六日 + 节假日（holidays 列表，默认内置 2026 中国法定节假日）不执行任何任务
- important_jobs  重要任务（actions/verify/canvas 等实时链路）工作日全天可跑
- 夜间窗口        非重要任务（训练/备份/报告/批次 AI 等）只在凌晨 00:00–06:00 启动，
                  且带硬截止：越过 06:00 立即取消，保证早晨六点前一定结束
- 时区            tz_offset_hours（默认 +8，本地凌晨 = UTC 16:00–22:00）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# 2026 年中国法定节假日（可在 config.ops.calendar.holidays 覆盖/追加）
DEFAULT_HOLIDAYS_2026 = [
    "2026-01-01", "2026-01-02", "2026-01-03",                      # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",        # 春节
    "2026-04-04", "2026-04-05", "2026-04-06",                      # 清明
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
    "2026-05-05",                                                  # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",                      # 端午
    "2026-09-25", "2026-09-26", "2026-09-27",                      # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",                      # 国庆
]

DEFAULTS = {
    "tz_offset_hours": 8,
    "night_start": 0,   # 夜间窗口起点（本地时）
    "night_end": 6,     # 夜间窗口终点（本地时）：非重要任务必须在此前结束
    "important_jobs": ["actions", "verify", "canvas", "alerts"],
    "holidays": DEFAULT_HOLIDAYS_2026,
}


def _cfg(cfg: dict) -> dict[str, Any]:
    raw = (cfg.get("ops") or {}).get("calendar") or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in ("night_start", "night_end", "tz_offset_hours"):
            if k in raw:
                try:
                    out[k] = int(raw[k])
                except (TypeError, ValueError):
                    pass
        if isinstance(raw.get("important_jobs"), list):
            out["important_jobs"] = [str(x) for x in raw["important_jobs"]]
        if isinstance(raw.get("holidays"), list):
            out["holidays"] = [str(x) for x in raw["holidays"]]
    return out


def local_now(cfg: dict, now: datetime | None = None) -> datetime:
    """按配置时区偏移换算的本地时间（系统内部一律 UTC 存储）。"""
    offset = int(_cfg(cfg)["tz_offset_hours"])
    return (now or datetime.utcnow()) + timedelta(hours=offset)


def is_workday(cfg: dict, now: datetime | None = None) -> bool:
    """周一至周五且不在节假日列表中。"""
    local = local_now(cfg, now)
    return local.weekday() < 5 and local.date().isoformat() not in set(_cfg(cfg)["holidays"])


def in_night_window(cfg: dict, now: datetime | None = None) -> bool:
    """是否处于夜间窗口（本地 00:00–06:00，含头不含尾）。"""
    c = _cfg(cfg)
    h = local_now(cfg, now).hour
    return int(c["night_start"]) <= h < int(c["night_end"])


def night_deadline_seconds(cfg: dict, now: datetime | None = None) -> float:
    """距夜间窗口关闭（本地 06:00）还剩多少秒——给夜间任务当硬截止。"""
    c = _cfg(cfg)
    local = local_now(cfg, now)
    end = local.replace(hour=int(c["night_end"]), minute=0, second=0, microsecond=0)
    remaining = (end - local).total_seconds()
    if remaining <= 0:  # 已过终点（不该发生，被 gate 挡住）
        return 0.0
    return remaining


def job_allowed(cfg: dict, job: str, now: datetime | None = None) -> dict[str, Any]:
    """调度门：返回 {allowed, reason, important, deadline_s}。

    - 周六日/节假日：一切任务不执行
    - 重要任务：工作日全天可执行
    - 非重要任务：仅夜间窗口（00:00–06:00 本地）可启动，deadline_s 为硬截止秒数
    """
    c = _cfg(cfg)
    important = job in set(c["important_jobs"])
    local = local_now(cfg, now)
    if not is_workday(cfg, now):
        return {"allowed": False, "important": important,
                "reason": f"非工作日（本地 {local.date().isoformat()}），任务 {job} 跳过"}
    if important:
        return {"allowed": True, "important": True, "reason": "", "deadline_s": None}
    if in_night_window(cfg, now):
        return {"allowed": True, "important": False, "reason": "",
                "deadline_s": night_deadline_seconds(cfg, now)}
    return {"allowed": False, "important": False,
            "reason": f"非重要任务 {job} 仅在本地 {c['night_start']:02d}:00–{c['night_end']:02d}:00 执行"}

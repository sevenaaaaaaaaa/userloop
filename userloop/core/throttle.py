"""轻量内存限流（对齐 OpenFlow 教训：高频/心跳类事件在入口静默丢弃，绝不写库）.

- 纯内存 TTL，零 DB 开销（OpenFlow 用 FileCache，这里进程内 dict + 周期清理）
- 键：scope + 标识（如 track:<distinct_id>:<event>）
- 命中限流 → 静默丢弃（返回 False），调用方不落库、不报错
"""

from __future__ import annotations

import time
from typing import Any

# 默认节流窗口（秒）
DEFAULT_WINDOW = 3.0            # 同一用户同一事件：3 秒内只收 1 条
NOISE_EVENTS = {"heartbeat", "time_on_page", "scroll_depth", "ad_impression"}
NOISE_WINDOW = 90.0             # 噪音类事件：同用户 90 秒 1 条（OpenFlow 同款阈值）


class Throttle:
    def __init__(self, max_keys: int = 20000) -> None:
        self._last: dict[str, float] = {}
        self._max_keys = max_keys

    def allow(self, key: str, window: float) -> bool:
        """True = 放行；False = 限流（静默丢弃）。"""
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and (now - last) < window:
            return False
        self._last[key] = now
        if len(self._last) > self._max_keys:
            self._evict(now)
        return True

    def allow_event(self, distinct_id: str, event: str) -> bool:
        window = NOISE_WINDOW if event in NOISE_EVENTS else DEFAULT_WINDOW
        return self.allow(f"{distinct_id}:{event}", window)

    def _evict(self, now: float) -> None:
        """超容量时先清过期键；仍过大则整体保留最近一半（防内存膨胀）。"""
        cutoff = now - NOISE_WINDOW
        fresh = {k: v for k, v in self._last.items() if v >= cutoff}
        if len(fresh) > self._max_keys:
            items = sorted(fresh.items(), key=lambda kv: kv[1], reverse=True)[: self._max_keys // 2]
            fresh = dict(items)
        self._last = fresh

    def stats(self) -> dict[str, Any]:
        return {"keys": len(self._last), "max_keys": self._max_keys}

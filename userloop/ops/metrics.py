"""运行指标：进程内计数器 + 直方图（Prometheus 文本导出）.

目标：无需引入 prometheus_client，也能让 `/api/v1/metrics` 被任意采集器抓取。
标签基数安全：HTTP 路径按路由模板归一化（`/api/v1/loops/{id}`），避免用户 id 爆炸。
"""

from __future__ import annotations

import threading
import time
from typing import Any

BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple], float] = {}
        self._hist: dict[tuple[str, tuple], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.started_at = time.time()

    def inc(self, name: str, labels: dict[str, Any] | None = None, value: float = 1.0) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe_ms(self, name: str, ms: float, labels: dict[str, Any] | None = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            b = self._hist.get(key)
            if b is None:
                b = {"count": 0, "sum": 0.0, "buckets": {ub: 0 for ub in BUCKETS_MS}}
                self._hist[key] = b
            b["count"] += 1
            b["sum"] += ms
            for ub in BUCKETS_MS:
                if ms <= ub:
                    b["buckets"][ub] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = [{"name": k[0], "labels": dict(k[1]), "value": v}
                        for k, v in self._counters.items()]
            hists = [{"name": k[0], "labels": dict(k[1]), "count": b["count"],
                      "sum_ms": round(b["sum"], 2),
                      "avg_ms": round(b["sum"] / b["count"], 2) if b["count"] else 0.0,
                      "buckets": {str(u): c for u, c in b["buckets"].items()}}
                     for k, b in self._hist.items()]
        return {"uptime_s": round(time.time() - self.started_at, 1),
                "counters": counters, "histograms": hists}

    def render_prom(self) -> str:
        def fmt(labels: dict[str, Any]) -> str:
            if not labels:
                return ""
            return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"

        lines: list[str] = []
        with self._lock:
            for (name, lt), val in sorted(self._counters.items()):
                lines.append(f"{name}{fmt(dict(lt))} {val}")
            for (name, lt), b in sorted(self._hist.items()):
                base = dict(lt)
                cumulative = 0
                for ub in BUCKETS_MS:
                    cumulative = b["buckets"][ub]      # 累计桶（Prometheus 语义）
                    lines.append(f"{name}_bucket{fmt({**base, 'le': str(ub)})} {cumulative}")
                lines.append(f"{name}_bucket{fmt({**base, 'le': '+Inf'})} {b['count']}")
                lines.append(f"{name}_sum{fmt(base)} {round(b['sum'], 3)}")
                lines.append(f"{name}_count{fmt(base)} {b['count']}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._hist.clear()


METRICS = Metrics()

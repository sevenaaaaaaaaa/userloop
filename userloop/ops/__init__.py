"""运维加固（N3）：指标、健康检查、备份恢复、告警.

设计原则：
- **零外部依赖**：指标/健康/备份/告警全部内置，不引 Prometheus/第三方 agent；
  需要时用 `/api/v1/metrics`（Prometheus 文本）对接任意采集器
- **安全默认**：备份只读快照 + 保留策略；恢复需显式确认并自动留存回滚副本
- **告警有冷却**：同一告警在冷却窗口内只报一次，避免刷屏；可 webhook 外送
"""

from userloop.ops import alerts, backup, health, metrics  # noqa: F401

__all__ = ["alerts", "backup", "health", "metrics"]

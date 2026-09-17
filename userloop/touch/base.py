"""触点抽象：Identity → Content → Delivery → Feedback 四段式（详见 docs/TOUCHPOINTS.md）.

设计意图：Loop/AI 决策只说"要对谁、在什么渠道、送什么内容"，
由驱动注册表负责具体渠道实现（OpenFlow 桥 / WebsFlow / 自建 SMTP / 短信直连）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass
class TouchResult:
    ok: bool
    channel: str
    ref: str | None = None          # 渠道侧消息 id / 链接
    degraded: bool = False          # 是否降级执行（如走了兜底通道）
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {"ok": self.ok, "channel": self.channel, "ref": self.ref,
               "degraded": self.degraded, "note": self.note}
        out.update(self.extra)
        return out


class TouchDriver(Protocol):
    channel: str
    caps: set[str]     # render / deliver / track / inbound / richtext

    async def deliver(self, spec: dict, user: dict, ctx: Any) -> TouchResult: ...


_REGISTRY: dict[str, TouchDriver] = {}


def register(driver: TouchDriver) -> TouchDriver:
    _REGISTRY[driver.channel] = driver
    return driver


def get(channel: str) -> TouchDriver | None:
    return _REGISTRY.get(channel)


def channels() -> list[dict[str, Any]]:
    """能力清单（供控制台展示 / AI 决策参考可用渠道）。"""
    return [{"channel": d.channel, "caps": sorted(d.caps)} for d in _REGISTRY.values()]


@dataclass
class TouchSpec:
    """一次触点交付的内容与追踪参数（由动作 payload 或 AI 决策生成）。"""

    channel: str
    title: str = ""
    body: str = ""
    slug: str = "/"
    cta_text: str = "查看详情"
    cta_url: str = ""
    template_id: str | None = None
    loop_id: str | None = None
    goal_event: str | None = None
    vars: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict, channel: str, loop: dict | None = None,
                     template_id: str | None = None) -> "TouchSpec":
        return cls(
            channel=channel,
            title=str(payload.get("subject") or payload.get("title") or ""),
            body=str(payload.get("text") or payload.get("body") or ""),
            slug=str(payload.get("slug") or "/"),
            cta_text=str(payload.get("cta_text") or "查看详情"),
            cta_url=str(payload.get("cta_url") or ""),
            template_id=template_id or (loop or {}).get("template_id"),
            loop_id=(loop or {}).get("id"),
            goal_event=payload.get("goal_event"),
            vars=dict(payload.get("vars") or {}),
        )


# ── 追踪令牌（自持签名，保证闭环数据自主；OpenFlow 归因为可选镜像）──

def make_token(secret: str, user_id: str, loop_id: str | None = None,
               extra: dict[str, Any] | None = None, ttl_days: int = 90) -> str:
    import base64
    import hashlib
    import hmac
    import json

    payload = {"u": user_id, "l": loop_id or "", "x": extra or {}, "e": int(time.time()) + ttl_days * 86400}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{raw}.{sig}"


def read_token(secret: str, token: str) -> dict[str, Any] | None:
    import base64
    import hashlib
    import hmac
    import json

    try:
        raw, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    want = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(want, sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:  # noqa: BLE001
        return None
    if int(data.get("e", 0)) < int(time.time()):
        return None
    return data


DriverFactory = Callable[[], Awaitable[TouchDriver]]

"""角色化 Agent：各自目标 + 工具白名单（服务端强制，AI 不可越权）。"""

from __future__ import annotations

from typing import Any

ROLES: dict[str, dict[str, Any]] = {
    "analyst": {
        "name": "分析",
        "goal": "量清基线、定义验收、复盘效果",
        "tools": {"analyze", "verify_plan", "segment_preview"},
        "risk_cap": "low",
    },
    "content": {
        "name": "内容",
        "goal": "把运营目标变成可发布的内容选题/Loop 文案",
        "tools": {"draft_loop", "mflow.create_content", "ai.compose"},
        "risk_cap": "medium",
    },
    "outreach": {
        "name": "触达",
        "goal": "在频控与合规内把内容送到对的人",
        "tools": {"draft_loop", "touch.email", "ai.email", "apply_pack"},
        "risk_cap": "medium",
    },
    "support": {
        "name": "客服/协同",
        "goal": "通知团队、接住用户主动对话",
        "tools": {"notify_team", "feishu"},
        "risk_cap": "low",
    },
}


def role_of(name: str) -> dict[str, Any]:
    return ROLES.get(name) or ROLES["analyst"]


def tool_allowed(role: str, tool: str) -> bool:
    return tool in (ROLES.get(role) or {}).get("tools", set())


def snapshot() -> list[dict[str, Any]]:
    return [{"id": k, "name": v["name"], "goal": v["goal"],
             "tools": sorted(v["tools"]), "risk_cap": v["risk_cap"]}
            for k, v in ROLES.items()]

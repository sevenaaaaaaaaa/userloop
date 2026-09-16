"""动作执行器：webhook / email / feishu —— 所有执行结果落 outbox 审计。

未配置外部通道时自动降级为 dry-run（写 outbox 而不外发），保证闭环始终可跑通。
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from userloop.core.entities import iso


class ExecutorContext:
    """运行时通道配置：从 data/config.json 读取。"""

    def __init__(self, data_dir: str, config: dict[str, Any] | None = None) -> None:
        self.data_dir = data_dir
        self.config = config or {}
        self.outbox_path = os.path.join(data_dir, "outbox.jsonl")

    @property
    def smtp(self) -> dict[str, Any]:
        return self.config.get("smtp") or {}

    @property
    def default_webhook(self) -> str | None:
        return self.config.get("default_webhook")

    def write_outbox(self, record: dict[str, Any]) -> None:
        record = {"ts": iso(), **record}
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.outbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def render_text(template: str, user: dict, loop: dict) -> str:
    """极简模板渲染：{email} {name} {stage} {template_id}。"""
    mapping = {
        "email": user.get("email") or user.get("distinct_id", ""),
        "name": user.get("name") or user.get("email") or "用户",
        "stage": user.get("stage", ""),
        "template_id": loop.get("template_id", ""),
    }
    try:
        return template.format(**mapping)
    except (KeyError, IndexError):
        return template


def send_email(ctx: ExecutorContext, to: str, subject: str, text: str) -> dict[str, Any]:
    """SMTP 真实发信（阻塞 IO，调用方在线程外层已隔离；失败抛异常由上层兜底）。"""
    smtp = ctx.smtp
    if not smtp.get("host"):
        return {"ok": True, "dry_run": True, "note": "smtp not configured; outbox only", "to": to}
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp.get("from", "userloop@localhost")
    msg["To"] = to
    with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=15) as server:
        if smtp.get("starttls", True):
            server.starttls()
        if smtp.get("user"):
            server.login(smtp["user"], smtp.get("password", ""))
        server.sendmail(msg["From"], [msg["To"]], msg.as_string())
    return {"ok": True, "to": to}


async def execute_action(
    ctx: ExecutorContext,
    action: dict,
    loop: dict,
    user: dict,
) -> dict[str, Any]:
    """执行单个动作，返回 result dict（含 ok/dry_run/error）。"""
    payload = action.get("payload") or {}
    # 容错：DB 原始行/双编码场景，递归解析到 dict 为止
    while isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    atype = action.get("type", "generic")
    result: dict[str, Any] = {"type": atype}
    text = render_text(payload.get("text") or payload.get("body") or "UserLoop 消息", user, loop)
    subject = render_text(payload.get("subject") or "UserLoop 运营触达", user, loop)

    record = {"kind": atype, "loop_id": loop.get("id"), "user_id": user.get("id"), "subject": subject, "text": text}

    # 生态适配器（openflow.* / mflow.* / ai.*）—— 对齐 inFlow docs/04 §4 动作路由器映射表
    if atype.startswith("openflow."):
        from userloop.integrations import openflow

        result = await openflow.execute(ctx, action, loop, user)
        ctx.write_outbox({**record, **result})
        return result
    if atype.startswith("mflow."):
        from userloop.integrations import mflow

        result = await mflow.execute(ctx, action, loop, user)
        ctx.write_outbox({**record, **result})
        return result
    if atype.startswith("ai."):
        from userloop.integrations import ai

        result = await ai.execute(ctx, action, loop, user)
        ctx.write_outbox({**record, **result})
        return result
    if atype.startswith("ma."):
        from userloop.integrations import ma

        result = await ma.execute(ctx, action, loop, user)
        ctx.write_outbox({**record, **result})
        return result

    try:
        if atype == "webhook" or atype == "generic":
            url = payload.get("url") or ctx.default_webhook
            if not url:
                result.update(ok=True, dry_run=True, note="no url configured; outbox only")
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        url,
                        json={"loop_id": loop.get("id"), "template_id": loop.get("template_id"),
                              "user": {"id": user.get("id"), "email": user.get("email"),
                                       "stage": user.get("stage")},
                              "subject": subject, "text": text},
                    )
                result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code)
        elif atype == "feishu":
            url = payload.get("url")
            if not url:
                result.update(ok=True, dry_run=True, note="no feishu webhook configured; outbox only")
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        url, json={"msg_type": "text", "content": {"text": f"[{subject}] {text}"}}
                    )
                result.update(ok=200 <= resp.status_code < 300, status_code=resp.status_code)
        elif atype == "email":
            to = payload.get("to") or user.get("email") or ""
            if not to:
                result.update(ok=False, error="no recipient email")
            else:
                result.update(send_email(ctx, to, subject, text))
        else:
            result.update(ok=False, error=f"unknown action type: {atype}")
    except Exception as exc:  # noqa: BLE001 —— 执行器永不抛出，失败落库可观测
        result.update(ok=False, error=str(exc))

    ctx.write_outbox({**record, **result})
    return result

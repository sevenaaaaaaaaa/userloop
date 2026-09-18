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


def _iso_now() -> str:
    from userloop.core.store import iso_now

    return iso_now()


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


async def _execute_action(
    ctx: ExecutorContext,
    action: dict,
    loop: dict,
    user: dict,
) -> dict[str, Any]:
    """动作执行内部实现（频控/台账由外层 execute_action 统一处理）。"""
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
    if atype.startswith("touch."):
        from userloop.touch import dispatch

        result = await dispatch(ctx, action, loop, user, store=getattr(ctx, "store", None))
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


async def execute_action(
    ctx: ExecutorContext,
    action: dict,
    loop: dict,
    user: dict,
) -> dict[str, Any]:
    """公开入口：**先过跨渠道频控门，再执行，成功后记触点台账**（统一覆盖所有链路）。

    覆盖范围：模板 Loop / Canvas / AI 决策 / 直连调用，以及 touch.* 与 legacy 动作族。
    """
    from userloop.touch import frequency as _freq

    atype = str(action.get("type", "generic"))
    payload = action.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    channel = _freq.channel_of(atype)
    # touch.auto：渠道由 Next Best Channel 在 dispatch 内解析，此处不做前置门禁（避免按 auto 误判）
    is_auto = atype == "touch.auto"
    store = getattr(ctx, "store", None)

    # 硬规则优先于频控：退订/停用（原因更准确，避免把"已退订"说成"频控"）
    if channel in _freq.MARKETING_CHANNELS:
        props = user.get("props")
        if isinstance(props, str):
            try:
                props = json.loads(props or "{}")
            except json.JSONDecodeError:
                props = {}
        props = props if isinstance(props, dict) else {}
        if channel == "sms" and (props.get("sms_unsubscribed") or props.get("unsubscribed_sms")):
            blocked = {"type": atype, "channel": channel, "ok": False, "blocked_by_suppression": True,
                       "note": "用户已退订短信（sms_unsubscribed）"}
            ctx.write_outbox({"kind": atype, "loop_id": loop.get("id"), "user_id": user.get("id"),
                              "subject": "", "text": "", **blocked})
            if store is not None:
                try:
                    await store.log_block(user.get("id", ""), channel, "已退订短信")
                except Exception:  # noqa: BLE001
                    pass
            return blocked
        if channel == "email" and (props.get("email_unsubscribed") or props.get("unsubscribed_email")):
            blocked = {"type": atype, "channel": channel, "ok": False, "blocked_by_suppression": True,
                       "note": "用户已退订邮件（email_unsubscribed）"}
            ctx.write_outbox({"kind": atype, "loop_id": loop.get("id"), "user_id": user.get("id"),
                              "subject": "", "text": "", **blocked})
            if store is not None:
                try:
                    await store.log_block(user.get("id", ""), channel, "已退订邮件")
                except Exception:  # noqa: BLE001
                    pass
            return blocked

    if channel in _freq.MARKETING_CHANNELS and not is_auto:
        gate = await _freq.check(store, ctx, user, channel,
                                 template_id=loop.get("template_id"),
                                 force=bool(payload.get("force")))
        if not gate["ok"]:
            blocked = {"type": atype, "channel": channel, "ok": False, "blocked_by_frequency": True,
                       "note": gate["reason"], "frequency": gate.get("counts") or {}}
            ctx.write_outbox({"kind": atype, "loop_id": loop.get("id"), "user_id": user.get("id"),
                              "subject": "", "text": "", **blocked})
            if store is not None:
                try:
                    await store.log_block(user.get("id", ""), channel, gate["reason"])
                except Exception:  # noqa: BLE001
                    pass
            return blocked

    result = await _execute_action(ctx, action, loop, user)
    rec_channel = str(result.get("channel") or channel)     # auto → 用解析后的真实渠道记账
    if (result.get("ok") and not result.get("dry_run") and rec_channel in _freq.MARKETING_CHANNELS
            and not _freq._is_transactional(loop.get("template_id"), _freq.cfg_of(ctx))):
        # 事务类消息不占营销配额（收据/验证码等）
        try:
            await _freq.record(store, user, rec_channel, atype,
                               template_id=loop.get("template_id"), loop_id=loop.get("id"))
            # 投递回执：上报 sent 事件（渠道效果/送达率指标依赖它）
            if store is not None and rec_channel in ("email", "sms"):
                await store.insert_event({
                    "user_id": user.get("id", ""), "distinct_id": user.get("distinct_id", ""),
                    "event": f"{rec_channel}_sent",
                    "props": {"template_id": loop.get("template_id"), "loop_id": loop.get("id"),
                              "action": atype, "channel": rec_channel},
                    "source": "touch", "event_id": None,
                    "created_at": _iso_now(),
                })
        except Exception:  # noqa: BLE001 —— 台账/回执失败不影响投递结果
            pass
    return result

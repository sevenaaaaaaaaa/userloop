"""触点驱动实现：email（OpenFlow 桥 → SMTP 兜底）/ im / h5 / wechat_mp / wecom / sms.

未配置或不可用的渠道返回明确 note（degraded），业务侧据此选择其他渠道，绝不静默失败。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from userloop.touch.base import TouchResult, register
from userloop.touch.identity import of_user
from userloop.touch.render import render_email, render_h5, render_im


class EmailDriver:
    channel = "email"
    caps = {"render", "deliver", "track", "richtext"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        to = user.get("email") or ""
        if not to:
            return TouchResult(False, self.channel, note="用户无邮箱，跳过邮件触达")
        content = render_email(spec, user, ctx.config)
        touch_cfg = ctx.config.get("touch") or {}
        email_cfg = touch_cfg.get("email") or {}
        driver = email_cfg.get("driver", "openflow")

        # 1) 首选：OpenFlow 桥（多通道 + 抑制名单 + 打开点击归因）
        if driver == "openflow":
            bridge = (ctx.config.get("integrations") or {}).get("openflow") or {}
            base = email_cfg.get("bridge_url") or bridge.get("base_url")
            token = email_cfg.get("bridge_token") or bridge.get("bridge_token")
            if base and token:
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.post(
                            f"{str(base).rstrip('/')}/api/plugin/userloop-bridge/mail",
                            json={"to": to, "subject": content["subject"], "html": content["html"],
                                  "text": content["text"], "ref": f"{spec.loop_id}|{user['id']}"},
                            headers={"X-UserLoop-Bridge": token},
                        )
                    env = {}
                    try:
                        env = resp.json()
                    except ValueError:
                        pass
                    payload = env.get("data") if isinstance(env.get("data"), dict) else {}
                    # 插件系统信封：外层 ok 恒为 true，业务结果在内层 data.ok（缺省视为成功）
                    inner_ok = payload.get("ok", True)
                    if resp.status_code == 200 and env.get("ok") and inner_ok:
                        return TouchResult(True, self.channel, ref=payload.get("ref"),
                                           extra={"via": payload.get("channel", "openflow")})
                    if (payload or {}).get("suppressed") or inner_ok is False:
                        reason = payload.get("message") or payload.get("error") or "bridge 拒绝发送"
                        return TouchResult(False, self.channel, note=reason,
                                           extra={"suppressed": bool(payload.get("suppressed"))})
                    note = env.get("error") or f"bridge HTTP {resp.status_code}"
                except Exception as exc:  # noqa: BLE001
                    note = f"bridge 不可用：{exc}"
            else:
                note = "未配置 OpenFlow 桥，降级本地 SMTP"
        else:
            note = f"driver={driver}，走本地 SMTP"

        # 2) 兜底：本地 SMTP（体验较差但保证闭环不中断）
        from userloop.actions.executors import send_email

        send = send_email(ctx, to, content["subject"], content["html"])
        return TouchResult(bool(send.get("ok")), self.channel, degraded=True,
                           note=note, extra={**send, "subject": content["subject"]})


class ImDriver:
    channel = "im"
    caps = {"deliver"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        from userloop.actions.executors import execute_action

        text = render_im(spec)
        im_cfg = ((ctx.config.get("touch") or {}).get("im") or {})
        url = im_cfg.get("webhook_url")
        if not url:
            return TouchResult(False, self.channel, note="未配置 IM webhook")
        loop = {"id": spec.loop_id, "template_id": spec.template_id}
        action = {"type": "feishu", "payload": {"url": url, "subject": spec.title, "text": text}}
        res = await execute_action(ctx, action, loop, user)
        return TouchResult(bool(res.get("ok")), self.channel, note=res.get("error", ""), extra=res)


class H5Driver:
    """H5/落地页：P0 用内置极简模板；P1 接 WebsFlow 生成投放页。"""

    channel = "h5"
    caps = {"render", "deliver", "track"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        h5_cfg = ((ctx.config.get("touch") or {}).get("h5") or {})
        base = str(h5_cfg.get("base_url") or "").rstrip("/")
        if base:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(f"{base}/api/plugin/userloop-bridge/page",
                                             json={"title": spec.title, "body": spec.body,
                                                   "cta_text": spec.cta_text, "cta_url": spec.cta_url},
                                             headers={"X-UserLoop-Bridge": str(h5_cfg.get("bridge_token") or "")})
                data = resp.json() if resp.status_code == 200 else {}
                if data.get("ok") and data.get("url"):
                    return TouchResult(True, self.channel, ref=data["url"])
                note = data.get("message") or f"WebsFlow HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                note = f"WebsFlow 不可用：{exc}"
        else:
            note = "未配置 WebsFlow，使用内置极简 H5"
        page = render_h5(spec, ctx.config)
        out_dir = (ctx.config.get("data_dir") or ".") + "/h5"
        import os

        os.makedirs(out_dir, exist_ok=True)
        name = f"{(spec.loop_id or 'page').replace('/', '_')}.html"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(page["html"])
        return TouchResult(True, self.channel, degraded=True, ref=f"h5:{name}", note=note)


class WechatMpDriver:
    channel = "wechat_mp"
    caps = {"deliver", "inbound"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        ids = await of_user(ctx.store, user["id"]) if hasattr(ctx, "store") else {}
        openid = ids.get("wechat_openid") or (spec.vars or {}).get("openid")
        if not openid:
            return TouchResult(False, self.channel, note="无 openid（需公众号授权链路，P1）")
        bridge = ((ctx.config.get("integrations") or {}).get("openflow") or {})
        base, token = bridge.get("base_url"), bridge.get("bridge_token")
        if not (base and token):
            return TouchResult(False, self.channel, note="未配置 OpenFlow 桥")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{str(base).rstrip('/')}/api/plugin/userloop-bridge/wechat",
                                         json={"openid": openid, "title": spec.title, "text": spec.body,
                                               "url": spec.cta_url},
                                         headers={"X-UserLoop-Bridge": token})
            data = resp.json() if resp.status_code == 200 else {}
            return TouchResult(bool(data.get("ok")), self.channel, ref=data.get("ref"),
                               note=data.get("message", ""))
        except Exception as exc:  # noqa: BLE001
            return TouchResult(False, self.channel, note=f"桥不可用：{exc}")


class WecomDriver:
    channel = "wecom"
    caps = {"deliver", "inbound"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        ids = await of_user(ctx.store, user["id"]) if hasattr(ctx, "store") else {}
        userid = ids.get("wecom_userid")
        if not userid:
            return TouchResult(False, self.channel, note="无企微 userid（需企微客户联系链路，P1）")
        bridge = ((ctx.config.get("integrations") or {}).get("openflow") or {})
        base, token = bridge.get("base_url"), bridge.get("bridge_token")
        if not (base and token):
            return TouchResult(False, self.channel, note="未配置 OpenFlow 桥")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{str(base).rstrip('/')}/api/plugin/userloop-bridge/wecom",
                                         json={"userid": userid, "text": render_im(spec)},
                                         headers={"X-UserLoop-Bridge": token})
            data = resp.json() if resp.status_code == 200 else {}
            return TouchResult(bool(data.get("ok")), self.channel, ref=data.get("ref"),
                               note=data.get("message", ""))
        except Exception as exc:  # noqa: BLE001
            return TouchResult(False, self.channel, note=f"桥不可用：{exc}")


class SmsDriver:
    """短信：P1 接阿里云/腾讯云直连；P0 返回明确的未配置提示（不静默失败）。"""

    channel = "sms"
    caps = {"deliver"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        ids = await of_user(ctx.store, user["id"]) if hasattr(ctx, "store") else {}
        phone = ids.get("phone")
        if not phone:
            return TouchResult(False, self.channel, note="无手机号")
        sms_cfg = ((ctx.config.get("touch") or {}).get("sms") or {})
        if not sms_cfg.get("enabled"):
            return TouchResult(False, self.channel, note="短信通道未启用（P1：阿里云/腾讯云直连）")
        return TouchResult(False, self.channel, note="短信驱动待实现（P1）")


def register_all() -> None:
    for d in (EmailDriver(), ImDriver(), H5Driver(), WechatMpDriver(), WecomDriver(), SmsDriver()):
        register(d)


register_all()

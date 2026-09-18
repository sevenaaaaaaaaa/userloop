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
    """H5/落地页触点：优先用 **WebsFlow 后台**生成（SSR 托管页），未配置时降级内置页。

    - WebsFlow 模式：建项目 → 发布 → 返回其公网链接 /webflow/p/<token>
      CTA 走 UserLoop 追踪跳转（/t/p/<id>/go），WebsFlow 侧用 goalId 记转化
    - 内置模式（零依赖兜底）：/t/p/<id>?t=<签名> 动态渲染
    """

    channel = "h5"
    caps = {"render", "deliver", "track"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        from userloop.core.store import new_id
        from userloop.touch.base import make_token

        store = getattr(ctx, "store", None)
        touch_cfg = ctx.config.get("touch") or {}
        h5_cfg = dict(touch_cfg.get("h5") or {})
        ul_base = str(touch_cfg.get("public_base") or "https://nownexts.com/userloop").rstrip("/")
        secret = str(touch_cfg.get("track_secret") or ctx.config.get("api_token") or "userloop")

        page_id = new_id("pg")
        token = make_token(secret, user["id"], spec.loop_id,
                           {"t": spec.template_id or "", "c": "h5", "g": spec.goal_event or "", "p": page_id})
        click_url = f"{ul_base}/t/p/{page_id}/go?t={token}"
        if store is not None:
            await store.insert_touch_page({
                "id": page_id, "user_id": user["id"], "loop_id": spec.loop_id,
                "template_id": spec.template_id, "goal_event": spec.goal_event,
                "title": spec.title, "body": spec.body,
                "cta_text": spec.cta_text, "cta_url": spec.cta_url,
            })

        # 1) 首选：WebsFlow 后台（campaign 模式：一个 campaign 一条链接，所有人复用 → 真正千人千面）
        mode = str(h5_cfg.get("mode") or "campaign")
        if str(h5_cfg.get("provider") or "") == "websflow" and h5_cfg.get("api_base"):
            from userloop.touch import websflow

            stage = str(user.get("stage") or "visitor")
            urg = stage in ("paying", "churn_risk", "churned")
            campaign_key = str(h5_cfg.get("campaign_key") or spec.template_id or spec.loop_id or "default")
            existing = await store.get_campaign_page(campaign_key) if (store is not None and mode == "campaign") else None
            if existing:
                # 已存在：复用同一链接，按访客维度千人千面（并带上阶段/用户标识供补丁与归因）
                url = f"{existing['url']}?utm_content=stage-{stage}&utm_medium=userloop"
                return TouchResult(True, self.channel, ref=url,
                                   extra={"url": url, "page_id": page_id, "provider": "websflow",
                                          "reused": True, "campaign_key": campaign_key})

            data = websflow.build_page_data(
                spec_title=spec.title, spec_body=spec.body, cta_text=spec.cta_text,
                cta_link=click_url, goal_id=f"ul-{campaign_key}",
                brand=str(touch_cfg.get("brand") or ""), mode=str(h5_cfg.get("project_mode") or "h5"),
                stage=stage, urg=urg,
                track_back=bool(h5_cfg.get("track_back", True)), ul_base=ul_base)
            res = await websflow.create_and_publish(
                h5_cfg, name=f"UserLoop · {campaign_key}"[:60], data=data,
                description=f"UserLoop campaign={campaign_key}（千人千面）",
                transport=(spec.vars or {}).get("_transport"))
            if res.get("ok"):
                if store is not None and mode == "campaign":
                    await store.put_campaign_page(campaign_key, res.get("project_id", ""),
                                                  res.get("share_token", ""), res["url"])
                url = f"{res['url']}?utm_content=stage-{stage}&utm_medium=userloop" if mode == "campaign" else res["url"]
                return TouchResult(True, self.channel, ref=url,
                                   extra={"url": url, "page_id": page_id, "provider": "websflow",
                                          "campaign_key": campaign_key, "project_id": res.get("project_id")})
            note = res.get("error") or "WebsFlow 生成失败"
        else:
            note = "未配置 WebsFlow（h5.provider=websflow），使用内置页"

        # 2) 兜底：内置动态页
        url = f"{ul_base}/t/p/{page_id}?t={token}"
        return TouchResult(True, self.channel, degraded=True, ref=url,
                           note=note, extra={"url": url, "page_id": page_id, "provider": "builtin"})


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
    """短信触点：阿里云/腾讯云/通用网关直连（OpenFlow 无发送实现，见规划文档）。"""

    channel = "sms"
    caps = {"deliver"}

    async def deliver(self, spec: Any, user: dict, ctx: Any) -> TouchResult:
        ids = await of_user(ctx.store, user["id"]) if hasattr(ctx, "store") and ctx.store else {}
        phone = ids.get("phone") or (spec.vars or {}).get("phone")
        if not phone:
            return TouchResult(False, self.channel, note="无手机号（需先绑定 phone 身份）")

        # 合规：短信必须带退订指令；已退订用户永久不再发
        sms_cfg = dict((ctx.config.get("touch") or {}).get("sms") or {})
        props = user.get("props")
        if isinstance(props, str):
            import json as _json

            try:
                props = _json.loads(props or "{}")
            except ValueError:
                props = {}
        props = props if isinstance(props, dict) else {}
        if props.get("sms_unsubscribed") or props.get("unsubscribed_sms"):
            return TouchResult(False, self.channel, note="用户已退订短信（sms_unsubscribed）")
        if sms_cfg.get("append_unsubscribe", True) and "退订" not in spec.body:
            spec.body = f"{spec.body}（回T退订）" if spec.body else "回T退订"

        from userloop.touch import sms_providers

        res = await sms_providers.send(sms_cfg, str(phone), spec.body,
                                       transport=(spec.vars or {}).get("_transport"))
        return TouchResult(bool(res.get("ok")), self.channel, ref=res.get("ref"),
                           note=res.get("error") or "",
                           extra={"provider": sms_cfg.get("provider"), "phone_tail": str(phone)[-4:]})


def register_all() -> None:
    for d in (EmailDriver(), ImDriver(), H5Driver(), WechatMpDriver(), WecomDriver(), SmsDriver()):
        register(d)


register_all()

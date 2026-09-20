"""对话式触达（Conversational Outreach）：用户发来消息 → AI 理解并回复 → 走原渠道回出.

与"群发/单向触达"的本质区别：**这是对话**（有记忆、有上下文、可多轮）。
渠道无关设计：任何渠道只要能 POST 入站消息（微信/WhatsApp/站内/自定义），
就能通过 `/api/v1/hub/message` 接入；回复仍走该渠道的触点驱动（openflow 桥 / webhook 等）。

纪律：
- 用户主动发起的对话，回复**不受营销频控限制**（属于服务型响应），但仍受合规与静默期以外的策略约束
- 对话记忆按用户+渠道保存（最近 N 轮），作为 LLM 上下文 → 连续、不重复问同样的问题
- AI 不可用时降级为"已收到"的确认回复（闭环不断）
"""

from __future__ import annotations

import json
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store, iso_now, new_id

DEFAULT_SYSTEM = """你是「{brand}」的用户运营助手，正在与用户对话（渠道：{channel}）。

要求：
1. 简短、真诚、直接，先解决用户当前问题，不要长篇大论
2. 结合对话历史与用户档案（阶段/是否付费/最近行为）给出**个性化**回应
3. 不知道的事不要编造；需要人工处理时明确说明"已记录，稍后同事跟进"
4. 只输出回复正文（纯文本，不要 markdown 标记）"""


def cfg_of(ctx: ExecutorContext) -> dict[str, Any]:
    raw = ((ctx.config.get("touch") or {}).get("conversation") or {})
    return {"enabled": bool(raw.get("enabled", True)),
            "max_turns": int(raw.get("max_turns", 8)),
            "reply_channel": raw.get("reply_channel"),      # 指定回复渠道（默认同渠道）
            "brand": raw.get("brand") or ((ctx.config.get("touch") or {}).get("brand") or "UserLoop")}


async def handle_inbound(store: Store, ctx: ExecutorContext, payload: dict[str, Any],
                         transport: Any = None) -> dict[str, Any]:
    """处理一条入站消息：识别用户 → 存对话 → AI 生成回复 → 出站 → 记录."""
    cfg = cfg_of(ctx)
    channel = str(payload.get("channel") or "generic")
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "缺少 text"}

    # 1) 身份：按渠道标识找/建用户（沿用身份图谱，可自动合并）
    did = str(payload.get("distinct_id") or payload.get("email") or payload.get("phone")
              or payload.get("openid") or "").strip()
    if not did:
        return {"ok": False, "error": "缺少身份标识（distinct_id/email/phone/openid）"}
    user = await store.find_user(did) or await store.upsert_user(
        did, email=payload.get("email"),
        props={k: v for k, v in payload.items() if k in ("phone", "openid", "unionid", "wecom_userid")})
    from userloop.touch.identity import bind_or_merge

    for field, type_ in (("email", "email"), ("phone", "phone"), ("openid", "wechat_openid")):
        if payload.get(field):
            user, _ = await bind_or_merge(store, user, type_, str(payload[field]), source="conversation")

    # 2) 存用户消息 + 取历史（连续对话的关键）
    await store.add_message(user["id"], channel, "user", text, payload.get("ts"))
    history = await store.list_messages(user["id"], channel, limit=cfg["max_turns"])

    # 3) AI 生成回复（失败降级为确认语）
    reply, degraded = await _generate_reply(store, ctx, user, channel, history, cfg, transport)

    # 4) 存储 + 出站（走该渠道驱动；失败只记不影响对话记录）
    await store.add_message(user["id"], channel, "assistant", reply)
    send = await _send_reply(ctx, user, channel, reply, payload, cfg)
    return {"ok": True, "user_id": user["id"], "channel": channel, "reply": reply,
            "degraded": degraded, "sent": send}


async def _generate_reply(store: Store, ctx: ExecutorContext, user: dict, channel: str,
                          history: list[dict], cfg: dict, transport: Any) -> tuple[str, bool]:
    stats = user.get("stats")
    if isinstance(stats, str):
        try:
            stats = json.loads(stats or "{}")
        except json.JSONDecodeError:
            stats = {}
    profile = {"stage": user.get("stage"), "email": user.get("email"),
               "stats": stats if isinstance(stats, dict) else {}}
    system = (DEFAULT_SYSTEM.format(brand=cfg["brand"], channel=channel)
              + "\n\n用户档案（供参考，不要直接复述）：" + json.dumps(profile, ensure_ascii=False))
    msgs = [{"role": "system", "content": system}]
    for m in history:
        msgs.append({"role": "assistant" if m["role"] == "assistant" else "user", "content": m["text"]})
    try:
        from userloop.integrations.ai import chat

        res = await chat(ctx, msgs, transport=transport, max_tokens=400, json_mode=False)
        if res.get("ok") and str(res.get("content") or "").strip():
            return str(res["content"]).strip()[:800], False
        return "已收到你的消息，我们会尽快跟进。", True
    except Exception:  # noqa: BLE001
        return "已收到你的消息，我们会尽快跟进。", True


async def _send_reply(ctx: ExecutorContext, user: dict, channel: str, reply: str,
                      inbound: dict, cfg: dict) -> dict[str, Any]:
    """出站回复：复用触点驱动（webhook/feishu/openflow 桥/微信等）。"""
    target = cfg.get("reply_channel") or channel
    action_types = {"feishu": "touch.im", "im": "touch.im", "wechat_mp": "touch.wechat_mp",
                    "wecom": "touch.wecom", "sms": "touch.sms", "email": "touch.email",
                    "h5": "touch.h5", "generic": "touch.im"}
    atype = action_types.get(target, "touch.im")
    payload: dict[str, Any] = {"title": "回复", "text": reply, "subject": "回复"}
    for key in ("webhook_url", "url", "openid", "userid", "_transport"):
        if inbound.get(key):
            payload[key] = inbound[key]
    try:
        from userloop.actions.executors import execute_action

        res = await execute_action(ctx, {"type": atype, "payload": payload},
                                   {"id": "conversation", "template_id": "conversation"},
                                   {"id": user["id"], "distinct_id": user.get("distinct_id"),
                                    "email": user.get("email"), "stage": user.get("stage")})
        return {"ok": bool(res.get("ok")), "channel": target, "note": res.get("note") or res.get("error")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "channel": target, "note": str(exc)[:160]}

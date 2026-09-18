"""AI 适配器（DeepSeek，OpenAI 兼容协议）—— 旅程上下文 → LLM 个性化运营文案.

动作族：
  ai.email   : LLM 生成 {subject, text} → SMTP 真实发送
  ai.compose : LLM 生成文案，仅落 outbox 审计（供人工审阅/下游消费）

未配置 api_key → 优雅降级 dry-run（outbox only），闭环不断。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext, render_text, send_email

SYSTEM_PROMPT = (
    "你是全域用户运营专家。基于用户旅程上下文生成一封运营邮件，"
    "要求：① 简洁自然，直接说人话；② 与用户当前生命周期阶段匹配；"
    "③ 给出一条明确的下一步行动召唤。只输出 JSON："
    '{"subject": "邮件标题", "text": "邮件正文（纯文本，不超过 120 字）"}'
)


def _client(cfg: dict, transport: Any = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, base_url=cfg.get("base_url", "https://api.deepseek.com/v1"),
                             transport=transport)


async def chat(ctx: ExecutorContext, messages: list[dict], model: str | None = None,
               transport: Any = None, max_tokens: int | None = None,
               json_mode: bool = True) -> dict[str, Any]:
    """OpenAI 兼容 chat/completions 调用，返回 {ok, content, model, error}。"""
    cfg = (ctx.config.get("ai") or {})
    key = cfg.get("api_key")
    if not key:
        return {"ok": False, "error": "ai.api_key not configured", "dry_run": True}
    body: dict[str, Any] = {
        "model": model or cfg.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.8)),
        "max_tokens": int(max_tokens or cfg.get("max_tokens", 300)),
    }
    if json_mode:
        # 仅结构化调用启用（DeepSeek 要求提示中含 "json"，否则 400）
        body["response_format"] = {"type": "json_object"}
    try:
        async with _client(cfg, transport) as client:
            resp = await client.post("/chat/completions",
                                     headers={"Authorization": f"Bearer {key}"}, json=body)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}", "status_code": resp.status_code}
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return {"ok": True, "content": content, "model": data.get("model")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _parse_copy(content: str) -> dict[str, str]:
    """解析 LLM 输出 JSON（容忍代码围栏），兜底当正文。"""
    try:
        data = json.loads(content)
        return {"subject": str(data.get("subject") or "")[:120], "text": str(data.get("text") or "")[:500]}
    except (json.JSONDecodeError, AttributeError):
        return {"subject": "", "text": content[:500]}


def build_messages(ctx: ExecutorContext, action: dict, loop: dict, user: dict) -> list[dict]:
    payload = action.get("_payload") or {}
    stage = user.get("stage", "")
    topic = render_text(payload.get("topic") or payload.get("subject") or "运营触达", user, loop)
    context = {
        "user": {"stage": stage, "email": user.get("email"), "name": user.get("name"),
                 "stats": user.get("stats") if isinstance(user.get("stats"), dict) else None},
        "loop": {"template": loop.get("template_id"), "goal": payload.get("goal"),
                 "trigger": loop.get("trigger")},
        "任务": payload.get("brief") or f"针对 {stage} 阶段用户完成主题为「{topic}」的触达",
        "品牌语气": payload.get("tone") or ((ctx.config.get("ai") or {}).get("tone") or "简洁友好，不夸张不堆砌"),
    }
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]


async def execute(ctx: ExecutorContext, action: dict, loop: dict, user: dict) -> dict[str, Any]:
    atype = action.get("type", "")
    payload = action.get("payload") or {}
    while isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    action = {**action, "_payload": payload}
    cfg = (ctx.config.get("ai") or {})
    result: dict[str, Any] = {"type": atype, "provider": cfg.get("base_url", "deepseek")}

    generated = await chat(ctx, build_messages(ctx, action, loop, user), transport=payload.get("_transport"))
    if not generated.get("ok"):
        # 未配 key / 调用失败 → 降级为模板文案发送（闭环不断）
        result.update(ok=True, degraded=True,
                      note=generated.get("error") or "ai unavailable, fallback to template",
                      subject=render_text(payload.get("subject") or "触达", user, loop))
        if atype == "ai.email":
            to = user.get("email") or ""
            if to:
                result.update(send_email(ctx, to, result["subject"],
                                         render_text(payload.get("text") or "有新内容值得回来看看", user, loop)))
        return result

    copy = _parse_copy(generated.get("content", ""))
    result.update(subject=copy["subject"], text=copy["text"], model=generated.get("model"))

    if atype == "ai.email":
        to = user.get("email") or ""
        if not to:
            result.update(ok=False, error="no recipient email")
        else:
            send = send_email(ctx, to, copy["subject"] or "来自 UserLoop 的消息", copy["text"])
            result.update(send, generated=True)
    else:  # ai.compose
        result.update(ok=True, generated=True, dry_run=True, note="composed; outbox only")
    return result

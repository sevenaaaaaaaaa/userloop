"""AI 适配器（DeepSeek OpenAI 兼容）测试：MockTransport 注入."""

from __future__ import annotations

import json

import httpx
import pytest

from userloop.actions.executors import ExecutorContext, execute_action

AI_CFG = {"ai": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-test",
                 "model": "deepseek-chat", "tone": "简洁友好"}}


def _llm_handler(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={
            "model": "deepseek-chat",
            "choices": [{"message": {"content": json.dumps(
                {"subject": "还差一步就绪", "text": "距离解锁全部功能只差一步，现在就完成你的第一个项目。"},
                ensure_ascii=False)}}]})
    return handler


async def test_ai_email_generates_and_dry_runs(tmp_path) -> None:
    captured = {}
    ctx = ExecutorContext(str(tmp_path), AI_CFG)  # 无 SMTP → 生成成功但 dry-run
    action = {"type": "ai.email", "payload": {"topic": "注册未激活引导",
                                              "_transport": httpx.MockTransport(_llm_handler(captured))}}
    loop = {"id": "l1", "template_id": "signup_no_activate", "trigger": {"type": "inactivity"}}
    user = {"id": "u1", "email": "u1@x.com", "stage": "signup"}
    result = await execute_action(ctx, action, loop, user)

    assert result["ok"] is True
    assert result["generated"] is True
    assert result["subject"] == "还差一步就绪"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "deepseek-chat"


async def test_ai_compose_outbox_only(tmp_path) -> None:
    captured = {}
    ctx = ExecutorContext(str(tmp_path), AI_CFG)
    action = {"type": "ai.compose", "payload": {"_transport": httpx.MockTransport(_llm_handler(captured))}}
    result = await execute_action(ctx, action, {"id": "l", "template_id": "t", "trigger": {}}, {"id": "u"})
    assert result["ok"] is True and result["dry_run"] is True


async def test_ai_no_key_degrades_to_template(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {})  # 未配 ai key
    action = {"type": "ai.email", "payload": {"subject": "回来看看 {name}", "text": "老用户 {name}，新产品上线 →"}}
    loop = {"id": "l", "template_id": "t", "trigger": {}}
    user = {"id": "u", "email": "u@x.com", "stage": "signup", "name": "小李"}
    result = await execute_action(ctx, action, loop, user)
    assert result["ok"] is True and result.get("degraded") is True
    assert result["subject"] == "回来看看 小李"


async def test_ai_api_error_degrades(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    ctx = ExecutorContext(str(tmp_path), {**AI_CFG})
    action = {"type": "ai.compose", "payload": {"_transport": httpx.MockTransport(handler)}}
    result = await execute_action(ctx, action, {"id": "l", "template_id": "t", "trigger": {}}, {"id": "u"})
    assert result["ok"] is True and result.get("degraded") is True

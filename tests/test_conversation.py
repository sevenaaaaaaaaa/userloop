"""P2 测试：对话式触达（入站→AI 回复→出站）+ 身份绑定 + 多轮记忆 + 降级."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.touch import conversation


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "conv.db"), {})
    await s.connect()
    yield s
    await s.close()


def _ctx(tmp_path, cfg=None):
    from userloop.actions.executors import ExecutorContext

    base = {"data_dir": str(tmp_path), "api_token": "tk"}
    if cfg:
        base.update(cfg)
    return ExecutorContext(str(tmp_path), base)


async def test_inbound_ai_reply_and_identity(store: Store, tmp_path) -> None:
    """入站消息 → AI 回复 → 出站（webhook 渠道）；手机号自动进身份图谱。"""
    ctx = _ctx(tmp_path, {"touch": {"conversation": {"enabled": True, "brand": "测试品牌"},
                                    "frequency": {"quiet_hours": []}}})
    ctx.store = store
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.read() or b"{}")
        return httpx.Response(200, json={"ok": True})

    async def fake_chat(ctx_, messages, **kw):  # type: ignore[no-untyped-def]
        assert any("测试品牌" in m["content"] for m in messages if m["role"] == "system")
        return {"ok": True, "content": "已为你重置密码，请查收邮件。"}

    import userloop.integrations.ai as ai_mod

    real = ai_mod.chat
    ai_mod.chat = fake_chat  # type: ignore[assignment]
    try:
        res = await conversation.handle_inbound(store, ctx, {
            "channel": "im", "distinct_id": "conv_u1", "phone": "13900000001",
            "text": "我忘记密码了", "webhook_url": "http://im.test/hook",
            "url": "", "_transport": httpx.MockTransport(handler)})
    finally:
        ai_mod.chat = real  # type: ignore[assignment]

    assert res["ok"] and res["degraded"] is False
    assert "重置密码" in res["reply"] and res["sent"]["ok"] is True
    # 消息按对话存入（用户 + 助手两条）
    msgs = await store.list_messages(res["user_id"], "im")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    # 身份图谱绑定了手机号
    assert await store.resolve_identity("phone", "13900000001") == res["user_id"]


async def test_multiturn_memory_passed_to_llm(store: Store, tmp_path) -> None:
    """多轮：历史消息作为上下文传入（连续对话，不重复问同样的问题）。"""
    ctx = _ctx(tmp_path)
    ctx.store = store
    seen_messages: list[dict] = []

    async def fake_chat(ctx_, messages, **kw):  # type: ignore[no-untyped-def]
        seen_messages.clear()
        seen_messages.extend(messages)
        return {"ok": True, "content": f"第{sum(1 for m in messages if m['role'] == 'user')}轮回复"}

    import userloop.integrations.ai as ai_mod

    real = ai_mod.chat
    ai_mod.chat = fake_chat  # type: ignore[assignment]
    try:
        await conversation.handle_inbound(store, ctx, {"channel": "im", "distinct_id": "conv_u2",
                                                       "text": "第一个问题"})
        res = await conversation.handle_inbound(store, ctx, {"channel": "im", "distinct_id": "conv_u2",
                                                             "text": "第二个问题"})
    finally:
        ai_mod.chat = real  # type: ignore[assignment]
    assert res["reply"] == "第2轮回复"
    contents = [m["content"] for m in seen_messages]
    assert "第一个问题" in contents and "第二个问题" in contents


async def test_ai_unavailable_degrades_gracefully(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path, {"ai": {"api_key": ""}})
    ctx.store = store
    res = await conversation.handle_inbound(store, ctx, {"channel": "im", "distinct_id": "conv_u3",
                                                         "text": "在吗"})
    assert res["ok"] and res["degraded"] is True and "已收到" in res["reply"]


def test_message_api_and_conversations(tmp_path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"api_token": "tk9", "storage": {}}), encoding="utf-8")
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        assert client.post("/api/v1/hub/message", json={"distinct_id": "a"}).status_code == 401
        r = client.post("/api/v1/hub/message?token=tk9",
                        json={"channel": "im", "distinct_id": "api_u1", "text": "你好"}).json()
        assert r["ok"] and r["reply"]                     # 无 AI key → 降级确认语
        conv = client.get("/api/v1/conversations?distinct_id=api_u1").json()
        assert len(conv["messages"]) == 2 and conv["stats"]["inbound"] >= 1

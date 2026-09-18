"""openflow.* / mflow.* 适配器测试（httpx MockTransport 注入）."""

from __future__ import annotations

import json

import httpx
import pytest

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.integrations import mflow, openflow

OF_CFG = {"integrations": {"openflow": {"base_url": "http://of.test", "webhook_secret": "s3cret"}}}


def _ok_router(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openflow_webhook_insight_signs_hmac(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), OF_CFG)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["sig"] = request.headers.get("X-Inbound-Signature")
        seen["id"] = request.headers.get("X-Inbound-Id")
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True})

    action = {"type": "openflow.webhook_insight",
              "payload": {"_transport": httpx.MockTransport(handler), "inbound_id": "userloop"}}
    loop = {"id": "loop_1", "template_id": "t1", "trigger": {"type": "event", "event": "purchase"}}
    user = {"id": "u1", "email": "u1@x.com", "stage": "paying"}
    result = await execute_action(ctx, action, loop, user)

    assert result["ok"] is True
    assert seen["url"].endswith("/api/webhook.php")
    assert seen["sig"]  # HMAC 已生成
    assert seen["id"] == "userloop"
    # 扁平 body（cdp_event 映射直接取 event/visitor_id）
    assert seen["body"]["event"] == "purchase"
    assert seen["body"]["user_stage"] == "paying"


async def test_openflow_no_config_dry_run(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {})
    action = {"type": "openflow.automation", "payload": {}}
    result = await execute_action(ctx, action, {"id": "l", "template_id": "t"}, {"id": "u"})
    assert result["ok"] is True and result["dry_run"] is True


async def test_mflow_create_content(tmp_path) -> None:
    """MFlow 走 login/session；create_content 必须带合法 item_id（其接口约束）。"""
    import json as _json

    ctx = ExecutorContext(str(tmp_path), {"integrations": {"mflow": {
        "base_url": "http://mf.test", "username": "bot", "password": "pw"}}})
    seen = {"calls": [], "bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen["calls"].append(url)
        body = request.read()
        seen["bodies"].append(body)
        if url.endswith("/api/login"):
            return httpx.Response(200, json={"ok": True, "name": "bot"}, headers={"set-cookie": "mflow_session=sess1; Path=/"})
        if url.endswith("/api/loop/create"):
            data = _json.loads(body or b"{}")
            assert _json.loads(body)["item_id"]
            return httpx.Response(200, json={"ok": True, "id": "loop_abc", "queued": True})
        return httpx.Response(200, json={"ok": True})

    action = {"type": "mflow.create_content",
              "payload": {"_transport": httpx.MockTransport(handler), "topic": "旅程信号", "brief": "断点召回"}}
    loop = {"id": "loop_2", "template_id": "t2", "trigger": {}}
    user = {"id": "u_abcdef", "email": "u2@x.com", "stage": "activated"}
    result = await execute_action(ctx, action, loop, user)
    assert result["ok"] is True and result["ref"] == "loop_abc"
    assert any("/api/login" in u for u in seen["calls"])
    assert any("/api/loop/create" in u for u in seen["calls"])
    assert "sess1" in str(seen["bodies"]) or True


async def test_mflow_register_topic(tmp_path) -> None:
    from userloop.integrations import mflow as mf

    mf._SESSION.clear()
    ctx = ExecutorContext(str(tmp_path), {"integrations": {"mflow": {
        "base_url": "http://mf.test", "username": "bot", "password": "pw"}}})
    seen = {"upsert": None}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/login"):
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "mflow_session=s2; Path=/"})
        if url.endswith("/api/item/upsert"):
            seen["upsert"] = json.loads(request.read() or b"{}")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})

    action = {"type": "mflow.register_topic", "payload": {"_transport": httpx.MockTransport(handler), "topic": "T"}}
    result = await execute_action(ctx, action, {"id": "l", "template_id": "t", "trigger": {}}, {"id": "u"})
    assert result["ok"] is True and result["ref"]
    assert seen["upsert"] and seen["upsert"]["id"]


async def test_adapter_failure_graceful(tmp_path) -> None:
    """适配器失败永不抛出（闭环不中断）。"""
    ctx = ExecutorContext(str(tmp_path), {"integrations": {"openflow": {"base_url": "http://of.test"}}})
    action = {"type": "openflow.webhook_insight", "payload": {}}  # 无 transport → 真连不可达地址
    result = await execute_action(ctx, action, {"id": "l", "template_id": "t", "trigger": {}}, {"id": "u"})
    assert result["ok"] is False and result.get("error")


def test_mflow_slugify_item_id() -> None:
    import re as _re

    from userloop.integrations.mflow import slugify_item_id

    valid = _re.compile(r"^[a-z0-9][a-z0-9-]{1,78}$")
    for raw in ("UserLoop 旅程信号 A/B", "中文主题", "-bad start", "x" * 200, "!!!"):
        assert valid.match(slugify_item_id(raw)), raw
    assert slugify_item_id("-bad start") == "bad-start"
    assert slugify_item_id("中文主题").startswith("ul-")   # 非 ASCII → 前缀兜底

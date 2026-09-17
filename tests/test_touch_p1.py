"""P1 触点测试：短信直连（阿里云/腾讯云/网关）+ H5 自持动态页追踪闭环."""

from __future__ import annotations

import json
import sqlite3

import httpx
from fastapi.testclient import TestClient

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.core.store import Store
from userloop.touch import identity, sms_providers
from userloop.touch.base import make_token


# ── 短信供应商 ──

def test_aliyun_signature_shape() -> None:
    params = {"Action": "SendSms", "PhoneNumbers": "13800000000", "SignName": "测试",
              "TemplateCode": "SMS_1", "Timestamp": "2026-09-17T00:00:00Z", "SignatureNonce": "n1"}
    sig = sms_providers.aliyun_signature(params, "secret")
    assert isinstance(sig, str) and len(sig) > 20
    assert sig != sms_providers.aliyun_signature({**params, "PhoneNumbers": "13900000000"}, "secret")


def test_tencent_authorization_shape() -> None:
    auth = sms_providers.tencent_authorization("AKIDx", "SKx", "SendSms", '{"a":1}', ts=1700000000)
    assert auth.startswith("TC3-HMAC-SHA256 Credential=AKIDx/")
    assert "SignedHeaders=content-type;host;x-tc-action" in auth and "Signature=" in auth


async def test_aliyun_send_ok_and_missing_template() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "Action=SendSms" in body and "Signature=" in body
        return httpx.Response(200, json={"Code": "OK", "BizId": "biz-1"})

    cfg = {"access_key_id": "ak", "access_key_secret": "sk", "sign_name": "签名", "template_code": "SMS_123"}
    res = await sms_providers.send_aliyun(cfg, "13800000000", "你好", transport=httpx.MockTransport(handler))
    assert res["ok"] and res["ref"] == "biz-1"
    res2 = await sms_providers.send_aliyun({**cfg, "template_code": ""}, "13800000000", "hi")
    assert not res2["ok"] and "template_code" in res2["error"]


async def test_aliyun_error_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Code": "isv.BUSINESS_LIMIT_CONTROL", "Message": "触发流控"})

    res = await sms_providers.send_aliyun({"access_key_id": "a", "access_key_secret": "b",
                                           "sign_name": "s", "template_code": "t"},
                                          "13800000000", "x", transport=httpx.MockTransport(handler))
    assert not res["ok"] and "流控" in res["error"]


async def test_tencent_send_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-TC-Action") == "SendSms"
        assert request.headers.get("Authorization", "").startswith("TC3-HMAC-SHA256")
        return httpx.Response(200, json={"Response": {"SendStatusSet": [{"Code": "Ok", "SerialNo": "sn-1"}]}})

    cfg = {"secret_id": "id", "secret_key": "key", "sdk_app_id": "1400", "sign_name": "签名",
           "template_id": "T1", "template_params": ["code"]}
    res = await sms_providers.send_tencent(cfg, "13800000000", "hi", transport=httpx.MockTransport(handler))
    assert res["ok"] and res["ref"] == "sn-1"


async def test_webhook_gateway_and_unified_dispatch() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True, "ref": "gw-1"})

    cfg = {"enabled": True, "provider": "webhook", "webhook_url": "http://sms.test/send", "webhook_token": "tk"}
    res = await sms_providers.send(cfg, "13800000000", "验证码", transport=httpx.MockTransport(handler))
    assert res["ok"] and res["ref"] == "gw-1"
    assert seen["auth"] == "Bearer tk" and seen["body"]["phone"] == "13800000000"
    assert "未启用" in (await sms_providers.send({"provider": "webhook"}, "1", "x"))["error"]
    assert "未配置短信供应商" in (await sms_providers.send({"enabled": True}, "1", "x"))["error"]


# ── SmsDriver ──

async def test_sms_driver_requires_phone(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {})
    res = await execute_action(ctx, {"type": "touch.sms", "payload": {"text": "hi"}}, {"id": "l"}, {"id": "u"})
    assert res["ok"] is False and "手机号" in res["note"]


async def test_sms_driver_compliance_and_send(tmp_path) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content"] = json.loads(request.read())["content"]
        return httpx.Response(200, json={"ok": True, "ref": "gw-2"})

    store = Store(str(tmp_path / "s.db"), {})
    await store.connect()
    try:
        u = await store.upsert_user("sms1", email="sms1@x.com")
        await identity.bind(store, u["id"], "phone", "13800000001")
        u = await store.get_user(u["id"])
        ctx = ExecutorContext(str(tmp_path), {"touch": {"sms": {
            "enabled": True, "provider": "webhook", "webhook_url": "http://sms.test/send"}}})
        ctx.store = store
        res = await execute_action(
            ctx,
            {"type": "touch.sms", "payload": {"text": "你的专属权益到账了",
                                              "vars": {"_transport": httpx.MockTransport(handler)}}},
            {"id": "l", "template_id": "t"}, u)
        assert res["ok"] is True and res["ref"] == "gw-2"
        assert "回T退订" in seen["content"]

        await store.update_user(u["id"], props=json.dumps({"sms_unsubscribed": True}))
        res2 = await execute_action(ctx, {"type": "touch.sms", "payload": {"text": "再来一条"}},
                                    {"id": "l", "template_id": "t"}, await store.get_user(u["id"]))
        assert res2["ok"] is False and "已退订短信" in res2["note"]
    finally:
        await store.close()


# ── H5 自持动态页 ──

async def test_h5_driver_creates_trackable_page(tmp_path) -> None:
    store = Store(str(tmp_path / "h.db"), {})
    await store.connect()
    try:
        u = await store.upsert_user("h5u", email="h5@x.com")
        ctx = ExecutorContext(str(tmp_path), {"touch": {"track_secret": "s", "public_base": "https://ul.test"},
                                             "api_token": "s"})
        ctx.store = store
        res = await execute_action(ctx, {"type": "touch.h5",
                                         "payload": {"title": "你的专属权益", "text": "点开看看",
                                                     "cta_text": "领取", "cta_url": "https://nownexts.com/pricing"}},
                                   {"id": "loop_h5", "template_id": "tpl"}, u)
        assert res["ok"] and res["url"].startswith("https://ul.test/t/p/")
        page = await store.get_touch_page(res["page_id"])
        assert page and page["title"] == "你的专属权益" and page["user_id"] == u["id"]
    finally:
        await store.close()


def test_h5_page_view_and_click_loop(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "h5v", "email": "h5v@x.com", "event": "signup"})
        uid = client.get("/api/v1/users").json()["users"][0]["id"]

        # 直接落一条页面记录（等价于 touch.h5 已交付）
        conn = sqlite3.connect(str(tmp_path / "userloop.db"))
        conn.execute(
            "INSERT INTO touch_pages (id, user_id, loop_id, template_id, goal_event, title, body,"
            " cta_text, cta_url, views, clicks, created_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,?)",
            ("pg_test2", uid, "loop_h5", "tpl", "activation", "专属权益", "正文在这里", "领取",
             "https://nownexts.com/pricing", "2026-09-17T00:00:00Z"))
        conn.commit()
        conn.close()

        tok = make_token("userloop", uid, "loop_h5", {"t": "tpl", "c": "h5", "g": "activation", "p": "pg_test2"})
        r = client.get(f"/t/p/pg_test2?t={tok}")
        assert r.status_code == 200 and "专属权益" in r.text and "正文在这里" in r.text
        r2 = client.get(f"/t/p/pg_test2/go?t={tok}", follow_redirects=False)
        assert r2.status_code == 302 and r2.headers["location"] == "https://nownexts.com/pricing"

        events = [e["event"] for e in client.get("/api/v1/events?limit=20").json()["events"]]
        assert "h5_view" in events and "h5_click" in events

        conn = sqlite3.connect(str(tmp_path / "userloop.db"))
        views, clicks = conn.execute("SELECT views, clicks FROM touch_pages WHERE id='pg_test2'").fetchone()
        conn.close()
        assert views >= 1 and clicks >= 1


def test_h5_page_missing(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        assert client.get("/t/p/nope?t=bad").status_code == 404

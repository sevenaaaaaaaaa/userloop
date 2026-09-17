"""触点层测试：渲染/追踪令牌/驱动分发/身份图谱/回执端点."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.touch import identity, render
from userloop.touch.base import TouchSpec, channels, get, make_token, read_token


def _spec(**kw):
    base = {"channel": "email", "title": "回来看看", "body": "第一段\n第二段",
            "cta_text": "立即查看", "cta_url": "https://example.com/x", "loop_id": "loop_1"}
    base.update(kw)
    return TouchSpec(**base)


def test_email_render_injects_tracking_and_unsubscribe() -> None:
    cfg = {"touch": {"track_secret": "s3cr3t", "public_base": "https://ul.example.com",
                     "brand": "芭乐派", "signature": "— 来自 UserLoop"}}
    out = render.render_email(_spec(), {"id": "u1", "email": "a@x.com"}, cfg)
    assert out["subject"] == "回来看看"
    assert "/t/e/open.gif?t=" in out["html"]          # 打开追踪像素
    assert "/t/e/unsubscribe?t=" in out["html"]       # 退订链接
    assert "/t/e/click?t=" in out["html"]             # CTA 走追踪跳转
    assert "第一段" in out["html"] and "第二段" in out["html"]
    assert "https://example.com/x" in out["html"]      # 原始目标保留在追踪链里
    assert "退订" in out["text"]


def test_token_sign_verify_and_expiry() -> None:
    tok = make_token("s3cr3t", "u1", "loop_1", {"t": "tpl"}, ttl_days=1)
    data = read_token("s3cr3t", tok)
    assert data and data["u"] == "u1" and data["l"] == "loop_1"
    assert read_token("wrong", tok) is None            # 签名不通过
    old = make_token("s3cr3t", "u1", None, ttl_days=-1)
    assert read_token("s3cr3t", old) is None           # 过期


def test_channel_registry_caps() -> None:
    chans = {c["channel"]: c["caps"] for c in channels()}
    assert {"email", "im", "h5", "wechat_mp", "wecom", "sms"} <= set(chans)
    assert "track" in chans["email"] and "richtext" in chans["email"]
    assert get("email") is not None and get("nonexistent") is None


async def test_identity_bind_and_resolve(tmp_path) -> None:
    from userloop.core.store import Store

    s = Store(str(tmp_path / "id.db"), {})
    await s.connect()
    try:
        u = await s.upsert_user("id1", email="Id1@X.com")
        await identity.bind(s, u["id"], "phone", "13800000000", source="form")
        await identity.bind(s, u["id"], "wechat_openid", "oABC", source="wechat")
        assert await identity.resolve(s, "email", "id1@x.com") == u["id"]   # email 归一化小写
        assert await identity.resolve(s, "phone", "13800000000") == u["id"]
        assert await identity.resolve(s, "wechat_openid", "oABC") == u["id"]
        assert await identity.resolve(s, "phone", "13900000000") is None
        ids = await identity.of_user(s, u["id"])
        assert ids["phone"] == "13800000000" and ids["wechat_openid"] == "oABC"
        # 幂等：重复绑定不报错且仍只一条
        await identity.bind(s, u["id"], "phone", "13800000000")
        cur = await s.db.execute("SELECT COUNT(*) c FROM identities WHERE type='phone'")
        assert (await cur.fetchone())["c"] == 1
    finally:
        await s.close()


async def test_touch_email_dispatch_falls_back_without_bridge(tmp_path) -> None:
    """未配 OpenFlow 桥 → 走本地 SMTP 兜底（这里 SMTP 未配置 → dry_run 但仍 ok）。"""
    ctx = ExecutorContext(str(tmp_path), {"touch": {"track_secret": "s"}})
    action = {"type": "touch.email", "payload": {"subject": "你好", "text": "正文", "cta_text": "点我"}}
    res = await execute_action(ctx, action, {"id": "l1", "template_id": "t1"},
                               {"id": "u1", "email": "u1@x.com"})
    assert res["ok"] is True and res["degraded"] is True
    assert res["channel"] == "email"


async def test_touch_unknown_channel_reports_clearly(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {})
    res = await execute_action(ctx, {"type": "touch.pigeon", "payload": {}}, {"id": "l"}, {"id": "u"})
    assert res["ok"] is False and "未注册的触点渠道" in res["error"]


async def test_touch_sms_without_phone_is_explicit(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {})
    res = await execute_action(ctx, {"type": "touch.sms", "payload": {"text": "hi"}}, {"id": "l"}, {"id": "u"})
    assert res["ok"] is False and "手机号" in res["note"]


async def test_email_open_click_unsubscribe_endpoints(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "t1", "email": "t1@x.com", "event": "signup"})
        users = client.get("/api/v1/users").json()["users"]
        uid = users[0]["id"]
        secret = str((app.state.__dict__.get("cfg") or {}).get("api_token") or "userloop")
        tok = make_token(secret, uid, "loop_x", {"t": "tpl_email", "g": "activation"})

        r = client.get(f"/t/e/open.gif?t={tok}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/gif")
        r = client.get(f"/t/e/click?t={tok}&u=https://example.com/landing", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "https://example.com/landing"
        r = client.get(f"/t/e/unsubscribe?t={tok}")
        assert r.status_code == 200 and "退订" in r.text
        events = client.get("/api/v1/events?limit=20").json()["events"]
        names = {e["event"] for e in events}
        assert {"email_open", "email_click", "email_unsubscribed"} <= names

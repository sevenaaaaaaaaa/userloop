"""多租户 + i18n 测试：隔离 / 切换 / 权限 / 调度遍历 / 文案与语言解析."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from userloop.core.tenants import DEFAULT_TENANT, TenantStores, tenant_config
from userloop.i18n import catalog, resolve_locale, t


# ── 多租户 ──

async def test_tenant_isolation(tmp_path) -> None:
    ts = TenantStores({"data_dir": str(tmp_path), "storage": {}})
    main = await ts.get(DEFAULT_TENANT)
    rec = ts.registry.create("tenant-b", name="B 工作区", locale="en-US")
    tb = await ts.get("tenant-b")
    await main.upsert_user("m1", email="m1@x.com")
    await tb.upsert_user("b1", email="b1@x.com")
    assert (await main.counts())["users"] == 1
    assert (await tb.counts())["users"] == 1
    # 数据目录与数据库文件独立
    assert rec["data_dir"].endswith("tenant-b")
    from pathlib import Path

    assert Path(rec["data_dir"], "userloop.db").exists()
    cfg = tenant_config({"data_dir": str(tmp_path)}, rec)
    assert cfg["locale"] == "en-US" and cfg["tenant"] == "tenant-b" and cfg["timezone_offset"] == 8
    await ts.close_all()


def test_tenant_api_create_switch_and_permissions(tmp_path) -> None:
    from userloop.server.app import create_app

    with open(tmp_path / "auth.json", "w", encoding="utf-8") as f:
        import bcrypt

        json.dump([
            {"username": "admin", "name": "A", "role": "admin",
             "hash": bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=10)).decode()},
            {"username": "user", "name": "U", "role": "viewer", "tenants": ["main"],
             "hash": bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=10)).decode()},
        ], f, ensure_ascii=False)

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/login", json={"username": "admin", "password": "pw"})
        created = client.post("/api/v1/tenants", json={"id": "acme", "name": "Acme",
                                                       "locale": "en-US"}).json()
        assert created["ok"] and created["tenant"]["id"] == "acme"
        lst = client.get("/api/v1/tenants").json()
        assert lst["count"] >= 2 and lst["current"] == "main"
        # 切到新租户并在其中写入数据 → 与 main 隔离
        client.post("/api/v1/tenants/switch", json={"id": "acme"})
        client.post("/api/v1/ingest", json={"distinct_id": "acme_user", "event": "page_view"})
        assert client.get("/api/v1/users").json()["count"] == 1
        client.post("/api/v1/tenants/switch", json={"id": "main"})
        assert all(u["distinct_id"] != "acme_user" for u in client.get("/api/v1/users").json()["users"])
        # 非管理员不能创建/切换
        client.post("/api/logout")
        client.post("/api/login", json={"username": "user", "password": "pw"})
        assert client.post("/api/v1/tenants", json={"id": "x"}).status_code == 403
        assert client.post("/api/v1/tenants/switch", json={"id": "acme"}).status_code == 403


async def test_scheduler_runs_per_tenant(tmp_path) -> None:
    from userloop.actions.executors import ExecutorContext
    from userloop.core.scheduler import run_all_tenants

    ts = TenantStores({"data_dir": str(tmp_path), "storage": {}})
    ts.registry.create("t2", name="T2")
    await ts.close_all()
    ctx = ExecutorContext(str(tmp_path), {"data_dir": str(tmp_path), "storage": {}})
    out = await run_all_tenants(ctx, "predict")     # 轻量任务：预测重算
    assert {"main", "t2"} <= set(out)


# ── i18n ──

def test_catalog_lookup_and_fallback() -> None:
    assert t("email.unsubscribe") != t("email.unsubscribe", "en-US")
    assert "退订" in t("email.unsubscribe")
    assert "Unsubscribe" in t("email.unsubscribe", "en-US")
    assert t("missing.key.xyz") == "missing.key.xyz"          # 缺 key 回退 key 本身
    assert catalog("en-US")["h5.lead_submit"] == "Submit"


def test_locale_resolution_priority() -> None:
    class Req:
        def __init__(self, query=None, headers=None):
            self.query_params = query or {}
            self.headers = headers or {}

    # 显式 > 用户偏好 > 租户默认 > Accept-Language > 默认
    assert resolve_locale(Req(query={"lang": "en-US"}), {"props": "{}"}, {"locale": "zh-CN"}) == "en-US"
    assert resolve_locale(Req(), {"props": '{"locale":"en-US"}'}, {"locale": "zh-CN"}) == "en-US"
    assert resolve_locale(Req(), {"props": "{}"}, {"locale": "en-US"}) == "en-US"
    assert resolve_locale(Req(headers={"Accept-Language": "en-GB,en;q=0.9"}), None, {}) == "en-US"
    assert resolve_locale(Req(headers={"Accept-Language": "zh-CN,zh;q=0.9"}), None, {}) == "zh-CN"
    assert resolve_locale(Req(), None, {}) == "zh-CN"


def test_email_and_h5_localized_content() -> None:
    from userloop.touch.base import TouchSpec
    from userloop.touch.render import render_email, render_h5

    spec = TouchSpec(channel="email", title="Hello", body="Body", cta_text="")
    zh = render_email(spec, {"id": "u1", "email": "a@x.com", "props": "{}"}, {"touch": {"track_secret": "s"}})
    en = render_email(spec, {"id": "u2", "email": "b@x.com", "props": '{"locale":"en-US"}'},
                      {"touch": {"track_secret": "s"}})
    assert "退订" in zh["html"] and "Unsubscribe" in en["html"]
    assert "Learn more" in en["html"]              # 默认 CTA 本地化

    h5 = render_h5(TouchSpec(channel="h5", title="T", body="B", cta_text=""), {"touch": {}},
                   {"title": "T", "body": "B", "dwell": {}, "locale": "en-US",
                    "lead": {"enabled": True, "endpoint": "/x", "token": "t"}})
    assert "Submit" in h5["html"] and "Email (optional)" in h5["html"]


async def test_new_tenant_never_inherits_base_event_storage(tmp_path) -> None:
    """回归：新租户不得继承基础配置的 MySQL 事件存储（否则事件串库）。"""
    base = {"data_dir": str(tmp_path),
            "storage": {"events": {"backend": "mysql", "mysql": {"enabled": True, "host": "127.0.0.1",
                                                                 "port": 3307, "user": "userloop",
                                                                 "password": "x", "database": "userloop"}}}}
    ts = TenantStores(base)
    ts.registry.create("isolated", name="隔离租户")
    t = ts.registry.get("isolated")
    cfg = tenant_config(base, t)
    assert cfg["storage"] == {}, "新租户必须默认 SQLite（不继承主租户事件库）"
    # 主租户保留其自身配置
    main_cfg = tenant_config(base, ts.registry.get(DEFAULT_TENANT))
    assert main_cfg["storage"].get("events", {}).get("backend") == "mysql"
    st = await ts.get("isolated")
    assert getattr(st.events, "backend", "").startswith("sqlite")
    await ts.close_all()

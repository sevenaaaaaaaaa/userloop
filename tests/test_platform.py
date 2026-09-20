"""N4：插件四类 / OpenAPI+MCP 契约 / 租户用量配额。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.billing import meter as billing
from userloop.core.store import Store
from userloop.plugins import registry as plug


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "plat.db"), {})
    await s.connect()
    yield s
    await s.close()


def test_builtin_plugins_cover_four_kinds() -> None:
    items = plug.load("data")
    kinds = {p["kind"] for p in items}
    assert kinds == {"source", "action", "model", "template"}
    assert any(p["id"] == "source.shopify" for p in items)
    assert any(p["id"] == "template.ecommerce-growth" for p in items)


def test_disk_plugin_overrides_builtin(tmp_path) -> None:
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "source.shopify.json").write_text(
        json.dumps({"id": "source.shopify", "kind": "source", "name": "Shopify 定制",
                    "enabled": True, "description": "覆盖"}),
        encoding="utf-8")
    (tmp_path / "plugins" / "action.demo.json").write_text(
        json.dumps({"id": "action.demo", "kind": "action", "name": "Demo Hook",
                    "enabled": True, "webhook": "https://example.test/hook"}),
        encoding="utf-8")
    shop = plug.get(str(tmp_path), "source.shopify")
    assert shop and shop["name"] == "Shopify 定制"
    demo = plug.get(str(tmp_path), "action.demo")
    assert demo and demo["kind"] == "action"
    listed = plug.list_plugins({"data_dir": str(tmp_path)})
    assert any(p["id"] == "action.demo" for p in listed)


async def test_plugin_action_posts_webhook(tmp_path) -> None:
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "action.demo.json").write_text(
        json.dumps({"id": "action.demo", "kind": "action", "name": "Demo",
                    "enabled": True, "webhook": "https://example.test/hook"}),
        encoding="utf-8")
    ctx = ExecutorContext(str(tmp_path), {"data_dir": str(tmp_path)})
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(204)

    res = await execute_action(ctx, {
        "type": "plugin.action.demo",
        "payload": {"_transport": httpx.MockTransport(handler)},
    }, {"id": "loop1"}, {"id": "u1", "email": "a@x.com", "stage": "paying"})
    assert res.get("ok") and res.get("ref") == "action.demo"
    assert "example.test" in seen["url"]
    assert seen["body"]["plugin"] == "action.demo"
    assert seen["body"]["user"]["id"] == "u1"


async def test_billing_records_and_enforces(store: Store, tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {"billing": {"enforce": True, "quotas": {"events": 2}}})
    assert (await billing.check(store, ctx, "events"))["ok"]
    await billing.record(store, "events", 2)
    gate = await billing.check(store, ctx, "events")
    assert not gate["ok"] and "events" in gate["reason"]
    # 默认不强制：即使超配额也放行
    loose = ExecutorContext(str(tmp_path), {"billing": {"enforce": False, "quotas": {"events": 1}}})
    assert (await billing.check(store, loose, "events"))["ok"]
    snap = await billing.snapshot(store, ctx)
    ev = next(m for m in snap["meters"] if m["meter"] == "events")
    assert ev["used"] == 2 and ev["remaining"] == 0


def test_platform_api_and_openapi(tmp_path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"storage": {}, "billing": {"enforce": True, "quotas": {"events": 0}}}),
        encoding="utf-8")
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        caps = client.get("/api/v1/capabilities").json()
        assert set(caps["plugins"]["kinds"]) == {"source", "action", "model", "template"}
        assert "repurchase" in caps["agents"]["recipes"]
        assert caps["openapi"] == "/api/v1/openapi.json"
        spec = client.get("/api/v1/openapi.json")
        assert spec.status_code == 200
        paths = spec.json().get("paths") or {}
        assert any("/agents/campaigns" in p for p in paths)
        assert any("/plugins" in p for p in paths)
        docs = client.get("/api/docs")
        assert docs.status_code == 200
        plat = client.get("/api/v1/platform").json()
        assert {p["kind"] for p in plat["plugins"]} == {"source", "action", "model", "template"}
        assert plat["billing"]["enforce"] is True
        blocked = client.post("/api/v1/ingest",
                              json={"distinct_id": "u-quota", "event": "page_view"})
        assert blocked.status_code == 429

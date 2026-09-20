"""全域数据中枢：多源归一化 + MA 适配器测试."""

from __future__ import annotations

import json

import httpx
import pytest

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.core.bus import handle
from userloop.core.store import Store
from userloop.hub.normalize import detect_source, normalize


def test_detect_source() -> None:
    assert detect_source({"userId": "u1", "event": "x", "properties": {}}) == "segment"
    assert detect_source({"client_id": "c", "events": [{"name": "purchase", "params": {}}]}) == "ga4"
    assert detect_source({"email": "a@x.com", "financial_status": "paid", "total_price": "9"}) == "shopify"
    assert detect_source([{"subscriptionType": "contact.propertyChange", "objectId": 1}]) == "hubspot"
    assert detect_source({"distinct_id": "d1", "event": "e", "properties": {}}) == "segment"  # cn 归一
    assert detect_source({"distinct_id": "d1", "event": "e", "props": {}}) == "generic"


def test_normalize_segment_and_ga4() -> None:
    seg = normalize("segment", {"userId": "u1", "event": "order_completed", "properties": {"amount": 99},
                                "email": "u1@x.com"})
    assert seg[0]["event"] == "order_completed" and seg[0]["props"]["amount"] == 99
    assert seg[0]["source"] == "hub:segment"
    ga4 = normalize("ga4", {"client_id": "c1", "events": [{"name": "page_view", "params": {"p": "/"}}]})
    assert ga4[0]["distinct_id"] == "c1" and ga4[0]["event"] == "page_view"


def test_normalize_shopify() -> None:
    out = normalize("shopify", {"email": "s@x.com", "financial_status": "paid", "total_price": "199",
                                "currency": "USD", "line_items": [{"title": "Plan", "quantity": 1, "price": "199"}]})
    assert out[0]["event"] == "purchase" and out[0]["props"]["amount"] == "199"
    assert out[0]["email"] == "s@x.com" and out[0]["props"]["email"] == "s@x.com"
    assert out[0]["props"]["items"][0]["title"] == "Plan"
    nested = normalize("shopify", {"financial_status": "paid", "total_price": "1",
                                   "customer": {"email": "c@x.com"}})
    assert nested[0]["email"] == "c@x.com"
    # 非支付状态 → 空（纯指标行不进总线）
    assert normalize("shopify", {"email": "s@x.com", "financial_status": "pending"}) == []


def test_normalize_hubspot() -> None:
    out = normalize("hubspot", [{"subscriptionType": "contact.propertyChange", "objectId": 123,
                                 "properties": {"email": "h@x.com", "lifecycle_stage": "lead"}}])
    assert out[0]["distinct_id"] == "h@x.com"
    assert out[0]["event"] == "ma_contact_sync"
    assert out[0]["email"] == "h@x.com"


async def test_hub_ingest_end_to_end(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        r = client.post("/api/v1/hub/ingest?source=shopify", json={
            "email": "buyer@x.com", "financial_status": "paid", "total_price": "299"})
        assert r.json()["accepted"] == 1
        users = client.get("/api/v1/users").json()
        buyer = [u for u in users["users"] if u["distinct_id"] == "buyer@x.com"]
        assert buyer and buyer[0]["stage"] == "paying"
        assert buyer[0]["email"] == "buyer@x.com"
        # 原始档案留存
        assert (tmp_path / "hub" / "shopify.jsonl").exists()


# ---- MA 适配器 ----

MA_CFG = {"integrations": {"ma": {"hubspot": {"token": "pat-test"}, "webhook_url": "http://ma.test/hook"}}}


async def test_ma_hubspot_upsert(tmp_path) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(201, json={"id": "hs_123"})

    ctx = ExecutorContext(str(tmp_path), MA_CFG)
    action = {"type": "ma.hubspot.contact_upsert",
              "payload": {"_transport": httpx.MockTransport(handler)}}
    user = {"id": "u", "email": "c@x.com", "stage": "paying"}
    r = await execute_action(ctx, action, {"id": "l", "template_id": "t", "trigger": {}}, user)
    assert r["ok"] is True and r["ref"] == "hs_123"
    assert "idProperty=email" in seen["url"]
    assert seen["body"]["properties"]["lifecycle_stage"] == "customer"


async def test_ma_hubspot_no_token_dry_run(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {})
    action = {"type": "ma.hubspot.contact_upsert", "payload": {}}
    r = await execute_action(ctx, action, {"id": "l", "template_id": "t", "trigger": {}}, {"id": "u", "email": "a@x.com"})
    assert r["ok"] is True and r["dry_run"] is True


async def test_ma_webhook_hmac(tmp_path) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sig"] = request.headers.get("X-MA-Signature")
        return httpx.Response(200)

    ctx = ExecutorContext(str(tmp_path), MA_CFG)
    action = {"type": "ma.webhook", "payload": {"_transport": httpx.MockTransport(handler), "secret": "s1"}}
    r = await execute_action(ctx, action, {"id": "l", "template_id": "t", "trigger": {}}, {"id": "u", "email": "a@x.com"})
    assert r["ok"] is True and seen["sig"]

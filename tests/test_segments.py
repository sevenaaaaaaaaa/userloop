"""阶段三：自然语言分群测试（规则校验 / 求值 / API）."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.segments import nl


def test_rule_validation_and_fallback() -> None:
    seg, warns = nl._clamp_rules({"name": "X", "logic": "OR", "rules": [
        {"field": "stage", "op": "in", "value": ["paying"]},
        {"field": "不存在的字段", "op": "eq", "value": 1},
        {"field": "churn", "op": "不支持", "value": 0.5},
    ]})
    assert seg["logic"] == "or"
    assert len(seg["rules"]) == 2                      # 非法字段被丢弃
    assert seg["rules"][1]["op"] == "eq"               # 非法操作符回退
    assert any("不支持" in w for w in warns)

    seg2, w2 = nl._clamp_rules({"rules": []})
    assert seg2["rules"][0]["field"] == "stage"        # 无合法规则 → 回退付费用户
    assert any("回退" in w for w in w2)


async def test_match_rule_semantics() -> None:
    facts = {"stage": "paying", "silence_days": 3, "purchases": 2, "churn": 0.2, "ltv": 0.6,
             "tier": "high", "_events": {"purchase", "view_pricing"}, "props": {"vip": True},
             "total_amount": 599}
    assert nl.match_rule(facts, {"field": "stage", "op": "in", "value": ["paying", "retained"]})
    assert nl.match_rule(facts, {"field": "silence_days", "op": "lte", "value": 7})
    assert nl.match_rule(facts, {"field": "purchases", "op": "gte", "value": 2})
    assert nl.match_rule(facts, {"field": "has_event", "op": "eq", "value": "view_pricing"})
    assert nl.match_rule(facts, {"field": "tier", "op": "in", "value": ["high", "vip"]})
    assert nl.match_rule(facts, {"field": "props", "op": "eq", "value": {"vip": True}})
    assert not nl.match_rule(facts, {"field": "churn", "op": "gte", "value": 0.9})


async def test_preview_and_save(tmp_path) -> None:
    s = Store(str(tmp_path / "seg.db"), {})
    await s.connect()
    try:
        a = await s.upsert_user("sg_a", email="a@x.com")
        await s.update_user(a["id"], stage="paying", stats=json.dumps({"purchases": 3, "total_amount": 900}))
        b = await s.upsert_user("sg_b", email="b@x.com")
        await s.update_user(b["id"], stage="activated")
        c = await s.upsert_user("sg_c")                      # 匿名 → 默认不扫
        await s.update_user(c["id"], stage="visitor")

        seg = {"id": "seg_test", "name": "高价值付费", "logic": "and",
               "rules": [{"field": "stage", "op": "in", "value": ["paying"]},
                         {"field": "purchases", "op": "gte", "value": 2}]}
        p = await nl.preview(s, seg)
        assert p["count"] == 1 and p["sample"][0]["distinct_id"] == "sg_a"
        await nl.save(s, seg)
        assert (await s.get_segment("seg_test"))["name"] == "高价值付费"
    finally:
        await s.close()


def test_segments_nl_api_with_mock(tmp_path, monkeypatch) -> None:
    """一句话 → 规则 → 预览 → 保存（LLM 用 mock）。"""
    from userloop.integrations import ai as ai_mod

    async def fake_chat(ctx, messages, **kw):  # type: ignore[no-untyped-def]
        return {"ok": True, "model": "mock", "content": json.dumps({
            "name": "高流失风险付费用户", "logic": "and",
            "rules": [{"field": "churn", "op": "gte", "value": 0.5},
                      {"field": "stage", "op": "in", "value": ["paying", "retained"]}]}, ensure_ascii=False)}

    monkeypatch.setattr(ai_mod, "chat", fake_chat)

    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "n1", "email": "n1@x.com", "event": "purchase"})
        client.post("/api/v1/predictions/recompute", json={"limit": 10})
        r = client.post("/api/v1/segments/nl", json={"prompt": "找出可能流失的付费用户"}).json()
        assert r["ok"] and r["segment"]["rules"][0]["field"] == "churn"
        assert "preview" in r
        saved = client.post("/api/v1/segments", json={"segment": r["segment"]}).json()
        assert saved["ok"]
        lst = client.get("/api/v1/segments").json()
        assert lst["count"] == 1 and lst["segments"][0]["name"] == "高流失风险付费用户"

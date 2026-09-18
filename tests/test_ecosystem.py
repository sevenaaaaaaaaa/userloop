"""生态互联测试：inFlow 洞察→Loop 草稿 / 回执；MFlow 发布回流→验证 verdict."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.integrations import inflow, mflow_publish


def _ctx(tmp_path, cfg=None):
    from userloop.actions.executors import ExecutorContext

    base = {"data_dir": str(tmp_path), "api_token": "x"}
    if cfg:
        base.update(cfg)
    return ExecutorContext(str(tmp_path), base)


def _insight(**kw):
    base = {"id": "ins_1", "workspace_id": "ws", "type": "traffic_anomaly",
            "title": "GA4 会话下降 32%", "summary": "近 7 天自然流量显著下滑",
            "severity": "high", "confidence": 0.8}
    base.update(kw)
    return base


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "eco.db"), {})
    await s.connect()
    yield s
    await s.close()


# ── inFlow → Loop ──

def test_recipe_mapping_and_draft_validation() -> None:
    out = inflow.draft_from_insight(_insight())
    draft = out["draft"]
    assert draft["enabled"] is False                       # 一律草稿态
    assert draft["trigger"] == {"type": "inactivity", "stage": "activated", "days": 7}
    assert draft["goal_event"] == "activation"
    assert draft["actions"][0]["type"] == "touch.email"
    assert draft["source_insight"] == "ins_1"

    # 未映射类型 → 兜底配方（仅团队通知）
    unknown = inflow.draft_from_insight({"id": "i2", "type": "whatever", "title": "T"})
    assert unknown["draft"]["actions"][0]["type"] == "feishu"

    # 文案占位符被洞察内容替换
    kw = inflow.draft_from_insight(_insight(type="keyword_opportunity", title="AI 落地页"))
    body = json.dumps(kw["draft"], ensure_ascii=False)
    assert "AI 落地页" in body and "mflow.create_content" in body


async def test_sync_creates_drafts_and_mirrors(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path, {"integrations": {"inflow": {"base_url": "http://if.test",
                                                      "workspace_id": "ws"}}})

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/v1/insights" in str(request.url) and request.method == "GET":
            return httpx.Response(200, json={"insights": [_insight(), _insight(id="ins_2", type="conversion_low",
                                                                              severity="low")]})
        if "/ack" in str(request.url):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    res = await inflow.sync(store, ctx, transport=httpx.MockTransport(handler))
    assert res["ok"] and res["fetched"] == 2
    assert res["created"] == 1                              # low 级别被 min_severity 过滤
    rows = await store.list_external_insights()
    assert len(rows) == 1 and rows[0]["source"] == "inflow"
    tpls = [t for t in await store.get_templates(enabled_only=False) if t.get("source_insight")]
    assert tpls and tpls[0]["enabled"] is False
    # 幂等：再次同步不会重复建
    res2 = await inflow.sync(store, ctx, transport=httpx.MockTransport(handler))
    assert res2["created"] == 0 and res2["skipped"] >= 1


async def test_outbound_recipe_not_auto_enabled(store: Store, tmp_path) -> None:
    """含用户外发动作的配方即使用 auto_enable_low_risk 也不自动启用（保护用户）。"""
    ctx = _ctx(tmp_path, {"integrations": {"inflow": {"base_url": "http://if.test",
                                                      "workspace_id": "ws",
                                                      "auto_enable_low_risk": True}}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"insights": [_insight(type="traffic_anomaly")]})
        return httpx.Response(200, json={"ok": True})

    await inflow.sync(store, ctx, transport=httpx.MockTransport(handler))
    tpls = [t for t in await store.get_templates(enabled_only=False) if t.get("source_insight")]
    assert tpls and tpls[0]["enabled"] is False


# ── MFlow 发布回流 → 验证 ──

async def test_publication_record_and_verify(store: Store) -> None:
    past = (datetime.utcnow() - timedelta(days=15)).isoformat(timespec="seconds") + "Z"
    out = await mflow_publish.record_publish(store, {
        "item_id": "ul-demo-1", "title": "召回旅程拆解", "url": "https://nownexts.com/blog/recall",
        "published_at": past})
    assert out["ok"] and out["publication"]["status"] == "verifying"

    # 窗口内造 6 条归因事件（utm_campaign 命中）
    u = await store.upsert_user("pub_v")
    for i in range(6):
        await store.insert_event({"user_id": u["id"], "distinct_id": "pub_v", "event": "page_view",
                                  "props": {"utm_campaign": "ul-demo-1", "url": "https://nownexts.com/blog/recall"},
                                  "source": "web", "event_id": f"pub-{i}",
                                  "created_at": (datetime.utcnow() - timedelta(days=2)).isoformat(timespec="seconds") + "Z"})
    done = await mflow_publish.verify_due_publications(store)
    assert len(done) == 1 and done[0]["verdict"] == "effective"
    assert done[0]["evidence"]["hits"] >= 6
    # feedback 落库（内容效果进入统一回流体系）
    stats = await store.feedback_stats()
    assert "content.mflow" in stats
    # 幂等：已 verified 不再重复验证
    assert await mflow_publish.verify_due_publications(store) == []


def test_ecosystem_apis(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        # MFlow 发布回调
        r = client.post("/api/v1/hub/mflow/publish", json={"item_id": "m1", "title": "T",
                                                           "url": "https://x/1"}).json()
        assert r["ok"] and r["publication"]["item_id"] == "m1"
        assert client.get("/api/v1/content/publications").json()["count"] == 1
        assert client.post("/api/v1/content/verify").json()["verified"] == 0   # 未到期
        # inFlow 未配置 → 明确报错而非静默
        assert client.post("/api/v1/integrations/inflow/sync").json()["ok"] is False
        assert client.get("/api/v1/integrations/inflow/insights").json()["count"] == 0


def test_recipes_cover_real_inflow_types() -> None:
    """覆盖 inFlow 真实产出的洞察类型（情报→动作要对得上）。"""
    real_types = ["keyword_opportunity", "topic_negative_alert", "topic_digest", "narrative_metric",
                  "competitor_pricing", "keyword_gap", "traffic_anomaly", "journey_gap", "site_change",
                  "rfm_at_risk", "conversion_low", "nps_shift", "journey_content_gap",
                  "retention_decline", "ltv_cac_unhealthy", "competitor_move"]
    mapped = [t for t in real_types if t in inflow.INSIGHT_RECIPES]
    assert len(mapped) >= 14, f"仅映射 {len(mapped)}/{len(real_types)}"
    # 关键可执行类型必须有专属配方（而非兜底）
    for t in ("rfm_at_risk", "retention_decline", "journey_gap", "topic_negative_alert",
              "ltv_cac_unhealthy", "keyword_gap"):
        assert t in inflow.INSIGHT_RECIPES and inflow.INSIGHT_RECIPES[t]["actions"]
        assert inflow.recipe_for(t)["name"] != inflow.DEFAULT_RECIPE["name"]


async def test_count_events_matching_uses_event_backend(store: Store) -> None:
    """回归：内容归因计数必须走事件存储后端（此前查 SQLite 兜底表 → 恒 0）。"""
    u = await store.upsert_user("cm1")
    await store.insert_event({"user_id": u["id"], "distinct_id": "cm1", "event": "page_view",
                              "props": {"utm_campaign": "cmp-1", "url": "https://x/a"},
                              "source": "web", "event_id": "cm-1", "created_at": "2026-09-08T10:00:00Z"})
    n = await store.count_events_matching("utm_campaign", "cmp-1", "2026-09-02T00:00:00Z",
                                          "2026-09-16T00:00:00Z")
    assert n == 1
    assert await store.count_events_matching("utm_campaign", "cmp-1", "2026-09-09T00:00:00Z",
                                             "2026-09-16T00:00:00Z") == 0

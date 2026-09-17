"""AI 大脑测试：决策解析 / 阶段策略裁剪 / 护栏 / 风险审批门 / 审计."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest

from userloop.actions.executors import ExecutorContext
from userloop.ai import brain, guardrails
from userloop.core.store import Store

AI_CFG = {"ai": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-t", "model": "deepseek-chat",
                 "brain": {"enabled": True, "auto_execute_medium": False, "min_gap_hours": 24,
                            "quiet_hours": [0, 0]}}}


def _llm(intent="send_email", topic="引导激活", brief="引导完成第一个项目", confidence=0.8):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "deepseek-chat", "choices": [{"message": {"content": json.dumps(
            {"intent": intent, "topic": topic, "brief": brief,
             "reasoning": "该用户注册后未激活，建议个性化引导", "confidence": confidence,
             "urgency": "medium", "expected_effect": "激活率 +10%"}, ensure_ascii=False)}}]})
    return httpx.MockTransport(handler)


@pytest.fixture
async def store(tmp_path):
    s = Store(":memory:")
    await s.connect()
    from userloop.core.templates import seed_templates

    await seed_templates(s, str(tmp_path))
    yield s
    await s.close()


def _ctx(tmp_path, cfg=None) -> ExecutorContext:
    return ExecutorContext(str(tmp_path), cfg or AI_CFG)


async def test_stage_policy_and_intent_whitelist() -> None:
    assert "send_email" not in brain.stage_policy("visitor")   # 访客不外发
    assert "send_email" in brain.stage_policy("signup")
    assert "noop" in brain.stage_policy("visitor")
    for name, spec in brain.INTENTS.items():
        assert spec["risk"] in ("low", "medium", "high")


async def test_medium_risk_goes_to_approval(store: Store, tmp_path) -> None:
    user = await store.upsert_user("b1", email="b1@x.com")
    await store.update_user(user["id"], stage="signup")
    user = await store.get_user(user["id"])
    rec = await brain.run_for_user(store, _ctx(tmp_path), user, transport=_llm("send_email"))
    assert rec["status"] == "pending_approval" and rec["risk"] == "medium"
    assert rec["payload"]["type"] == "ai.email"
    # 审批门：未批准不产生 Loop
    assert not [l for l in await store.list_loops() if l["template_id"] == "ai.send_email"]


async def test_low_risk_auto_executes(store: Store, tmp_path) -> None:
    user = await store.upsert_user("b2", email="b2@x.com")
    await store.update_user(user["id"], stage="activated")
    user = await store.get_user(user["id"])
    rec = await brain.run_for_user(store, _ctx(tmp_path), user, transport=_llm("compose_email"))
    assert rec["status"] == "auto_executed" and rec["loop_id"]
    loops = [l for l in await store.list_loops() if l["template_id"] == "ai.compose_email"]
    assert loops and loops[0]["status"] in ("verified", "running", "verifying")


async def test_ai_out_of_whitelist_trimmed_to_noop(store: Store, tmp_path) -> None:
    """AI 越权（访客阶段要发邮件）→ 服务端裁剪为 noop。"""
    user = await store.upsert_user("b3", email="b3@x.com")  # visitor
    rec = await brain.run_for_user(store, _ctx(tmp_path), user, transport=_llm("send_email"))
    assert rec["intent"] == "noop" and "策略裁剪" in rec["reasoning"]


async def test_frequency_guardrail_blocks_second_touch(store: Store, tmp_path) -> None:
    user = await store.upsert_user("b4", email="b4@x.com")
    await store.update_user(user["id"], stage="activated")
    user = await store.get_user(user["id"])
    ctx = _ctx(tmp_path)
    first = await brain.run_for_user(store, ctx, user, transport=_llm("compose_email"))
    assert first["status"] == "auto_executed"
    second = await brain.run_for_user(store, ctx, await store.get_user(user["id"]), transport=_llm("compose_email"))
    assert second["status"] == "skipped" and "频控" in second["reasoning"]
    # force 可人工覆盖
    third = await brain.run_for_user(store, ctx, await store.get_user(user["id"]),
                                     transport=_llm("compose_email"), force=True)
    assert third["status"] == "auto_executed"


def test_quiet_hours() -> None:
    cfg = dict(guardrails.DEFAULTS)
    cfg["quiet_hours"] = [22, 8]
    cfg["tz_offset_hours"] = 8
    utc_15 = datetime(2026, 9, 17, 15, 0, 0)   # 北京 23:00 → 静默
    utc_02 = datetime(2026, 9, 17, 2, 0, 0)    # 北京 10:00 → 可触达
    assert guardrails.in_quiet_hours(cfg, utc_15) is True
    assert guardrails.in_quiet_hours(cfg, utc_02) is False


async def test_daily_budget(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path, {"ai": {"api_key": "sk-t", "brain": {"enabled": True, "daily_budget": 1, "quiet_hours": [0, 0]}}})
    user = await store.upsert_user("b5", email="b5@x.com")
    await store.update_user(user["id"], stage="activated")
    await brain.run_for_user(store, ctx, await store.get_user(user["id"]), transport=_llm("compose_email"))
    assert await store.ai_decisions_today() == 1
    second = await brain.run_for_user(store, ctx, await store.get_user(user["id"]), transport=_llm("compose_email"),
                                      force=True)
    assert second["status"] == "skipped" and "预算" in second["reasoning"]


async def test_brain_disabled_by_default(tmp_path) -> None:
    store = Store(":memory:")
    await store.connect()
    ctx = _ctx(tmp_path, {"ai": {"api_key": "sk-t"}})  # brain.enabled 未设
    assert await brain.run_batch(store, ctx, limit=3) == []
    await store.close()

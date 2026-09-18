"""自进化测试：遥测 / 自诊断规则 / 提案（AI+mock）/ 配置应用白名单与回滚 / Lessons."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.evolve import engine as evo


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "evo.db"), {})
    await s.connect()
    yield s
    await s.close()


def _ctx(tmp_path, cfg=None):
    from userloop.actions.executors import ExecutorContext

    base = {"data_dir": str(tmp_path), "api_token": "x"}
    if cfg:
        base.update(cfg)
    return ExecutorContext(str(tmp_path), base)


async def test_telemetry_and_diagnose(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path)
    ctx.store = store
    # 造问题：失败动作 + 待批积压 + 低识别率
    await store.upsert_user("u1", email="u1@x.com")
    for i in range(60):
        await store.upsert_user(f"anon{i}")
    from userloop.core.store import new_id

    loop_id = new_id("loop")
    await store.insert_loop({"id": loop_id, "template_id": "t", "user_id": "u1", "status": "running",
                             "trigger": {}, "context": {}, "created_at": "2026-09-18T00:00:00Z",
                             "updated_at": "2026-09-18T00:00:00Z"})
    await store.insert_action({"id": new_id("act"), "loop_id": loop_id, "seq": 1, "type": "touch.email",
                               "payload": {}, "delay_minutes": 0, "status": "failed",
                               "scheduled_at": "2026-09-18T00:00:00Z", "executed_at": "2026-09-18T00:00:00Z",
                               "result": "{}"})
    for i in range(25):
        await store.insert_ai_decision({"id": f"ai{i}", "user_id": "u1", "intent": "send_email",
                                        "status": "pending_approval", "risk": "medium", "confidence": 0.5})

    tel = await evo.collect_telemetry(store, ctx)
    assert tel["identified_rate"] < 20 and tel["tenant"]
    dg = await evo.diagnose(store, ctx)
    codes = {i["code"] for i in dg["issues"]}
    assert "actions_failed" in codes and "ai_pending" in codes and "identified_rate_low" in codes
    assert dg["ok"] is False and dg["counts"]["high"] >= 1
    # 遥测落盘
    import os

    day = __import__("datetime").datetime.utcnow().date().isoformat()
    assert os.path.exists(os.path.join(str(tmp_path), "telemetry", f"{day}.jsonl"))


async def test_propose_and_apply_config_with_rollback(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path, {"touch": {"frequency": {"weekly_cap": 3}}})
    ctx.store = store

    async def fake_chat(ctx_, messages, **kw):  # type: ignore[no-untyped-def]
        return {"ok": True, "model": "mock", "content": json.dumps({"proposals": [
            {"title": "放宽周触达上限", "rationale": "拦截率 60% 偏高", "kind": "config",
             "impact": "触达 +30%", "risk": "low", "validation": "观察 7 天转化与退订率",
             "apply_key": "frequency.weekly_cap", "apply_value": 5},
            {"title": "重构触点驱动层", "rationale": "架构债", "kind": "code",
             "impact": "可维护性", "risk": "medium", "validation": "回归测试通过"},
        ]}, ensure_ascii=False)}

    import userloop.integrations.ai as ai_mod

    real = ai_mod.chat
    ai_mod.chat = fake_chat  # type: ignore[assignment]
    try:
        res = await evo.propose(store, ctx)
    finally:
        ai_mod.chat = real  # type: ignore[assignment]
    assert res["ok"] and res["count"] == 2

    props = {p["title"]: p for p in await store.list_evolution_proposals()}
    cfg_prop = props["放宽周触达上限"]
    code_prop = props["重构触点驱动层"]
    assert cfg_prop["kind"] == "config" and code_prop["kind"] == "code"

    # 应用配置类 → 落文件 + 可回滚
    out = await evo.apply_proposal(store, ctx, cfg_prop["id"])
    assert out["ok"] and out["new_value"] == 5 and out["rollback"]["value"] == 3
    with open(str(tmp_path / "config.json"), encoding="utf-8") as f:
        assert json.load(f)["touch"]["frequency"]["weekly_cap"] == 5
    assert ctx.config["touch"]["frequency"]["weekly_cap"] == 5

    # 代码类 → 登记待办 + Lessons
    out2 = await evo.apply_proposal(store, ctx, code_prop["id"])
    assert out2["ok"] and out2["status"] == "accepted_todo"
    lessons = await store.list_lessons()
    assert any("代码类改进待办" in l["title"] for l in lessons)

    # 非白名单 key 拒绝
    bad = {"id": "evo_bad", "title": "危险变更", "kind": "config", "apply_key": "storage.events.backend",
           "apply_value": "mysql"}
    await store.put_evolution_proposal(bad)
    assert (await evo.apply_proposal(store, ctx, "evo_bad"))["ok"] is False


async def test_lessons_render_markdown(store: Store) -> None:
    await store.add_lesson({"category": "bug", "title": "多租户串库",
                            "detail": "新租户继承了主租户事件库", "fix": "tenant_config 不回退 base storage"})
    md = evo.render_lessons_md(await store.list_lessons())
    assert "多租户串库" in md and "## bug" in md and "# UserLoop Lessons" in md


def test_evolve_api(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "e1", "email": "e1@x.com", "event": "signup"})
        st = client.get("/api/v1/evolve/status").json()
        assert st["telemetry"]["counts"]["users"] >= 1 and "diagnostics" in st
        dg = client.get("/api/v1/evolve/diagnose").json()
        assert "issues" in dg
        r = client.post("/api/v1/evolve/lessons", json={"category": "ops", "title": "手动记录一条",
                                                        "detail": "d", "fix": "f"}).json()
        assert r["ok"]
        html = client.get("/api/v1/evolve/lessons")
        assert "手动记录一条" in html.text

"""P1 测试：跨渠道全局频控 + Next Best Channel（含 touch.auto 端到端）."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.core.store import Store
from userloop.touch import frequency, select


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "f.db"), {})
    await s.connect()
    yield s
    await s.close()


def _ctx(tmp_path, cfg=None) -> ExecutorContext:
    return ExecutorContext(str(tmp_path), cfg or {})


async def _touch(store: Store, user: dict, atype: str, template: str = "tpl", hours_ago: float = 0) -> None:
    """构造一条历史触达（写触点台账，等价于"真的发过一条"）。"""
    from userloop.touch import frequency as fm

    channel = fm.channel_of(atype)
    await store.log_touch(user["id"], channel, atype, template_id=template, loop_id="loop_hist")
    if hours_ago:   # 回填时间（台账默认 now）
        await store.db.execute(
            "UPDATE touch_log SET created_at=? WHERE id=(SELECT id FROM touch_log "
            "WHERE user_id=? AND channel=? AND template_id=? ORDER BY id DESC LIMIT 1)",
            ((datetime.utcnow() - timedelta(hours=hours_ago)).isoformat(timespec="seconds") + "Z",
             user["id"], channel, template))


# ── 频控门 ──

async def test_frequency_global_gap_and_weekly_cap(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path, {"touch": {"frequency": {"global_gap_hours": 24, "weekly_cap": 3,
                                                 "quiet_hours": [0, 0]}}})
    u = await store.upsert_user("fq1", email="fq1@x.com")
    assert (await frequency.check(store, ctx, u, "email", "tpl"))["ok"] is True

    await _touch(store, u, "touch.email", hours_ago=2)          # 2 小时前发过邮件
    r = await frequency.check(store, ctx, u, "sms", "tpl")      # 换渠道也要受全局间隔约束
    assert r["ok"] is False and "全局间隔" in r["reason"]

    # 周上限场景：全新用户，三条历史触达均超出全局间隔（30/40/50h 前）
    u2 = await store.upsert_user("fq1b", email="fq1b@x.com")
    await _touch(store, u2, "touch.email", hours_ago=30)
    await _touch(store, u2, "touch.im", hours_ago=40)
    await _touch(store, u2, "touch.sms", hours_ago=50)
    r2 = await frequency.check(store, ctx, u2, "email", "tpl")
    assert r2["ok"] is False and "周触达已达上限" in r2["reason"]


async def test_frequency_per_channel_caps(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path, {"touch": {"frequency": {
        "global_gap_hours": 0, "weekly_cap": 10, "quiet_hours": [0, 0],
        "channel_weekly_cap": {"sms": 1}, "channel_gap_hours": {"sms": 168}}}})
    u = await store.upsert_user("fq2")
    await _touch(store, u, "touch.sms", hours_ago=1)
    r = await frequency.check(store, ctx, u, "sms", "tpl")
    assert r["ok"] is False and "sms 渠道" in r["reason"]


async def test_frequency_quiet_hours_and_transactional(store: Store, tmp_path) -> None:
    import userloop.touch.frequency as fm

    # 静默期纯函数（确定性时间，不受运行时时钟影响）
    ctx = _ctx(tmp_path, {"touch": {"frequency": {"quiet_hours": [22, 8], "tz_offset_hours": 8}}})
    assert fm.in_quiet_hours(fm.cfg_of(ctx), datetime(2026, 9, 18, 15, 0, 0)) is True    # 北京 23:00
    assert fm.in_quiet_hours(fm.cfg_of(ctx), datetime(2026, 9, 18, 2, 0, 0)) is False    # 北京 10:00
    off = _ctx(tmp_path, {"touch": {"frequency": {"quiet_hours": [0, 0]}}})
    assert fm.in_quiet_hours(fm.cfg_of(off)) is False

    u = await store.upsert_user("fq3", email="fq3@x.com")
    # 事务类豁免（收据/验证码不受营销频控约束）
    r2 = await frequency.check(store, ctx, u, "email", "order_receipt")
    assert r2["ok"] is True and "事务类豁免" in r2["reason"]
    # force（人工审批触达）跳过
    assert (await frequency.check(store, ctx, u, "email", "tpl", force=True))["ok"] is True


# ── Next Best Channel ──

async def test_selector_availability_and_prior(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path)
    u = await store.upsert_user("sel1")                      # 只有 phone，没有 email
    await store.db.execute("INSERT INTO identities (user_id,type,value,verified,source,created_at) "
                           "VALUES (?,?,?,0,'test',?)", (u["id"], "phone", "13800000001", "2026-09-18T00:00:00Z"))
    ids = await store.of_user_identities(u["id"])
    res = await select.score(store, u, ids, ctx, intent="reengage")
    assert res["channel"] == "sms" and res["scores"][0]["channel"] == "sms"

    u2 = await store.upsert_user("sel2", email="sel2@x.com")
    ids2 = await store.of_user_identities(u2["id"])
    res2 = await select.score(store, u2, ids2, ctx, intent="reengage")
    assert res2["channel"] == "email"


async def test_selector_uses_global_engagement(store: Store, tmp_path) -> None:
    """全局历史：短信点击率明显高于邮件 → 有手机+邮箱的用户优先短信（权重内比较）。"""
    ctx = _ctx(tmp_path, {"touch": {"selector": {"weights": {"email": 1.0, "sms": 1.0}}}})
    for i in range(20):
        u = await store.upsert_user(f"eng{i}", email=f"eng{i}@x.com")
        await store.insert_event({"user_id": u["id"], "distinct_id": f"eng{i}", "event": "sms_click",
                                  "props": {}, "source": "t", "event_id": f"sc-{i}",
                                  "created_at": "2026-09-17T00:00:00Z"})
    u = await store.upsert_user("sel3", email="sel3@x.com")
    await store.db.execute("INSERT INTO identities (user_id,type,value,verified,source,created_at) "
                           "VALUES (?,?,?,0,'test',?)", (u["id"], "phone", "13800000009", "2026-09-18T00:00:00Z"))
    ids = await store.of_user_identities(u["id"])
    res = await select.score(store, u, ids, ctx, intent="reengage")
    assert res["channel"] == "sms", res
    assert any(s["channel"] == "email" for s in res["scores"])


async def test_selector_no_channel(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path)
    u = await store.upsert_user("sel4")            # 无任何标识
    res = await select.score(store, u, {}, ctx, intent="reengage")
    assert res["channel"] is None and "无可用渠道" in res["rationale"]


# ── touch.auto 端到端 ──

async def test_touch_auto_picks_channel_and_sends(tmp_path) -> None:
    store = Store(str(tmp_path / "auto.db"), {})
    await store.connect()
    try:
        seen = {}

        def handler(request):  # type: ignore[no-untyped-def]
            import json as _json

            import httpx as _httpx

            url = str(request.url)
            if url.endswith("/api/login"):
                return _httpx.Response(200, json={"ok": True}, headers={"set-cookie": "mflow_session=s; Path=/"})
            seen["url"] = url
            seen["body"] = _json.loads(request.read() or b"{}")
            return _httpx.Response(200, json={"ok": True, "ref": "gw-1"})

        import httpx

        u = await store.upsert_user("auto1")            # 只有手机 → 应选 sms
        await store.db.execute("INSERT INTO identities (user_id,type,value,verified,source,created_at) "
                              "VALUES (?,?,?,0,'t',?)", (u["id"], "phone", "13800000002", "2026-09-18T00:00:00Z"))
        ctx = ExecutorContext(str(tmp_path), {
            "touch": {"frequency": {"enabled": True, "quiet_hours": [0, 0], "global_gap_hours": 0},
                      "sms": {"enabled": True, "provider": "webhook", "webhook_url": "http://sms.test/send"}},
            "api_token": "x"})
        ctx.store = store
        res = await execute_action(ctx, {"type": "touch.auto",
                                         "payload": {"intent": "reengage", "title": "回来看看", "text": "有新内容",
                                                     "vars": {"_transport": httpx.MockTransport(handler)}}},
                                   {"id": "loop_auto", "template_id": "tpl"}, u)
        assert res["ok"] is True
        assert res["selector"]["chosen"] == "sms"
        assert res["channel"] == "sms"
    finally:
        await store.close()


async def test_touch_auto_blocked_by_frequency(tmp_path) -> None:
    store = Store(str(tmp_path / "auto2.db"), {})
    await store.connect()
    try:
        u = await store.upsert_user("auto2", email="auto2@x.com")
        ctx = ExecutorContext(str(tmp_path), {
            "touch": {"frequency": {"enabled": True, "quiet_hours": [0, 0], "global_gap_hours": 24},
                      "track_secret": "s", "public_base": "https://ul.test"},
            "api_token": "x"})
        ctx.store = store
        await _touch(store, u, "touch.email", hours_ago=1)          # 刚发过
        res = await execute_action(ctx, {"type": "touch.auto",
                                         "payload": {"intent": "reengage", "title": "t", "text": "b"}},
                                   {"id": "l", "template_id": "tpl"}, u)
        assert res["ok"] is False and res.get("blocked_by_frequency") is True
        assert "全局间隔" in res["note"]
    finally:
        await store.close()


async def test_legacy_email_action_respects_frequency(store: Store, tmp_path) -> None:
    """模板 Loop 常用的 legacy 动作（ai.email/email/feishu）也必须过频控门。"""
    ctx = ExecutorContext(str(tmp_path), {"touch": {"frequency": {"enabled": True, "quiet_hours": [0, 0],
                                                                 "global_gap_hours": 24}}})
    ctx.store = store
    u = await store.upsert_user("leg1", email="leg1@x.com")
    await _touch(store, u, "touch.email", hours_ago=1)          # 1 小时前刚触达
    res = await execute_action(ctx, {"type": "ai.email", "payload": {"subject": "s", "text": "b"}},
                               {"id": "l", "template_id": "tpl"}, u)
    assert res["ok"] is False and res.get("blocked_by_frequency") is True
    assert "全局间隔" in res["note"]


async def test_transactional_does_not_consume_quota(tmp_path) -> None:
    """事务类消息（收据）发出后不应占用营销周配额。"""
    store = Store(str(tmp_path / "tx.db"), {})
    await store.connect()
    try:
        ctx = ExecutorContext(str(tmp_path), {"touch": {"frequency": {"enabled": True, "quiet_hours": [0, 0],
                                                                     "global_gap_hours": 24, "weekly_cap": 3}}})
        ctx.store = store
        # 通过包装层真实执行（dry-run 也算成功，但不该入台账）
        u = await store.upsert_user("tx1", email="tx1@x.com")
        res = await execute_action(ctx, {"type": "email", "payload": {"subject": "订单收据", "text": "收据"}},
                                   {"id": "l", "template_id": "order_receipt"}, u)
        assert res.get("ok") is True
        rows = await store.recent_touches(u["id"], hours=24)
        assert rows == [], "事务类不应写入营销台账"
        assert (await frequency.check(store, ctx, u, "email", "tpl"))["ok"] is True
    finally:
        await store.close()


async def test_touch_auto_records_resolved_channel(tmp_path) -> None:
    """touch.auto 成功后应按**解析出的真实渠道**记账，使后续触达被频控拦住。"""
    import httpx

    store = Store(str(tmp_path / "arct.db"), {})
    await store.connect()
    try:
        u = await store.upsert_user("arc1", email="arc1@x.com")

        def handler(request):  # type: ignore[no-untyped-def]
            return httpx.Response(200, json={"ok": True, "ref": "m1"})

        ctx = ExecutorContext(str(tmp_path), {
            "touch": {"frequency": {"enabled": True, "quiet_hours": [0, 0], "global_gap_hours": 24},
                      "email": {"driver": "openflow", "bridge_url": "http://of.test", "bridge_token": "t"}},
            "integrations": {"openflow": {"base_url": "http://of.test", "bridge_token": "t"}},
            "api_token": "x"})
        ctx.store = store
        import userloop.touch.drivers as drv
        real = httpx.AsyncClient

        class Patched(real):
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(handler)
                super().__init__(*a, **kw)

        orig = drv.httpx.AsyncClient
        drv.httpx.AsyncClient = Patched                      # type: ignore[assignment]
        try:
            r1 = await execute_action(ctx, {"type": "touch.auto",
                                            "payload": {"intent": "reengage", "title": "t", "text": "b"}},
                                      {"id": "l1", "template_id": "tpl"}, u)
            assert r1["ok"] and r1["channel"] == "email"
            rows = await store.recent_touches(u["id"], hours=24)
            assert len(rows) == 1 and rows[0]["channel"] == "email", rows
            r2 = await execute_action(ctx, {"type": "touch.sms", "payload": {"text": "x"}},
                                      {"id": "l2", "template_id": "tpl"}, u)
            assert r2.get("blocked_by_frequency") is True
        finally:
            drv.httpx.AsyncClient = orig                     # type: ignore[assignment]
    finally:
        await store.close()


async def test_scheduler_path_logs_block_and_sent(tmp_path) -> None:
    """调度路径（无显式 store 传参）也必须能写频控台账与投递回执。"""
    import httpx

    store = Store(str(tmp_path / "sch.db"), {})
    await store.connect()
    try:
        u = await store.upsert_user("sch1", email="sch1@x.com")
        await store.log_touch(u["id"], "email", "touch.email", "tpl", "l")   # 先占一次触达

        def handler(request):  # type: ignore[no-untyped-def]
            return httpx.Response(200, json={"ok": True})

        ctx = ExecutorContext(str(tmp_path), {
            "touch": {"frequency": {"enabled": True, "quiet_hours": [0, 0], "global_gap_hours": 24}},
            "api_token": "x"})
        # 不显式设置 ctx.store → 走"调度路径"场景
        from userloop.core.scheduler import process_due_actions
        from userloop.core.store import new_id

        loop_id = new_id("loop")
        await store.insert_loop({"id": loop_id, "template_id": "tpl", "user_id": u["id"], "status": "running",
                                 "trigger": {}, "context": {}, "created_at": "2026-09-18T00:00:00Z",
                                 "updated_at": "2026-09-18T00:00:00Z"})
        await store.insert_action({"id": new_id("act"), "loop_id": loop_id, "seq": 1, "type": "touch.email",
                                   "payload": {"subject": "s", "text": "b"}, "delay_minutes": 0,
                                   "status": "pending", "scheduled_at": "2026-09-18T00:00:00Z"})
        await process_due_actions(store, ctx, limit=5)
        blocks = await store.block_stats(days=1)
        assert blocks["total"] >= 1, blocks          # 拦截被记入台账
        assert ctx.store is store                     # 入口已自动挂载
    finally:
        await store.close()


async def test_sto_defers_to_best_hour_once(tmp_path) -> None:
    """STO：不在用户活跃时段 → 推迟一次（改了 scheduled_at 且带 _sto_deferred 标记）。"""
    from datetime import datetime, timedelta
    from userloop.core.store import new_id

    store = Store(str(tmp_path / "sto.db"), {})
    await store.connect()
    try:
        u = await store.upsert_user("sto1", email="sto1@x.com")
        # best_hour 设为当前本地小时的 +6（确保不在窗口内）
        local_hour = (datetime.utcnow() + timedelta(hours=8)).hour
        bh = (local_hour + 6) % 24
        await store.upsert_user_score({"user_id": u["id"], "churn": 0, "ltv": 0, "propensity": 0,
                                       "tier": "low", "best_hour": bh, "reasons": {},
                                       "computed_at": "2026-09-18T00:00:00Z"})
        ctx = ExecutorContext(str(tmp_path), {
            "touch": {"frequency": {"enabled": False}, "sto": {"enabled": True, "window_hours": 2},
                      "qc": {"enabled": False}, "track_secret": "s", "public_base": "https://ul.test"},
            "api_token": "x"})
        ctx.store = store
        loop_id = new_id("loop")
        await store.insert_loop({"id": loop_id, "template_id": "tpl", "user_id": u["id"], "status": "running",
                                 "trigger": {}, "context": {}, "created_at": "2026-09-18T00:00:00Z",
                                 "updated_at": "2026-09-18T00:00:00Z"})
        aid = new_id("act")
        await store.insert_action({"id": aid, "loop_id": loop_id, "seq": 1, "type": "touch.email",
                                   "payload": {"subject": "s", "text": "b"}, "delay_minutes": 0,
                                   "status": "pending", "scheduled_at": "2026-09-18T00:00:00Z"})
        from userloop.core.scheduler import process_due_actions

        results = await process_due_actions(store, ctx)
        assert results and results[0].get("deferred_by_sto") is True
        rows = await store.actions_for_loop(loop_id)
        assert rows[0]["status"] == "pending"                       # 已重排回 pending
        import json as _json

        assert _json.loads(rows[0]["payload"]).get("_sto_deferred") is True
    finally:
        await store.close()

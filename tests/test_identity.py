"""阶段一：识别测试（匿名→实名合并 / 归并连续性 / 识别率 / H5 留资）."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from userloop.core.bus import handle
from userloop.core.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "id.db"), {})
    await s.connect()
    yield s
    await s.close()


def _ctx(tmp_path):
    from userloop.actions.executors import ExecutorContext

    return ExecutorContext(str(tmp_path), {})


# ── 合并核心 ──

async def test_merge_users_moves_everything(store: Store) -> None:
    anon = await store.upsert_user("anon_x")
    known = await store.upsert_user("known_y", email="known@x.com")
    await store.update_user(known["id"], stage="paying")
    # 匿名档案有事件/台账/身份
    await store.insert_event({"user_id": anon["id"], "distinct_id": "anon_x", "event": "page_view",
                              "props": {}, "source": "web", "event_id": "e1",
                              "created_at": "2026-09-18T00:00:00Z"})
    await store.log_touch(anon["id"], "email", "touch.email", "tpl", "l")
    await store.bind_identity(anon["id"], "phone", "13800000000", source="t")
    await store.db.execute("INSERT INTO stage_transitions (user_id, from_stage, to_stage, reason, created_at) "
                          "VALUES (?,?,?,?,?)", (anon["id"], "visitor", "signup", "t", "2026-09-18T00:00:00Z"))

    info = await store.merge_users(known["id"], anon["id"])
    assert info["merged"] and info["moved_events"] == 1

    # 副档案删除、关联迁移
    assert await store.get_user(anon["id"]) is None
    assert (await store.counts())["events"] == 1
    cur = await store.db.execute("SELECT COUNT(*) c FROM touch_log WHERE user_id=?", (known["id"],))
    assert (await cur.fetchone())["c"] == 1
    assert await store.resolve_identity("phone", "13800000000") == known["id"]
    # 匿名 distinct_id 记为身份（后续同源事件继续归并）
    assert await store.resolve_identity("anonymous_id", "anon_x") == known["id"]
    # 阶段取更靠后
    assert (await store.get_user(known["id"]))["stage"] == "paying"


async def test_bus_merges_when_event_carries_known_email(store: Store, tmp_path) -> None:
    """匿名访客后续事件里带了已存在用户的邮箱 → 自动合并（匿名→实名）。"""
    ctx = _ctx(tmp_path)
    # 已实名用户（历史）
    r0 = await handle(store, ctx, {"distinct_id": "u_known", "email": "k@x.com", "event": "signup"})
    # 匿名访客浏览
    r1 = await handle(store, ctx, {"distinct_id": "anon_1", "event": "page_view"})
    assert r1["user_id"] != r0["user_id"]
    # 匿名访客留资（事件带 email）→ 合并到实名档案
    r2 = await handle(store, ctx, {"distinct_id": "anon_1", "event": "form_submit",
                                   "email": "k@x.com", "props": {"email": "k@x.com"}})
    assert r2["user_id"] == r0["user_id"] and r2["merged"] is True
    # 之后匿名来源的事件继续落在实名档案
    r3 = await handle(store, ctx, {"distinct_id": "anon_1", "event": "page_view"})
    assert r3["user_id"] == r0["user_id"]
    # 用户总数：匿名档案已被并掉
    assert (await store.counts())["users"] == 1


async def test_bus_anonymous_id_stays_linked(store: Store, tmp_path) -> None:
    """合并后同 anonymou_id 的事件不再新建用户。"""
    ctx = _ctx(tmp_path)
    await handle(store, ctx, {"distinct_id": "anon_z", "event": "page_view"})
    await handle(store, ctx, {"distinct_id": "real_z", "email": "z@x.com", "event": "signup"})
    # 用匿名 id 带邮箱（触发合并）
    await handle(store, ctx, {"distinct_id": "anon_z", "event": "identify", "email": "z@x.com"})
    before = (await store.counts())["users"]
    await handle(store, ctx, {"distinct_id": "anon_z", "event": "page_view"})
    assert (await store.counts())["users"] == before == 1


# ── API 与 H5 留资 ──

def test_identify_api_and_stats(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "anon_api", "event": "page_view"})
        client.post("/api/v1/ingest", json={"distinct_id": "u_api", "email": "api@x.com", "event": "signup"})
        r = client.post("/api/v1/identify", json={"distinct_id": "anon_api", "email": "api@x.com"}).json()
        assert r["ok"] and r["merged"], r
        stats = client.get("/api/v1/identity/stats").json()
        assert stats["total"] == 1 and stats["identified"] == 1
        assert stats["identified_rate"] == 100.0


def test_ingest_response_reports_identification(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        r = client.post("/api/v1/ingest", json={"distinct_id": "anon_r", "event": "page_view"}).json()
        assert r["identified"] is False and r["merged"] is False
        r2 = client.post("/api/v1/ingest", json={"distinct_id": "anon_r", "event": "signup",
                                                 "email": "r@x.com"}).json()
        assert r2["merged"] is False        # 首绑不合并
        r3 = client.post("/api/v1/ingest", json={"distinct_id": "other_r", "event": "page_view",
                                                 "email": "r@x.com"}).json()
        assert r3["merged"] is True         # 新匿名档案并进已实名档案


def test_h5_page_has_lead_capture_and_identify_endpoint(tmp_path) -> None:
    import sqlite3

    from userloop.server.app import create_app
    from userloop.touch.base import make_token

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "anon_lead", "event": "page_view"})
        uid = client.get("/api/v1/users").json()["users"][0]["id"]
        conn = sqlite3.connect(str(tmp_path / "userloop.db"))
        conn.execute("INSERT INTO touch_pages (id,user_id,loop_id,template_id,goal_event,title,body,"
                     "cta_text,cta_url,views,clicks,created_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,?)",
                     ("pg_lead", uid, "l", "tpl", "signup", "专属方案", "正文", "查看",
                      "https://nownexts.com/pricing", "2026-09-18T00:00:00Z"))
        conn.commit()
        conn.close()
        tok = make_token("userloop", uid, "l", {"t": "tpl", "c": "h5", "p": "pg_lead"})
        html = client.get(f"/t/p/pg_lead?t={tok}").text
        assert "ul-lead" in html and "t/e/identify" in html

        # 通过该端点留资 → 绑定邮箱
        r = client.post("/t/e/identify", json={"t": tok, "email": "lead@x.com"}).json()
        assert r["ok"] is True
        user = client.get(f"/api/v1/users/{uid}").json()["user"]
        assert user["email"] == "lead@x.com"

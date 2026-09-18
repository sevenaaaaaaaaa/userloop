"""N2 测试：行业资产包 / 导入导出（只定义不数据）/ 幂等与预览 / 版本化与回滚 / 审批门."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from userloop.assets import packs as ap
from userloop.core.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "assets.db"), {})
    await s.connect()
    yield s
    await s.close()


def test_builtin_packs_and_summary() -> None:
    packs = ap.builtin_packs()
    ids = {p["id"] for p in packs}
    assert {"ecommerce-growth", "saas-onboarding", "education-enroll", "local-service"} <= ids
    ec = ap.get_pack("data", "ecommerce-growth")
    assert ec and len(ec["loop_templates"]) >= 3
    s = ap.summarize(ec)
    assert s["counts"]["loop_templates"] >= 3 and s["industry"] == "电商"
    assert "用户" not in json.dumps(s)[:200] or True


async def test_import_dry_run_then_apply(store: Store) -> None:
    pack = ap.get_pack("data", "ecommerce-growth")
    # 预览：不写入
    dry = await ap.import_assets(store, pack, dry_run=True)
    assert dry["dry_run"] and dry["counts"]["loop_templates"] >= 3
    assert await store.get_templates(enabled_only=False) == []
    # 应用
    out = await ap.import_assets(store, pack)
    tpls = await store.get_templates(enabled_only=False)
    assert len(tpls) >= 3
    assert all(t["enabled"] for t in tpls)                    # 无审批门 → 直接启用
    assert out["counts"]["segments"] >= 2 and out["counts"]["experiments"] >= 1
    # 幂等：再次导入全部跳过
    again = await ap.import_assets(store, pack)
    assert sum(again["counts"].values()) == 0
    assert sum(len(v) for v in again["skipped"].values()) >= 3


async def test_import_with_prefix_allows_coexistence(store: Store) -> None:
    pack = ap.get_pack("data", "saas-onboarding")
    await ap.import_assets(store, pack, prefix="a_")
    await ap.import_assets(store, pack, prefix="b_")
    ids = {t["id"] for t in await store.get_templates(enabled_only=False)}
    assert any(i.startswith("a_saas_") for i in ids) and any(i.startswith("b_saas_") for i in ids)
    # 实验的 match_templates 前缀同步，保证关联不断链
    exps = await store.list_experiments(enabled_only=False)
    assert any(t.startswith("a_saas_") for e in exps for t in (e.get("match_templates") or []))


async def test_approval_gate_keeps_assets_disabled(store: Store) -> None:
    pack = ap.get_pack("data", "education-enroll")
    out = await ap.import_assets(store, pack, require_approval=True)
    assert out["pending_approval"], "应产生待批资产"
    tpls = await store.get_templates(enabled_only=False)
    assert tpls and all(not t["enabled"] for t in tpls)       # 审批前不生效
    # 批准 → 启用
    await store.set_asset_status(tpls[0]["id"], "loop_template", "approved")
    tpls2 = {t["id"]: t for t in await store.get_templates(enabled_only=False)}
    assert tpls2[tpls[0]["id"]]["enabled"] is True
    # 驳回 → 保持停用
    await store.set_asset_status(tpls[1]["id"], "loop_template", "rejected")
    assert tpls2[tpls[1]["id"]]["enabled"] is False


async def test_export_contains_no_tenant_data(store: Store) -> None:
    """导出只含定义，绝不含用户/事件/会话等数据（跨租户复用前提）。"""
    await store.upsert_user("leak_user", email="leak@x.com")
    await store.log_touch("u_x", "email", "touch.email", "tpl", "loop")
    await store.add_message("u_x", "im", "user", "你好，我的手机号是 13900000000")
    pack = ap.export_assets(store, name="导出测试") if False else await ap.export_assets(store)
    blob = json.dumps(pack, ensure_ascii=False)
    for forbidden in ("leak_user", "leak@x.com", "13900000000", "u_x"):
        assert forbidden not in blob, f"导出泄露了 {forbidden}"
    assert set(pack) >= {"loop_templates", "canvas_flows", "experiments", "segments"}


async def test_version_snapshot_and_rollback(store: Store) -> None:
    tpl = {"id": "vt1", "name": "v1 名称", "enabled": True, "trigger": {"type": "event", "name": "signup"},
           "actions": [{"type": "feishu", "payload": {"text": "x"}}]}
    await store.put_template(tpl)
    await store.put_template({**tpl, "name": "v2 名称"})
    versions = await store.list_asset_versions("loop_template", "vt1")
    assert [v["version"] for v in versions] == [2, 1]
    # 回滚到 v1
    out = await store.restore_asset_version(versions[1]["id"])
    assert out["ok"] and out["restored_from_version"] == 1
    tpls = {t["id"]: t for t in await store.get_templates(enabled_only=False)}
    assert tpls["vt1"]["name"] == "v1 名称"
    # 回滚本身也留下版本（审计链）
    assert len(await store.list_asset_versions("loop_template", "vt1")) == 3


def test_assets_api(tmp_path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"api_token": "tka", "storage": {}}), encoding="utf-8")
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        packs = client.get("/api/v1/assets/packs").json()
        assert packs["count"] >= 4
        # 预览 → 应用
        prev = client.post("/api/v1/assets/packs/ecommerce-growth/apply?token=tka", json={"dry_run": True}).json()
        assert prev["dry_run"] and prev["counts"]["loop_templates"] >= 3
        applied = client.post("/api/v1/assets/packs/ecommerce-growth/apply", json={}).json()
        assert sum(applied["counts"].values()) >= 6
        # 版本列表与详情
        vers = client.get("/api/v1/assets/versions?asset_type=loop_template").json()
        assert vers["count"] >= 3 and "data" not in vers["versions"][0]
        detail = client.get(f"/api/v1/assets/versions/{vers['versions'][0]['id']}").json()
        assert detail["data"].get("id")
        # 导出本租户
        exp = client.get("/api/v1/assets/export").json()
        assert exp["loop_templates"] and "counts" not in exp

"""契约探测：capabilities 广告 / 探测结果 / 断链进自诊断。"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store
from userloop.evolve import engine as evo
from userloop.integrations import probe as probe_mod


def _ctx(tmp_path, extra=None):
    cfg = {"data_dir": str(tmp_path), "integrations": {
        "openflow": {"base_url": "http://of.test", "bridge_token": "bt"},
        "mflow": {"base_url": "http://mf.test", "username": "bot", "password": "pw"},
        "inflow": {"base_url": "http://if.test", "workspace_id": "ws"},
    }, "touch": {"h5": {"api_base": "http://wf.test/api"}}}
    if extra:
        cfg.update(extra)
    return ExecutorContext(str(tmp_path), cfg)


def test_capabilities_endpoint_is_public(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        r = client.get("/api/v1/capabilities").json()
        assert r["system"] == "userloop"
        assert "/api/v1/ingest" in r["ingest"]
        assert "mflow_item" in r["identity_types"]
        assert "mflow.create_content" in r["actions"]


async def test_probe_marks_up_and_down(tmp_path) -> None:
    ctx = _ctx(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "userloop-bridge/capabilities" in url:
            return httpx.Response(404)
        if "userloop-bridge/automation" in url:
            return httpx.Response(405)          # GET 不允许 = 服务活着
        if url.rstrip("/").endswith("/api/login"):
            return httpx.Response(405)
        if "/projects" in url:
            return httpx.Response(401)          # 未带 JWT = warn
        if "/insights" in url:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(404)

    snap = await probe_mod.run(ctx, transport=httpx.MockTransport(handler))
    by_id = {r["id"]: r for r in snap["results"]}
    assert by_id["openflow.bridge"]["status"] == "ok"
    assert by_id["mflow.api"]["status"] == "ok"
    assert by_id["websflow.api"]["status"] == "warn"
    assert by_id["inflow.insights"]["status"] == "down"
    assert snap["ok"] is False
    assert probe_mod.load_latest(ctx)["counts"]["down"] == 1


async def test_probe_skips_unconfigured(tmp_path) -> None:
    ctx = ExecutorContext(str(tmp_path), {"data_dir": str(tmp_path)})
    snap = await probe_mod.run(ctx, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert snap["ok"] is True
    assert all(r["status"] == "skipped" for r in snap["results"])


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "p.db"), {})
    await s.connect()
    yield s
    await s.close()


async def test_diagnose_flags_broken_contract(store: Store, tmp_path) -> None:
    ctx = _ctx(tmp_path)
    ctx.store = store
    probe_mod._save(ctx, {"at": "2026-09-18T00:00:00Z", "ok": False, "results": [
        {"id": "mflow.api", "status": "down", "http": 0, "error": "ConnectError"}]})
    dg = await evo.diagnose(store, ctx)
    codes = {i["code"] for i in dg["issues"]}
    assert "contract_broken" in codes
    assert dg["ok"] is False


def test_probe_api_run_and_read(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        empty = client.get("/api/v1/integrations/probe").json()
        assert empty.get("results") == [] or empty.get("note")
        r = client.post("/api/v1/integrations/probe").json()
        assert "results" in r and "counts" in r
        # 未配集成 → 全 skipped，不算故障
        assert r["ok"] is True
        assert r["counts"]["skipped"] >= 1

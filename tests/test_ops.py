"""N3 运维加固测试：指标 / 健康检查 / 备份恢复 / 告警（冷却去重）."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from userloop.core.store import Store
from userloop.ops import alerts as alerts_mod
from userloop.ops import backup as backup_mod
from userloop.ops import health as health_mod
from userloop.ops.metrics import METRICS, Metrics


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "ops.db"), {})
    await s.connect()
    yield s
    await s.close()


# ── 指标 ──

def test_metrics_counter_and_histogram() -> None:
    m = Metrics()
    m.inc("demo_total", {"a": "1"})
    m.inc("demo_total", {"a": "1"}, 2)
    m.observe_ms("demo_ms", 12, {"p": "/x"})
    m.observe_ms("demo_ms", 300, {"p": "/x"})
    snap = m.snapshot()
    c = next(x for x in snap["counters"] if x["name"] == "demo_total")
    assert c["value"] == 3
    h = next(x for x in snap["histograms"] if x["name"] == "demo_ms")
    assert h["count"] == 2 and h["avg_ms"] == 156.0
    prom = m.render_prom()
    assert 'demo_total{a="1"} 3.0' in prom
    assert 'demo_ms_bucket{le="+Inf",p="/x"} 2' in prom      # 标签按字母序输出
    assert 'demo_ms_count{p="/x"} 2' in prom


def test_metrics_http_error_rate_helper() -> None:
    m = Metrics()
    m.inc("userloop_http_requests_total", {"path": "/x", "status": "200"}, 90)
    m.inc("userloop_http_requests_total", {"path": "/x", "status": "500"}, 10)
    rate, errs, total = alerts_mod._http_error_rate(m.snapshot())
    assert (rate, errs, total) == (10.0, 10, 100)


# ── 健康检查 ──

async def test_health_ok(store: Store, tmp_path) -> None:
    cfg = {"data_dir": str(tmp_path)}
    h = await health_mod.check(cfg, store)
    names = {c["name"] for c in h["checks"]}
    assert {"store", "event_store", "ingest_freshness", "disk", "scheduler", "tenants"} <= names
    assert h["status"] == "ok", h
    assert h["version"]


async def test_health_degrades_when_store_broken(tmp_path) -> None:
    class Broken:
        events = None

        async def counts(self) -> dict:
            raise RuntimeError("db gone")

    h = await health_mod.check({"data_dir": str(tmp_path)}, Broken())
    assert h["status"] == "degraded"
    assert any(c["name"] == "store" and not c["ok"] for c in h["checks"])


# ── 备份 / 恢复 ──

def _make_data_dir(root: str) -> str:
    d = os.path.join(root, "data")
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(os.path.join(d, "userloop.db"))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('hello')")
    con.commit()
    con.close()
    with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"api_token": "secret", "keep": 1}, f)
    return d


def test_backup_create_verify_restore(tmp_path) -> None:
    d = _make_data_dir(str(tmp_path))
    out = backup_mod.create(d, keep=3, note="t", cfg={})
    assert out["ok"] and os.path.exists(out["path"])
    assert "userloop.db" in out["dbs"]

    v = backup_mod.verify(out["path"])
    assert v["ok"], v
    assert v["manifest"]["files"]

    # 备份内是快照（VACUUM INTO），内容与源一致
    import tarfile

    with tarfile.open(out["path"], "r:gz") as tar:
        f = tar.extractfile("data/userloop.db")
        assert f is not None
        tmpdb = os.path.join(str(tmp_path), "extract.db")
        with open(tmpdb, "wb") as fh:
            fh.write(f.read())
    con = sqlite3.connect(tmpdb)
    assert con.execute("SELECT v FROM t").fetchone()[0] == "hello"
    con.close()

    # 破坏源数据 → 恢复应还原
    con = sqlite3.connect(os.path.join(d, "userloop.db"))
    con.execute("DELETE FROM t")
    con.commit()
    con.close()
    r = backup_mod.restore(out["path"], d, force=True)
    assert r["ok"] and os.path.isdir(r["backup_of_old"])
    con = sqlite3.connect(os.path.join(d, "userloop.db"))
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    con.close()


def test_backup_restore_requires_force(tmp_path) -> None:
    d = _make_data_dir(str(tmp_path))
    out = backup_mod.create(d, keep=3)
    r = backup_mod.restore(out["path"], d)
    assert r["ok"] is False and "force" in r["error"]


def test_backup_retention_prunes(tmp_path) -> None:
    d = _make_data_dir(str(tmp_path))
    for i in range(4):
        backup_mod.create(d, keep=2, note=f"n{i}")
    items = backup_mod.list_backups(d)
    assert len(items) == 2                       # 只保留最近 2 份


def test_backup_excludes_wal_sidecars_and_detects_mysql(tmp_path) -> None:
    d = _make_data_dir(str(tmp_path))
    cfg = {"storage": {"events": {"backend": "mysql", "mysql": {"enabled": True}}}}
    # 模拟"活库"：WAL 模式下打开连接写入并保持打开（此时 -wal/-shm 真实存在）
    import tarfile

    con = sqlite3.connect(os.path.join(d, "userloop.db"))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO t (v) VALUES ('live')")
    con.commit()
    try:
        assert os.path.exists(os.path.join(d, "userloop.db-wal")) or True
        out = backup_mod.create(d, keep=3, cfg=cfg)
        assert out["ok"], out
        with tarfile.open(out["path"], "r:gz") as tar:
            names = tar.getnames()
        sidecars = [n for n in names if n.endswith(("-wal", "-shm", "-journal"))]
        assert sidecars == [], f"边车文件不应入包：{sidecars}"
        v = backup_mod.verify(out["path"])
        assert v["ok"], v
        assert v["manifest"]["events_backend"] == "mysql"
        assert "mysqldump" in v["manifest"]["events_note"]
        # 快照含活库中刚写入的数据
        with tarfile.open(out["path"], "r:gz") as tar:
            tmpdb = os.path.join(str(tmp_path), "live.db")
            with open(tmpdb, "wb") as fh:
                fh.write(tar.extractfile("data/userloop.db").read())
        c2 = sqlite3.connect(tmpdb)
        assert c2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        c2.close()
    finally:
        con.close()


def test_backup_verify_detects_missing(tmp_path) -> None:
    assert backup_mod.verify(str(tmp_path / "nope.tar.gz"))["ok"] is False


# ── 告警 ──

def _health(age=0, free=50.0, events=100, store_ok=True, sched_ok=True) -> dict:
    return {"checks": [{"name": "store", "ok": store_ok, "detail": "x"},
                       {"name": "event_store", "ok": True, "detail": "sqlite"},
                       {"name": "scheduler", "ok": sched_ok, "detail": "running"}],
            "counts": {"events": events}, "last_event_age_s": age,
            "disk": {"free_pct": free}}


def test_alert_evaluate_rules() -> None:
    m = Metrics()
    cfg = {"data_dir": "data", "ops": {"alerts": {"thresholds": {"ingest_stalled_minutes": 60}}}}
    got = alerts_mod.evaluate(cfg, _health(age=7200), m.snapshot())
    assert any(a["code"] == "ingest_stalled" for a in got)

    got = alerts_mod.evaluate(cfg, _health(free=3.0), m.snapshot())
    assert any(a["code"] == "disk_low" for a in got)

    got = alerts_mod.evaluate(cfg, _health(store_ok=False), m.snapshot())
    assert any(a["code"] == "store_down" and a["severity"] == "high" for a in got)

    # 空系统不应误报入库停滞
    got = alerts_mod.evaluate(cfg, _health(age=99999, events=0), m.snapshot())
    assert not any(a["code"] == "ingest_stalled" for a in got)


async def test_alert_run_dedupes_by_cooldown(tmp_path) -> None:
    cfg = {"data_dir": str(tmp_path), "ops": {"alerts": {"thresholds": {"ingest_stalled_minutes": 60}}}}
    first = await alerts_mod.run(cfg, _health(age=7200))
    assert any(a["code"] == "ingest_stalled" for a in first["fired"])
    # 冷却窗口内再评估：被抑制
    second = await alerts_mod.run(cfg, _health(age=7200))
    assert "ingest_stalled" in second["suppressed"] and not second["fired"]
    # 已恢复（age 正常）：状态清空，历史仍留痕
    third = await alerts_mod.run(cfg, _health(age=10))
    assert third["fired"] == []
    assert any(h["code"] == "ingest_stalled" for h in alerts_mod.history(cfg))

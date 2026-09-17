"""P2 A/B 版式实验测试：分桶稳定性 / 版式覆盖 / 指标归因 / 显著性 / winner 提升 / API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from userloop.core.store import Store
from userloop.experiments import engine as ab
from userloop.touch.base import TouchSpec


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "ab.db"), {})
    await s.connect()
    await ab.seed_experiments(s, str(tmp_path))
    yield s
    await s.close()


def test_two_proportion_confidence() -> None:
    assert ab.two_proportion_confidence(5, 10, 5, 10) == 0.0          # 相同比例
    high = ab.two_proportion_confidence(10, 100, 30, 100)             # 10% vs 30%
    assert high > 0.99
    low = ab.two_proportion_confidence(10, 100, 13, 100)              # 差异很小
    assert low < 0.8
    assert ab.two_proportion_confidence(1, 3, 2, 3) == 0.0            # 样本不足


def test_apply_variant_overrides_slots() -> None:
    spec = TouchSpec(channel="email", title="原标题", body="正文", cta_text="查看")
    ab.apply_variant(spec, {"id": "B", "overrides": {"cta_text": "立即完成", "title_suffix": "｜限今天",
                                                     "body_prefix": "【重要】"}})
    assert spec.cta_text == "立即完成"
    assert spec.title == "原标题｜限今天"
    assert spec.body.startswith("【重要】")
    assert spec.vars["_variant"] == "B"


async def test_stable_bucketing_same_user_same_variant(store: Store) -> None:
    seen = {}
    for _ in range(5):
        picked = await ab.pick(store, "email", "signup_no_activate", "user-42", "loop1")
        assert picked is not None
        exp, variant = picked
        seen.setdefault(exp["id"], set()).add(variant["id"])
    assert all(len(v) == 1 for v in seen.values()), "同一用户必须始终落在同一变体"


async def test_bucketing_distribution_and_recording(store: Store) -> None:
    for i in range(200):
        await ab.pick(store, "email", "signup_no_activate", f"u{i}", "loop1")
    counts = await store.assignment_counts("email_cta_urgency")
    assert set(counts) == {"A", "B"}
    # 50/50 权重下 200 人分桶应大致均衡（宽松断言，避免随机性误报）
    assert abs(counts["A"] - counts["B"]) < 80


async def test_promote_forces_winner(store: Store) -> None:
    exp = await store.get_experiment("email_cta_urgency")
    exp["promoted_variant"] = "B"
    await store.put_experiment(exp)
    for i in range(10):
        _, variant = await ab.pick(store, "email", "signup_no_activate", f"pu{i}", "l")  # type: ignore[misc]
        assert variant["id"] == "B"
    assert variant["overrides"]["cta_text"] == "立即完成（限今天）"


async def test_evaluate_insufficient_then_winner(store: Store) -> None:
    # 样本不足
    r0 = await ab.evaluate(store, "email_cta_urgency")
    assert r0["verdict"] == "insufficient"

    exp = await store.get_experiment("email_cta_urgency")
    exp["min_samples"] = 30
    exp["confidence"] = 0.95
    await store.put_experiment(exp)

    # 造样本：A/B 各 40 人（用内部 user_id 分桶，和线上一致）
    ids = {"A": [], "B": []}
    for variant in ("A", "B"):
        for i in range(40):
            u = await store.upsert_user(f"{variant}{i}", email=f"{variant}{i}@x.com")
            ids[variant].append(u["id"])
            await store.record_assignment({"experiment_id": exp["id"], "variant": variant,
                                           "user_id": u["id"], "channel": "email"})

    # A 组 5 人点击，B 组 20 人点击（B 显著更好）
    for variant, clicks in (("A", 5), ("B", 20)):
        for i in range(clicks):
            await store.insert_event({"user_id": ids[variant][i], "distinct_id": f"{variant}{i}",
                                      "event": "email_click", "props": {}, "source": "test",
                                      "event_id": f"click-{variant}-{i}", "created_at": "2026-09-17T00:00:00Z"})

    r = await ab.evaluate(store, "email_cta_urgency")
    assert r["verdict"] == "winner" and r["leader"] == "B"
    assert r["confidence"] >= 0.95
    assert r["metrics"]["B"]["click_rate"] > r["metrics"]["A"]["click_rate"]

    p = await ab.promote(store, "email_cta_urgency")
    assert p["ok"] and p["promoted"] == "B"
    assert "experiment.email_cta_urgency" in await store.feedback_stats()


async def test_touch_dispatch_applies_experiment(tmp_path) -> None:
    """触点分发时自动分桶 + 覆盖 CTA，并在结果里回报 ab 信息。"""
    from userloop.actions.executors import ExecutorContext, execute_action

    store = Store(str(tmp_path / "t.db"), {})
    await store.connect()
    await ab.seed_experiments(store, str(tmp_path))
    try:
        u = await store.upsert_user("ab_touch", email="ab@x.com")
        ctx = ExecutorContext(str(tmp_path), {"touch": {"track_secret": "s", "public_base": "https://ul.test"},
                                             "api_token": "s"})
        ctx.store = store
        res = await execute_action(ctx, {"type": "touch.h5",
                                         "payload": {"title": "你的权益", "text": "点开", "cta_text": "查看",
                                                     "cta_url": "https://nownexts.com/pricing"}},
                                   {"id": "loop_ab", "template_id": "tpl"}, u)
        assert res["ok"] and "ab" in res
        assert res["ab"]["experiment"] == "h5_hero_style"
        assert res["ab"]["variant"] in ("A", "B")
        assert res["spec"]["cta"] in ("领取权益", "马上开始")   # 变体覆盖了 CTA
    finally:
        await store.close()


def test_experiments_api(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json={"distinct_id": "e1", "email": "e1@x.com", "event": "signup"})
        r = client.get("/api/v1/experiments").json()
        assert r["count"] >= 2
        exp = next(e for e in r["experiments"] if e["id"] == "email_cta_urgency")
        assert "verdict" in exp and "metrics" in exp
        ev = client.post("/api/v1/experiments/email_cta_urgency/evaluate").json()
        assert ev["verdict"] in ("insufficient", "running", "winner")
        pm = client.post("/api/v1/experiments/email_cta_urgency/promote", json={}).json()
        assert pm["ok"] is False or pm["promoted"]        # 无显著 winner 时拒绝提升

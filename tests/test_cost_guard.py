"""成本护栏与执行日历测试：token 计量 / 日预算 / 大户上限 / 工作日窗口 / 每日简报."""

from __future__ import annotations

from datetime import datetime

import pytest

from userloop.billing import tokens as tok
from userloop.core.store import Store
from userloop.ops import digest as digest_mod
from userloop.ops import workdays

CFG = {"ai": {"api_key": "k", "caller_caps": {"brain": 10_000_000}},
       "digest": {"recipients": ["admin@x.com"]}}


def _cfg_with(tmp_path) -> dict:
    return {**CFG, "data_dir": str(tmp_path)}


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "cost.db"), {})
    await s.connect()
    yield s
    await s.close()


# ── token 记账与聚合 ──

async def test_token_record_and_daily_hourly_top(store) -> None:
    await tok.record(store, CFG, caller="brain", model="deepseek-chat",
                     prompt_tokens=1_000_000, completion_tokens=250_000)
    await tok.record(store, CFG, caller="copilot", model="deepseek-chat",
                     prompt_tokens=100_000, completion_tokens=50_000)
    daily = await store.token_usage_daily(days=7)
    assert len(daily) == 1
    assert daily[0]["tokens"] == 1_400_000
    # 成本：1M*2/1M + 250k*8/1M + (100k*2+50k*8)/1M = 2+2 + 0.2+0.4 ≈ 4.6 元
    assert daily[0]["cost"] == pytest.approx(4.6)
    hourly = await store.token_usage_hourly()
    assert len(hourly) == 1 and hourly[0]["calls"] == 2
    top = await store.token_usage_top_callers(limit=5)
    assert top[0]["caller"] == "brain"


async def test_budget_exceeded_gate_and_alert_once(store, tmp_path, monkeypatch) -> None:
    cfg = _cfg_with(tmp_path)
    sent = []
    monkeypatch.setattr("userloop.actions.executors.send_email",
                        lambda ctx, to, subject, text: sent.append((to, subject)) or {"ok": True})
    # 5 元默认预算：花 4.6 元未超
    await tok.record(store, cfg, caller="brain", model="m", prompt_tokens=1_000_000,
                     completion_tokens=250_000)
    st = await tok.budget_status(store, cfg)
    assert not st["exceeded"]
    g = await tok.gate(store, cfg, caller="brain")
    assert g["ok"]
    # 再花 1 元 → 超支 + 告警邮件 1 封
    await tok.record(store, cfg, caller="brain", model="m", prompt_tokens=500_000,
                     completion_tokens=0)
    st = await tok.budget_status(store, cfg)
    assert st["exceeded"] and st["remaining"] == 0
    # 门禁拦截
    g = await tok.gate(store, cfg, caller="brain")
    assert not g["ok"] and g["budget_exceeded"]
    assert len(sent) == 1 and sent[0][0] == "admin@x.com"
    # 超支邮件每自然日最多一次：继续消费不再发
    await tok.record(store, cfg, caller="copilot", model="m", prompt_tokens=10_000,
                     completion_tokens=0)
    assert len(sent) == 1


async def test_budget_override_single_day(store) -> None:
    await store.set_budget_override(datetime.utcnow().date().isoformat(), 20.0, note="大促")
    st = await tok.budget_status(store, CFG)
    assert st["limit"] == 25.0 and st["base_limit"] == 5.0 and st["extra"] == 20.0
    # 第二天：override 不生效（按日键）→ 自动回落
    import datetime as dt

    tomorrow = (dt.datetime.utcnow() + dt.timedelta(days=1)).date().isoformat()
    assert await store.get_budget_override(tomorrow) == 0.0


async def test_caller_cap_degrades_biggest_spender(store) -> None:
    cfg = {**CFG, "ai": {"api_key": "k", "caller_caps": {"brain": 100}}}
    await tok.record(store, cfg, caller="brain", model="m", prompt_tokens=500,
                     completion_tokens=0)
    g = await tok.gate(store, cfg, caller="brain")
    assert not g["ok"] and g["cap_exceeded"]
    # 其他调用方不受影响
    g2 = await tok.gate(store, cfg, caller="copilot")
    assert g2["ok"]


# ── 执行日历 ──

def test_workday_gate_blocks_weekend_and_holiday() -> None:
    cfg = {"ops": {"calendar": {"tz_offset_hours": 8}}}
    sat_utc = datetime(2026, 10, 10, 2, 0)     # 周六 10:00 北京时间
    assert not workdays.is_workday(cfg, sat_utc)
    g = workdays.job_allowed(cfg, "actions", sat_utc)
    assert not g["allowed"]
    holiday = datetime(2026, 10, 1, 2, 0)      # 国庆（周四）
    assert not workdays.is_workday(cfg, holiday)
    # 周三工作日
    wed = datetime(2026, 10, 7, 2, 0)          # 但 10-07 是国庆调休外最后一天
    assert not workdays.is_workday(cfg, wed)   # 2026-10-07 仍是国庆假期
    thu = datetime(2026, 10, 8, 2, 0)          # 10-08 周四，假期结束
    assert workdays.is_workday(cfg, thu)


def test_night_window_for_unimportant_jobs() -> None:
    cfg = {"ops": {"calendar": {"tz_offset_hours": 8}}}
    wed_02_utc = datetime(2026, 10, 8, 2, 0)   # 本地 10:00 工作日白天
    g = workdays.job_allowed(cfg, "train", wed_02_utc)
    assert not g["allowed"] and not g["important"]
    wed_17_utc = datetime(2026, 10, 8, 17, 0)  # 本地 01:00 夜间窗口
    g2 = workdays.job_allowed(cfg, "train", wed_17_utc)
    assert g2["allowed"] and g2["deadline_s"] and g2["deadline_s"] <= 5 * 3600
    # 重要任务白天可跑
    g3 = workdays.job_allowed(cfg, "actions", wed_02_utc)
    assert g3["allowed"] and g3["important"]


def test_night_deadline_enforced() -> None:
    cfg = {"ops": {"calendar": {"tz_offset_hours": 8}}}
    # 本地 05:59（UTC 21:59 前一天）→ 剩余约 60 秒
    remaining = workdays.night_deadline_seconds(cfg, datetime(2026, 10, 8, 21, 59))
    assert 0 < remaining <= 120


# ── 每日简报 ──

async def test_digest_collect_and_render(store) -> None:
    day = datetime.utcnow().date().isoformat()
    await store.add_token_usage("brain", "m", 1000, 500, 0.01,
                                f"{day}T01:00:00Z", day, 1)
    d = await digest_mod.collect(store, CFG, hours=12)
    assert d["exec_12h"] and "tokens" in d and d["tokens"]["budget"]["limit"] == 5.0
    md = digest_mod._render(d, "morning")
    assert "清晨简报" in md and "token 消耗" in md and "消耗大户" in md
    md2 = digest_mod._render(d, "evening")
    assert "傍晚简报" in md2


async def test_digest_send_without_smtp_returns_data(store, tmp_path) -> None:
    cfg = {**CFG, "data_dir": str(tmp_path)}
    from userloop.actions.executors import ExecutorContext

    ctx = ExecutorContext(str(tmp_path), cfg)  # 无 SMTP 配置 → dry_run
    out = await digest_mod.send_digest(store, ctx, variant="morning")
    assert out["ok"] and out["sent"]["admin@x.com"] is True
    assert out["path"].endswith("-morning.md")


def test_default_2026_holidays_loaded() -> None:
    assert "2026-10-01" in workdays.DEFAULT_HOLIDAYS_2026
    c = workdays._cfg({})
    assert c["night_start"] == 0 and c["night_end"] == 6
    assert "actions" in c["important_jobs"] and "digest" not in c["important_jobs"]

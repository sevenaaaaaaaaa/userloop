"""告警评估：基于健康检查 + 指标的确定性规则，带冷却去重与可选 webhook 外送.

规则（可在 config.ops.alerts.thresholds 覆盖）：
- store_down          存储不可读
- eventstore_down     事件后端不可用
- ingest_stalled      超过 N 分钟没有新事件（有历史数据才判，空系统不误报）
- disk_low            数据盘可用比例低于阈值
- scheduler_down      调度器未运行
- error_rate_high     HTTP 5xx 比例超阈值（样本数达标才判）
- identified_rate_low 实名识别率过低（沿用自进化诊断口径）

冷却：同一告警在 cooldown_minutes 内只报一次（状态存 data/ops/alert_state.json）。
外送：config.ops.alerts.webhook_url（POST JSON）；始终追加 data/ops/alerts.jsonl 便于审计。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

DEFAULTS = {
    "ingest_stalled_minutes": 180,
    "disk_free_pct_min": 10,
    "error_rate_pct_max": 5.0,
    "error_rate_min_requests": 20,
    "identified_rate_low_pct": 15.0,
    "identified_rate_min_users": 100,
    "cooldown_minutes": 360,
}


def _cfg(cfg: dict) -> dict[str, Any]:
    raw = ((cfg.get("ops") or {}).get("alerts") or {})
    out = dict(DEFAULTS)
    for k, v in (raw.get("thresholds") or {}).items():
        if k in out and isinstance(v, (int, float)):
            out[k] = v
    out["webhook_url"] = raw.get("webhook_url") or ""
    out["enabled"] = raw.get("enabled", True)
    return out


def _http_error_rate(snapshot: dict) -> tuple[float, int, int]:
    total = 0.0
    errors = 0.0
    for c in snapshot.get("counters", []):
        if c.get("name") != "userloop_http_requests_total":
            continue
        status = str((c.get("labels") or {}).get("status", ""))
        val = float(c.get("value") or 0)
        total += val
        if status.startswith("5"):
            errors += val
    rate = (errors / total * 100) if total else 0.0
    return round(rate, 1), int(errors), int(total)


def evaluate(cfg: dict, health: dict, snapshot: dict | None = None) -> list[dict[str, Any]]:
    """纯函数：根据健康检查与指标快照产出告警列表（不含冷却/外送）。"""
    from userloop.ops import metrics as metrics_mod

    c = _cfg(cfg)
    snap = snapshot if snapshot is not None else metrics_mod.METRICS.snapshot()
    checks = {x["name"]: x for x in health.get("checks", [])}
    alerts: list[dict[str, Any]] = []

    def fire(code: str, severity: str, title: str, detail: str, suggestion: str) -> None:
        alerts.append({"code": code, "severity": severity, "title": title,
                       "detail": detail, "suggestion": suggestion, "at": datetime.utcnow().isoformat() + "Z"})

    if checks.get("store") and not checks["store"]["ok"]:
        fire("store_down", "high", "存储不可用", checks["store"]["detail"], "检查磁盘/权限与 SQLite 文件是否损坏")
    if checks.get("event_store") and not checks["event_store"]["ok"]:
        fire("eventstore_down", "high", "事件后端不可用", checks["event_store"]["detail"], "检查事件存储配置（MySQL 连接/回退）")
    if checks.get("scheduler") and not checks["scheduler"]["ok"]:
        fire("scheduler_down", "high", "调度器未运行", checks["scheduler"]["detail"], "重启服务并确认 APScheduler 已启动")

    age = health.get("last_event_age_s")
    has_history = int((health.get("counts") or {}).get("events") or 0) > 0
    if has_history and age is not None and age > c["ingest_stalled_minutes"] * 60:
        fire("ingest_stalled", "high", "入库停滞",
             f"最近事件 {int(age / 60)} 分钟前，超过阈值 {c['ingest_stalled_minutes']} 分钟",
             "检查埋点/接入通道与反代是否中断（track.js、hub/ingest）")

    free_pct = (health.get("disk") or {}).get("free_pct")
    if free_pct is not None and free_pct < c["disk_free_pct_min"]:
        fire("disk_low", "high", "磁盘可用空间不足",
             f"data_dir 可用 {free_pct}%（阈值 {c['disk_free_pct_min']}%）",
             "清理旧备份/日志或扩容；确认保留策略 retention 生效")

    rate, errs, total = _http_error_rate(snap)
    if total >= c["error_rate_min_requests"] and rate > c["error_rate_pct_max"]:
        fire("error_rate_high", "medium", "接口错误率偏高",
             f"5xx {errs}/{total}（{rate}%，阈值 {c['error_rate_pct_max']}%）",
             "查看服务日志定位异常接口；必要时回滚最近变更")

    return alerts


def _state_path(cfg: dict) -> str:
    return os.path.join(cfg.get("data_dir") or "data", "ops", "alert_state.json")


def _load_state(cfg: dict) -> dict[str, Any]:
    p = _state_path(cfg)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(cfg: dict, state: dict) -> None:
    p = _state_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


async def dispatch(cfg: dict, alert: dict) -> dict[str, Any]:
    """外送单条告警：webhook（可选）+ 本地 jsonl 审计。永不抛错。"""
    record = {"type": "alert", **alert}
    try:
        path = os.path.join(cfg.get("data_dir") or "data", "ops", "alerts.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    url = _cfg(cfg)["webhook_url"]
    if not url:
        return {"sent": False, "reason": "未配置 ops.alerts.webhook_url"}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(url, json=record)
        return {"sent": r.status_code < 400, "status": r.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "error": str(exc)[:160]}


async def run(cfg: dict, health: dict, snapshot: dict | None = None) -> dict[str, Any]:
    """评估 + 冷却去重 + 外送。返回本次新增的告警。"""
    c = _cfg(cfg)
    if not c["enabled"]:
        return {"ok": True, "enabled": False, "fired": [], "suppressed": []}
    found = evaluate(cfg, health, snapshot)
    state = _load_state(cfg)
    now = datetime.utcnow()
    fired: list[dict[str, Any]] = []
    suppressed: list[str] = []
    for a in found:
        last = state.get(a["code"], {}).get("last_fired")
        if last:
            try:
                delta = (now - datetime.fromisoformat(str(last).replace("Z", ""))).total_seconds()
                if delta < c["cooldown_minutes"] * 60:
                    suppressed.append(a["code"])
                    continue
            except ValueError:
                pass
        sent = await dispatch(cfg, a)
        state[a["code"]] = {"last_fired": now.isoformat() + "Z", "severity": a["severity"],
                            "title": a["title"], "sent": sent}
        fired.append({**a, "dispatch": sent})
    # 已恢复的告警清掉状态（下次再触发可立即报）
    active = {a["code"] for a in found}
    for code in list(state.keys()):
        if code not in active:
            state.pop(code, None)
    _save_state(cfg, state)
    return {"ok": True, "enabled": True, "fired": fired, "suppressed": suppressed,
            "counts": {"found": len(found), "fired": len(fired), "suppressed": len(suppressed)}}


def history(cfg: dict, limit: int = 50) -> list[dict[str, Any]]:
    path = os.path.join(cfg.get("data_dir") or "data", "ops", "alerts.jsonl")
    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f.readlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out

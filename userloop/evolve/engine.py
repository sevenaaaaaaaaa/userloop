"""自进化机制（对齐 OpenFlow/MFlow 的迭代飞轮，并用自身能力进化自身）.

四件套 + 一条特色：
  ① 版本化交付：VERSION + CHANGELOG（deploy/release.sh）
  ② 运行数据回流：telemetry 快照落 data/telemetry/*.jsonl
  ③ Lessons：错误只犯一次（evolution_lessons → docs/LESSONS.md）
  ④ Demo 驱动验收：每个能力都有 CLI/控制台一键 demo（既有实践）
  ⑤ 自诊断 + AI 提案 + 审批应用：用自身 AI 大脑与审批门改造自身

本模块提供：collect_telemetry / diagnose / propose / apply_proposal / lessons。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from userloop.actions.executors import ExecutorContext
from userloop.core.store import Store, iso_now, new_id

def _get_path(cfg: dict, spec: tuple[str, ...]) -> tuple[bool, Any]:
    """按键路径取值；返回 (是否存在, 值)。**区分"缺失"与"空值"**，决定回滚是删除还是恢复。"""
    cur: Any = cfg
    for node in spec:
        if not isinstance(cur, dict) or node not in cur:
            return False, None
        cur = cur[node]
    return True, cur


def _set_path(cfg: dict, spec: tuple[str, ...], value: Any) -> None:
    cur: Any = cfg
    for node in spec[:-1]:
        cur = cur.setdefault(node, {})
    cur[spec[-1]] = value


def _del_path(cfg: dict, spec: tuple[str, ...]) -> None:
    cur: Any = cfg
    for node in spec[:-1]:
        if not isinstance(cur.get(node), dict):
            return
        cur = cur[node]
    cur.pop(spec[-1], None)


# 允许自进化自动应用的配置项（白名单：只改参数，不改逻辑）
APPLY_WHITELIST: dict[str, tuple[str, tuple[str, ...]]] = {
    "frequency.weekly_cap": ("touch", "frequency", "weekly_cap"),
    "frequency.global_gap_hours": ("touch", "frequency", "global_gap_hours"),
    "sto.window_hours": ("touch", "sto", "window_hours"),
    "sto.max_delay_hours": ("touch", "sto", "max_delay_hours"),
    "brain.batch_size": ("ai", "brain", "batch_size"),
    "brain.min_gap_hours": ("ai", "brain", "min_gap_hours"),
    "brain.daily_budget": ("ai", "brain", "daily_budget"),
    "predict.batch_size": ("predict", "batch_size"),
    "report.pending_alert_threshold": ("report", "pending_alert_threshold"),
    "qc.mode": ("touch", "qc", "mode"),
}


# ── ② 运行数据回流 ──

async def collect_telemetry(store: Store, ctx: ExecutorContext) -> dict[str, Any]:
    """采集运行遥测（只读）并落盘 data/telemetry/YYYY-MM-DD.jsonl。"""
    cfg = ctx.config
    counts = await store.counts()
    ai_counts = await store.ai_decision_counts()
    blocks = await store.block_stats(days=1)
    loops = await store.count_loops_by_status()

    cur = await store.db.execute(
        "SELECT status, COUNT(*) c FROM actions WHERE executed_at>=? GROUP BY status",
        ((datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds") + "Z",))
    action_stats = {r["status"]: r["c"] for r in await cur.fetchall()}

    cur = await store.db.execute("SELECT COUNT(*) c FROM touch_log WHERE created_at>=?",
                                 ((datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds") + "Z",))
    touches_24h = int((await cur.fetchone())["c"])
    cur = await store.db.execute("SELECT COUNT(*) c FROM touch_blocks WHERE created_at>=?",
                                 ((datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds") + "Z",))
    blocks_24h = int((await cur.fetchone())["c"])

    stages = await store.count_users_by_stage()
    total = sum(stages.values()) or 1
    identified = total - stages.get("visitor", 0)

    from userloop.predict import model as mdl

    models = {k: (mdl.load(cfg.get("data_dir") or "data", k) or {}).get("metrics") for k in ("churn", "propensity")}

    snapshot = {
        "at": iso_now(), "tenant": cfg.get("tenant") or "main",
        "counts": counts, "loops": loops, "ai": ai_counts,
        "actions_24h": action_stats, "touches_24h": touches_24h, "blocks_24h": blocks_24h,
        "blocks_by_channel": blocks.get("by_channel"),
        "identified_rate": round(identified / total * 100, 1),
        "models": models,
        "tenant_count": len(_tenant_ids(cfg)),
    }
    out_dir = os.path.join(cfg.get("data_dir") or "data", "telemetry")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"{datetime.utcnow().date().isoformat()}.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return snapshot


def _tenant_ids(cfg: dict) -> list[str]:
    try:
        from userloop.core.tenants import TenantRegistry

        return [t["id"] for t in TenantRegistry(cfg.get("data_dir") or "data").list()]
    except Exception:  # noqa: BLE001
        return ["main"]


# ── ③ 自诊断（确定性规则，可解释）──

async def diagnose(store: Store, ctx: ExecutorContext) -> dict[str, Any]:
    """健康自检：返回 {ok, issues[]}，每条带 severity/evidence/recommendation。"""
    issues: list[dict[str, Any]] = []
    cfg = ctx.config
    now = datetime.utcnow()

    def add(sev: str, code: str, title: str, evidence: Any, rec: str) -> None:
        issues.append({"severity": sev, "code": code, "title": title,
                       "evidence": evidence, "recommendation": rec})

    # 1) 事件存储后端（是否如配置一致 / 是否降级）
    backend = getattr(store.events, "backend", "?")
    if getattr(store.events, "reason", ""):
        add("high", "storage_degraded", "事件存储降级运行",
            {"backend": backend, "reason": store.events.reason},
            "检查 MySQL 实例与凭据；恢复后重启服务即可自动切回")
    elif "mysql" in backend and "ms" in backend:
        try:
            ms = int(backend.split("(")[1].rstrip("ms)"))
            if ms > 200:
                add("medium", "storage_slow", "事件库连接偏慢", {"backend": backend},
                    "检查 MySQL 负载与网络；必要时调大连接池")
        except (IndexError, ValueError):
            pass

    # 2) 失败动作（近 24h）
    cur = await store.db.execute(
        "SELECT type, COUNT(*) c FROM actions WHERE status='failed' AND executed_at>=? GROUP BY type",
        ((now - timedelta(days=1)).isoformat(timespec="seconds") + "Z",))
    failed = {r["type"]: r["c"] for r in await cur.fetchall()}
    if failed:
        add("high", "actions_failed", "存在失败动作（近 24h）", failed,
            "查看 outbox 错误详情：多为凭证/目标地址或通道配置问题")

    # 3) 队列积压（到期未执行）
    cur = await store.db.execute(
        "SELECT COUNT(*) c FROM actions WHERE status='pending' AND scheduled_at<=?",
        (iso_now(),))
    backlog = int((await cur.fetchone())["c"])
    if backlog > 20:
        add("medium", "queue_backlog", "待执行动作积压", {"pending_due": backlog},
            "确认调度器在运行；若因 STO/频控持续推迟属正常，否则检查执行器凭证")

    # 4) AI 待批积压
    ai_counts = await store.ai_decision_counts()
    pending = ai_counts.get("pending_approval", 0)
    threshold = int(((cfg.get("report") or {}).get("pending_alert_threshold")) or 20)
    if pending >= threshold:
        add("medium", "ai_pending", "AI 待批决策积压", {"pending": pending, "threshold": threshold},
            "在控制台批量批准/驳回，或提高 auto_execute_medium 让低风险自动执行")

    # 5) 识别率过低（数据质量瓶颈）
    stages = await store.count_users_by_stage()
    total = sum(stages.values()) or 1
    rate = round((total - stages.get("visitor", 0)) / total * 100, 1)
    if total >= 50 and rate < 20:
        add("medium", "identified_rate_low", "实名识别率偏低（影响分析与归因）",
            {"identified_rate": rate, "total": total, "anonymous": stages.get("visitor", 0)},
            "在关键页面启用留资/登录引导（H5 留资表单已内置），并检查埋点是否携带 email")

    # 6) 拦截率过高（可能过度打扰或频控过严）
    cur = await store.db.execute("SELECT COUNT(*) c FROM touch_log")
    touches = int((await cur.fetchone())["c"])
    cur = await store.db.execute("SELECT COUNT(*) c FROM touch_blocks")
    blocks = int((await cur.fetchone())["c"])
    if touches + blocks >= 30:
        ratio = round(blocks / (touches + blocks) * 100, 1)
        if ratio >= 50:
            add("medium", "blocks_high", "触达被门禁拦下比例过高",
                {"blocks": blocks, "touches": touches, "block_ratio": ratio},
                "检查是否有 Loop 对同一批用户重复触发；或适当放宽 weekly_cap/global_gap_hours")

    # 7) 孤儿 Loop（running 但无 pending 动作且超时）
    cur = await store.db.execute(
        "SELECT COUNT(*) c FROM loops l WHERE l.status='running' AND l.created_at<? AND NOT EXISTS "
        "(SELECT 1 FROM actions a WHERE a.loop_id=l.id AND a.status='pending')",
        ((now - timedelta(hours=6)).isoformat(timespec="seconds") + "Z",))
    orphan = int((await cur.fetchone())["c"])
    if orphan:
        add("low", "orphan_loops", "存在停滞 Loop（running 且无待执行动作）", {"count": orphan},
            "多为历史中断遗留；可用调度器复跑或手动置为 verified/skipped")

    # 8) 模型未训练（样本积累后应训练）
    from userloop.predict import model as mdl

    for kind in ("churn", "propensity"):
        m = mdl.load(cfg.get("data_dir") or "data", kind)
        if not m:
            add("low", f"model_{kind}_untrained", f"{kind} 模型尚未训练",
                {"hint": "样本不足时会自动回退规则版"},
                "数据积累后系统会自动训练；也可在预测面板点「训练/更新模型」")

    # 9) 契约探测：家族 API 断链
    from userloop.integrations import probe as probe_mod

    snap = probe_mod.load_latest(ctx)
    if snap:
        down = [r for r in (snap.get("results") or []) if r.get("status") == "down"]
        if down:
            add("high", "contract_broken", "生态链路断链",
                [{"id": r.get("id"), "http": r.get("http"), "error": r.get("error")} for r in down],
                "检查对方服务与凭据；同机互调应走 127.0.0.1，勿走公网域名")
        try:
            age_h = (now - datetime.fromisoformat(str(snap.get("at", "")).replace("Z", "+00:00"))
                     .replace(tzinfo=None)).total_seconds() / 3600
            if age_h > 36:
                add("low", "probe_stale", "契约探测超过 36 小时未跑",
                    {"last": snap.get("at")}, "确认调度器在运行，或在控制台点「立即探测」")
        except (TypeError, ValueError):
            pass

    high = [i for i in issues if i["severity"] == "high"]
    return {"at": iso_now(), "tenant": cfg.get("tenant") or "main", "ok": not high,
            "issues": issues, "counts": {"high": len(high),
                                         "medium": len([i for i in issues if i["severity"] == "medium"]),
                                         "low": len([i for i in issues if i["severity"] == "low"])}}


# ── ⑤ AI 提案（基于遥测 + 诊断）──

SYSTEM = """你是 UserLoop 的架构与运营优化顾问。基于给定的**运行遥测**与**自诊断问题**，
提出最多 3 条**可执行**的改进提案。

输出严格 JSON：
{"proposals": [{
  "title": "简短标题",
  "rationale": "为什么（引用给定数据，不编造）",
  "kind": "config|content|code",
  "impact": "预期影响（可量化则量化）",
  "risk": "low|medium|high",
  "validation": "如何验证生效",
  "apply_key": "仅 config 类填写，取值来自白名单",
  "apply_value": "仅 config 类填写，新值"
}]}

config 类只能改这些参数（apply_key 必须完全一致）：
frequency.weekly_cap(数字) / frequency.global_gap_hours(数字) / sto.window_hours(数字) /
sto.max_delay_hours(数字) / brain.batch_size(数字) / brain.min_gap_hours(数字) /
brain.daily_budget(数字) / predict.batch_size(数字) / report.pending_alert_threshold(数字) /
qc.mode("block"|"warn")

纪律：
1. 只针对给定问题提改进，不为改而改；没有值得改的就返回空数组
2. 代码类改进写 kind=code，只给方向（具体实现由人来做）
3. 每条都必须可验证（validation 写清怎么判断有没有效果）"""


async def propose(store: Store, ctx: ExecutorContext, telemetry: dict | None = None,
                  diag: dict | None = None, transport: Any = None) -> dict[str, Any]:
    """生成改进提案（AI）→ 落库待审批。"""
    from userloop.integrations.ai import chat

    tel = telemetry or await collect_telemetry(store, ctx)
    dg = diag or await diagnose(store, ctx)
    payload = json.dumps({"遥测": tel, "诊断": dg["issues"]}, ensure_ascii=False)
    from userloop.i18n import t as _t

    lang = _t("ai.language", ctx.config.get("locale") or "zh-CN")
    res = await chat(ctx, [{"role": "system", "content": SYSTEM + f"\n\n输出语言要求：{lang}"},
                           {"role": "user", "content": payload}],
                     transport=transport, max_tokens=1200)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "AI 不可用", "degraded": True}

    from userloop.ai.copilot import _loads_lenient

    data = _loads_lenient(res.get("content") or "") or {}
    raw = data.get("proposals") if isinstance(data, dict) else None
    out: list[dict] = []
    warnings: list[str] = []
    for p in (raw or [])[:3]:
        if not isinstance(p, dict) or not p.get("title"):
            continue
        item = {"id": new_id("evo"), "title": str(p["title"])[:80],
                "rationale": str(p.get("rationale") or "")[:400],
                "kind": str(p.get("kind") or "code"),
                "impact": str(p.get("impact") or "")[:200],
                "risk": str(p.get("risk") or "low"),
                "validation": str(p.get("validation") or "")[:200],
                "created_at": iso_now()}
        if item["kind"] == "config":
            key = str(p.get("apply_key") or "")
            if key not in APPLY_WHITELIST:
                warnings.append(f"提案「{item['title']}」的 apply_key 不在白名单，已降级为 code 类")
                item["kind"] = "code"
            else:
                item["apply_key"] = key
                item["apply_value"] = p.get("apply_value")
        await store.put_evolution_proposal(item)
        out.append(item)
    return {"ok": True, "count": len(out), "proposals": out, "warnings": warnings,
            "model": res.get("model")}


# ── 应用（配置白名单 + 审计 + 可回滚）──

async def apply_proposal(store: Store, ctx: ExecutorContext, proposal_id: str) -> dict[str, Any]:
    """应用配置类提案：改配置 → 记录旧值（可回滚）→ 标记 applied。"""
    rows = await store.list_evolution_proposals(limit=200)
    prop = next((p for p in rows if p["id"] == proposal_id), None)
    if not prop:
        return {"ok": False, "error": "提案不存在"}
    if prop.get("status") != "pending":
        return {"ok": False, "error": f"提案状态为 {prop.get('status')}，不可应用"}
    if prop.get("kind") != "config":
        await store.set_evolution_status(proposal_id, "accepted_todo")
        await store.add_lesson({"category": "evolution", "title": f"代码类改进待办：{prop['title']}",
                                "detail": prop.get("rationale", ""), "fix": prop.get("validation", ""),
                                "source": "evolution"})
        return {"ok": True, "kind": "code", "status": "accepted_todo",
                "note": "代码类改进已登记为待办与 Lessons，由人实现"}

    key = str(prop.get("apply_key") or "")
    spec = APPLY_WHITELIST.get(key)
    if not spec:
        return {"ok": False, "error": f"apply_key 不在白名单：{key}"}
    value = prop.get("apply_value")
    # 数值型校验
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        value = int(value) if str(value).lstrip("-").isdigit() else float(value)
        if not (0 <= value <= 100000):
            return {"ok": False, "error": "数值超出合理范围"}

    # 写配置文件（保留旧值以便回滚）
    import os

    cfg_path = os.path.join(ctx.config.get("data_dir") or "data", "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            file_cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        file_cfg = {}
    found, old = _get_path(file_cfg, spec)
    if not found:
        found, old = _get_path(ctx.config, spec)
    _set_path(file_cfg, spec, value)
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(file_cfg, f, ensure_ascii=False, indent=2)
    # 运行中的进程同步内存值（下次重启也生效）
    _set_path(ctx.config, spec, value)

    prop.update({"applied_at": iso_now(), "old_value": old, "old_present": bool(found)})
    await store.put_evolution_proposal(prop, status="applied")
    await store.add_lesson({"category": "evolution", "title": f"配置自进化：{key} → {value}",
                            "detail": prop.get("rationale", ""), "fix": prop.get("validation", ""),
                            "source": "evolution"})
    return {"ok": True, "kind": "config", "status": "applied", "key": key,
            "old_value": old, "old_present": bool(found), "new_value": value,
            "rollback": {"key": key, "value": old, "restore": "set" if found else "remove"}}


async def rollback_proposal(store: Store, ctx: ExecutorContext, proposal_id: str) -> dict[str, Any]:
    """回滚已应用的配置类提案（恢复旧值），并在 Lessons 里留痕。"""
    rows = await store.list_evolution_proposals(limit=200)
    prop = next((x for x in rows if x["id"] == proposal_id), None)
    if not prop:
        return {"ok": False, "error": "提案不存在"}
    if prop.get("status") != "applied":
        return {"ok": False, "error": f"仅可回滚已应用的提案（当前 {prop.get('status')}）"}
    spec = APPLY_WHITELIST.get(str(prop.get("apply_key") or ""))
    if not spec:
        return {"ok": False, "error": "该提案不可自动回滚"}
    import os

    cfg_path = os.path.join(ctx.config.get("data_dir") or "data", "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            file_cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        file_cfg = {}
    # 原本不存在该配置 → 回滚即删除（回到模块默认），绝不写入空值
    if prop.get("old_present"):
        _set_path(file_cfg, spec, prop.get("old_value"))
        _set_path(ctx.config, spec, prop.get("old_value"))
    else:
        _del_path(file_cfg, spec)
        _del_path(ctx.config, spec)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(file_cfg, f, ensure_ascii=False, indent=2)
    prop["status"] = "rolled_back"
    await store.put_evolution_proposal(prop, status="rolled_back")
    await store.add_lesson({"category": "evolution",
                            "title": f"回滚配置变更：{prop['apply_key']} → {prop['old_value']}",
                            "detail": f"该变更已被评估为不适用（原值 {prop.get('old_value')}，"
                                      f"试改值 {prop.get('apply_value')}）",
                            "fix": "回滚后观察指标恢复情况", "source": "evolution"})
    return {"ok": True, "rollback": {"key": prop["apply_key"],
                                     "restored": prop.get("old_value") if prop.get("old_present") else "默认值",
                                     "action": "set" if prop.get("old_present") else "remove"}}


# ── Lessons 渲染 ──

def render_lessons_md(lessons: list[dict]) -> str:
    lines = ["# UserLoop Lessons（错误只犯一次）", "",
             f"> 自动生成于 {iso_now()}；来源：evolution / 手工记录", ""]
    by_cat: dict[str, list[dict]] = {}
    for l in lessons:
        by_cat.setdefault(l.get("category") or "general", []).append(l)
    for cat, items in by_cat.items():
        lines.append(f"## {cat}")
        for l in items:
            lines.append(f"- **{l['title']}**")
            if l.get("detail"):
                lines.append(f"  - 现象/原因：{l['detail']}")
            if l.get("fix"):
                lines.append(f"  - 处理/验证：{l['fix']}")
            lines.append(f"  - 记录于 {l.get('created_at')}（{l.get('source')}）")
        lines.append("")
    return "\n".join(lines)

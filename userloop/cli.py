"""UserLoop CLI：serve / init / demo / stats.

demo 端到端演示：写入一批模拟用户旅程事件 → 总线处理 → 调度器执行 → 验证回流。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from datetime import datetime, timedelta
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from userloop import __version__
from userloop.config import load_config

console = Console()


def _sync(cfg: dict) -> Any:
    """打开存储连接。"""

    async def _open() -> Any:
        from userloop.core.store import Store

        store = Store(cfg["db_path"], cfg)
        await store.connect()
        from userloop.core.templates import seed_templates

        await seed_templates(store, cfg["data_dir"])
        return store

    return asyncio.run(_open())


@click.group()
@click.version_option(__version__, prog_name="userloop")
def main() -> None:
    """UserLoop —— 全域自动化用户运营工具（用户旅程自动形成 Loop）."""


@main.command()
@click.option("--data-dir", default=None, help="数据目录（默认 ./data）")
def init(data_dir: str | None) -> None:
    """初始化工作区（建库 + 内置 Loop 模板 + 示例配置）。"""
    cfg = load_config(data_dir)
    _sync(cfg)
    config_example = os.path.join(cfg["data_dir"], "config.json")
    if not os.path.exists(config_example):
        with open(config_example, "w", encoding="utf-8") as f:
            json.dump({
                "api_token": None,
                "default_webhook": None,
                "smtp": {"host": "", "port": 587, "user": "", "password": "", "from": ""},
            }, f, indent=2, ensure_ascii=False)
    console.print(f"[green]工作区就绪:[/] {cfg['data_dir']} (db: {os.path.basename(cfg['db_path'])})")
    console.print("下一步: [bold]userloop demo[/bold] 跑端到端演示，或 [bold]userloop serve[/bold] 启动服务")


@main.command()
@click.option("--data-dir", default=None)
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
def serve(data_dir: str | None, host: str | None, port: int | None) -> None:
    """启动 API 服务 + Web 控制台（含内置调度器）。"""
    import uvicorn

    cfg = load_config(data_dir)
    host = host or cfg["host"]
    port = port or int(cfg["port"])
    console.print(f"[bold green]UserLoop[/] 控制台 → http://localhost:{port}  (db: {cfg['db_path']})")
    uvicorn.run("userloop.server.app:create_app", factory=True, host=host, port=port, log_level="info")


@main.command()
@click.option("--data-dir", default=None)
def mcp(data_dir: str | None) -> None:
    """启动 MCP Server（stdio）：只读旅程 + 写操作走审批门。"""
    import os

    from userloop.config import load_config

    cfg = load_config(data_dir)
    os.environ["USERLOOP_DATA"] = cfg["data_dir"]
    from userloop.mcp_server import main as mcp_main

    mcp_main()


@main.command()
@click.option("--data-dir", default=None)
@click.option("--user", "distinct_id", default=None, help="只对指定用户决策（distinct_id）")
@click.option("--limit", default=3, help="批次用户数")
@click.option("--force", is_flag=True, help="跳过频控/静默期/审批门（人工触发）")
def brain(data_dir: str | None, distinct_id: str | None, limit: int, force: bool) -> None:
    """AI 大脑：为生命周期中的用户决定下一步最佳动作（全域全运营 AI）。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.ai import brain as brain_mod
        from userloop.core.store import Store
        from userloop.core.templates import seed_templates

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        await seed_templates(store, cfg["data_dir"])
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        ctx.store = store
        try:
            if distinct_id:
                user = await store.find_user(distinct_id)
                if not user:
                    console.print(f"[red]用户不存在：{distinct_id}[/]")
                    return
                rec = await brain_mod.run_for_user(store, ctx, user, force=force)
                _print_decisions([rec])
            else:
                recs = await brain_mod.run_batch(store, ctx, limit=limit, force=force)
                if not recs:
                    console.print("（AI 大脑未启用：config.json → ai.brain.enabled=true 后生效，或用 --force）")
                    return
                _print_decisions(recs)
        finally:
            await store.close()

    asyncio.run(_run())


def _print_decisions(recs: list[dict]) -> None:
    table = Table(title="AI 决策（全域全生命周期运营）")
    table.add_column("用户", style="cyan")
    table.add_column("阶段")
    table.add_column("意图")
    table.add_column("风险 / 状态")
    table.add_column("理由", overflow="fold")
    table.add_column("置信")
    for r in recs:
        table.add_row(
            (r.get("user_id") or "")[:14], str(r.get("stage") or ""), str(r.get("intent") or ""),
            f"{r.get('risk')} / {r.get('status')}", (r.get("reasoning") or "")[:80],
            f"{float(r.get('confidence') or 0):.2f}",
        )
    console.print(table)


@main.group()
def db() -> None:
    """存储运维（分层存储：events 主用 MySQL / 兜底 SQLite）。"""


@db.command("status")
@click.option("--data-dir", default=None)
def db_status(data_dir: str | None) -> None:
    """查看当前 events 后端与规模。"""

    async def _run() -> None:
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        try:
            backend = getattr(store.events, "backend", "?")
            reason = getattr(store.events, "reason", "")
            counts = await store.counts()
            storage = ((cfg.get("storage") or {}).get("events") or {})
            console.print(f"events 后端: [bold]{backend}[/]  配置: {storage.get('backend', 'auto')}"
                          + (f"  降级原因: {reason}" if reason else ""))
            sqlite_events = 0
            try:
                cur = await store.db.execute("SELECT COUNT(*) c FROM events")
                sqlite_events = int((await cur.fetchone())["c"])
            except Exception:  # noqa: BLE001
                pass
            console.print(f"SQLite(兜底/备份): {cfg['db_path']}  events={sqlite_events}  |  "
                          + "  ".join(f"{k}={v}" for k, v in counts.items() if k != "events"))
            if backend.startswith("mysql"):
                console.print(f"MySQL events 行数（主用）: {await store.events.total()}")
        finally:
            await store.close()

    asyncio.run(_run())


@db.command("migrate-events")
@click.option("--data-dir", default=None)
@click.option("--keep-sqlite/--no-keep-sqlite", default=True, help="保留 SQLite 存量（默认保留，作备份）")
def db_migrate_events(data_dir: str | None, keep_sqlite: bool) -> None:
    """把 SQLite events 全量回填到 MySQL（幂等，可重复执行）。"""

    async def _run() -> None:
        from userloop.core.eventstore import MySqlEventStore, migrate_events_sqlite_to_mysql
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        storage = ((cfg.get("storage") or {}).get("events") or {})
        mysql_cfg = storage.get("mysql") or {}
        if not mysql_cfg.get("host"):
            console.print("[red]未配置 storage.events.mysql，无法迁移[/]")
            return
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        mysql = MySqlEventStore(mysql_cfg)
        try:
            result = await migrate_events_sqlite_to_mysql(store.db, mysql)
            console.print(f"迁移完成: {result}")
            if not keep_sqlite:
                console.print("（已选择不保留 SQLite 存量；如需清理请手动归档 data/*.db）")
        finally:
            try:
                await mysql.close()   # 迁移用连接池显式关闭（否则事件循环关闭时报错）
            except Exception:  # noqa: BLE001
                pass
            await store.close()

    asyncio.run(_run())


@main.command()
@click.argument("prompt")
@click.option("--data-dir", default=None)
@click.option("--apply", "do_apply", is_flag=True, help="落库为草稿模板（默认只预览）")
@click.option("--enable", is_flag=True, help="落库并立即启用")
def copilot(prompt: str, data_dir: str | None, do_apply: bool, enable: bool) -> None:
    """Copilot：用一句话生成运营 Loop（--apply 落库，--enable 立即生效）。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.ai import copilot as cp
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        ctx.store = store
        try:
            res = await cp.draft_loop(ctx, prompt)
            if not res.get("ok"):
                console.print(f"[red]生成失败：{res.get('error')}[/]")
                return
            console.print_json(json.dumps(res["draft"], ensure_ascii=False))
            for w in res.get("warnings") or []:
                console.print(f"[yellow]warn[/] {w}")
            if do_apply or enable:
                out = await cp.apply_loop(store, res["draft"], enable=enable)
                console.print(f"已落库：{out['id']}（enabled={out['enabled']}）")
        finally:
            await store.close()

    asyncio.run(_run())


@main.group()
def ops() -> None:
    """运维加固：健康检查 / 指标 / 备份恢复 / 告警。"""


async def _with_store(data_dir: str | None):
    from userloop.core.store import Store

    cfg = load_config(data_dir)
    store = Store(cfg["db_path"], cfg)
    await store.connect()
    return cfg, store


@ops.command("health")
@click.option("--data-dir", default=None)
def ops_health(data_dir: str | None) -> None:
    """健康检查（存储/事件后端/入库新鲜度/磁盘/调度器）。"""

    async def _run() -> None:
        from userloop.ops import health as health_mod

        cfg, store = await _with_store(data_dir)
        try:
            console.print_json(json.dumps(await health_mod.check(cfg, store), ensure_ascii=False, default=str))
        finally:
            await store.close()

    asyncio.run(_run())


@ops.command("metrics")
@click.option("--data-dir", default=None)
@click.option("--json", "as_json", is_flag=True, help="输出 JSON（默认 Prometheus 文本）")
def ops_metrics(data_dir: str | None, as_json: bool) -> None:
    """导出进程指标（Prometheus 文本 / JSON）。"""
    from userloop.ops.metrics import METRICS

    if as_json:
        console.print_json(json.dumps(METRICS.snapshot(), ensure_ascii=False))
    else:
        console.print(METRICS.render_prom(), end="", soft_wrap=True)


@ops.command("backup")
@click.option("--data-dir", default=None)
@click.option("--keep", default=7, help="保留最近 N 份")
@click.option("--note", default="cli", help="备注")
def ops_backup(data_dir: str | None, keep: int, note: str) -> None:
    """创建备份（SQLite 一致性快照 + tar.gz + 保留策略）。"""
    from userloop.config import load_config as _lc
    from userloop.ops import backup as backup_mod

    cfg = _lc(data_dir)
    out = backup_mod.create(cfg["data_dir"], keep=keep, note=note, cfg=cfg)
    console.print_json(json.dumps(out, ensure_ascii=False, default=str))


@ops.command("backups")
@click.option("--data-dir", default=None)
def ops_backups(data_dir: str | None) -> None:
    """列出已有备份。"""
    from userloop.config import load_config as _lc
    from userloop.ops import backup as backup_mod

    cfg = _lc(data_dir)
    items = backup_mod.list_backups(cfg["data_dir"])
    table = Table(title=f"备份（{len(items)} 份）")
    table.add_column("文件")
    table.add_column("大小 MB", justify="right")
    table.add_column("创建时间")
    for it in items:
        table.add_row(it["name"], f"{it['bytes'] / 1e6:.2f}", it["created_at"])
    console.print(table)


@ops.command("verify")
@click.argument("name")
@click.option("--data-dir", default=None)
def ops_verify(name: str, data_dir: str | None) -> None:
    """校验备份归档完整性（NAME 为备份文件名）。"""
    import os as _os

    from userloop.config import load_config as _lc
    from userloop.ops import backup as backup_mod

    cfg = _lc(data_dir)
    path = _os.path.join(cfg["data_dir"], backup_mod.BACKUP_DIR, _os.path.basename(name))
    console.print_json(json.dumps(backup_mod.verify(path), ensure_ascii=False, default=str))


@ops.command("restore")
@click.argument("name")
@click.option("--data-dir", default=None)
@click.option("--force", is_flag=True, help="确认覆盖当前数据（恢复后需重启服务）")
def ops_restore(name: str, data_dir: str | None, force: bool) -> None:
    """从备份恢复（会覆盖当前数据目录，恢复后需重启服务）。"""
    import os as _os

    from userloop.config import load_config as _lc
    from userloop.ops import backup as backup_mod

    cfg = _lc(data_dir)
    path = _os.path.join(cfg["data_dir"], backup_mod.BACKUP_DIR, _os.path.basename(name))
    out = backup_mod.restore(path, cfg["data_dir"], force=force)
    console.print_json(json.dumps(out, ensure_ascii=False, default=str))


@ops.command("alerts")
@click.option("--data-dir", default=None)
@click.option("--run", "do_run", is_flag=True, help="评估并按冷却窗口外送")
def ops_alerts(data_dir: str | None, do_run: bool) -> None:
    """评估运维告警（--run 则触发外送并记录）。"""

    async def _run() -> None:
        from userloop.ops import alerts as alerts_mod
        from userloop.ops import health as health_mod

        cfg, store = await _with_store(data_dir)
        try:
            h = await health_mod.check(cfg, store)
            if do_run:
                console.print_json(json.dumps(await alerts_mod.run(cfg, h), ensure_ascii=False, default=str))
            else:
                console.print_json(json.dumps(alerts_mod.evaluate(cfg, h), ensure_ascii=False, default=str))
        finally:
            await store.close()

    asyncio.run(_run())


@main.command()
@click.option("--data-dir", default=None)
@click.option("--days", default=7, help="统计窗口天数")
@click.option("--send", is_flag=True, help="生成后推送（飞书/邮件，需在配置中开启）")
def report(data_dir: str | None, days: int, send: bool) -> None:
    """生成运营周报（AI 叙事 + 硬指标附录）。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.ai import reporter
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        try:
            rep = await reporter.build(store, ctx, days=days)
            path = await reporter.save(store, ctx, rep)
            console.print(f"[green]已生成：[/]{path}" + ("[yellow]（AI 叙事降级）[/]" if rep["degraded"] else ""))
            console.print(rep["markdown"][:1500])
            if send:
                console.print(f"推送结果：{await reporter.send(ctx, rep)}")
        finally:
            await store.close()

    asyncio.run(_run())


@main.group()
def evolve() -> None:
    """自进化：遥测 / 自诊断 / AI 提案 / 应用 / Lessons。"""


@evolve.command("status")
@click.option("--data-dir", default=None)
def evolve_status(data_dir: str | None) -> None:
    """采集遥测 + 自诊断 + 提案/ Lessons 概览。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.core.store import Store
        from userloop.evolve import engine as evo

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        ctx.store = store
        try:
            tel = await evo.collect_telemetry(store, ctx)
            dg = await evo.diagnose(store, ctx)
            console.print_json(json.dumps({"telemetry": tel, "diagnostics": dg}, ensure_ascii=False))
        finally:
            await store.close()

    asyncio.run(_run())


@evolve.command("probe")
@click.option("--data-dir", default=None)
def evolve_probe(data_dir: str | None) -> None:
    """立即探测家族系统契约（OpenFlow/MFlow/WebsFlow/inFlow）。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.integrations import probe as probe_mod

        cfg = load_config(data_dir)
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        snap = await probe_mod.run(ctx)
        console.print_json(json.dumps(snap, ensure_ascii=False))

    asyncio.run(_run())


@evolve.command("propose")
@click.option("--data-dir", default=None)
@click.option("--apply", "do_apply", is_flag=True, help="自动应用低风险 config 类提案")
def evolve_propose(data_dir: str | None, do_apply: bool) -> None:
    """生成改进提案（AI）；--apply 自动应用低风险配置类。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.core.store import Store
        from userloop.evolve import engine as evo

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        ctx.store = store
        try:
            res = await evo.propose(store, ctx)
            for p in res.get("proposals") or []:
                console.print(f"[bold]{p['id']}[/] [{p['kind']}/{p['risk']}] {p['title']}")
                console.print(f"  理由：{p['rationale'][:120]}")
                console.print(f"  影响：{p['impact'][:100]} | 验证：{p['validation'][:100]}")
                if do_apply and p["kind"] == "config" and p.get("risk") == "low":
                    console.print(f"  → 自动应用：{await evo.apply_proposal(store, ctx, p['id'])}")
            if not (res.get("proposals") or []):
                console.print("（本轮无值得改进的提案）")
        finally:
            await store.close()

    asyncio.run(_run())


@evolve.command("apply")
@click.argument("proposal_id")
@click.option("--data-dir", default=None)
def evolve_apply(proposal_id: str, data_dir: str | None) -> None:
    """应用指定提案（配置类即时生效；代码类登记为待办）。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.core.store import Store
        from userloop.evolve import engine as evo

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        ctx.store = store
        try:
            console.print_json(json.dumps(await evo.apply_proposal(store, ctx, proposal_id), ensure_ascii=False))
        finally:
            await store.close()

    asyncio.run(_run())


@evolve.command("lessons")
@click.option("--data-dir", default=None)
@click.option("--write", is_flag=True, help="写入 docs/LESSONS.md")
def evolve_lessons(data_dir: str | None, write: bool) -> None:
    """查看/导出 Lessons（错误只犯一次）。"""

    async def _run() -> None:
        from userloop.core.store import Store
        from userloop.evolve import engine as evo

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        try:
            md = evo.render_lessons_md(await store.list_lessons(limit=200))
            if write:
                import os

                path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "docs", "LESSONS.md")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(md)
                console.print(f"已写入 {path}")
            else:
                console.print(md)
        finally:
            await store.close()

    asyncio.run(_run())


@main.command()
@click.option("--data-dir", default=None)
def stats(data_dir: str | None) -> None:
    """查看运营看板摘要。"""

    async def _run() -> None:
        from userloop.core.store import Store
        from userloop.core.templates import seed_templates

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        await seed_templates(store, cfg["data_dir"])
        try:
            counts = await store.counts()
            stages = await store.count_users_by_stage()
            loop_status = await store.count_loops_by_status()
            tstats = await store.feedback_stats()

            table = Table(title="UserLoop 看板")
            table.add_column("指标")
            table.add_column("值", justify="right")
            for k, v in counts.items():
                table.add_row(k, str(v))
            console.print(table)

            t2 = Table(title="旅程漏斗")
            t2.add_column("阶段")
            t2.add_column("用户数", justify="right")
            for s in ("visitor", "signup", "activated", "paying", "retained", "advocate", "churn_risk", "churned"):
                t2.add_row(s, str(stages.get(s, 0)))
            console.print(t2)

            t3 = Table(title="Loop 状态 / 模板效果")
            t3.add_column("Loop 状态")
            t3.add_column("数", justify="right")
            for k, v in loop_status.items():
                t3.add_row(k, str(v))
            for tid, v in tstats.items():
                t3.add_row(f"{tid}", f"effective={v.get('effective', 0)} neutral={v.get('neutral', 0)}")
            console.print(t3)
        finally:
            await store.close()

    asyncio.run(_run())


@main.command()
@click.option("--data-dir", default=None)
@click.option("--users", default=12, help="模拟用户数")
def demo(data_dir: str | None, users: int) -> None:
    """端到端演示：模拟全域旅程事件 → 自动 Loop → 动作执行 → 验证回流。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.core.bus import handle
        from userloop.core.scheduler import process_due_actions, sweep_inactivity, verify_due_loops
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        from userloop.core.templates import seed_templates

        await seed_templates(store, cfg["data_dir"])
        ctx = ExecutorContext(cfg["data_dir"], cfg)

        console.rule("[bold]1. 注入全域旅程事件（模拟）")
        events = _make_demo_events(users)
        stage_changes = 0
        for e in events:
            result = await handle(store, ctx, e)
            if result.get("stage_changed"):
                stage_changes += 1
                console.print(f"  [cyan]{e['distinct_id']}[/] {result.get('event')} → 阶段迁移: [bold]{result['stage']}[/]")
        console.print(f"共注入 [bold]{len(events)}[/] 个事件，产生 [bold]{stage_changes}[/] 次阶段迁移")

        console.rule("[bold]2. 滞留扫描（下行旅程断点）")
        stalled = await sweep_inactivity(store)
        console.print(f"滞留触发 Loop [bold]{len(stalled)}[/] 个")

        console.rule("[bold]3. 调度器执行到期动作")
        results = await process_due_actions(store, ctx)
        for r in results:
            mode = "dry-run(outbox)" if r.get("dry_run") else "sent"
            console.print(f"  [{r.get('type')}] loop={r['loop_id']} → {mode}")
        console.print(f"执行动作 [bold]{len(results)}[/] 个")

        console.rule("[bold]4. 验证窗口回流")
        # 演示：直接对 verifying Loop 立即验证（真实环境等 verify_before 到期）
        import json as _json

        async with store.db.execute("SELECT * FROM loops WHERE status='verifying'") as cur:
            verifying = [dict(r) for r in await cur.fetchall()]
        for loop in verifying:
            from userloop.core.verify import verify_loop

            fb = await verify_loop(store, loop)
            if fb:
                console.print(f"  loop={loop['id']} [{loop['template_id']}] → verdict=[bold]{fb['verdict']}[/]")

        console.rule("[bold green]看板")
        counts = await store.counts()
        stages = await store.count_users_by_stage()
        lstats = await store.count_loops_by_status()
        fstats = await store.feedback_stats()
        console.print(f"counts: {_json.dumps(counts)}")
        console.print(f"stages: {_json.dumps(stages)}")
        console.print(f"loops:  {_json.dumps(lstats)}")
        console.print(f"template stats: {_json.dumps(fstats)}")
        console.print("Web 控制台: [bold]userloop serve[/bold] → http://localhost:8600")
        await store.close()

    asyncio.run(_run())


def _make_demo_events(users: int) -> list[dict[str, Any]]:
    """构造一批跨旅程阶段的事件序列（含幂等 event_id、时间回溯）。"""
    now = datetime.utcnow()
    out: list[dict[str, Any]] = []
    pool = [f"user{i:02d}" for i in range(1, users + 1)]

    for idx, uid in enumerate(pool):
        t0 = now - timedelta(days=random.randint(1, 20))
        seq: list[tuple[timedelta, str, dict]] = [(timedelta(0), "page_view", {"page": "/home"})]

        # 旅程剧本：一半注册，其中一半激活，激活者里有人购买/复购/推荐
        if idx % 2 == 0:
            seq.append((timedelta(minutes=5), "signup", {}))
        if idx % 4 == 0:
            seq.append((timedelta(hours=2), "activation", {"feature": "first_project"}))
        if idx % 8 == 0:
            seq.append((timedelta(days=1), "purchase", {"amount": 199}))
        if idx % 16 == 0:
            seq.append((timedelta(days=3), "purchase", {"amount": 299}))
        if idx % 32 == 0:
            seq.append((timedelta(days=4), "referral", {}))
        if idx == 5:  # 演示购物车挽回
            seq.append((timedelta(hours=1), "add_to_cart", {"item": "pro-plan"}))

        for dt, ev, props in seq:
            out.append({
                "distinct_id": uid,
                "email": f"{uid}@example.com",
                "event": ev,
                "props": props,
                "source": "web" if idx % 3 else "app",
                "event_id": f"{uid}-{ev}-{int(dt.total_seconds())}",
                "ts": (t0 + dt).isoformat(timespec="seconds") + "Z",
            })
    return out


@main.group()
def agents() -> None:
    """N3 智能体运营团队：拆解目标 / 审批 / 执行。"""


@agents.command("plan")
@click.argument("goal")
def agents_plan(goal: str) -> None:
    """只拆解、不落库：看角色任务与是否需要审批。"""
    from userloop.agents import planner

    p = planner.plan(goal)
    console.print(f"[bold]{p['name']}[/]  recipe={p['recipe']}  验收 {p['metric']} +{int(p['target']*100)}%")
    console.print("需要审批：" + ("是（含中风险任务，人只点批准）" if p["needs_approval"] else "否"))
    table = Table(title="任务")
    table.add_column("#", justify="right")
    table.add_column("角色")
    table.add_column("工具")
    table.add_column("风险")
    table.add_column("标题")
    for t in p["tasks"]:
        table.add_row(str(t["seq"]), t["role_name"], t["tool"], t["risk"], t["title"])
    console.print(table)


@agents.command("create")
@click.argument("goal")
@click.option("--data-dir", default=None)
@click.option("--approve", "do_approve", is_flag=True, help="创建后立即批准（演示用）")
def agents_create(goal: str, data_dir: str | None, do_approve: bool) -> None:
    """拆解目标为战役；中风险任务待人审。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.agents import engine as agents
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        ctx.store = store
        try:
            out = await agents.create_campaign(store, ctx, goal)
            camp = out["campaign"]
            console.print(f"战役 {camp['id']}  状态={camp['status']}  需要审批={out['needs_approval']}")
            for t in out["tasks"]:
                console.print(f"  [{t['status']}] {t['role']} / {t['tool']}  {t['title']}")
            if do_approve:
                ap = await agents.approve_campaign(store, ctx, camp["id"])
                console.print(f"已批准 → {ap.get('campaign', {}).get('status')}")
                snap = await agents.snapshot(store, camp["id"])
                for t in snap.get("tasks") or []:
                    console.print(f"  [{t['status']}] {t['role']} / {t['tool']}  {t['title']}")
        finally:
            await store.close()

    asyncio.run(_run())


@agents.command("approve")
@click.argument("campaign_id")
@click.option("--data-dir", default=None)
def agents_approve(campaign_id: str, data_dir: str | None) -> None:
    """批准战役（状态机执行白名单任务）。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.agents import engine as agents
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        ctx.store = store
        try:
            console.print_json(json.dumps(await agents.approve_campaign(store, ctx, campaign_id),
                                          ensure_ascii=False, default=str))
        finally:
            await store.close()

    asyncio.run(_run())


@agents.command("list")
@click.option("--data-dir", default=None)
def agents_list(data_dir: str | None) -> None:
    """列出战役。"""

    async def _run() -> None:
        from userloop.core.store import Store

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        try:
            rows = await store.list_campaigns(limit=30)
            if not rows:
                console.print("（暂无战役。试：userloop agents create \"本月复购率 +10%\"）")
                return
            table = Table(title="Agent 战役")
            table.add_column("id")
            table.add_column("目标")
            table.add_column("状态")
            table.add_column("配方")
            for r in rows:
                table.add_row(r["id"], r["goal"][:40], r["status"], str((r.get("plan") or {}).get("recipe") or ""))
            console.print(table)
        finally:
            await store.close()

    asyncio.run(_run())


@main.command()
@click.option("--data-dir", default=None)
def platform(data_dir: str | None) -> None:
    """N4 平台摘要：插件 / 用量 / OpenAPI。"""

    async def _run() -> None:
        from userloop.actions.executors import ExecutorContext
        from userloop.billing import meter as billing
        from userloop.core.store import Store
        from userloop.plugins import registry as plug

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        ctx = ExecutorContext(cfg["data_dir"], cfg)
        try:
            plugins = plug.list_plugins(cfg)
            kinds: dict[str, int] = {}
            for p in plugins:
                kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
            console.print(f"插件 {len(plugins)} 个  " + "  ".join(f"{k}={v}" for k, v in kinds.items()))
            console.print_json(json.dumps(await billing.snapshot(store, ctx), ensure_ascii=False))
            console.print("OpenAPI: /api/v1/openapi.json   Docs: /api/docs   MCP: userloop mcp")
        finally:
            await store.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()

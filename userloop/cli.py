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

        store = Store(cfg["db_path"])
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
    """启动 MCP Server（stdio，只读）：供 OpenFlow AgentRuntime / Claude 等消费旅程数据."""
    import os

    from userloop.config import load_config

    cfg = load_config(data_dir)
    os.environ["USERLOOP_DATA"] = cfg["data_dir"]
    from userloop.mcp_server import main as mcp_main

    mcp_main()


@main.command()
@click.option("--data-dir", default=None)
def stats(data_dir: str | None) -> None:
    """查看运营看板摘要。"""

    async def _run() -> None:
        from userloop.core.store import Store
        from userloop.core.templates import seed_templates

        cfg = load_config(data_dir)
        store = Store(cfg["db_path"])
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
        store = Store(cfg["db_path"])
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


if __name__ == "__main__":
    main()

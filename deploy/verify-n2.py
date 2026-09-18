import asyncio
import json

from userloop.actions.executors import ExecutorContext
from userloop.assets import packs as ap
from userloop.config import load_config
from userloop.core.bus import handle
from userloop.core.store import Store
from userloop.core.tenants import TenantStores


async def main() -> None:
    base = load_config()
    ts = TenantStores(base)
    # 建一个全新的空租户（模拟新客户）
    if not ts.registry.get("n2demo"):
        ts.registry.create("n2demo", name="N2 演示租户", locale="zh-CN")
    st = await ts.get("n2demo")
    tcfg = dict(base)
    tcfg["data_dir"] = ts.registry.get("n2demo")["data_dir"]
    tcfg["db_path"] = st.path
    tcfg["tenant"] = "n2demo"
    ctx = ExecutorContext(tcfg["data_dir"], tcfg)
    ctx.store = st

    print("1) 空租户初始资产:", {"loops": len(await st.get_templates(enabled_only=False))})

    print("2) 预览电商增长包（dry_run）")
    prev = await ap.import_assets(st, ap.get_pack(base["data_dir"], "ecommerce-growth"), dry_run=True)
    print("   将创建:", prev["counts"], "| 依赖渠道:", prev["requires"])

    print("3) 一键应用")
    out = await ap.import_assets(st, ap.get_pack(base["data_dir"], "ecommerce-growth"))
    print("   已创建:", out["counts"], "| 跳过:", {k: len(v) for k, v in out["skipped"].items()})
    print("   模板:", [t["id"] for t in await st.get_templates(enabled_only=False)])
    print("   分群:", [s["id"] for s in await st.list_segments()])
    print("   实验:", [e["id"] for e in await st.list_experiments(enabled_only=False)])

    print("4) 新租户跑通一条 Loop（加购未付 → 触发 ec_cart_abandon）")
    r = await handle(st, ctx, {"distinct_id": "n2_buyer", "email": "n2-buyer@nownexts.com",
                               "event": "add_to_cart", "props": {"item": "pro"}})
    print("   总线:", {"stage": r["stage"], "loops_created": r["loops_created"],
                      "actions": r["actions_executed"]})
    loops = [x for x in await st.list_loops(limit=5) if x["template_id"] == "ec_cart_abandon"]
    if loops:
        acts = await st.actions_for_loop(loops[0]["id"])
        print("   Loop:", loops[0]["id"], loops[0]["status"], "| 动作:",
              [(a["type"], a["status"]) for a in acts])

    print("5) 版本与回滚")
    vers = await st.list_asset_versions("loop_template", "ec_cart_abandon")
    print("   版本:", [(v["version"], v["status"]) for v in vers])
    await st.put_template({**dict(await st.get_templates(enabled_only=False)[0] if False else {}),
                           **{"id": "ec_cart_abandon", "name": "改名后", "enabled": True,
                              "trigger": {"type": "event", "name": "add_to_cart"}, "actions": []}}
                          ) if False else None
    tpls = {t["id"]: t for t in await st.get_templates(enabled_only=False)}
    await st.put_template({**tpls["ec_cart_abandon"], "name": "加购挽回(已调整)"})
    vers2 = await st.list_asset_versions("loop_template", "ec_cart_abandon")
    rb = await st.restore_asset_version(vers2[1]["id"])          # 回滚到上一版
    print("   回滚:", rb["ok"], "| 名称恢复为:", (await st.get_templates(enabled_only=False))[0]["name"])
    await ts.close_all()


asyncio.run(main())

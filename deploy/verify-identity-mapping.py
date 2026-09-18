import asyncio

from userloop.actions.executors import ExecutorContext
from userloop.config import load_config
from userloop.core.bus import handle
from userloop.core.tenants import TenantStores
from userloop.touch.websflow import track_back_snippet


async def main() -> None:
    base = load_config()
    ts = TenantStores(base)
    st = await ts.get("n2demo")
    tcfg = dict(base)
    tcfg["data_dir"] = ts.registry.get("n2demo")["data_dir"]
    tcfg["db_path"] = st.path
    ctx = ExecutorContext(tcfg["data_dir"], tcfg)
    ctx.store = st

    print("== OpenFlow 身份映射（member_id ↔ email ↔ 跨设备归并）==")
    r0 = await handle(st, ctx, {"distinct_id": "of_anon_dev1", "event": "page_view",
                                "props": {"openflow_visitor_id": "of_anon_dev1"}})
    r1 = await handle(st, ctx, {"distinct_id": "of_anon_dev1", "event": "signup",
                                "email": "of-member@nownexts.com",
                                "props": {"openflow_member_id": "M-1001",
                                          "openflow_visitor_id": "of_anon_dev1"}})
    r2 = await handle(st, ctx, {"distinct_id": "of_anon_dev2", "event": "page_view",
                                "props": {"openflow_member_id": "M-1001"}})
    print("  匿名建档:", r0["user_id"])
    print("  注册实名:", r1["user_id"], "| 同档案:", r1["user_id"] == r0["user_id"])
    print("  他设备带同一 member_id:", r2["user_id"], "| 归并:", r2.get("merged") is True)
    print("  openflow_member 解析:", await st.resolve_identity("openflow_member", "M-1001"))
    print("  openflow_visitor 解析:", await st.resolve_identity("openflow_visitor", "of_anon_dev1"))
    print("  该租户用户数:", (await st.counts())["users"])

    print("== WebsFlow 表单自动实名（注入脚本能力）==")
    js = track_back_snippet("https://nownexts.com/userloop")
    print("  含 form_submit 抓取:", "form_submit" in js)
    print("  含自动 identify:", "userloop.identify" in js)
    print("  不拦截表单(无 preventDefault):", "preventDefault" not in js)
    await ts.close_all()


asyncio.run(main())

import asyncio
import json

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.config import load_config
from userloop.core.store import Store


async def main() -> None:
    cfg = load_config()
    s = Store(cfg["db_path"], cfg)
    await s.connect()
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    ctx.store = s
    user = await s.upsert_user("mflow_live", email="mflow-live@nownexts.com")
    await s.update_user(user["id"], stage="churned")
    user = await s.get_user(user["id"])

    res = await execute_action(
        ctx,
        {"type": "mflow.create_content", "payload": {
            "topic": "流失用户召回：如何用一次旅程触达唤回沉默用户",
            "brief": "面向已流失用户的内容选题，来自 UserLoop 旅程断点信号（自动化联调验证）",
            "type": "blog"}},
        {"id": "loop_mflow_live", "template_id": "churn_risk_rescue"}, user)
    print("mflow.create_content:", json.dumps(
        {k: res.get(k) for k in ("ok", "ref", "item_id", "queued", "register_ok", "pipeline_ok", "error", "dry_run")},
        ensure_ascii=False))

    if res.get("ref"):
        status = await execute_action(ctx, {"type": "mflow.content_status", "payload": {"loop_id": res["ref"]}},
                                      {"id": "loop_mflow_live"}, user)
        print("mflow.content_status:", json.dumps({k: status.get(k) for k in ("ok", "status", "error")},
                                                  ensure_ascii=False))
        print("MFLOW_LOOP_ID=" + str(res["ref"]))
    await s.close()


asyncio.run(main())

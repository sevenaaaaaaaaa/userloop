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
    u = await s.upsert_user("wf_live", email="wf-live@nownexts.com")
    await s.update_user(u["id"], stage="activated")
    res = await execute_action(
        ctx,
        {"type": "touch.h5", "payload": {
            "title": "你的 7 天专业版已到账",
            "text": "激活用户专属权益已发放。\n现在就能使用全部高级模块与自定义域名。",
            "cta_text": "立即领取", "cta_url": "https://nownexts.com/pricing"}},
        {"id": "loop_wf_live", "template_id": "ai.send_email"}, await s.get_user(u["id"]))
    print(json.dumps({k: res.get(k) for k in ("ok", "channel", "url", "provider", "page_id", "degraded", "note")},
                     ensure_ascii=False))
    await s.close()


asyncio.run(main())

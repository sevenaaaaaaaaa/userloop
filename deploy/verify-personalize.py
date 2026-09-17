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
    # 旅程阶段 = paying（会触发倒计时区块与阶段文案）
    u = await s.upsert_user("pz_live", email="pz-live@nownexts.com")
    await s.update_user(u["id"], stage="paying")
    res = await execute_action(
        ctx,
        {"type": "touch.h5", "payload": {
            "title": "你的专业版权益待领取", "text": "付费用户专属：专业版功能已解锁。\n领取后立即可用全部高级模块。",
            "cta_text": "领取权益", "cta_url": "https://nownexts.com/pricing"}},
        {"id": "loop_pz_live", "template_id": "ai.send_email"}, await s.get_user(u["id"]))
    print(json.dumps({k: res.get(k) for k in ("ok", "url", "provider", "provider_stage")}, ensure_ascii=False))
    await s.close()


asyncio.run(main())

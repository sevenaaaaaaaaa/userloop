import asyncio
import json

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.config import load_config
from userloop.core.store import Store
from userloop.touch import frequency, select


async def main() -> None:
    cfg = load_config()
    s = Store(cfg["db_path"], cfg)
    await s.connect()
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    ctx.store = s

    user = await s.upsert_user("ux_live", email="ux-live@nownexts.com")
    await s.update_user(user["id"], stage="activated")
    user = await s.get_user(user["id"])

    # 1) 先发一次邮件（真实经 OpenFlow 桥）
    r1 = await execute_action(
        ctx, {"type": "touch.email", "payload": {"subject": "第一封：权益已到账", "text": "点开领取",
                                                  "cta_text": "领取", "cta_url": "https://nownexts.com/pricing"}},
        {"id": "loop_ux1", "template_id": "ux_demo"}, user)
    print("1) 首次触达:", {k: r1.get(k) for k in ("ok", "channel", "ref")})

    # 2) 立刻再发一条（不同渠道）→ 应被全局频控拦下
    r2 = await execute_action(ctx, {"type": "touch.auto",
                                    "payload": {"intent": "reengage", "title": "第二封：再来看看", "text": "新内容"}},
                              {"id": "loop_ux2", "template_id": "ux_demo"}, user)
    print("2) 5 秒后再触达:", {k: r2.get(k) for k in ("ok", "channel", "blocked_by_frequency", "note")})

    # 3) 模板 Loop 常用的 legacy 动作（ai.email）也应被同一道门拦下
    r3 = await execute_action(ctx, {"type": "ai.email", "payload": {"subject": "第三封", "text": "x"}},
                              {"id": "loop_ux3", "template_id": "ux_demo"}, user)
    print("3) legacy ai.email:", {k: r3.get(k) for k in ("ok", "blocked_by_frequency", "note")})

    # 4) 事务类消息豁免（收据不受营销频控）
    r4 = await execute_action(ctx, {"type": "touch.email",
                                    "payload": {"subject": "订单收据", "text": "收据内容", "cta_text": "查看",
                                                "cta_url": "https://nownexts.com/pricing"}},
                              {"id": "loop_ux4", "template_id": "order_receipt"}, user)
    print("4) 事务类邮件:", {k: r4.get(k) for k in ("ok", "blocked_by_frequency")})

    # 5) Next Best Channel：给用户绑手机后，看它怎么选（此刻仍受频控约束 → 看评分本身）
    from userloop.touch import identity

    await identity.bind(s, user["id"], "phone", "13800005678", source="e2e")
    ids = await s.of_user_identities(user["id"])
    pick = await select.score(s, user, ids, ctx, intent="reengage")
    print("5) 渠道选择:", json.dumps({"chosen": pick["channel"], "rationale": pick["rationale"],
                                     "scores": pick["scores"]}, ensure_ascii=False))
    gate = await frequency.check(s, ctx, user, "email", "ux_demo")
    print("6) 频控状态:", json.dumps(gate, ensure_ascii=False))
    await s.close()


asyncio.run(main())

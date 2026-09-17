import asyncio
import json

from userloop.actions.executors import ExecutorContext, execute_action
from userloop.config import load_config
from userloop.core.store import Store
from userloop.experiments import engine as ab


async def main() -> None:
    cfg = load_config()
    s = Store(cfg["db_path"], cfg)
    await s.connect()
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    ctx.store = s
    await ab.seed_experiments(s, cfg["data_dir"])

    # 1) 40 个用户走 touch.h5 交付（自动分桶，观察各变体 CTA 文案不同）
    variants: dict[str, list[str]] = {"A": [], "B": []}
    samples = {}
    for i in range(40):
        u = await s.upsert_user(f"ab_live_{i}", email=f"ab-live-{i}@nownexts.com")
        res = await execute_action(
            ctx,
            {"type": "touch.h5", "payload": {"title": "你的权益已到账", "text": "激活用户专属 7 天专业版",
                                             "cta_text": "查看详情", "cta_url": "https://nownexts.com/pricing"}},
            {"id": f"loop_ab_live_{i}", "template_id": "tpl"}, await s.get_user(u["id"]))
        abinfo = res.get("ab") or {}
        v = abinfo.get("variant")
        if v in variants:
            variants[v].append(u["id"])
            samples.setdefault(v, res["spec"]["cta"])
    print("1) 分桶结果:", {k: len(v) for k, v in variants.items()}, "| 各变体 CTA:", samples)

    # 2) 模拟参与差异：A 组 20% 点击，B 组 50% 点击（B 显著更好）
    for variant, rate in (("A", 0.2), ("B", 0.5)):
        hits = int(len(variants[variant]) * rate)
        for uid in variants[variant][:hits]:
            u = await s.get_user(uid)
            await execute_action(ctx, {"type": "touch.h5",
                                       "payload": {"title": "x", "text": "y"}},
                                 {"id": f"loop_ab_live_{uid}", "template_id": "tpl"}, u)  # 触发分桶记录（若缺）
            await s.insert_event({"user_id": uid, "distinct_id": u["distinct_id"], "event": "h5_click",
                                  "props": {"experiment": "h5_hero_style"}, "source": "ab_test",
                                  "event_id": f"abclick-{uid}", "created_at": "2026-09-17T12:00:00Z"})
    print("2) 模拟参与完成（A 20% / B 50% 点击）")

    # 3) 判定
    verdict = await ab.evaluate(s, "h5_hero_style")
    print("3) 判定:", json.dumps({k: verdict.get(k) for k in ("verdict", "metric", "leader", "confidence", "reason")},
                                ensure_ascii=False))
    # 4) 提升 winner
    if verdict.get("verdict") == "winner":
        promoted = await ab.promote(s, "h5_hero_style")
        print("4) 提升:", promoted)
        r2 = await ab.pick(s, "h5", "tpl", "brand_new_user", "loop_x")
        print("   提升后新用户分桶:", r2[1]["id"] if r2 else None, "（应恒为 winner）")

    await s.close()


asyncio.run(main())

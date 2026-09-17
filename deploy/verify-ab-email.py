import asyncio

from userloop.config import load_config
from userloop.core.store import Store
from userloop.experiments import engine as ab
from userloop.touch.base import TouchSpec


async def main() -> None:
    cfg = load_config()
    s = Store(cfg["db_path"], cfg)
    await s.connect()
    print("邮件实验变体覆盖（仅验证槽位，不外发）：")
    for i in range(6):
        uid = f"ab_mail_probe_{i}"
        u = await s.upsert_user(uid, email=f"{uid}@nownexts.com")
        picked = await ab.pick(s, "email", "signup_no_activate", u["id"], "loop_mail_probe")
        if not picked:
            print("  未匹配实验")
            continue
        exp, variant = picked
        spec = TouchSpec(channel="email", title="快速上手", body="完成首次创建即可解锁全部功能",
                         cta_text="了解详情", template_id="signup_no_activate")
        ab.apply_variant(spec, variant)
        print("  用户%s: 变体%s(%s) -> CTA=[%s]" % (i, variant["id"], variant.get("name"), spec.cta_text))
    print("邮件实验分桶:", await s.assignment_counts("email_cta_urgency"))
    await s.close()


asyncio.run(main())

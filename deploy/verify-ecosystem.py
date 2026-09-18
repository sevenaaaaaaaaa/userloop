import asyncio

from userloop.config import load_config
from userloop.core.store import Store


async def main() -> None:
    cfg = load_config()
    s = Store(cfg["db_path"], cfg)
    await s.connect()
    rows = await s.list_external_insights(limit=30)
    tpls = {t["id"]: t for t in await s.get_templates(enabled_only=False)}
    print("镜像洞察:", len(rows), "| 含来源 Loop 的草稿:",
          sum(1 for t in tpls.values() if t.get("source_insight")))
    for r in rows[:10]:
        t = tpls.get(r.get("loop_id") or "", {})
        print("  [%-8s] %-24s -> %s" % (r.get("severity"), r.get("type"), t.get("name", "?")))
    pubs = await s.list_publications(limit=5)
    print("内容发布记录:", len(pubs), [(p["item_id"], p["status"]) for p in pubs])
    await s.close()


asyncio.run(main())

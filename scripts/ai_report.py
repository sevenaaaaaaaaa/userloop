import asyncio
import json

from userloop.config import load_config
from userloop.core.store import Store


async def main() -> None:
    cfg = load_config("/www/wwwroot/userloop/data")
    s = Store(cfg["db_path"])
    await s.connect()
    rows = await s.list_ai_decisions(limit=5)
    for r in rows:
        print("[%16s] intent=%-16s risk=%-7s conf=%.2f user=%s" % (
            r["status"], r["intent"], r["risk"], r["confidence"] or 0, r["user_id"]))
        print("   理由:", (r["reasoning"] or "")[:140].replace("\n", " "))
        if r.get("payload"):
            print("   动作:", json.dumps(json.loads(r["payload"]), ensure_ascii=False)[:140])
        if r.get("loop_id"):
            print("   Loop:", r["loop_id"])
    print("统计:", await s.ai_decision_counts())
    await s.close()


asyncio.run(main())

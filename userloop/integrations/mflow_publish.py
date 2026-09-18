"""MFlow 内容发布回流 → UserLoop 验证窗口（生态通道④：效果回流的内容侧）.

链路：MFlow 发布成稿 → 其 `publish_adapters/webhook.py` POST 到
`https://nownexts.com/userloop/api/v1/hub/mflow/publish`（带 X-UserLoop-Token）
→ 本模块登记发布 + 开启验证窗口 → 到期对比窗口内外的事件量 → verdict 写 feedback。

度量口径（可解释）：
- 归因信号：事件 props 命中内容 URL 或 utm_campaign=item_id
- effectiveness：窗口内命中事件数 > 基线（等长前置窗口）× 1.2 → effective；否则 neutral
  （内容不像 1v1 触达有明确目标事件，故用"关注度增量"作代理指标）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from userloop.core.store import Store, iso_now, new_id

DEFAULT_WINDOW_DAYS = 14


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


async def record_publish(store: Store, payload: dict[str, Any],
                         window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """登记一条内容发布并开启验证窗口（幂等：同 source+item_id 覆盖更新）。"""
    item_id = str(payload.get("item_id") or payload.get("id") or "").strip()
    if not item_id:
        return {"ok": False, "error": "缺少 item_id"}
    published_at = str(payload.get("published_at") or iso_now())
    base = _parse(published_at) or datetime.utcnow()
    pub = {
        "id": new_id("pub"), "source": str(payload.get("source") or "mflow"), "item_id": item_id,
        "title": str(payload.get("title") or payload.get("topic") or "")[:200],
        "url": str(payload.get("url") or payload.get("canonical_url") or "")[:500],
        "published_at": published_at,
        "verify_before": (base + timedelta(days=int(payload.get("window_days") or window_days)))
        .isoformat(timespec="seconds") + "Z",
    }
    row = await store.record_publication(pub)
    return {"ok": True, "publication": row}


async def verify_due_publications(store: Store, limit: int = 50) -> list[dict]:
    """验证窗口到期 → 计算 verdict → 写 content_publications + feedback（供报告/统计）。"""
    now_iso = iso_now()
    cur = await store.db.execute(
        "SELECT * FROM content_publications WHERE status='verifying' AND verify_before<=? LIMIT ?",
        (now_iso, limit))
    rows = [dict(r) for r in await cur.fetchall()]
    out: list[dict] = []
    for pub in rows:
        window_end = pub["verify_before"]
        start = _parse(pub["published_at"]) or datetime.utcnow()
        end = _parse(window_end) or datetime.utcnow()
        span = max(timedelta(hours=1), end - start)
        base_start = (start - span).isoformat(timespec="seconds") + "Z"
        base_end = pub["published_at"]

        pairs = [("utm_campaign", pub["item_id"])]
        if pub["url"]:
            pairs.append(("url", pub["url"]))
        # OR 去重：同一事件同时命中两个信号只计一次
        hits = await store.events.count_matching_any(pairs, pub["published_at"], window_end)
        baseline = await store.events.count_matching_any(
            [("url", pub["url"])] if pub["url"] else [], base_start, base_end)

        verdict = "effective" if hits > max(1, baseline) * 1.2 else "neutral"
        evidence = {"hits": hits, "baseline": baseline, "window": [pub["published_at"], window_end],
                    "signals": ["utm_campaign=item_id", "props.url==published_url"]}
        await store.update_publication(pub["id"], status="verified", verdict=verdict,
                                       evidence=__import__("json").dumps(evidence, ensure_ascii=False))
        await store.insert_feedback({
            "loop_id": f"content:{pub['item_id']}", "template_id": f"content.{pub['source']}",
            "verdict": verdict, "goal_event": None, "evidence": evidence, "created_at": iso_now(),
        })
        out.append({**pub, "verdict": verdict, "evidence": evidence})
    return out

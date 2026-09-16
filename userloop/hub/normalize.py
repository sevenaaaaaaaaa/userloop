"""全域营销数据中枢 —— 多源入站归一化（对齐 OpenFlow IngestAdapters 的格式感知思路）.

职责：把各渠道异构 payload 归一化为标准 ingest 事件（distinct_id/event/props/source），
再交给事件总线（CDP 建档 → 旅程 → 断点 → Loop）。UserLoop 自有 CDP，不依赖任何外部系统。

支持的源（可经 data/hub/README 扩展）：
  segment     Segment/Mixpanel 风格  {userId|anonymousId, event, properties}
  ga4         GA4 MP 风格            {client_id, events:[{name, params}]}
  shopify     Shopify 订单 webhook   {email, financial_status, total_price, line_items,...}
  hubspot     HubSpot webhook        [{subscriptionType, objectId, properties:{...}}]
  神策/cn     {distinct_id, event, properties}
  generic     兜底                   {distinct_id|email, event, props}
"""

from __future__ import annotations

from typing import Any

SOURCE_TYPES = ("segment", "ga4", "shopify", "hubspot", "generic")

# 电商状态 → 旅程事件
_SHOP_STATUS_MAP = {"paid": "purchase", "partially_paid": "purchase", "refunded": "refund"}


def detect_source(payload: dict | list) -> str:
    """自动识别外部数据源格式（open_source 显式指定优先于猜测）。"""
    if isinstance(payload, list):
        first = payload[0] if payload else {}
        if isinstance(first, dict) and ("subscriptionType" in first or "subscription_type" in first):
            return "hubspot"
        return "generic"
    if not isinstance(payload, dict):
        return "generic"
    if "events" in payload and isinstance(payload.get("events"), list):
        first = payload["events"][0] if payload["events"] else {}
        if "params" in first:
            return "ga4"
    if "properties" in payload and ("distinct_id" in payload or "userId" in payload):
        return "segment"
    if "financial_status" in payload and ("email" in payload or "customer" in payload):
        return "shopify"
    if "subscriptionType" in payload:
        return "hubspot"
    return "generic"


def normalize(source: str, payload: Any) -> list[dict]:
    """归一化为标准 ingest 事件列表（无身份的纯指标行返回空，调用方落原始档案）。"""
    if source == "cn_analytics":
        source = "segment"
    handler = _HANDLERS.get(source)
    if not handler:
        return []
    out = []
    for e in handler(payload):
        if e.get("event") and e.get("distinct_id"):
            e.setdefault("source", f"hub:{source}")
            out.append(e)
    return out


def _identity(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return None


def _norm_segment(payload: dict) -> list[dict]:
    did = payload.get("userId") or payload.get("anonymous_id") or payload.get("anonymousId")
    props = dict(payload.get("properties") or {})
    email = payload.get("email") or payload.get("context", {}).get("traits", {}).get("email")
    return [{"distinct_id": did, "event": payload.get("event"), "props": props, "email": email}]


def _norm_ga4(payload: dict) -> list[dict]:
    out = []
    did = payload.get("client_id") or payload.get("clientId") or payload.get("user_id")
    email = payload.get("user_email")
    for ev in payload.get("events") or []:
        params = dict(ev.get("params") or {})
        out.append({"distinct_id": did, "event": ev.get("name"), "props": params, "email": email})
    return out


def _norm_shopify(payload: dict) -> list[dict]:
    """订单 webhook → purchase/refund（旅程关键事件）。"""
    status = (payload.get("financial_status") or "").lower()
    email = payload.get("email") or (payload.get("customer") or {}).get("email")
    event = _SHOP_STATUS_MAP.get(status)
    if not event:
        return []
    props = {
        "order_id": payload.get("id") or payload.get("order_number"),
        "amount": payload.get("total_price") or payload.get("current_total_price"),
        "currency": payload.get("currency"),
        "items": [{"title": i.get("title"), "qty": i.get("quantity"),
                   "price": i.get("price")} for i in (payload.get("line_items") or [])][:10],
    }
    did = payload.get("distinct_id") or email
    return [{"distinct_id": did, "event": event, "props": props}]


def _norm_hubspot(payload: Any) -> list[dict]:
    """HubSpot webhook 批量 → ma_contact_sync / 表单提交事件（email 做身份）。"""
    items = payload if isinstance(payload, list) else [payload]
    out = []
    for item in items:
        props = item.get("properties") or {}
        email = props.get("email")
        sub = item.get("subscriptionType") or item.get("subscription_type") or ""
        event = "form_submit" if "form" in sub else "ma_contact_sync"
        merged = {k: v for k, v in props.items() if k != "email"}
        merged["subscription_type"] = sub
        out.append({"distinct_id": email or f"hs_{item.get('objectId')}", "event": event or "ma_contact_sync",
                    "props": merged, "email": email})
    return out


def _norm_cn(payload: dict) -> list[dict]:
    """神策/GrowingIO 风格 {distinct_id, event, properties}。"""
    return [{"distinct_id": payload.get("distinct_id"), "event": payload.get("event"),
             "props": dict(payload.get("properties") or {})}]


def _norm_generic(payload: dict) -> list[dict]:
    did = payload.get("distinct_id") or payload.get("user_id") or payload.get("anonymous_id") \
          or payload.get("email")
    return [{"distinct_id": did, "event": payload.get("event"),
             "props": dict(payload.get("props") or payload.get("properties") or {}),
             "email": payload.get("email")}]


_HANDLERS = {
    "segment": _norm_segment,
    "ga4": _norm_ga4,
    "shopify": _norm_shopify,
    "hubspot": _norm_hubspot,
    "cn_analytics": _norm_cn,
    "generic": _norm_generic,
}

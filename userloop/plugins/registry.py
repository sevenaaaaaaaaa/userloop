"""插件注册表：内置适配器包装 + data/plugins/*.json 扩展。

四类：
- source   入站源（接到 /hub/ingest）
- action   出站动作（plugin.<id> → webhook）
- model    预测/生成后端（声明能力，不替换现有回退规则）
- template 运营资产包引用
纪律：只流转定义；第三方插件不得带用户数据；动作必须过白名单。
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext

KINDS = ("source", "action", "model", "template")

_BUILTIN: list[dict[str, Any]] = [
    {"id": "source.shopify", "kind": "source", "name": "Shopify 订单",
     "version": "1.0.0", "enabled": True,
     "endpoint": "/api/v1/hub/ingest?source=shopify",
     "description": "订单 webhook → purchase/refund"},
    {"id": "source.hubspot", "kind": "source", "name": "HubSpot 联系人",
     "version": "1.0.0", "enabled": True,
     "endpoint": "/api/v1/hub/ingest?source=hubspot",
     "description": "联系人/表单 webhook → 实名建档"},
    {"id": "source.segment", "kind": "source", "name": "Segment / 神策",
     "version": "1.0.0", "enabled": True,
     "endpoint": "/api/v1/hub/ingest?source=segment",
     "description": "CDP 事件信封归一化"},
    {"id": "action.mflow", "kind": "action", "name": "MFlow 产稿",
     "version": "1.0.0", "enabled": True,
     "action_type": "mflow.create_content",
     "description": "旅程断点 → 内容选题（不代发）"},
    {"id": "action.openflow", "kind": "action", "name": "OpenFlow 画布",
     "version": "1.0.0", "enabled": True,
     "action_type": "openflow.automation",
     "description": "信号回推 OpenFlow flow_handle"},
    {"id": "action.hubspot", "kind": "action", "name": "HubSpot 同步",
     "version": "1.0.0", "enabled": True,
     "action_type": "ma.hubspot.contact_upsert",
     "description": "生命周期同步到外部 MA"},
    {"id": "model.rules_v1", "kind": "model", "name": "规则预测 v1",
     "version": "1.0.0", "enabled": True,
     "provides": ["churn", "ltv", "propensity"],
     "description": "可解释规则分，样本不足时的默认后端"},
    {"id": "model.logit_v2", "kind": "model", "name": "逻辑回归 v2",
     "version": "1.0.0", "enabled": True,
     "provides": ["churn", "propensity"],
     "description": "回放训练，样本不足自动回退规则"},
    {"id": "template.ecommerce-growth", "kind": "template", "name": "电商增长包",
     "version": "1.0.0", "enabled": True, "pack_id": "ecommerce-growth",
     "description": "N2 资产包：加购/首单/复购"},
    {"id": "template.saas-onboarding", "kind": "template", "name": "SaaS 增长包",
     "version": "1.0.0", "enabled": True, "pack_id": "saas-onboarding",
     "description": "N2 资产包：激活/试用/召回"},
]


def _disk(data_dir: str) -> list[dict[str, Any]]:
    folder = os.path.join(data_dir or "data", "plugins")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                item = json.load(f)
            if isinstance(item, dict) and item.get("id") and item.get("kind") in KINDS:
                out.append(item)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load(data_dir: str) -> list[dict[str, Any]]:
    """内置 + 磁盘，同 id 磁盘覆盖。"""
    by_id = {p["id"]: dict(p) for p in _BUILTIN}
    for p in _disk(data_dir):
        by_id[p["id"]] = {**by_id.get(p["id"], {}), **p}
    return list(by_id.values())


def get(data_dir: str, plugin_id: str) -> dict[str, Any] | None:
    return next((p for p in load(data_dir) if p["id"] == plugin_id), None)


def summarize(p: dict) -> dict[str, Any]:
    return {"id": p["id"], "kind": p["kind"], "name": p.get("name"), "version": p.get("version"),
            "enabled": bool(p.get("enabled", True)), "description": p.get("description", "")}


def _dir(cfg_or_dir: Any) -> str:
    if isinstance(cfg_or_dir, dict):
        return str(cfg_or_dir.get("data_dir") or "data")
    return str(cfg_or_dir or "data")


def list_plugins(cfg_or_dir: Any) -> list[dict[str, Any]]:
    return [summarize(p) for p in load(_dir(cfg_or_dir))]


def get_plugin(cfg_or_dir: Any, plugin_id: str) -> dict[str, Any] | None:
    p = get(_dir(cfg_or_dir), plugin_id)
    return {**summarize(p), **{k: p.get(k) for k in ("endpoint", "action_type", "webhook", "provides", "pack_id")
                               if p.get(k)}} if p else None


async def execute_action(ctx: ExecutorContext, action: dict, loop: dict, user: dict) -> dict[str, Any]:
    """plugin.<id> → 第三方 webhook（HMAC 可选）。失败降级，不中断闭环。"""
    pid = str(action.get("type") or "").split(".", 1)[-1]
    if str(action.get("type") or "").startswith("plugin."):
        pid = str(action["type"])[7:]
    plugin = get(ctx.config.get("data_dir") or "data", pid) or get(
        ctx.config.get("data_dir") or "data", f"action.{pid}")
    if not plugin or not plugin.get("enabled", True):
        return {"ok": False, "error": f"插件不存在或未启用：{pid}"}
    if plugin.get("action_type") and not plugin.get("webhook"):
        return {"ok": True, "dry_run": True,
                "note": f"请直接使用内置动作 {plugin['action_type']}"}
    raw_payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    url = plugin.get("webhook") or raw_payload.get("webhook")
    if not url:
        return {"ok": True, "dry_run": True, "note": "插件未配置 webhook"}
    safe_payload = {k: v for k, v in raw_payload.items() if k != "_transport"}
    body = {"event": "plugin.action", "plugin": plugin["id"], "user": {"id": user.get("id"),
            "email": user.get("email"), "stage": user.get("stage")},
            "loop_id": loop.get("id"), "payload": safe_payload}
    transport = raw_payload.get("_transport")
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.post(url, json=body)
        return {"ok": 200 <= resp.status_code < 300, "status_code": resp.status_code,
                "ref": plugin["id"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}

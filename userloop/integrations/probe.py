"""契约探测：探测家族系统线上 API 是否可达，断链写入自诊断。

原则：
- 只调对方公开 HTTP，不碰代码/数据库
- 同机优先 internal_url（127.0.0.1），公网地址留给外部
- 未配置的集成记 skipped，不算故障
- 探测结果落 data/telemetry/probes-latest.json，供诊断与控制台读取
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from userloop.actions.executors import ExecutorContext
from userloop.core.store import iso_now

# HTTP 响应视为「对端活着」：鉴权失败/方法不允许都说明服务在
_UP = {200, 201, 202, 204, 301, 302, 303, 307, 308, 400, 401, 403, 405, 409, 422}
_WARN = {401, 403}  # 活着但鉴权对不上


def capabilities(version: str = "") -> dict[str, Any]:
    """本系统契约广告（无密钥）。家族其它系统可按此探测。"""
    from userloop.ai.copilot import ALLOWED_ACTIONS

    ver = version or _read_version()
    return {
        "system": "userloop",
        "version": ver,
        "ingest": ["/api/v1/ingest", "/api/v1/track", "/api/v1/hub/ingest",
                   "/api/v1/hub/mflow/publish", "/api/v1/hub/message"],
        "identity": ["/api/v1/identify"],
        "actions": sorted(ALLOWED_ACTIONS),
        "identity_types": ["email", "phone", "openflow_member", "openflow_visitor",
                           "websflow_visitor", "mflow_item"],
    }


def _read_version() -> str:
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "VERSION"))
    try:
        return open(here, encoding="utf-8").read().strip() or "0"
    except OSError:
        return "0"


def _integ(ctx: ExecutorContext, spec: dict) -> dict:
    path = spec.get("cfg_path")
    if path:
        cur: Any = ctx.config
        for key in path:
            cur = (cur or {}).get(key) if isinstance(cur, dict) else {}
        return cur if isinstance(cur, dict) else {}
    raw = ((ctx.config.get("integrations") or {}).get(spec.get("cfg") or "")) or {}
    return raw if isinstance(raw, dict) else {}


def _base(cfg: dict, *keys: str) -> str:
    for key in keys:
        v = str(cfg.get(key) or "").rstrip("/")
        if v:
            return v
    return ""


async def _http(method: str, url: str, headers: dict | None = None, transport: Any = None,
                json_body: dict | None = None) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=6, transport=transport) as client:
            resp = await client.request(method, url, headers=headers or {}, json=json_body)
        ms = int((time.monotonic() - t0) * 1000)
        code = resp.status_code
        if code in _UP:
            status = "warn" if code in _WARN else "ok"
            return {"status": status, "http": code, "ms": ms}
        return {"status": "down", "http": code, "ms": ms, "error": f"HTTP {code}"}
    except Exception as exc:  # noqa: BLE001
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "down", "http": 0, "ms": ms, "error": str(exc)[:160]}


async def probe_one(ctx: ExecutorContext, spec: dict, transport: Any = None) -> dict[str, Any]:
    """探测一条契约。spec: id/name/cfg/url_keys/path/fallback/headers/method。"""
    cfg = _integ(ctx, spec)
    base = _base(cfg, "internal_url", *spec.get("url_keys", ("base_url",)))
    out: dict[str, Any] = {"id": spec["id"], "name": spec["name"], "at": iso_now()}
    if spec.get("need") and not all(cfg.get(k) for k in spec["need"]):
        return {**out, "status": "skipped", "note": "未配置"}
    if not base:
        return {**out, "status": "skipped", "note": "未配置"}
    headers = {}
    for hk, ck in (spec.get("headers") or {}).items():
        val = cfg.get(ck) or ""
        if not val:
            continue
        if hk.lower() == "authorization" and not str(val).lower().startswith("bearer "):
            headers[hk] = f"Bearer {val}"
        else:
            headers[hk] = val
    method = spec.get("method") or "GET"

    def _with_query(path: str) -> str:
        extra = []
        for qk, ck in (spec.get("query") or {}).items():
            qv = cfg.get(ck)
            if qv:
                extra.append(f"{qk}={qv}")
        if not extra:
            return path
        return path + ("&" if "?" in path else "?") + "&".join(extra)

    paths = [_with_query(spec["path"])] + [_with_query(p) for p in (spec.get("fallback") or [])]
    last: dict[str, Any] = {"status": "down", "error": "no path"}
    for path in paths:
        url = f"{base}{path}"
        last = await _http(method, url, headers=headers, transport=transport)
        last["url"] = _safe_url(url)
        if last["status"] in ("ok", "warn"):
            return {**out, **last}
        # 404 试下一条；其它 down 也试 fallback
        if last.get("http") not in (404, 0) and last["status"] == "down" and not spec.get("fallback"):
            break
    return {**out, **last}


def _safe_url(url: str) -> str:
    """回显用：去掉 query 里可能的 token。"""
    return url.split("?")[0]


def specs() -> list[dict[str, Any]]:
    return [
        {"id": "openflow.bridge", "name": "OpenFlow 桥接", "cfg": "openflow",
         "url_keys": ("internal_url", "base_url"),
         "path": "/api/plugin/userloop-bridge/capabilities",
         "fallback": ["/api/plugin/userloop-bridge/automation"],
         "headers": {"X-UserLoop-Bridge": "bridge_token"},
         "need": ("base_url",)},
        {"id": "mflow.api", "name": "MFlow API", "cfg": "mflow",
         "url_keys": ("internal_url", "base_url"),
         "path": "/api/login",
         "method": "GET",
         "need": ("base_url",)},
        {"id": "websflow.api", "name": "WebsFlow API", "cfg": "websflow",
         "cfg_path": ("touch", "h5"),
         "url_keys": ("internal_url", "api_base"),
         "path": "/projects",
         "need": ("api_base",)},
        {"id": "inflow.insights", "name": "inFlow 洞察", "cfg": "inflow",
         "url_keys": ("internal_url", "base_url"),
         "path": "/api/v1/insights?limit=1",
         "query": {"workspace_id": "workspace_id"},
         "headers": {"Authorization": "token"},
         "need": ("base_url", "workspace_id")},
    ]


async def run(ctx: ExecutorContext, transport: Any = None) -> dict[str, Any]:
    """探测全部已配置集成，落盘并返回快照。"""
    results = [await probe_one(ctx, spec, transport=transport) for spec in specs()]
    down = [r for r in results if r.get("status") == "down"]
    warn = [r for r in results if r.get("status") == "warn"]
    snapshot = {
        "at": iso_now(),
        "ok": not down,
        "results": results,
        "counts": {
            "ok": len([r for r in results if r.get("status") == "ok"]),
            "warn": len(warn),
            "down": len(down),
            "skipped": len([r for r in results if r.get("status") == "skipped"]),
        },
    }
    _save(ctx, snapshot)
    return snapshot


def load_latest(ctx: ExecutorContext) -> dict[str, Any] | None:
    path = _path(ctx)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _path(ctx: ExecutorContext) -> str:
    return os.path.join(ctx.config.get("data_dir") or "data", "telemetry", "probes-latest.json")


def _save(ctx: ExecutorContext, snapshot: dict) -> None:
    path = _path(ctx)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

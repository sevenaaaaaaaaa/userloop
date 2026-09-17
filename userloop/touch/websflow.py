"""WebsFlow 客户端：复用其后台能力生成 H5/落地页（建项目 → 发布 → 取公网链接）.

对接真实接口（已实测）：
  POST {api_base}/users/login    {email, password}         → {token}
  POST {api_base}/projects       {name, mode, data, description} → {project:{id}}
  POST {api_base}/projects/{id}/publish                    → {token, url}
  公网页：{public_base}/{share_token}（SSR，Apache 已反代 /webflow/p/ → :3001）

区块契约（来自其 schema）：blocks: [{id, type, variant?, props, audience?}]，
常用类型：nav / hero / text / features / stats / cta / faq / footer（modes 含 h5）。
cta 区块的 props.goalId 会在点击时向 WebsFlow 统计端点上报转化。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}   # email -> (token, expire_ts)


async def login(cfg: dict, transport: Any = None, force: bool = False) -> str | None:
    """登录取 JWT（带进程内缓存，默认 6 小时）。"""
    email = str(cfg.get("email") or "")
    password = str(cfg.get("password") or "")
    if not email or not password:
        return None
    cached = _TOKEN_CACHE.get(email)
    if cached and not force and cached[1] > time.time():
        return cached[0]
    base = str(cfg.get("api_base") or "").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=12, transport=transport) as client:
            resp = await client.post(f"{base}/users/login", json={"email": email, "password": password})
        data = resp.json() if resp.status_code == 200 else {}
        token = data.get("token") or (data.get("data") or {}).get("token")
        if token:
            _TOKEN_CACHE[email] = (token, time.time() + 6 * 3600)
            return token
    except Exception:  # noqa: BLE001
        return None
    return None


def build_page_data(spec_title: str, spec_body: str, cta_text: str, cta_link: str,
                    goal_id: str = "", brand: str = "", mode: str = "h5") -> dict:
    """按 WebsFlow 区块契约拼装页面（内容槽位 → 区块 props）。"""
    paragraphs = [p.strip() for p in (spec_body or "").split("\n") if p.strip()]
    blocks: list[dict[str, Any]] = [
        {"id": "b_hero", "type": "hero", "variant": "center",
         "props": {"badge": "", "title": spec_title or "专为你准备",
                   "subtitle": paragraphs[0] if paragraphs else "",
                   "btnText": cta_text or "立即查看", "btnLink": cta_link or "#",
                   "bgType": "gradient", "gradient": "indigo", "align": "center"}},
    ]
    if len(paragraphs) > 1:
        blocks.append({"id": "b_text", "type": "text",
                       "props": {"title": "", "body": "\n".join(paragraphs[1:]), "align": "center"}})
    blocks.append({"id": "b_cta", "type": "cta", "variant": "panel",
                   "props": {"title": spec_title or "现在就开始", "subtitle": "",
                             "btnText": cta_text or "立即查看", "btnLink": cta_link or "#",
                             "style": "gradient", "goalId": goal_id}})
    if brand:
        blocks.append({"id": "b_footer", "type": "footer",
                       "props": {"brand": brand, "desc": "", "links": [], "copyright": f"© {brand}"}})
    return {"pages": [{"id": "main", "name": "首页", "slug": "", "blocks": blocks}]}


async def create_and_publish(cfg: dict, name: str, data: dict, description: str = "",
                             transport: Any = None) -> dict[str, Any]:
    """建项目并发布，返回 {ok, url, share_token, project_id, error}。"""
    base = str(cfg.get("api_base") or "").rstrip("/")
    public_base = str(cfg.get("public_base") or "").rstrip("/")
    mode = str(cfg.get("project_mode") or "h5")
    token = await login(cfg, transport=transport)
    if not token:
        return {"ok": False, "error": "WebsFlow 登录失败（检查 api_base/email/password）"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            r1 = await client.post(f"{base}/projects",
                                   json={"name": name, "mode": mode, "data": data, "description": description},
                                   headers=headers)
            d1 = r1.json() if r1.status_code in (200, 201) else {}
            project = d1.get("project") or {}
            pid = project.get("id")
            if not pid:
                return {"ok": False, "error": d1.get("error") or f"建项目失败 HTTP {r1.status_code}"}
            r2 = await client.post(f"{base}/projects/{pid}/publish", headers=headers)
            d2 = r2.json() if r2.status_code == 200 else {}
            share = d2.get("token") or project.get("share_token")
            if not share:
                return {"ok": False, "error": d2.get("error") or f"发布失败 HTTP {r2.status_code}", "project_id": pid}
        url = f"{public_base}/{share}"
        return {"ok": True, "url": url, "share_token": share, "project_id": pid}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"WebsFlow 调用失败：{exc}"}

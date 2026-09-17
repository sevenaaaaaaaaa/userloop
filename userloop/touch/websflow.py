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
                    goal_id: str = "", brand: str = "", mode: str = "h5",
                    stage: str = "visitor", urg: bool = False) -> dict:
    """按 WebsFlow 区块契约拼装**千人千面**页面（内容槽位 → 区块 props）。

    - 设备：移动/桌面双版主视觉（SSR audience 互斥显示）
    - 来源/UTM/回访/登录/阶段：personalize 运行时补丁（只改文案，不重复区块）
    - 互动时长：紧迫阶段追加 countdown 区块（内置页另有 N 秒切换 CTA 的脚本）
    """
    from userloop.touch import personalize as pz

    paragraphs = [p.strip() for p in (spec_body or "").split("\n") if p.strip()]
    title = spec_title or "专为你准备"
    subtitle = paragraphs[0] if paragraphs else ""
    blocks: list[dict[str, Any]] = pz.hero_pair(title, subtitle, cta_text or "立即查看", cta_link or "#", stage)
    if len(paragraphs) > 1:
        blocks.append({"id": "b_text", "type": "text",
                       "personalize": pz.personalize_rules(stage),
                       "props": {"title": "", "body": "\n".join(paragraphs[1:]), "align": "center"}})
    blocks.append(pz.cta_block(title, cta_text or "立即查看", cta_link or "#", goal_id, stage))
    if urg:
        blocks.append(pz.countdown_block(minutes=30, title="权益领取倒计时"))
    if brand:
        blocks.append({"id": "b_footer", "type": "footer",
                       "props": {"brand": brand, "desc": "", "links": [], "copyright": f"© {brand}"}})
    return {"segments": pz.segments(), "pages": [{"id": "main", "name": "首页", "slug": "", "blocks": blocks}]}


async def unpublish_other_projects(cfg: dict, token: str, keep_id: str | None = None,
                                   transport: Any = None) -> int:
    """发布额度不足时，先下架我们自己的其他已发布项目（保持 1 个在线）。"""
    base = str(cfg.get("api_base") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    freed = 0
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            resp = await client.get(f"{base}/projects", headers=headers)
            for p in (resp.json().get("projects") or []):
                if p.get("published") and p.get("id") != keep_id:
                    await client.post(f"{base}/projects/{p['id']}/unpublish", headers=headers)
                    freed += 1
    except Exception:  # noqa: BLE001
        return freed
    return freed


async def publish_project(cfg: dict, project_id: str, token: str, transport: Any = None) -> dict[str, Any]:
    """发布项目；额度不足时自动下架我们的旧页后重试。"""
    base = str(cfg.get("api_base") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _publish() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            r = await client.post(f"{base}/projects/{project_id}/publish", headers=headers)
            d = r.json() if r.status_code == 200 else {}
            if d.get("token"):
                return {"ok": True, "share_token": d["token"]}
            return {"ok": False, "error": d.get("error") or f"HTTP {r.status_code}", "quota": bool(d.get("quota"))}

    res = await _publish()
    if not res.get("ok") and res.get("quota"):
        await unpublish_other_projects(cfg, token, keep_id=project_id, transport=transport)
        res = await _publish()
    return res


async def create_project(cfg: dict, name: str, data: dict, description: str = "",
                         transport: Any = None) -> dict[str, Any]:
    """建项目（不发布）；返回 {ok, project_id, token, error}。"""
    base = str(cfg.get("api_base") or "").rstrip("/")
    token = await login(cfg, transport=transport)
    if not token:
        return {"ok": False, "error": "WebsFlow 登录失败（检查 api_base/email/password）"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    mode = str(cfg.get("project_mode") or "h5")
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            r1 = await client.post(f"{base}/projects",
                                   json={"name": name, "mode": mode, "data": data, "description": description},
                                   headers=headers)
            d1 = r1.json() if r1.status_code in (200, 201) else {}
            pid = (d1.get("project") or {}).get("id")
            if not pid:
                return {"ok": False, "error": d1.get("error") or f"建项目失败 HTTP {r1.status_code}",
                        "quota": bool(d1.get("quota"))}
        return {"ok": True, "project_id": pid, "token": token}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"WebsFlow 调用失败：{exc}"}


async def create_and_publish(cfg: dict, name: str, data: dict, description: str = "",
                             transport: Any = None) -> dict[str, Any]:
    """建项目并发布，返回 {ok, url, share_token, project_id, error}。"""
    public_base = str(cfg.get("public_base") or "").rstrip("/")
    created = await create_project(cfg, name, data, description, transport=transport)
    if not created.get("ok"):
        return created
    pub = await publish_project(cfg, created["project_id"], created["token"], transport=transport)
    if not pub.get("ok"):
        return {"ok": False, "error": pub.get("error") or "发布失败",
                "project_id": created["project_id"], "quota": pub.get("quota")}
    share = pub["share_token"]
    return {"ok": True, "url": f"{public_base}/{share}", "share_token": share,
            "project_id": created["project_id"]}

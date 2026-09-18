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


_TRACK_BACK = """(function(){
  function load(){
    var s=document.createElement('script');s.src='__UL__/track.js';s.defer=true;
    document.head.appendChild(s);
    var t=setInterval(function(){if(window.userloop){clearInterval(t);hook();}},300);
    setTimeout(function(){clearInterval(t);},8000);
  }
  function pick(form,sel){try{var e=form.querySelector(sel);return e&&e.value?String(e.value).trim():'';}catch(e){return '';}}
  function scan(form){
    var email=pick(form,'input[type=email],input[name*=email i],input[id*=email i]');
    var phone=pick(form,'input[type=tel],input[name*=phone i],input[name*=mobile i],input[id*=phone i],input[id*=mobile i]');
    if(!email||!phone){
      var ins=(form.querySelectorAll?form.querySelectorAll('input'):[]);
      for(var i=0;i<ins.length;i++){var v=(ins[i].value||'').trim();
        if(!email&&/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(v))email=v;
        if(!phone&&/^\\+?\\d[\\d\\s-]{5,}$/.test(v))phone=v;}
    }
    return {email:email,phone:phone};
  }
  function hook(){
    try{var v=localStorage.getItem('wf_vid')||'';
      if(v)window.userloop.track('websflow_link',{visitor_id:v});}catch(e){}
    // 表单提交：抓取邮箱/手机号自动实名（不拦截提交，失败静默）
    document.addEventListener('submit',function(ev){
      var form=ev.target;
      if(!form||form.tagName!=='FORM')return;
      var id=scan(form);
      try{window.userloop.track('form_submit',
        {page:location.pathname,has_email:!!id.email,has_phone:!!id.phone});}catch(e){}
      try{if(id.email||id.phone)window.userloop.identify({email:id.email,phone:id.phone});}catch(e){}
    },true);
  }
  if(document.readyState==='complete')load();else window.addEventListener('load',load);
})();"""


def track_back_snippet(ul_base: str) -> str:
    """WebsFlow 页面回传脚本（注入 data.global.tracking.custom）。

    - 载入 UserLoop tracker（page_view / element_click 全量回流）
    - 把 WebsFlow 的访客 id（localStorage.wf_vid）作为 visitor_id 上报
      → 事件总线按 websflow_visitor 绑定身份，实现跨系统身份链接
    - **表单提交自动实名**：抓取表单里的邮箱/手机号 → `form_submit` 事件 + identify
      （不拦截表单提交，失败静默；这是提升识别率的关键一环）
    """
    return _TRACK_BACK.replace("__UL__", ul_base.rstrip("/"))


def build_page_data(spec_title: str, spec_body: str, cta_text: str, cta_link: str,
                    goal_id: str = "", brand: str = "", mode: str = "h5",
                    stage: str = "visitor", urg: bool = False,
                    track_back: bool = True, ul_base: str = "") -> dict:
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
    data: dict[str, Any] = {"segments": pz.segments(),
                            "pages": [{"id": "main", "name": "首页", "slug": "", "blocks": blocks}]}
    if track_back and ul_base:
        # 官方扩展点：WebsFlow 发布页支持 global.tracking.custom（自定义脚本注入）
        data["global"] = {"tracking": {"custom": track_back_snippet(ul_base.rstrip("/"))}}
    return data


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


async def delete_own_projects(cfg: dict, token: str, keep_id: str | None = None,
                              only_unpublished: bool = True, transport: Any = None) -> int:
    """额度不足时清理我们自己的旧项目（默认只删未发布的，保留已发布页）。"""
    base = str(cfg.get("api_base") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    freed = 0
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            resp = await client.get(f"{base}/projects", headers=headers)
            for p in (resp.json().get("projects") or []):
                if p.get("id") == keep_id:
                    continue
                if only_unpublished and p.get("published"):
                    continue
                r = await client.delete(f"{base}/projects/{p['id']}", headers=headers)
                if r.status_code in (200, 204):
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
            try:
                d = r.json()
            except ValueError:
                d = {}
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
            try:
                d1 = r1.json()
            except ValueError:
                d1 = {}
            pid = (d1.get("project") or {}).get("id")
            if not pid and d1.get("quota"):
                # 项目额度不足 → 清理自有未发布项目后重试一次
                await delete_own_projects(cfg, token, only_unpublished=True, transport=transport)
                r2 = await client.post(f"{base}/projects",
                                       json={"name": name, "mode": mode, "data": data, "description": description},
                                       headers=headers)
                try:
                    d2 = r2.json()
                except ValueError:
                    d2 = {}
                pid = (d2.get("project") or {}).get("id")
                if not pid:
                    return {"ok": False, "error": d2.get("error") or "建项目失败（清理后仍额度不足）",
                            "quota": True}
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

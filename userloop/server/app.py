"""FastAPI 服务：ingest 入口 + 查询 API + Canvas 编排 + 登录门禁 + 子路径部署.

- 登录：auth.json（bcrypt 多用户，迁移自 OpenFlow/MFlow），无文件则免登录（本地开发）
- 子路径：环境变量 USERLOOP_PREFIX=/userloop 时所有路由挂前缀（对齐 MFlow nownexts.com/mflow 部署）
- 可选 Token 鉴权：config.api_token 设置后，ingest 需带 X-UserLoop-Token
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from userloop.actions.executors import ExecutorContext
from userloop.core.bus import handle
from userloop.core.scheduler import (
    build_scheduler,
    process_due_actions,
    resume_canvas_waits,
    sweep_inactivity,
    verify_due_loops,
)
from userloop.core.store import Store, iso_now, pj
from userloop.core.tenants import DEFAULT_TENANT, StoreProxy, TenantStores, set_current_store
from userloop.core.throttle import Throttle
from userloop.touch.base import TouchSpec
from userloop.server.auth import COOKIE_NAME, Sessions, auth_enabled, sid_from_cookie, verify_user


def create_app(data_dir: str | None = None) -> Any:
    from userloop.config import load_config

    prefix = os.environ.get("USERLOOP_PREFIX", "").rstrip("/")
    # 免登录路径（埋点/接入/登录自身）
    # 免登录端点（自带 token 校验的入站通道；埋点/接入/登录自身）
    public_api = {"/api/v1/ingest", "/api/v1/ingest/batch", "/api/v1/track",
                  "/api/v1/hub/ingest", "/api/v1/hub/mflow/publish",
                  "/api/v1/loops/trigger", "/api/v1/hub/message",
                  "/api/v1/capabilities", "/api/v1/openapi.json", "/api/docs",
                  "/api/login", "/api/logout", "/api/auth/me"}

    cfg = load_config(data_dir)
    tenants = TenantStores(cfg)          # 租户注册表 + Store 缓存
    store = StoreProxy()                 # 请求级代理：自动路由当前租户的 Store
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    sessions = Sessions()
    throttle = Throttle()
    authed_mode = auth_enabled(cfg["data_dir"])
    # 聚合看板缓存（10s TTL）：多标签页/多管理员不重复打库（对齐 OpenFlow 性能教训）
    overview_cache: dict[str, Any] = {"ts": 0.0, "stamp": None, "data": None}
    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    from userloop.integrations.probe import capabilities as _caps

    _ver = _caps().get("version") or "0.5.3"
    app = FastAPI(title="UserLoop", version=_ver,
                  description="全域营销数据中枢 + 用户旅程 Loop 引擎。写操作一律走状态机 + 审批门。",
                  openapi_url=f"{prefix}/api/v1/openapi.json",
                  docs_url=f"{prefix}/api/docs", redoc_url=None, redirect_slashes=False)

    def _page(name: str) -> HTMLResponse:
        with open(os.path.join(web_dir, name), encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})

    def _sid(request: Request) -> str | None:
        return sid_from_cookie(request.headers.get("Cookie", ""))

    def _user(request: Request) -> dict | None:
        return sessions.get(_sid(request)) if authed_mode else {"username": "local", "role": "admin"}

    @app.middleware("http")
    async def _tenant_ctx(request: Request, call_next: Any) -> Any:
        """解析当前租户并注入其 Store（越权：只能访问自己可见的租户）。"""
        user = sessions.get(_sid(request)) if authed_mode else {"username": "local", "role": "admin",
                                                                "tenants": [DEFAULT_TENANT]}
        allowed = set((user or {}).get("tenants") or [DEFAULT_TENANT])
        wanted = (request.headers.get("X-Tenant")
                  or request.query_params.get("tenant")
                  or (user or {}).get("tenant")
                  or DEFAULT_TENANT)
        tenant_id = wanted if wanted in allowed or (user or {}).get("role") == "admin" else DEFAULT_TENANT
        try:
            store = await tenants.get(tenant_id)
        except KeyError:
            store = await tenants.get(DEFAULT_TENANT)
            tenant_id = DEFAULT_TENANT
        set_current_store(store, tenant_id)
        ctx.store = store
        response = await call_next(request)
        response.headers.setdefault("X-UserLoop-Tenant", tenant_id)
        return response

    @app.middleware("http")
    async def _gate(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith(prefix):
            rel = path[len(prefix):]
            pages = ("", "/", "/canvas")
            if (rel in public_api or rel.startswith("/static/") or rel.startswith("/api/docs")
                    or not rel.startswith("/api/")):
                # 页面门禁：未登录返回登录页（对齐 MFlow）
                if authed_mode and rel in pages and not sessions.get(_sid(request)):
                    return _page("login.html")
                response = await call_next(request)
                if rel in pages or rel.startswith("/api/"):
                    # 动态内容禁缓存：页面按登录态变化，API 按会话/实时数据
                    response.headers.setdefault("Cache-Control", "no-store")
                return response
            if (not authed_mode) or sessions.get(_sid(request)) or _token_ok(request, cfg):
                response = await call_next(request)
                response.headers.setdefault("Cache-Control", "no-store")
                return response
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    def _token_ok(request: Request, cfg: dict) -> bool:
        token = cfg.get("api_token")
        return bool(token) and request.headers.get("X-UserLoop-Token") == token

    @app.middleware("http")
    async def _metrics_mw(request: Request, call_next: Any) -> Any:
        """请求计数 + 时延直方图（路径按路由模板归一，避免标签基数爆炸）。"""
        import time

        from userloop.ops.metrics import METRICS

        t0 = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - t0) * 1000
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        if not path:
            seg = [s for s in request.url.path.split("/") if s][:3]
            path = "/" + "/".join(seg)
        try:
            METRICS.inc("userloop_http_requests_total",
                        {"method": request.method, "path": path, "status": str(response.status_code)})
            METRICS.observe_ms("userloop_http_request_duration_ms", ms, {"path": path})
        except Exception:  # noqa: BLE001 —— 指标绝不影响请求
            pass
        return response

    @app.on_event("startup")
    async def _startup() -> None:
        default_store = await tenants.get(DEFAULT_TENANT)
        set_current_store(default_store, DEFAULT_TENANT)
        ctx.store = default_store
        from userloop.core.templates import seed_canvas, seed_templates
        from userloop.experiments.engine import seed_experiments

        await seed_templates(default_store, cfg["data_dir"])
        await seed_canvas(default_store, cfg["data_dir"])
        await seed_experiments(default_store, cfg["data_dir"])
        sched = build_scheduler(store, ctx)
        sched.start()
        state["sched"] = sched

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if state["sched"]:
            state["sched"].shutdown()
        await tenants.close_all()

    state = {"store": store, "ctx": ctx, "cfg": cfg, "sched": None}

    # ---- 登录 ----

    @app.post(f"{prefix}/api/login")
    async def login(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        user = verify_user(cfg["data_dir"], str(body.get("username", "")).strip(), str(body.get("password", "")))
        if not user:
            return JSONResponse({"ok": False, "error": "用户名或密码错误"}, status_code=403)
        user = {**user, "tenants": user.get("tenants") or [DEFAULT_TENANT],
                "tenant": user.get("tenant") or DEFAULT_TENANT}
        sid = sessions.create(user)
        resp = JSONResponse({"ok": True, "name": user["name"], "role": user["role"],
                             "tenants": user["tenants"], "tenant": user["tenant"]})
        resp.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="lax", path=prefix or "/")
        return resp

    @app.post(f"{prefix}/api/logout")
    async def logout(request: Request) -> JSONResponse:
        sessions.drop(_sid(request))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path=prefix or "/")
        return resp

    @app.get(f"{prefix}/api/auth/me")
    async def me(request: Request) -> JSONResponse:
        user = _user(request)
        if not user:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from userloop.core.tenants import current_tenant

        return JSONResponse({**user, "current_tenant": current_tenant()})

    @app.get(f"{prefix}/api/v1/capabilities")
    async def capabilities() -> JSONResponse:
        """契约广告（无密钥）：家族系统可按此探测本服务。"""
        from userloop.agents import planner as _planner
        from userloop.integrations.probe import capabilities as caps
        from userloop.plugins.registry import KINDS

        ad = caps()
        ad["agents"] = {"roles": ["analyst", "content", "outreach", "support"],
                        "recipes": [r["id"] for r in _planner.list_recipes()]}
        ad["plugins"] = {"kinds": list(KINDS)}
        ad["openapi"] = "/api/v1/openapi.json"
        ad["mcp"] = {"stdio": "userloop-mcp", "writes": "approval"}
        return JSONResponse(ad)

    # ---- ingest ----

    @app.post(f"{prefix}/api/v1/ingest")
    async def ingest(request: Request) -> JSONResponse:
        _require_token(request, cfg)
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        from userloop.billing import meter as billing

        gate = await billing.check(store, ctx, "events")
        if not gate["ok"]:
            raise HTTPException(status_code=429, detail=gate["reason"])
        try:
            result = await handle(store, ctx, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await billing.record(store, "events")
        return JSONResponse(result)

    @app.post(f"{prefix}/api/v1/ingest/batch")
    async def ingest_batch(request: Request) -> JSONResponse:
        _require_token(request, cfg)
        try:
            items = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="body must be a list of events")
        from userloop.billing import meter as billing

        gate = await billing.check(store, ctx, "events", quantity=max(1, len(items)))
        if not gate["ok"]:
            raise HTTPException(status_code=429, detail=gate["reason"])
        results = [await handle(store, ctx, item) for item in items]
        if results:
            await billing.record(store, "events", amount=len(results))
        return JSONResponse({"count": len(results), "results": results})

    # ---- 旅程埋点采集（web-tracker 插件入口）----

    @app.post(f"{prefix}/api/v1/track")
    async def track(request: Request) -> JSONResponse:
        from userloop.tracking.snippet import normalize_batch

        try:
            batch = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        try:
            events = normalize_batch(batch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        accepted, throttled = 0, 0
        for item in events:
            # 入口限流（对齐 OpenFlow 教训）：同一用户同一事件高频重放静默丢弃，绝不写库
            if not throttle.allow_event(str(item.get("distinct_id", "")), str(item.get("event", ""))):
                throttled += 1
                continue
            await handle(store, ctx, item)
            accepted += 1
        if accepted:
            from userloop.billing import meter as billing

            await billing.record(store, "events", amount=accepted)
        return JSONResponse({"accepted": accepted, "throttled": throttled})

    @app.get(f"{prefix}/track.js", response_class=HTMLResponse)
    async def track_js(request: Request) -> HTMLResponse:
        from userloop.tracking.snippet import snippet

        fwd_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
        base = f"{scheme}://{fwd_host}" if fwd_host else str(request.base_url).rstrip("/")
        return HTMLResponse(snippet(f"{base}{prefix}/api/v1/track"), media_type="application/javascript",
                            headers={"Cache-Control": "no-store"})

    # ---- 全域数据中枢（Hub）：多源入站归一化 ----

    @app.post(f"{prefix}/api/v1/hub/ingest")
    async def hub_ingest(request: Request) -> JSONResponse:
        """外部系统任意格式入站：?source=segment|ga4|shopify|hubspot|generic（缺省自动识别）。"""
        import hashlib
        import hmac as hmac_mod

        from userloop.hub.normalize import detect_source, normalize

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc

        source = request.query_params.get("source") or detect_source(payload)
        # 源级鉴权（可选）：hub.sources.<source>.secret + X-Hub-Signature=sha256(raw, secret)
        hub_cfg = (cfg.get("hub") or {}).get("sources") or {}
        secret = (hub_cfg.get(source) or {}).get("secret") if isinstance(hub_cfg, dict) else None
        if secret:
            raw = await request.body()
            sig = request.headers.get("X-Hub-Signature", "").removeprefix("sha256=")
            if hmac_mod.new(secret.encode(), raw, hashlib.sha256).hexdigest() != sig:
                raise HTTPException(status_code=401, detail="invalid hub signature")

        events = normalize(source, payload)
        results = []
        for item in events:
            try:
                results.append(await handle(store, ctx, item))
            except ValueError:
                continue  # 无身份的指标行跳过
        # 原始档案留存（file-first，供对账/回放）
        import os

        raw_dir = os.path.join(cfg["data_dir"], "hub")
        os.makedirs(raw_dir, exist_ok=True)
        src_name = source or "generic"
        with open(os.path.join(raw_dir, f"{src_name}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso_now(), "payload": payload}, ensure_ascii=False, default=str) + "\n")
        if results:
            from userloop.billing import meter as billing

            await billing.record(store, "events", amount=len(results))
        return JSONResponse({"source": src_name, "accepted": len(results)})

    # ---- 查询 API ----

    async def _build_overview() -> tuple[dict, str]:
        """构建聚合看板数据（含批量动作查询，消除 N+1）。"""
        counts = await store.counts()
        stages = await store.count_users_by_stage()
        loop_status = await store.count_loops_by_status()
        template_stats = await store.feedback_stats()
        transitions = await store.list_transitions(limit=15)
        loops = await store.list_loops(limit=12)
        acts = await store.actions_for_loops([x["id"] for x in loops])
        loops_out = [{**_hydr(x), "trigger": pj(x.get("trigger"), {}) or {},
                      "actions": acts.get(x["id"], [])} for x in loops]
        events = [_hydr(e) for e in await store.list_events(limit=14)]
        users = [_hydr(u) for u in await store.list_users(limit=12)]
        ai_rows = await store.list_ai_decisions(limit=12)
        ai_out = []
        for r in ai_rows:
            item = dict(r)
            if item.get("payload"):
                item["payload"] = pj(item["payload"], {}) or {}
            ai_out.append(item)
        newest = await store.newest_event_at()
        version = (f"{counts.get('users')}-{counts.get('events')}-{counts.get('loops')}-"
                   f"{counts.get('feedback')}-{len(ai_out)}-{newest or ''}")
        from userloop.experiments import engine as _ab

        ab_out = []
        for exp in await store.list_experiments(enabled_only=False):
            verdict = {k: v for k, v in (await _ab.evaluate(store, exp["id"])).items()
                       if k in ("verdict", "metric", "leader", "confidence", "reason")}
            ab_out.append({"id": exp["id"], "name": exp.get("name"), "channel": exp.get("channel"),
                           "metric": _ab.cfg_of(exp)["metric"], "promoted_variant": exp.get("promoted_variant"),
                           "variants": [{"id": v["id"], "name": v.get("name")} for v in (exp.get("variants") or [])],
                           "metrics": await _ab.metrics(store, exp), "verdict": verdict})
        data = {"channels": await store.channel_engagement(days=30), "counts": counts,
                "stages": stages, "funnel": _funnel(stages),
                "loop_status": loop_status, "template_stats": template_stats,
                "recent_transitions": transitions, "loops": loops_out,
                "events": events, "users": users,
                "ai_decisions": ai_out, "ai_counts": await store.ai_decision_counts(),
                "experiments": ab_out}
        return data, version

    @app.get(f"{prefix}/api/v1/overview")
    async def overview(request: Request, refresh: int = 0) -> JSONResponse:
        """控制台单一聚合端点：1 次请求取代 5 个接口；10s TTL 缓存吸收多标签页轮询。"""
        import time

        now = time.monotonic()
        if refresh or overview_cache["data"] is None or (now - overview_cache["ts"]) > 10:
            data, version = await _build_overview()
            overview_cache.update(ts=now, stamp=version, data=data)
        return JSONResponse({
            "me": _user(request) or {},
            "overview": overview_cache["data"],
            "stamp": overview_cache["stamp"],
            "cached_for": round(now - overview_cache["ts"], 1),
        })

    @app.get(f"{prefix}/api/v1/heartbeat")
    async def heartbeat(request: Request) -> JSONResponse:
        """保活心跳：零 DB 访问（对齐 OpenFlow 教训：心跳不得打库）。"""
        return JSONResponse({"ok": True, "ts": iso_now(), "authed": bool(_user(request))})

    @app.get(f"{prefix}/api/v1/dashboard")
    async def dashboard() -> JSONResponse:
        counts = await store.counts()
        stages = await store.count_users_by_stage()
        loop_status = await store.count_loops_by_status()
        template_stats = await store.feedback_stats()
        transitions = await store.list_transitions(limit=15)
        funnel = _funnel(stages)
        return JSONResponse({
            "counts": counts,
            "stages": stages,
            "funnel": funnel,
            "loop_status": loop_status,
            "template_stats": template_stats,
            "recent_transitions": transitions,
        })

    @app.get(f"{prefix}/api/v1/users")
    async def users(limit: int = 100) -> JSONResponse:
        rows = await store.list_users(limit=limit)
        return JSONResponse({"count": len(rows), "users": [_hydr(u) for u in rows]})

    @app.get(f"{prefix}/api/v1/users/{{user_id}}")
    async def user_detail(user_id: str) -> JSONResponse:
        u = await store.get_user(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        return JSONResponse({
            "user": _hydr(u),
            "transitions": await store.list_transitions(user_id=user_id),
        })

    @app.get(f"{prefix}/api/v1/loops")
    async def loops(status: str | None = None, limit: int = 100) -> JSONResponse:
        rows = await store.list_loops(status=status, limit=limit)
        acts = await store.actions_for_loops([r["id"] for r in rows])  # 批量，消除 N+1
        out = [{**_hydr(loop), "actions": acts.get(loop["id"], [])} for loop in rows]
        return JSONResponse({"count": len(out), "loops": out})

    @app.get(f"{prefix}/api/v1/events")
    async def events(limit: int = 100) -> JSONResponse:
        rows = await store.list_events(limit=limit)
        return JSONResponse({"count": len(rows), "events": [_hydr(e) for e in rows]})

    # ---- 身份识别 ----

    @app.post(f"{prefix}/api/v1/identify")
    async def identify(request: Request) -> JSONResponse:
        """把渠道标识绑定到用户；若标识已属他人则合并（匿名→实名归一）。

        入参：{distinct_id, email?, phone?, wechat_openid?, wecom_userid?}
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        did = str(body.get("distinct_id") or "").strip()
        if not did:
            raise HTTPException(status_code=400, detail="缺少 distinct_id")
        user = await store.find_user(did) or await store.upsert_user(did)
        mapping = {"email": "email", "phone": "phone", "wechat_openid": "wechat_openid",
                   "wechat_unionid": "wechat_unionid", "wecom_userid": "wecom_userid",
                   "openflow_member_id": "openflow_member", "openflow_visitor_id": "openflow_visitor",
                   "mflow_item_id": "mflow_item", "visitor_id": "websflow_visitor"}
        merged = []
        for field, type_ in mapping.items():
            val = body.get(field)
            if not val:
                continue
            owner = await store.resolve_identity(type_, str(val))
            if owner and owner != user["id"]:
                info = await store.merge_users(owner, user["id"])
                if info.get("merged"):
                    merged.append({"into": owner, "from": user["id"]})
                user = await store.get_user(owner) or user
            else:
                await store.bind_identity(user["id"], type_, str(val), source="identify_api", verified=True)
        return JSONResponse({"ok": True, "user_id": user["id"], "stage": user.get("stage"),
                             "merged": merged})

    @app.get(f"{prefix}/api/v1/identity/stats")
    async def identity_stats() -> JSONResponse:
        """识别率与漏斗（周报/看板共用口径）。"""
        stages = await store.count_users_by_stage()
        total = sum(stages.values()) or 1
        anon = stages.get("visitor", 0)
        cur = await store.db.execute("SELECT COUNT(DISTINCT user_id) c FROM identities WHERE type='email'")
        with_email = int((await cur.fetchone())["c"])
        return JSONResponse({"total": total, "anonymous_visitor": anon, "identified": total - anon,
                             "identified_rate": round((total - anon) / total * 100, 1),
                             "with_email": with_email})

    # ---- 触点回执（邮件打开/点击/退订；公开端点，自持追踪保证闭环数据自主）----

    async def _touch_event(request: Request, event: str, extra: dict | None = None) -> dict | None:
        from userloop.touch.base import read_token

        secret = str((cfg.get("touch") or {}).get("track_secret") or cfg.get("api_token") or "userloop")
        data = read_token(secret, request.query_params.get("t", ""))
        if not data:
            return None
        user = await store.get_user(data.get("u", ""))
        if not user:
            return None
        props = {"loop_id": data.get("l") or "", "template_id": (data.get("x") or {}).get("t") or "",
                 "channel": "email", "goal": (data.get("x") or {}).get("g") or ""}
        if extra:
            props.update(extra)
        try:
            await handle(store, ctx, {"distinct_id": user["distinct_id"], "event": event,
                                      "props": props, "source": "touch",
                                      "event_id": f"{event}:{data.get('u')}:{data.get('l')}:{props.get('url','')}"[:180]})
        except ValueError:
            pass
        return data

    @app.get(f"{prefix}/t/e/open.gif")
    async def touch_open(request: Request) -> Any:
        from fastapi.responses import Response

        await _touch_event(request, "email_open")
        pixel = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
                 b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
        return Response(content=pixel, media_type="image/gif",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

    @app.get(f"{prefix}/t/e/click")
    async def touch_click(request: Request) -> Any:
        from fastapi.responses import RedirectResponse

        target = request.query_params.get("u", "") or "/"
        await _touch_event(request, "email_click", {"url": target[:500]})
        if not target.lower().startswith(("http://", "https://")):
            target = "/"
        return RedirectResponse(target, status_code=302)

    @app.get(f"{prefix}/t/e/unsubscribe", response_class=HTMLResponse)
    async def touch_unsubscribe(request: Request) -> HTMLResponse:
        data = await _touch_event(request, "email_unsubscribed")
        # 同步 OpenFlow 抑制名单（永久不再发，保护发件声誉）
        if data:
            mail = (cfg.get("touch") or {}).get("email") or {}
            bridge_token = mail.get("bridge_token") or ((cfg.get("integrations") or {}).get("openflow") or {}).get("bridge_token")
            base = mail.get("bridge_url") or ((cfg.get("integrations") or {}).get("openflow") or {}).get("base_url")
            to = ""
            user = await store.get_user(data.get("u", ""))
            if user:
                to = user.get("email") or ""
            if base and bridge_token and to:
                import httpx

                try:
                    async with httpx.AsyncClient(timeout=8) as client:
                        await client.post(f"{str(base).rstrip('/')}/api/plugin/userloop-bridge/suppress",
                                          json={"email": to, "reason": "userloop_unsubscribe"},
                                          headers={"X-UserLoop-Bridge": str(bridge_token)})
                except Exception:  # noqa: BLE001 —— 抑制同步失败不影响退订本身
                    pass
        from userloop.i18n import resolve_locale, t as _t

        locale = resolve_locale(request)
        user_obj = await store.get_user((data or {}).get("u", "")) if data else None
        if user_obj:
            locale = resolve_locale(request, user_obj, cfg)
        body = (_t("unsub.ok", locale) if data else _t("unsub.bad", locale))
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>退订</title></head>
<body style="font-family:-apple-system,'PingFang SC',sans-serif;padding:60px 20px;text-align:center;color:#1f2937">
<h2 style="font-size:18px">{_t("unsub.title", locale)}</h2><p style="color:#6b7280">{body}</p></body></html>""")

    # ---- H5 触点页（自持动态页：打开/点击追踪回流入旅程）----

    @app.get(f"{prefix}/t/p/{{page_id}}", response_class=HTMLResponse)
    async def touch_page(page_id: str, request: Request) -> HTMLResponse:
        from userloop.touch.base import read_token
        from userloop.touch.render import render_h5

        page = await store.get_touch_page(page_id)
        if not page:
            return HTMLResponse("<h1 style='font-family:sans-serif;padding:40px'>页面不存在或已下线</h1>",
                                status_code=404)
        secret = str((cfg.get("touch") or {}).get("track_secret") or cfg.get("api_token") or "userloop")
        data = read_token(secret, request.query_params.get("t", ""))
        user = await store.get_user(page["user_id"])
        if data and user:
            try:
                await handle(store, ctx, {"distinct_id": user["distinct_id"], "event": "h5_view",
                                          "props": {"page_id": page_id, "loop_id": page.get("loop_id") or "",
                                                    "template_id": page.get("template_id") or "", "channel": "h5"},
                                          "source": "touch",
                                          "event_id": f"h5_view:{page_id}:{data.get('u')}"})
            except ValueError:
                pass
            await store.bump_touch_page(page_id, "views")
        from userloop.touch import personalize as pz

        spec = TouchSpec(channel="h5", title=page["title"], body=page["body"],
                         cta_text=page["cta_text"], cta_url="", loop_id=page.get("loop_id"),
                         template_id=page.get("template_id"))
        if page["cta_text"]:
            spec.cta_url = f"{prefix}/t/p/{page_id}/go?t={request.query_params.get('t', '')}"
        # 千人千面：设备/UTM/来源/Cookie/阶段 → 内容与互动时长策略
        content = pz.builtin_content(request, (user or {}).get("stage", "visitor"), cfg,
                                     {"title": page["title"], "body": page["body"],
                                      "subtitle": "", "cta_text": page["cta_text"]})
        # 识别引导：匿名用户可留邮箱/手机（绑定即完成匿名→实名归一）
        lead_cfg = (cfg.get("touch") or {}).get("lead_capture") or {}
        if lead_cfg.get("enabled", True) and user and not user.get("email"):
            content["lead"] = {"enabled": True, "endpoint": f"{prefix}/t/e/identify",
                               "title": lead_cfg.get("title") or "留下联系方式，获取专属方案",
                               "token": request.query_params.get("t", "")}
        page_html = render_h5(spec, cfg, content)["html"]
        resp = HTMLResponse(page_html, headers={"Cache-Control": "no-store"})
        resp.set_cookie("ul_seen", "1", max_age=90 * 86400, samesite="lax", httponly=True)
        return resp

    @app.post(f"{prefix}/t/e/identify")
    async def touch_identify(request: Request) -> JSONResponse:
        """H5 留资/登录：带签名令牌说明"是谁的匿名档案"，绑定后即完成识别。"""
        from userloop.touch.base import read_token

        try:
            body = await request.json()
        except Exception:
            body = {}
        secret = str((cfg.get("touch") or {}).get("track_secret") or cfg.get("api_token") or "userloop")
        data = read_token(secret, str(body.get("t") or ""))
        if not data:
            raise HTTPException(status_code=401, detail="签名无效或已过期")
        user = await store.get_user(data.get("u", ""))
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        merged = []
        for field, type_ in (("email", "email"), ("phone", "phone")):
            val = body.get(field)
            if not val:
                continue
            owner = await store.resolve_identity(type_, str(val))
            if owner and owner != user["id"]:
                info = await store.merge_users(owner, user["id"])
                if info.get("merged"):
                    merged.append(owner)
                user = await store.get_user(owner) or user
            else:
                await store.bind_identity(user["id"], type_, str(val), source="h5_lead", verified=False)
        # 留资即旅程事件（驱动 stage/Loop）
        if body.get("email"):
            await handle(store, ctx, {"distinct_id": user["distinct_id"], "event": "identify",
                                      "props": {"page_id": (data.get("x") or {}).get("p"),
                                                "email": body.get("email"), "phone": body.get("phone")},
                                      "source": "h5_lead"})
        return JSONResponse({"ok": True, "user_id": user["id"], "merged": merged,
                             "message": "已记录，我们会尽快联系你"})

    @app.get(f"{prefix}/t/p/{{page_id}}/go")
    async def touch_page_go(page_id: str, request: Request) -> Any:
        from fastapi.responses import RedirectResponse

        from userloop.touch.base import read_token

        page = await store.get_touch_page(page_id)
        if not page:
            raise HTTPException(status_code=404, detail="page not found")
        secret = str((cfg.get("touch") or {}).get("track_secret") or cfg.get("api_token") or "userloop")
        data = read_token(secret, request.query_params.get("t", ""))
        user = await store.get_user(page["user_id"])
        if data and user:
            try:
                await handle(store, ctx, {"distinct_id": user["distinct_id"], "event": "h5_click",
                                          "props": {"page_id": page_id, "loop_id": page.get("loop_id") or "",
                                                    "template_id": page.get("template_id") or "",
                                                    "url": page["cta_url"][:500], "channel": "h5"},
                                          "source": "touch",
                                          "event_id": f"h5_click:{page_id}:{data.get('u')}"})
            except ValueError:
                pass
            await store.bump_touch_page(page_id, "clicks")
        target = page["cta_url"] or "/"
        if not str(target).lower().startswith(("http://", "https://")):
            target = "/"
        return RedirectResponse(target, status_code=302)

    # ---- Canvas 编排 API ----

    @app.get(f"{prefix}/api/v1/canvas")
    async def canvas_list() -> JSONResponse:
        rows = await store.list_canvas(enabled_only=False)
        return JSONResponse({"count": len(rows), "flows": rows})

    @app.get(f"{prefix}/api/v1/canvas/{{flow_id}}")
    async def canvas_get(flow_id: str) -> JSONResponse:
        flow = await store.get_canvas(flow_id)
        if not flow:
            raise HTTPException(status_code=404, detail="canvas flow not found")
        runs = await store.list_canvas_runs(limit=20)
        return JSONResponse({
            "flow": flow,
            "runs": [r for r in runs if r["flow_id"] == flow_id],
        })

    @app.put(f"{prefix}/api/v1/canvas/{{flow_id}}")
    async def canvas_upsert(flow_id: str, request: Request) -> JSONResponse:
        try:
            flow = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        if not isinstance(flow, dict) or not flow.get("nodes"):
            raise HTTPException(status_code=400, detail="flow requires nodes[]")
        flow["id"] = flow_id
        await store.put_canvas(flow)
        return JSONResponse({"ok": True, "flow": flow})

    @app.post(f"{prefix}/api/v1/canvas/{{flow_id}}/test")
    async def canvas_test(flow_id: str, request: Request) -> JSONResponse:
        """测试触发：用指定 distinct_id/event 模拟一次事件（只走画布，不走模板 Loop）。"""
        flow = await store.get_canvas(flow_id)
        if not flow:
            raise HTTPException(status_code=404, detail="canvas flow not found")
        try:
            body = await request.json()
        except Exception:
            body = {}
        distinct_id = body.get("distinct_id") or "canvas_test_user"
        event = body.get("event") or "page_view"
        props = body.get("props") or {}
        user = await store.upsert_user(distinct_id, email=body.get("email"))
        stats = user.get("stats")
        if isinstance(stats, str):
            user = {**user, "stats": json.loads(stats or "{}")}
        from userloop.core import canvas as canvas_mod

        run = {
            "id": f"cr_test_{canvas_mod.new_id('x')[2:]}",
            "flow_id": flow_id, "user_id": user["id"], "status": "running",
            "context": {"props": props, "test": True},
            "trace": [], "created_at": iso_now(), "updated_at": iso_now(),
        }
        await store.insert_canvas_run(run)
        cc = canvas_mod.CanvasContext(store, ctx, flow, run, user)
        entries = [n for n in flow.get("nodes", []) if n.get("type") == "trigger"]
        for entry in entries:
            await canvas_mod.walk(cc, entry["id"])
        await cc.flush()
        return JSONResponse({"ok": True, "run_id": run["id"], "status": run["status"], "trace": run["trace"]})

    @app.get(f"{prefix}/api/v1/canvas-runs")
    async def canvas_runs(status: str | None = None, limit: int = 50) -> JSONResponse:
        rows = await store.list_canvas_runs(status=status, limit=limit)
        out = []
        for r in rows:
            out.append({**r, "trace": json.loads(r.get("trace") or "[]")})
        return JSONResponse({"count": len(out), "runs": out})

    # ---- AI 大脑（全域全生命周期运营 AI）----

    @app.get(f"{prefix}/api/v1/ai/decisions")
    async def ai_decisions(status: str | None = None, limit: int = 30) -> JSONResponse:
        rows = await store.list_ai_decisions(status=status, limit=limit)
        out = []
        for r in rows:
            item = dict(r)
            if item.get("payload"):
                item["payload"] = pj(item["payload"], {}) or {}
            out.append(item)
        return JSONResponse({"count": len(out), "decisions": out,
                             "counts": await store.ai_decision_counts()})

    @app.post(f"{prefix}/api/v1/ai/decide")
    async def ai_decide(request: Request) -> JSONResponse:
        """手动触发：对指定用户（或批次）跑一次 AI 决策。"""
        from userloop.ai import brain as brain_mod

        try:
            body = await request.json()
        except Exception:
            body = {}
        distinct_id = body.get("distinct_id")
        force = bool(body.get("force"))
        if distinct_id:
            user = await store.find_user(distinct_id)
            if not user:
                raise HTTPException(status_code=404, detail="user not found")
            rec = await brain_mod.run_for_user(store, ctx, user, force=force)
            return JSONResponse({"ok": True, "decision": rec})
        recs = await brain_mod.run_batch(store, ctx, limit=int(body.get("limit", 3)), force=True)
        return JSONResponse({"ok": True, "count": len(recs), "decisions": recs})

    @app.post(f"{prefix}/api/v1/ai/decisions/{{decision_id}}/approve")
    async def ai_approve(decision_id: str) -> JSONResponse:
        """人工批准：执行待审批的 AI 决策（风险门的人工出口）。"""
        from userloop.ai import brain as brain_mod

        rec = await store.get_ai_decision(decision_id)
        if not rec:
            raise HTTPException(status_code=404, detail="decision not found")
        if rec["status"] != "pending_approval":
            raise HTTPException(status_code=400, detail=f"status is {rec['status']}, not pending_approval")
        user = await store.get_user(rec["user_id"])
        payload = pj(rec.get("payload"), {}) or {}
        loop = await brain_mod.execute_intent(store, ctx, user, rec["intent"],
                                              {"reasoning": rec.get("reasoning"),
                                               "expected_effect": rec.get("expected_effect")}, payload=payload)
        await store.update_ai_decision(decision_id, status="approved", loop_id=loop["id"] if loop else None)
        return JSONResponse({"ok": True, "loop_id": loop["id"] if loop else None})

    @app.post(f"{prefix}/api/v1/ai/approve-batch")
    async def ai_approve_batch(request: Request) -> JSONResponse:
        """批量批准待批决策（按风险/意图/阶段筛选，逐条执行并回报结果）。"""
        from userloop.ai import brain as brain_mod

        try:
            body = await request.json()
        except Exception:
            body = {}
        risk = str(body.get("risk") or "medium")
        intent = str(body.get("intent") or "")
        stage = str(body.get("stage") or "")
        limit = max(1, min(int(body.get("limit") or 20), 100))
        ids = set(body.get("ids") or [])

        rows = await store.list_ai_decisions(status="pending_approval", limit=200)
        picked = [r for r in rows
                  if (not ids or r["id"] in ids)
                  and (not risk or r.get("risk") == risk)
                  and (not intent or r.get("intent") == intent)
                  and (not stage or r.get("stage") == stage)][:limit]

        results, approved, blocked, failed = [], 0, 0, 0
        for rec in picked:
            user = await store.get_user(rec["user_id"])
            if not user:
                await store.update_ai_decision(rec["id"], status="failed")
                failed += 1
                continue
            payload = pj(rec.get("payload"), {}) or {}
            loop = await brain_mod.execute_intent(store, ctx, user, rec["intent"],
                                                  {"reasoning": rec.get("reasoning"),
                                                   "expected_effect": rec.get("expected_effect")},
                                                  payload=payload)
            actions = await store.actions_for_loop(loop["id"]) if loop else []
            blocked_by = next((a for a in actions if a["status"] == "failed" and "频控" in str(a.get("result") or "")), None)
            if loop and blocked_by is None:
                await store.update_ai_decision(rec["id"], status="approved", loop_id=loop["id"])
                approved += 1
                results.append({"id": rec["id"], "status": "approved", "loop_id": loop["id"]})
            else:
                # 频控/落库失败：标记 blocked，避免无限重试
                await store.update_ai_decision(rec["id"], status="blocked",
                                               error="frequency/execute blocked")
                blocked += 1
                results.append({"id": rec["id"], "status": "blocked"})
        return JSONResponse({"approved": approved, "blocked": blocked, "failed": failed,
                             "picked": len(picked), "results": results})

    @app.post(f"{prefix}/api/v1/ai/reject-batch")
    async def ai_reject_batch(request: Request) -> JSONResponse:
        """批量驳回待批决策。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        ids = set(body.get("ids") or [])
        limit = max(1, min(int(body.get("limit") or 50), 200))
        rows = await store.list_ai_decisions(status="pending_approval", limit=200)
        picked = [r for r in rows if (not ids or r["id"] in ids)][:limit]
        for rec in picked:
            await store.update_ai_decision(rec["id"], status="rejected")
        return JSONResponse({"rejected": len(picked)})

    @app.post(f"{prefix}/api/v1/ai/decisions/{{decision_id}}/reject")
    async def ai_reject(decision_id: str) -> JSONResponse:
        rec = await store.get_ai_decision(decision_id)
        if not rec:
            raise HTTPException(status_code=404, detail="decision not found")
        await store.update_ai_decision(decision_id, status="rejected")
        return JSONResponse({"ok": True})

    # ---- A/B 实验 API ----

    @app.get(f"{prefix}/api/v1/experiments")
    async def experiments_list(evaluate: int = 1) -> JSONResponse:
        from userloop.experiments import engine as ab

        exps = await store.list_experiments(enabled_only=False)
        out = []
        for exp in exps:
            item = {"id": exp["id"], "name": exp.get("name"), "channel": exp.get("channel"),
                    "enabled": exp.get("enabled", True), "metric": ab.cfg_of(exp)["metric"],
                    "promoted_variant": exp.get("promoted_variant"),
                    "variants": [{"id": v["id"], "name": v.get("name"), "weight": v.get("weight")}
                                 for v in (exp.get("variants") or [])]}
            item["metrics"] = await ab.metrics(store, exp)
            if evaluate:
                item["verdict"] = {k: v for k, v in (await ab.evaluate(store, exp["id"])).items()
                                   if k in ("verdict", "metric", "leader", "confidence", "reason")}
            out.append(item)
        return JSONResponse({"count": len(out), "experiments": out})

    @app.post(f"{prefix}/api/v1/experiments/{{exp_id}}/evaluate")
    async def experiments_evaluate(exp_id: str) -> JSONResponse:
        from userloop.experiments import engine as ab

        return JSONResponse(await ab.evaluate(store, exp_id))

    @app.post(f"{prefix}/api/v1/experiments/{{exp_id}}/promote")
    async def experiments_promote(exp_id: str, request: Request) -> JSONResponse:
        from userloop.experiments import engine as ab

        try:
            body = await request.json()
        except Exception:
            body = {}
        return JSONResponse(await ab.promote(store, exp_id, body.get("variant")))

    # ---- Copilot：自然语言建流程（AI Native 编排）----

    @app.post(f"{prefix}/api/v1/copilot/draft")
    async def copilot_draft(request: Request) -> JSONResponse:
        from userloop.ai import copilot

        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="缺少 prompt")
        return JSONResponse(await copilot.draft_loop(ctx, prompt))

    @app.post(f"{prefix}/api/v1/copilot/apply")
    async def copilot_apply(request: Request) -> JSONResponse:
        from userloop.ai import copilot

        try:
            body = await request.json()
        except Exception:
            body = {}
        draft = body.get("draft") or {}
        if not isinstance(draft, dict) or not draft.get("actions"):
            raise HTTPException(status_code=400, detail="draft 无效（缺少 actions）")
        return JSONResponse(await copilot.apply_loop(store, draft, enable=bool(body.get("enable"))))

    # ---- 运营周报 ----

    @app.get(f"{prefix}/api/v1/reports")
    async def reports_list() -> JSONResponse:
        import glob
        import os

        out_dir = os.path.join(cfg["data_dir"], "reports")
        items = []
        for path in sorted(glob.glob(os.path.join(out_dir, "*.md")), reverse=True)[:20]:
            items.append({"name": os.path.basename(path),
                          "size": os.path.getsize(path),
                          "mtime": os.path.getmtime(path)})
        return JSONResponse({"count": len(items), "reports": items})

    @app.get(f"{prefix}/api/v1/reports/latest", response_class=HTMLResponse)
    async def reports_latest() -> HTMLResponse:
        import glob
        import os

        out_dir = os.path.join(cfg["data_dir"], "reports")
        files = sorted(glob.glob(os.path.join(out_dir, "*.md")), reverse=True)
        if not files:
            return HTMLResponse("<div style='color:var(--faint)'>暂无周报（点「生成周报」）</div>")
        with open(files[0], encoding="utf-8") as f:
            md = f.read()
        return HTMLResponse(_md_to_html(md))

    @app.post(f"{prefix}/api/v1/reports/generate")
    async def reports_generate(request: Request) -> JSONResponse:
        from userloop.ai import reporter

        try:
            body = await request.json()
        except Exception:
            body = {}
        rep = await reporter.build(store, ctx, days=int(body.get("days") or 7))
        path = await reporter.save(store, ctx, rep)
        sent = await reporter.send(ctx, rep) if body.get("send") else {}
        return JSONResponse({"ok": True, "name": rep["name"], "path": path,
                             "degraded": rep["degraded"], "sent": sent,
                             "markdown": rep["markdown"][:4000]})

    # ---- 运维加固（N3）：健康 / 指标 / 备份 / 告警 ----

    @app.get(f"{prefix}/api/v1/health")
    async def health() -> JSONResponse:
        from userloop.ops import health as health_mod

        return JSONResponse(await health_mod.check(cfg, store, state.get("sched")))

    @app.get(f"{prefix}/api/v1/metrics")
    async def metrics(format: str = "prom") -> Any:
        from userloop.ops.metrics import METRICS

        if format == "json":
            return JSONResponse(METRICS.snapshot())
        return PlainTextResponse(METRICS.render_prom(), media_type="text/plain; version=0.0.4")

    @app.get(f"{prefix}/api/v1/ops/overview")
    async def ops_overview() -> JSONResponse:
        """控制台运维面板单一端点：健康 + 指标摘要 + 当前告警 + 备份列表。"""
        from userloop.ops import alerts as alerts_mod
        from userloop.ops import backup as backup_mod
        from userloop.ops import health as health_mod
        from userloop.ops.metrics import METRICS

        h = await health_mod.check(cfg, store, state.get("sched"))
        snap = METRICS.snapshot()
        cur = alerts_mod.evaluate(cfg, h, snap)
        return JSONResponse({
            "health": h,
            "metrics": {"uptime_s": snap["uptime_s"], "counters": len(snap["counters"]),
                        "histograms": len(snap["histograms"])},
            "alerts_current": cur,
            "alerts_history": alerts_mod.history(cfg, limit=15),
            "backups": backup_mod.list_backups(cfg["data_dir"])[:10],
        })

    @app.get(f"{prefix}/api/v1/ops/backups")
    async def ops_backups() -> JSONResponse:
        from userloop.ops import backup as backup_mod

        return JSONResponse({"backups": backup_mod.list_backups(cfg["data_dir"])})

    @app.post(f"{prefix}/api/v1/ops/backup")
    async def ops_backup(request: Request) -> JSONResponse:
        import asyncio

        from userloop.ops import backup as backup_mod

        try:
            body = await request.json()
        except Exception:
            body = {}
        ops = (cfg.get("ops") or {}).get("backup") or {}
        keep = int(body.get("keep") or ops.get("keep") or 7)
        out = await asyncio.to_thread(backup_mod.create, cfg["data_dir"], keep,
                                      str(body.get("note") or "manual"), cfg)
        return JSONResponse(out)

    @app.get(f"{prefix}/api/v1/ops/backups/verify")
    async def ops_backup_verify(name: str) -> JSONResponse:
        import asyncio
        import os

        from userloop.ops import backup as backup_mod

        path = os.path.join(cfg["data_dir"], backup_mod.BACKUP_DIR, os.path.basename(name))
        return JSONResponse(await asyncio.to_thread(backup_mod.verify, path))

    @app.get(f"{prefix}/api/v1/ops/alerts")
    async def ops_alerts() -> JSONResponse:
        from userloop.ops import alerts as alerts_mod
        from userloop.ops import health as health_mod

        h = await health_mod.check(cfg, store, state.get("sched"))
        return JSONResponse({"current": alerts_mod.evaluate(cfg, h),
                             "history": alerts_mod.history(cfg, limit=30)})

    @app.post(f"{prefix}/api/v1/ops/alerts/run")
    async def ops_alerts_run() -> JSONResponse:
        from userloop.ops import alerts as alerts_mod
        from userloop.ops import health as health_mod

        h = await health_mod.check(cfg, store, state.get("sched"))
        return JSONResponse(await alerts_mod.run(cfg, h))

    # ---- 运营资产市场（N2）----

    @app.get(f"{prefix}/api/v1/assets/packs")
    async def assets_packs() -> JSONResponse:
        from userloop.assets import packs as ap

        items = [ap.summarize(p) for p in ap.load_packs(cfg["data_dir"])]
        imported = await store.list_asset_packs()
        return JSONResponse({"count": len(items), "packs": items, "imported": imported})

    @app.get(f"{prefix}/api/v1/assets/packs/{{pack_id}}")
    async def assets_pack_detail(pack_id: str) -> JSONResponse:
        from userloop.assets import packs as ap

        pack = ap.get_pack(cfg["data_dir"], pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="资产包不存在")
        return JSONResponse({"pack": pack, "summary": ap.summarize(pack)})

    @app.get(f"{prefix}/api/v1/assets/packs/{{pack_id}}/export")
    async def assets_pack_export(pack_id: str) -> Any:
        from fastapi.responses import Response

        from userloop.assets import packs as ap

        pack = ap.get_pack(cfg["data_dir"], pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="资产包不存在")
        body = json.dumps(pack, ensure_ascii=False, indent=2)
        return Response(content=body.encode(), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{pack_id}.json"'})

    @app.get(f"{prefix}/api/v1/assets/export")
    async def assets_export(name: str = "租户资产导出") -> JSONResponse:
        """导出当前租户的定义类资产（仅定义，不含任何用户数据）。"""
        from userloop.assets import packs as ap

        return JSONResponse(await ap.export_assets(store, name=name))

    @app.post(f"{prefix}/api/v1/assets/import")
    async def assets_import(request: Request) -> JSONResponse:
        """导入资产包（dry_run 预览 / require_approval 停用待批 / prefix 多套共存）。"""
        from userloop.assets import packs as ap

        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        pack = body.get("pack")
        if not isinstance(pack, dict) or not pack.get("id"):
            raise HTTPException(status_code=400, detail="缺少 pack（资产包对象）")
        default_approval = bool(((cfg.get("assets") or {}).get("require_approval")))
        return JSONResponse(await ap.import_assets(
            store, pack, dry_run=bool(body.get("dry_run")),
            require_approval=bool(body.get("require_approval", default_approval)),
            prefix=str(body.get("prefix") or "")))

    @app.post(f"{prefix}/api/v1/assets/packs/{{pack_id}}/apply")
    async def assets_apply(pack_id: str, request: Request) -> JSONResponse:
        """一键应用内置/自定义资产包到当前租户。"""
        from userloop.assets import packs as ap

        pack = ap.get_pack(cfg["data_dir"], pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="资产包不存在")
        try:
            body = await request.json()
        except Exception:
            body = {}
        default_approval = bool(((cfg.get("assets") or {}).get("require_approval")))
        return JSONResponse(await ap.import_assets(
            store, pack, dry_run=bool(body.get("dry_run")),
            require_approval=bool(body.get("require_approval", default_approval)),
            prefix=str(body.get("prefix") or "")))

    @app.get(f"{prefix}/api/v1/assets/versions")
    async def assets_versions(asset_type: str | None = None, asset_id: str | None = None,
                              limit: int = 50) -> JSONResponse:
        rows = await store.list_asset_versions(asset_type=asset_type, asset_id=asset_id, limit=limit)
        for r in rows:
            r.pop("data", None)          # 列表不带全量数据（详情接口有）
        return JSONResponse({"count": len(rows), "versions": rows})

    @app.get(f"{prefix}/api/v1/assets/versions/{{version_id}}")
    async def assets_version_detail(version_id: int) -> JSONResponse:
        ver = await store.get_asset_version(version_id)
        if not ver:
            raise HTTPException(status_code=404, detail="版本不存在")
        return JSONResponse(ver)

    @app.post(f"{prefix}/api/v1/assets/versions/{{version_id}}/rollback")
    async def assets_rollback(version_id: int) -> JSONResponse:
        return JSONResponse(await store.restore_asset_version(version_id))

    @app.post(f"{prefix}/api/v1/assets/approve")
    async def assets_approve(request: Request) -> JSONResponse:
        """审批：批准导入的资产（启用）或驳回（保持停用）。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        asset_type = str(body.get("asset_type") or "loop_template")
        asset_id = str(body.get("asset_id") or "")
        approve = bool(body.get("approve", True))
        if not asset_id:
            raise HTTPException(status_code=400, detail="缺少 asset_id")
        await store.set_asset_status(asset_id, asset_type, "approved" if approve else "rejected")
        return JSONResponse({"ok": True, "asset_id": asset_id, "asset_type": asset_type,
                             "status": "approved" if approve else "rejected"})

    # ---- 生态互联：被外部系统调用（OpenFlow 画布/自动化 → UserLoop Loop）----

    @app.post(f"{prefix}/api/v1/loops/trigger")
    async def loops_trigger(request: Request) -> JSONResponse:
        """外部系统触发 UserLoop Loop：按 template_id 或按触发器（event/stage）启动。

        入参：{template_id?|event?|stage?, distinct_id|email, props?}
        纪律：仍走 Loop 冷却；执行时再过频控/合规门（外部调用不能绕过护栏）。
        """
        _require_token(request, cfg)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        did = str(body.get("distinct_id") or body.get("email") or "").strip()
        if not did:
            raise HTTPException(status_code=400, detail="缺少 distinct_id/email")
        user = await store.find_user(did) or await store.upsert_user(
            did, email=body.get("email"), props=body.get("props") or {})
        templates = await store.get_templates(enabled_only=True)
        tpl_id = str(body.get("template_id") or "")
        event = str(body.get("event") or "")
        stage = str(body.get("stage") or "")
        targets = []
        for t in templates:
            if tpl_id and t["id"] == tpl_id:
                targets.append(t)
                continue
            trig = t.get("trigger") or {}
            if event and trig.get("type") == "event" and trig.get("name") == event:
                targets.append(t)
            elif stage and trig.get("type") == "stage_enter" and trig.get("stage") == stage:
                targets.append(t)
        if not targets:
            return JSONResponse({"ok": False, "error": "没有匹配的启用模板",
                                 "hint": "检查 template_id/event/stage，或先启用模板"})
        from userloop.core.loops import create_loop_from_template

        created = []
        for t in targets:
            loop = await create_loop_from_template(
                store, t, user, {"type": "external", "event": event, "stage": stage},
                {"source": str(body.get("source") or "external"), "props": body.get("props") or {}})
            if loop:
                created.append({"loop_id": loop["id"], "template_id": t["id"]})
        # 即期动作立即执行（延迟动作交调度器）
        if created:
            from userloop.core.scheduler import process_due_actions

            await process_due_actions(store, ctx)
        return JSONResponse({"ok": True, "user_id": user["id"], "created": created,
                             "skipped_cooldown": len(targets) - len(created)})

    # ---- 对话式触达（任何渠道只要能 POST 入站消息即可接入）----

    @app.post(f"{prefix}/api/v1/hub/message")
    async def hub_message(request: Request) -> JSONResponse:
        """入站消息 → AI 回复 → 原渠道回出。

        入参：{channel, distinct_id|email|phone|openid, text, webhook_url?（回复地址）}
        微信/WhatsApp/站内/自定义均可接入（渠道无关）。
        """
        _require_token(request, cfg)
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        if isinstance(payload, list):
            out = []
            from userloop.touch import conversation

            for item in payload:
                out.append(await conversation.handle_inbound(store, ctx, item))
            return JSONResponse({"count": len(out), "results": out})
        from userloop.touch import conversation

        return JSONResponse(await conversation.handle_inbound(store, ctx, payload))

    @app.get(f"{prefix}/api/v1/conversations")
    async def conversations(distinct_id: str) -> JSONResponse:
        user = await store.find_user(distinct_id)
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        return JSONResponse({"user_id": user["id"], "stage": user.get("stage"),
                             "messages": await store.list_messages(user["id"], limit=50),
                             "stats": await store.message_stats()})

    # ---- 生态互联（inFlow 洞察 / MFlow 发布回流）----

    @app.post(f"{prefix}/api/v1/integrations/inflow/sync")
    async def inflow_sync(request: Request) -> JSONResponse:
        """拉取 inFlow 新洞察 → 生成 Loop 草稿（+ 回执 ack）。"""
        from userloop.integrations import inflow

        try:
            body = await request.json()
        except Exception:
            body = {}
        return JSONResponse(await inflow.sync(store, ctx, limit=int(body.get("limit") or 20)))

    @app.get(f"{prefix}/api/v1/integrations/inflow/insights")
    async def inflow_insights(limit: int = 30) -> JSONResponse:
        rows = await store.list_external_insights(limit=max(1, min(limit, 100)))
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = pj(item.get("payload"), {})
            out.append(item)
        return JSONResponse({"count": len(out), "insights": out})

    @app.post(f"{prefix}/api/v1/integrations/inflow/insights/{{insight_id}}/enable")
    async def inflow_enable(insight_id: str) -> JSONResponse:
        """启用该洞察生成的 Loop 草稿。"""
        rows = await store.list_external_insights(limit=200)
        row = next((r for r in rows if r["id"] == insight_id), None)
        if not row or not row.get("loop_id"):
            raise HTTPException(status_code=404, detail="洞察或对应 Loop 不存在")
        templates = await store.get_templates(enabled_only=False)
        tpl = next((t for t in templates if t["id"] == row["loop_id"]), None)
        if not tpl:
            raise HTTPException(status_code=404, detail="Loop 模板不存在")
        await store.put_template({**tpl, "enabled": True})
        await store.put_external_insight({**row, "payload": pj(row.get("payload"), {}),
                                          "external_id": row["external_id"], "enabled": True})
        return JSONResponse({"ok": True, "loop_id": tpl["id"], "name": tpl["name"]})

    @app.post(f"{prefix}/api/v1/hub/mflow/publish")
    async def mflow_publish(request: Request) -> JSONResponse:
        """MFlow 发布回调：登记内容 + 开启验证窗口（可用 api_token 保护）。"""
        from userloop.integrations import mflow_publish

        # MFlow 的 webhook 适配器不支持自定义 header → 支持 ?token=<api_token>（_require_token 已统一）
        _require_token(request, cfg)
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        if isinstance(payload, list):
            out = [await mflow_publish.record_publish(store, p) for p in payload]
            return JSONResponse({"count": len(out), "results": out})
        return JSONResponse(await mflow_publish.record_publish(store, payload))

    @app.get(f"{prefix}/api/v1/content/publications")
    async def content_publications(status: str | None = None, limit: int = 30) -> JSONResponse:
        rows = await store.list_publications(status=status, limit=max(1, min(limit, 100)))
        for r in rows:
            r["evidence"] = pj(r.get("evidence"), {})
        return JSONResponse({"count": len(rows), "publications": rows})

    @app.post(f"{prefix}/api/v1/content/verify")
    async def content_verify() -> JSONResponse:
        from userloop.integrations import mflow_publish

        rows = await mflow_publish.verify_due_publications(store)
        return JSONResponse({"verified": len(rows), "results": rows})

    # ---- 自进化（遥测/诊断/提案/Lessons）----

    @app.get(f"{prefix}/api/v1/evolve/status")
    async def evolve_status() -> JSONResponse:
        from userloop.evolve import engine as evo

        tel = await evo.collect_telemetry(store, ctx)
        dg = await evo.diagnose(store, ctx)
        props = await store.list_evolution_proposals(limit=20)
        lessons = await store.list_lessons(limit=20)
        from userloop.integrations import probe as probe_mod

        return JSONResponse({"telemetry": tel, "diagnostics": dg,
                             "proposals": props, "lessons": lessons,
                             "probes": probe_mod.load_latest(ctx)})

    @app.get(f"{prefix}/api/v1/evolve/diagnose")
    async def evolve_diagnose() -> JSONResponse:
        from userloop.evolve import engine as evo

        return JSONResponse(await evo.diagnose(store, ctx))

    @app.post(f"{prefix}/api/v1/evolve/propose")
    async def evolve_propose() -> JSONResponse:
        from userloop.evolve import engine as evo

        return JSONResponse(await evo.propose(store, ctx))

    @app.post(f"{prefix}/api/v1/evolve/proposals/{{proposal_id}}/apply")
    async def evolve_apply(proposal_id: str) -> JSONResponse:
        from userloop.evolve import engine as evo

        return JSONResponse(await evo.apply_proposal(store, ctx, proposal_id))

    @app.post(f"{prefix}/api/v1/evolve/proposals/{{proposal_id}}/rollback")
    async def evolve_rollback(proposal_id: str) -> JSONResponse:
        from userloop.evolve import engine as evo

        return JSONResponse(await evo.rollback_proposal(store, ctx, proposal_id))

    @app.post(f"{prefix}/api/v1/evolve/proposals/{{proposal_id}}/reject")
    async def evolve_reject(proposal_id: str) -> JSONResponse:
        await store.set_evolution_status(proposal_id, "rejected")
        return JSONResponse({"ok": True})

    @app.get(f"{prefix}/api/v1/evolve/lessons", response_class=HTMLResponse)
    async def evolve_lessons() -> HTMLResponse:
        from userloop.evolve import engine as evo

        return HTMLResponse(_md_to_html(evo.render_lessons_md(await store.list_lessons(limit=100))))

    @app.post(f"{prefix}/api/v1/evolve/lessons")
    async def evolve_add_lesson(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not str(body.get("title") or "").strip():
            raise HTTPException(status_code=400, detail="缺少 title")
        await store.add_lesson({"category": str(body.get("category") or "manual"),
                                "title": str(body["title"])[:120],
                                "detail": str(body.get("detail") or "")[:600],
                                "fix": str(body.get("fix") or "")[:600], "source": "manual"})
        return JSONResponse({"ok": True})

    @app.get(f"{prefix}/api/v1/integrations/probe")
    async def probe_status() -> JSONResponse:
        from userloop.integrations import probe as probe_mod

        return JSONResponse(probe_mod.load_latest(ctx) or {"ok": True, "results": [], "note": "尚未探测"})

    @app.post(f"{prefix}/api/v1/integrations/probe")
    async def probe_run() -> JSONResponse:
        from userloop.integrations import probe as probe_mod

        return JSONResponse(await probe_mod.run(ctx))

    # ---- 多租户 ----

    @app.get(f"{prefix}/api/v1/tenants")
    async def tenants_list(request: Request) -> JSONResponse:
        from userloop.core.tenants import current_tenant

        user = _user(request) or {}
        allowed = set(user.get("tenants") or [DEFAULT_TENANT])
        items = []
        for t in tenants.registry.list():
            if user.get("role") == "admin" or t["id"] in allowed:
                items.append({"id": t["id"], "name": t["name"], "locale": t.get("locale"),
                              "timezone_offset": t.get("timezone_offset"),
                              "enabled": t.get("enabled", True)})
        return JSONResponse({"count": len(items), "tenants": items, "current": current_tenant()})

    @app.post(f"{prefix}/api/v1/tenants")
    async def tenants_create(request: Request) -> JSONResponse:
        user = _user(request) or {}
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可创建租户")
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            rec = tenants.registry.create(str(body.get("id") or ""), name=str(body.get("name") or ""),
                                         locale=str(body.get("locale") or "zh-CN"),
                                         timezone_offset=int(body.get("timezone_offset") or 8),
                                         storage=body.get("storage") or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tstore = await tenants.get(rec["id"])
        from userloop.core.templates import seed_canvas, seed_templates
        from userloop.experiments.engine import seed_experiments

        await seed_templates(tstore, rec["data_dir"])
        await seed_canvas(tstore, rec["data_dir"])
        await seed_experiments(tstore, rec["data_dir"])
        return JSONResponse({"ok": True, "tenant": rec})

    @app.post(f"{prefix}/api/v1/tenants/switch")
    async def tenants_switch(request: Request) -> JSONResponse:
        """切换当前会话的租户（仅限自己可见的租户；管理员可切任意）。"""
        user = _user(request) or {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        tid = str(body.get("id") or "").strip()
        allowed = set(user.get("tenants") or [DEFAULT_TENANT])
        if user.get("role") != "admin" and tid not in allowed:
            raise HTTPException(status_code=403, detail="无权访问该租户")
        if not tenants.registry.get(tid):
            raise HTTPException(status_code=404, detail="租户不存在")
        sid = _sid(request)
        if sid and sid in sessions._store:
            sessions._store[sid] = {**sessions._store[sid], "tenant": tid}
        return JSONResponse({"ok": True, "tenant": tid})

    @app.patch(f"{prefix}/api/v1/tenants/{{tenant_id}}")
    async def tenants_update(tenant_id: str, request: Request) -> JSONResponse:
        user = _user(request) or {}
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可修改租户")
        try:
            body = await request.json()
        except Exception:
            body = {}
        rec = tenants.registry.update(tenant_id, **{k: v for k, v in body.items()
                                                    if k in ("name", "locale", "timezone_offset",
                                                             "enabled", "storage")})
        if not rec:
            raise HTTPException(status_code=404, detail="租户不存在")
        return JSONResponse({"ok": True, "tenant": rec})

    # ---- 合规中心（consent / DSAR）----

    @app.post(f"{prefix}/api/v1/compliance/consent")
    async def compliance_consent(request: Request) -> JSONResponse:
        """记录同意（purpose: marketing|analytics 等）。"""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        did = str(body.get("distinct_id") or "").strip()
        purpose = str(body.get("purpose") or "marketing").strip()
        if not did:
            raise HTTPException(status_code=400, detail="缺少 distinct_id")
        user = await store.find_user(did) or await store.upsert_user(did)
        granted = bool(body.get("granted"))
        await store.set_consent(user["id"], purpose, granted, source=str(body.get("source") or "api"))
        # 退订/撤回同意 → 直接抑制该渠道
        if not granted and purpose in ("marketing", "email", "sms"):
            props = pj(user.get("props"), {}) or {}
            props[f"{purpose}_unsubscribed"] = True
            await store.update_user(user["id"], props=json.dumps(props, ensure_ascii=False))
        return JSONResponse({"ok": True, "user_id": user["id"], "purpose": purpose, "granted": granted})

    @app.get(f"{prefix}/api/v1/compliance/consent")
    async def compliance_consent_get(distinct_id: str) -> JSONResponse:
        user = await store.find_user(distinct_id)
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        return JSONResponse({"user_id": user["id"], "consents": await store.list_consents(user["id"])})

    @app.get(f"{prefix}/api/v1/compliance/export")
    async def compliance_export(distinct_id: str) -> JSONResponse:
        """DSAR 导出：该用户全部数据（JSON 交付）。"""
        user = await store.find_user(distinct_id)
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        data = await store.export_user_data(user["id"])
        return JSONResponse({"ok": True, "distinct_id": distinct_id, "data": data})

    @app.post(f"{prefix}/api/v1/compliance/erase")
    async def compliance_erase(request: Request) -> JSONResponse:
        """DSAR 删除：不可逆清除该用户全部数据（需 confirm=ERASE 二次确认）。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        if str(body.get("confirm") or "") != "ERASE":
            raise HTTPException(status_code=400, detail="需要 confirm=ERASE 二次确认")
        did = str(body.get("distinct_id") or "").strip()
        user = await store.find_user(did)
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        out = await store.erase_user(user["id"])
        return JSONResponse(out)

    # ---- 自然语言分群 ----

    @app.post(f"{prefix}/api/v1/segments/nl")
    async def segments_nl(request: Request) -> JSONResponse:
        """一句话生成分群规则（草稿 + 强校验），并返回命中预览。"""
        from userloop.segments import nl as nlseg

        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="缺少 prompt")
        res = await nlseg.draft(ctx, prompt)
        if res.get("ok") and body.get("preview", True):
            res["preview"] = await nlseg.preview(store, res["segment"])
        return JSONResponse(res)

    @app.get(f"{prefix}/api/v1/segments")
    async def segments_list() -> JSONResponse:
        rows = await store.list_segments()
        out = []
        for r in rows:
            from userloop.segments import nl as nlseg

            p = await nlseg.preview(store, r)
            out.append({**r, "count": p["count"], "sample": p["sample"][:3]})
        return JSONResponse({"count": len(out), "segments": out})

    @app.post(f"{prefix}/api/v1/segments")
    async def segments_save(request: Request) -> JSONResponse:
        from userloop.segments import nl as nlseg

        try:
            body = await request.json()
        except Exception:
            body = {}
        seg = body.get("segment") or {}
        if not isinstance(seg, dict) or not seg.get("rules"):
            raise HTTPException(status_code=400, detail="segment 无效（缺少 rules）")
        seg, warnings = nlseg._clamp_rules(seg)
        await store.put_segment(seg)
        return JSONResponse({"ok": True, "id": seg["id"], "warnings": warnings, "segment": seg})

    @app.get(f"{prefix}/api/v1/segments/{{seg_id}}/users")
    async def segments_users(seg_id: str, limit: int = 100) -> JSONResponse:
        from userloop.segments import nl as nlseg

        seg = await store.get_segment(seg_id)
        if not seg:
            raise HTTPException(status_code=404, detail="segment not found")
        p = await nlseg.preview(store, seg)
        return JSONResponse({**p, "sample": p["sample"][: max(1, min(limit, 500))]})

    @app.get(f"{prefix}/api/v1/segments/{{seg_id}}/export.csv")
    async def segments_export(seg_id: str) -> Any:
        import csv
        import io

        from fastapi.responses import Response

        from userloop.segments import nl as nlseg

        seg = await store.get_segment(seg_id)
        if not seg:
            raise HTTPException(status_code=404, detail="segment not found")
        p = await nlseg.preview(store, seg)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "distinct_id", "email", "stage", "churn", "ltv"])
        for row in p["sample"]:
            w.writerow([row["user_id"], row["distinct_id"], row.get("email") or "",
                        row.get("stage") or "", row.get("churn"), row.get("ltv")])
        return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{seg_id}.csv"'})

    # ---- 预测层 API ----

    @app.get(f"{prefix}/api/v1/predictions")
    async def predictions(sort: str = "churn", limit: int = 20) -> JSONResponse:
        from userloop.predict import engine as predict

        rows = await predict.top(store, sort, limit=max(1, min(limit, 100)))
        return JSONResponse({"sort": sort, "count": len(rows), "items": rows})

    @app.get(f"{prefix}/api/v1/predictions/model")
    async def predictions_model() -> JSONResponse:
        from userloop.predict import model as mdl

        out = {}
        for kind in ("churn", "propensity"):
            m = mdl.load(cfg["data_dir"], kind)
            out[kind] = None if not m else {
                "version": m.get("version"), "metrics": m.get("metrics"),
                "trained_at": m.get("trained_at"), "features": m.get("features"),
                "weights": {f: round(w, 3) for f, w in zip(m.get("features") or [], m.get("w") or [])},
            }
        return JSONResponse({"models": out})

    @app.post(f"{prefix}/api/v1/predictions/train")
    async def predictions_train(request: Request) -> JSONResponse:
        from userloop.predict import model as mdl

        try:
            body = await request.json()
        except Exception:
            body = {}
        kinds = body.get("kinds") or ["churn", "propensity"]
        out = {}
        for kind in kinds:
            out[kind] = await mdl.train(store, cfg["data_dir"], kind,
                                        reference_days=(int(body["reference_days"])
                                                        if body.get("reference_days") else None),
                                        horizon_days=(int(body["horizon_days"])
                                                      if body.get("horizon_days") else None))
        return JSONResponse({"ok": True, "results": out})

    @app.get(f"{prefix}/api/v1/predictions/{{user_id}}")
    async def prediction_detail(user_id: str) -> JSONResponse:
        from userloop.predict import engine as predict

        user = await store.get_user(user_id)
        if not user:
            # 支持用 distinct_id 查询
            user = await store.find_user(user_id)
            if not user:
                raise HTTPException(status_code=404, detail="user not found")
        row = (await store.get_user_score(user["id"])
               or await predict.compute(store, user, data_dir=cfg["data_dir"]))
        return JSONResponse({"user_id": user["id"], "distinct_id": user["distinct_id"],
                             "stage": user.get("stage"), **row})

    @app.post(f"{prefix}/api/v1/predictions/recompute")
    async def predictions_recompute(request: Request) -> JSONResponse:
        from userloop.predict import engine as predict

        try:
            body = await request.json()
        except Exception:
            body = {}
        n = await predict.run_batch(store, limit=int(body.get("limit") or 50),
                                    data_dir=cfg["data_dir"])
        return JSONResponse({"ok": True, "computed": n})

    # ---- N3 Agent 运营团队 ----

    @app.get(f"{prefix}/api/v1/agents/roles")
    async def agent_roles() -> JSONResponse:
        from userloop.agents import roster

        return JSONResponse({"roles": roster.snapshot()})

    @app.get(f"{prefix}/api/v1/agents/recipes")
    async def agent_recipes() -> JSONResponse:
        from userloop.agents import planner

        return JSONResponse({"recipes": planner.list_recipes()})

    @app.get(f"{prefix}/api/v1/agents/campaigns")
    async def agent_campaigns() -> JSONResponse:
        return JSONResponse({"campaigns": await store.list_campaigns(limit=50)})

    @app.get(f"{prefix}/api/v1/agents/campaigns/{{campaign_id}}")
    async def agent_campaign_get(campaign_id: str) -> JSONResponse:
        from userloop.agents import engine as agents

        snap = await agents.snapshot(store, campaign_id)
        if not snap.get("ok"):
            raise HTTPException(status_code=404, detail="campaign not found")
        return JSONResponse(snap)

    @app.post(f"{prefix}/api/v1/agents/campaigns")
    async def agent_campaign_create(request: Request) -> JSONResponse:
        from userloop.agents import engine as agents

        try:
            body = await request.json()
        except Exception:
            body = {}
        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal required")
        target = body.get("target")
        try:
            target_f = float(target) if target is not None else None
        except (TypeError, ValueError):
            target_f = None
        return JSONResponse(await agents.create_campaign(
            store, ctx, goal, target=target_f, budget=body.get("budget"),
            recipe=body.get("recipe")))

    @app.post(f"{prefix}/api/v1/agents/campaigns/{{campaign_id}}/approve")
    async def agent_campaign_approve(campaign_id: str) -> JSONResponse:
        from userloop.agents import engine as agents

        try:
            return JSONResponse(await agents.approve(store, ctx, campaign_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(f"{prefix}/api/v1/agents/campaigns/{{campaign_id}}/reject")
    async def agent_campaign_reject(campaign_id: str, request: Request) -> JSONResponse:
        from userloop.agents import engine as agents

        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return JSONResponse(await agents.reject(store, campaign_id, reason=str(body.get("reason") or "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(f"{prefix}/api/v1/agents/run-ready")
    async def agent_run_ready() -> JSONResponse:
        from userloop.agents import engine as agents

        return JSONResponse({"ran": await agents.run_ready_all(store, ctx)})

    # ---- N4 插件市场 + 计费 + 平台摘要 ----

    @app.get(f"{prefix}/api/v1/plugins")
    async def plugins_list() -> JSONResponse:
        from userloop.plugins import registry as plug

        return JSONResponse({"plugins": plug.list_plugins(cfg)})

    @app.get(f"{prefix}/api/v1/plugins/{{plugin_id}}")
    async def plugins_get(plugin_id: str) -> JSONResponse:
        from userloop.plugins import registry as plug

        row = plug.get_plugin(cfg, plugin_id)
        if not row:
            raise HTTPException(status_code=404, detail="plugin not found")
        return JSONResponse(row)

    @app.get(f"{prefix}/api/v1/billing/usage")
    async def billing_usage() -> JSONResponse:
        from userloop.billing import meter as billing

        return JSONResponse(await billing.snapshot(store, ctx))

    @app.get(f"{prefix}/api/v1/platform")
    async def platform_summary() -> JSONResponse:
        from userloop.billing import meter as billing
        from userloop.core.tenants import TenantRegistry
        from userloop.plugins import registry as plug

        tenants_list = [{"id": t["id"], "name": t.get("name"), "enabled": t.get("enabled", True)}
                        for t in TenantRegistry(cfg["data_dir"]).list()]
        return JSONResponse({
            "openapi": f"{prefix}/api/v1/openapi.json",
            "docs": f"{prefix}/api/docs",
            "mcp": {"stdio": "userloop-mcp", "write_via": "approval"},
            "plugins": plug.list_plugins(cfg),
            "tenants": tenants_list,
            "billing": await billing.snapshot(store, ctx),
        })

    # ---- 运维 API ----

    @app.post(f"{prefix}/api/v1/ops/run-tick")
    async def run_tick() -> JSONResponse:
        """手动触发一轮调度（动作 + 滞留扫描 + 验证 + Canvas 恢复），便于外部 cron 驱动。"""
        actions = await process_due_actions(store, ctx)
        stalled = await sweep_inactivity(store)
        feedbacks = await verify_due_loops(store)
        canvas_resumed = await resume_canvas_waits(store, ctx)
        return JSONResponse({"actions": len(actions), "loops_created": len(stalled),
                             "feedbacks": len(feedbacks), "canvas_resumed": len(canvas_resumed)})

    # ---- 页面 ----

    app.mount(f"{prefix}/static", StaticFiles(directory=web_dir), name="static")

    if prefix:
        # redirect_slashes=False：无斜杠/斜杠两种形态都要显式注册
        app.get(f"{prefix}/", response_class=HTMLResponse)(lambda: _page("index.html"))
        app.get(f"{prefix}/canvas", response_class=HTMLResponse)(lambda: _page("canvas.html"))
    else:
        @app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return _page("index.html")

        @app.get("/canvas", response_class=HTMLResponse)
        async def canvas_page() -> HTMLResponse:
            return _page("canvas.html")

    return app


def _md_to_html(md: str) -> str:
    """极简 Markdown 渲染（标题/列表/粗体/引用），避免引入依赖。"""
    import html as _html

    out: list[str] = []
    for line in md.splitlines():
        s = _html.escape(line)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        if s.startswith("### "):
            out.append(f"<h3 style='font-size:14px;margin:14px 0 6px'>{s[4:]}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2 style='font-size:15px;margin:16px 0 8px'>{s[3:]}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1 style='font-size:17px;margin:0 0 8px'>{s[2:]}</h1>")
        elif s.startswith("&gt; "):
            out.append(f"<div style='border-left:3px solid var(--warn);padding-left:10px;margin:8px 0;color:var(--warn)'>{s[5:]}</div>")
        elif s.startswith("- "):
            out.append(f"<div style='margin:4px 0'>• {s[2:]}</div>")
        elif s.strip() in ("---", ""):
            out.append("<div style='height:8px'></div>")
        else:
            out.append(f"<div style='margin:6px 0'>{s}</div>")
    return "".join(out)


def _require_token(request: Request, cfg: dict) -> None:
    """入站鉴权：支持请求头 X-UserLoop-Token 或 query ?token=（后者供只能配 URL 的系统使用）。"""
    token = cfg.get("api_token")
    if not token:
        return
    provided = request.headers.get("X-UserLoop-Token") or request.query_params.get("token")
    if provided != token:
        raise HTTPException(status_code=401, detail="invalid token")


def _hydr(row: dict) -> dict:
    out = dict(row)
    for key in ("props", "stats", "context", "result"):
        if key in out and isinstance(out[key], str):
            out[key] = pj(out[key], {}) or {}
    return out


def _funnel(stages: dict[str, int]) -> list[dict]:
    from userloop.core.entities import STAGE_ORDER, Stage

    return [
        {"stage": s.value, "users": stages.get(s.value, 0)}
        for s in sorted(STAGE_ORDER, key=STAGE_ORDER.get)  # type: ignore[arg-type]
    ]

"""FastAPI 服务：ingest 入口 + 查询 API + Canvas 编排 + 登录门禁 + 子路径部署.

- 登录：auth.json（bcrypt 多用户，迁移自 OpenFlow/MFlow），无文件则免登录（本地开发）
- 子路径：环境变量 USERLOOP_PREFIX=/userloop 时所有路由挂前缀（对齐 MFlow nownexts.com/mflow 部署）
- 可选 Token 鉴权：config.api_token 设置后，ingest 需带 X-UserLoop-Token
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
from userloop.core.throttle import Throttle
from userloop.server.auth import COOKIE_NAME, Sessions, auth_enabled, sid_from_cookie, verify_user


def create_app(data_dir: str | None = None) -> Any:
    from userloop.config import load_config

    prefix = os.environ.get("USERLOOP_PREFIX", "").rstrip("/")
    # 免登录路径（埋点/接入/登录自身）
    public_api = {"/api/v1/ingest", "/api/v1/ingest/batch", "/api/v1/track",
                  "/api/v1/hub/ingest", "/api/login", "/api/logout", "/api/auth/me"}

    cfg = load_config(data_dir)
    store = Store(cfg["db_path"])
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    sessions = Sessions()
    throttle = Throttle()
    authed_mode = auth_enabled(cfg["data_dir"])
    # 聚合看板缓存（10s TTL）：多标签页/多管理员不重复打库（对齐 OpenFlow 性能教训）
    overview_cache: dict[str, Any] = {"ts": 0.0, "stamp": None, "data": None}
    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    app = FastAPI(title="UserLoop", version="0.1.0", description="全域自动化用户运营工具",
                  docs_url=None, redoc_url=None, redirect_slashes=False)

    def _page(name: str) -> HTMLResponse:
        with open(os.path.join(web_dir, name), encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})

    def _sid(request: Request) -> str | None:
        return sid_from_cookie(request.headers.get("Cookie", ""))

    def _user(request: Request) -> dict | None:
        return sessions.get(_sid(request)) if authed_mode else {"username": "local", "role": "admin"}

    @app.middleware("http")
    async def _gate(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith(prefix):
            rel = path[len(prefix):]
            pages = ("", "/", "/canvas")
            if rel in public_api or rel.startswith("/static/") or not rel.startswith("/api/"):
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

    @app.on_event("startup")
    async def _startup() -> None:
        await store.connect()
        from userloop.core.templates import seed_canvas, seed_templates

        await seed_templates(store, cfg["data_dir"])
        await seed_canvas(store, cfg["data_dir"])
        sched = build_scheduler(store, ctx)
        sched.start()
        state["sched"] = sched

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if state["sched"]:
            state["sched"].shutdown()
        await store.close()

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
        sid = sessions.create(user)
        resp = JSONResponse({"ok": True, "name": user["name"], "role": user["role"]})
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
        return JSONResponse(user)

    # ---- ingest ----

    @app.post(f"{prefix}/api/v1/ingest")
    async def ingest(request: Request) -> JSONResponse:
        _require_token(request, cfg)
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc
        try:
            result = await handle(store, ctx, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        results = [await handle(store, ctx, item) for item in items]
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
        newest = await store.newest_event_at()
        version = f"{counts.get('users')}-{counts.get('events')}-{counts.get('loops')}-{counts.get('feedback')}-{newest or ''}"
        data = {"counts": counts, "stages": stages, "funnel": _funnel(stages),
                "loop_status": loop_status, "template_stats": template_stats,
                "recent_transitions": transitions, "loops": loops_out,
                "events": events, "users": users}
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


def _require_token(request: Request, cfg: dict) -> None:
    token = cfg.get("api_token")
    if token and request.headers.get("X-UserLoop-Token") != token:
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

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
from userloop.server.auth import COOKIE_NAME, Sessions, auth_enabled, sid_from_cookie, verify_user


def create_app(data_dir: str | None = None) -> Any:
    from userloop.config import load_config

    prefix = os.environ.get("USERLOOP_PREFIX", "").rstrip("/")
    # 免登录路径（埋点/接入/登录自身）
    public_api = {"/api/v1/ingest", "/api/v1/ingest/batch", "/api/v1/track",
                  "/api/login", "/api/logout", "/api/auth/me"}

    cfg = load_config(data_dir)
    store = Store(cfg["db_path"])
    ctx = ExecutorContext(cfg["data_dir"], cfg)
    sessions = Sessions()
    authed_mode = auth_enabled(cfg["data_dir"])
    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    app = FastAPI(title="UserLoop", version="0.1.0", description="全域自动化用户运营工具", docs_url=None, redoc_url=None)

    def _page(name: str) -> HTMLResponse:
        with open(os.path.join(web_dir, name), encoding="utf-8") as f:
            return HTMLResponse(f.read())

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
                return await call_next(request)
            if (not authed_mode) or sessions.get(_sid(request)) or _token_ok(request, cfg):
                return await call_next(request)
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
        results = [await handle(store, ctx, item) for item in events]
        return JSONResponse({"accepted": len(results)})

    @app.get(f"{prefix}/track.js", response_class=HTMLResponse)
    async def track_js(request: Request) -> HTMLResponse:
        from userloop.tracking.snippet import snippet

        fwd_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
        base = f"{scheme}://{fwd_host}" if fwd_host else str(request.base_url).rstrip("/")
        return HTMLResponse(snippet(f"{base}{prefix}/api/v1/track"), media_type="application/javascript",
                            headers={"Cache-Control": "no-store"})

    # ---- 查询 API ----

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
        out = []
        for loop in rows:
            actions = await store.actions_for_loop(loop["id"])
            out.append({**_hydr(loop), "actions": actions})
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

    @app.get(prefix or "/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return _page("index.html")

    @app.get(f"{prefix}/canvas", response_class=HTMLResponse)
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

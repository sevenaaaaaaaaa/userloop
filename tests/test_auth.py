"""登录体系 + 子路径（USERLOOP_PREFIX）测试."""

from __future__ import annotations

import json

import bcrypt
import pytest
from fastapi.testclient import TestClient

from userloop.server import auth as auth_mod

AUTH = [{"username": "seven", "name": "Seven", "role": "admin",
         "hash": bcrypt.hashpw(b"pw123", bcrypt.gensalt(rounds=10)).decode()}]


@pytest.fixture
def app(tmp_path):
    from userloop.server.app import create_app

    with open(tmp_path / "auth.json", "w", encoding="utf-8") as f:
        json.dump(AUTH, f, ensure_ascii=False)
    return create_app(str(tmp_path))


def test_login_and_gate(app) -> None:
    with TestClient(app) as client:
        # 未登录看板 → 登录页
        r = client.get("/")
        assert "登录" in r.text
        # 受保护 API 未登录 → 401
        assert client.get("/api/v1/dashboard").status_code == 401
        # 错误密码
        assert client.post("/api/login", json={"username": "seven", "password": "bad"}).status_code == 403
        # 正确登录
        r = client.post("/api/login", json={"username": "seven", "password": "pw123"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert "userloop_session" in client.cookies
        # 登录后页面与 API 可用
        assert "UserLoop" in client.get("/").text
        assert client.get("/api/v1/dashboard").status_code == 200
        assert client.get("/api/auth/me").json()["username"] == "seven"
        # 登出
        client.post("/api/logout")
        assert client.get("/api/v1/dashboard").status_code == 401


def test_ingest_and_track_stay_public(app) -> None:
    with TestClient(app) as client:
        r = client.post("/api/v1/ingest", json={"distinct_id": "x1", "event": "page_view"})
        assert r.status_code == 200
        r = client.post("/api/v1/track", json={"events": [{"distinct_id": "x2", "event": "page_view"}]})
        assert r.status_code == 200


def test_sid_from_cookie() -> None:
    assert auth_mod.sid_from_cookie("a=1; userloop_session=abc") == "abc"
    assert auth_mod.sid_from_cookie("a=1") is None


def test_suffix_login_forms(tmp_path) -> None:
    """Seven / Seven@userloop / Seven@ul 均可登录；错误后缀拒绝；/me 返回带后缀的登录名."""
    import json as _json

    from fastapi.testclient import TestClient

    from userloop.server import auth as a
    from userloop.server.app import create_app

    with open(tmp_path / "auth.json", "w", encoding="utf-8") as f:
        _json.dump(AUTH, f, ensure_ascii=False)
    assert a.normalize_username("Seven@userloop") == "Seven"
    assert a.normalize_username(" Seven@UL ") == "Seven"
    assert a.normalize_username("Seven") == "Seven"
    assert a.display_username("Seven") == "Seven@userloop"

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        for name in ("Seven", "seven@userloop", "Seven@ul"):
            client.post("/api/logout")
            r = client.post("/api/login", json={"username": name, "password": "pw123"})
            assert r.status_code == 200, name
            assert client.get("/api/auth/me").json()["login"].lower() == "seven@userloop"
        client.post("/api/logout")
        assert client.post("/api/login", json={"username": "Seven@other", "password": "pw123"}).status_code == 403


def test_prefix_routes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("USERLOOP_PREFIX", "/userloop")
    from userloop.server import app as app_mod

    app = app_mod.create_app(str(tmp_path))
    with TestClient(app) as client:
        # 免登录模式（无 auth.json）→ 页面直出
        assert client.get("/userloop/").status_code == 200
        assert client.get("/userloop/api/v1/dashboard").status_code == 200
        assert client.get("/userloop/canvas").status_code == 200
        # 未挂前缀的路径不命中门禁
        assert client.get("/api/v1/dashboard").status_code == 404

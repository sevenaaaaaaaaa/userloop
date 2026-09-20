"""旅程埋点采集：批量 normalize + track 端点 + 嵌入脚本."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from userloop.tracking.snippet import TRACK_ENDPOINT, normalize_batch, snippet


def test_normalize_batch() -> None:
    batch = {
        "events": [
            {"distinct_id": "anon_1", "event": "page_view", "props": {"page": "/"}, "session_id": "s1", "event_id": "e1"},
            {"event": None},  # 无事件名 → 剔除
            {"distinct_id": "anon_2", "event": "element_click", "props": {}, "email": "a@x.com"},
        ]
    }
    out = normalize_batch(batch)
    assert len(out) == 2
    assert out[0]["props"]["session_id"] == "s1"
    assert out[1]["email"] == "a@x.com"
    assert out[1]["source"] == "web"
    ident = normalize_batch({"events": [
        {"distinct_id": "a", "event": "identify", "props": {"email": "i@x.com"}}]})
    assert ident[0]["email"] == "i@x.com"


def test_normalize_rejects_bad_shape() -> None:
    with pytest.raises(ValueError):
        normalize_batch({"events": "nope"})


def test_snippet_embeds_endpoint() -> None:
    js = snippet()
    assert "/api/v1/track" in js
    assert "page_view" in js
    assert "sendBeacon" in js
    assert "form_submit" in js and "identify" in js
    assert "preventDefault" not in js
    # 合法 JS 冒烟：大括号配对
    assert js.count("{") == js.count("}")


def test_track_endpoint_and_trackjs(tmp_path) -> None:
    from userloop.server.app import create_app

    app = create_app(str(tmp_path))
    with TestClient(app) as client:
        r = client.post("/api/v1/track", json={"events": [
            {"distinct_id": "w1", "event": "page_view", "props": {"page": "/"}, "event_id": "w-1"},
            {"distinct_id": "w1", "event": "element_click", "props": {"tag": "a"}, "event_id": "w-2"},
        ]})
        assert r.status_code == 200
        assert r.json()["accepted"] == 2
        # 幂等 + 限流：重复的 page_view 在 3 秒窗口内被静默丢弃（不写库）
        r2 = client.post("/api/v1/track", json={"events": [
            {"distinct_id": "w1", "event": "page_view", "props": {"page": "/"}, "event_id": "w-1"}]})
        assert r2.json()["accepted"] == 0
        assert r2.json()["throttled"] == 1
        users = client.get("/api/v1/users").json()
        assert users["count"] == 1
        # 埋点脚本可下载
        js = client.get("/track.js")
        assert js.status_code == 200 and "/api/v1/track" in js.text

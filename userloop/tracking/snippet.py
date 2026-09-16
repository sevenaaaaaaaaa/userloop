"""旅程埋点采集：零依赖 JS 追踪脚本 + 批量上报入口.

- /track.js 供任意站点嵌入一行 <script src=".../track.js"></script>
- 自动采集 page_view / element_click / 会话与匿名身份，sendBeacon 批量上报 /api/v1/track
- 服务端把事件转成标准 ingest 信封 → 事件总线（CDP 建档 → 旅程 → Loop）
"""

from __future__ import annotations

import json

TRACK_ENDPOINT = "/api/v1/track"

_TEMPLATE = """/* UserLoop Tracker v1 —— 嵌入：<script src="/track.js"></script> */
(function () {
  var EP = __ENDPOINT__;
  var K = "userloop_uid";
  function uid() {
    try {
      var v = localStorage.getItem(K);
      if (!v) { v = "anon_" + Math.random().toString(36).slice(2, 10); localStorage.setItem(K, v); }
      return v;
    } catch (e) { return "anon_session"; }
  }
  function sid() {
    try {
      var v = sessionStorage.getItem("userloop_sid");
      if (!v) { v = "s_" + Date.now().toString(36); sessionStorage.setItem("userloop_sid", v); }
      return v;
    } catch (e) { return "s_inline"; }
  }
  var buf = [];
  function track(event, props) {
    buf.push({
      distinct_id: uid(),
      event: event,
      props: props || {},
      source: "web",
      session_id: sid(),
      event_id: uid() + "-" + event + "-" + Date.now() + "-" + buf.length
    });
    if (buf.length >= 5) flush();
  }
  function flush() {
    if (!buf.length) return;
    var body = JSON.stringify({ events: buf.splice(0) });
    if (navigator.sendBeacon) {
      navigator.sendBeacon(EP, new Blob([body], { type: "application/json" }));
    } else {
      fetch(EP, { method: "POST", headers: { "Content-Type": "application/json" }, body: body, keepalive: true });
    }
  }
  window.userloop = { track: track, flush: flush, uid: uid, session: sid };
  track("page_view", { page: location.pathname, ref: document.referrer, title: document.title });
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-ul-track],a,button");
    if (!el) return;
    track("element_click", {
      page: location.pathname,
      tag: el.tagName.toLowerCase(),
      text: (el.textContent || "").slice(0, 40),
      label: el.getAttribute("data-ul-track") || ""
    });
  }, true);
  window.addEventListener("beforeunload", flush);
  setInterval(flush, 10000);
})();
"""


def snippet(endpoint: str = TRACK_ENDPOINT) -> str:
    return _TEMPLATE.replace("__ENDPOINT__", json.dumps(endpoint))


def normalize_batch(batch: dict) -> list[dict]:
    """/api/v1/track 批量信封 → 标准 ingest 事件列表。"""
    events = batch.get("events") if isinstance(batch, dict) else batch
    if not isinstance(events, list):
        raise ValueError("body must contain events: [...]")
    out = []
    for e in events:
        if not isinstance(e, dict) or not e.get("event"):
            continue
        props = e.get("props") or {}
        if e.get("session_id"):
            props = {**props, "session_id": e["session_id"]}
        item = {
            "distinct_id": e.get("distinct_id") or "anon_session",
            "event": e["event"],
            "props": props,
            "source": e.get("source", "web"),
            "event_id": e.get("event_id"),
        }
        if e.get("ts"):
            item["ts"] = e["ts"]
        if e.get("email"):
            item["email"] = e["email"]
        out.append(item)
    return out

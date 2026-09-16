"""web-tracker source 插件入口：collect() 返回嵌入片段与上报端点（对齐 MFlow source 插件约定）."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from userloop.tracking.snippet import TRACK_ENDPOINT, snippet  # noqa: E402


def collect(cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    return {
        "source": "web-tracker",
        "endpoint": cfg.get("endpoint", TRACK_ENDPOINT),
        "script": snippet(cfg.get("endpoint", TRACK_ENDPOINT)),
        "usage": '在站点 <body> 末尾加：<script src="{host}/track.js"></script>',
    }


if __name__ == "__main__":
    import json

    print(json.dumps(collect(), ensure_ascii=False, indent=2))

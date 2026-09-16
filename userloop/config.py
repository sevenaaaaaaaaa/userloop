"""工作区配置加载：data/config.json（可选）。"""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_DB = "userloop.db"


def load_config(data_dir: str | None = None) -> dict[str, Any]:
    base = data_dir or os.environ.get("USERLOOP_DATA") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    os.makedirs(base, exist_ok=True)
    cfg: dict[str, Any] = {
        "data_dir": base,
        "db_path": os.path.join(base, "userloop.db"),
        "host": "0.0.0.0",
        "port": 8600,
    }
    cfg_path = os.path.join(base, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["db_path"] = cfg.get("db_path") or os.path.join(base, DEFAULT_DB)
    return cfg

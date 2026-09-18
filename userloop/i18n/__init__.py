"""i18n：服务端文案目录 + 语言解析（租户默认语言 / 请求头 / 用户偏好）.

原则：
- **内容与版式分离**：语言只影响文案槽位与 AI 输出语言，不影响模板结构
- 解析顺序：请求显式指定 > 用户偏好 > 租户默认 > Accept-Language > zh-CN
- 缺 key 回退 zh-CN，再回退 key 本身（绝不抛错影响投递）
"""

from __future__ import annotations

import json
import os
from typing import Any

SUPPORTED = ("zh-CN", "en-US")
_DEFAULT = "zh-CN"
_CATALOGS: dict[str, dict[str, str]] = {}


def _load(locale: str) -> dict[str, str]:
    if locale in _CATALOGS:
        return _CATALOGS[locale]
    path = os.path.join(os.path.dirname(__file__), f"{locale}.json")
    try:
        with open(path, encoding="utf-8") as f:
            _CATALOGS[locale] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _CATALOGS[locale] = {}
    return _CATALOGS[locale]


def resolve_locale(request: Any = None, user: dict | None = None, tenant_cfg: dict | None = None) -> str:
    """解析当前语言（顺序：显式 query/header > 用户偏好 > 租户默认 > Accept-Language > 默认）。"""
    if request is not None:
        explicit = (request.query_params.get("lang") if hasattr(request, "query_params") else None) \
            or (request.headers.get("X-Locale") if hasattr(request, "headers") else None)
        if explicit in SUPPORTED:
            return explicit
    props = (user or {}).get("props")
    if isinstance(props, str):
        try:
            props = json.loads(props or "{}")
        except json.JSONDecodeError:
            props = {}
    loc = (props or {}).get("locale")
    if loc in SUPPORTED:
        return loc
    loc = (tenant_cfg or {}).get("locale")
    if loc in SUPPORTED:
        return loc
    if request is not None and hasattr(request, "headers"):
        for raw in (request.headers.get("Accept-Language") or "").split(","):
            code = raw.split(";")[0].strip()
            if code in SUPPORTED:
                return code
            if code.lower().startswith("en"):
                return "en-US"
            if code.lower().startswith("zh"):
                return "zh-CN"
    return _DEFAULT


def t(key: str, locale: str = _DEFAULT, **fmt: Any) -> str:
    """取文案（缺 key 回退默认语言，再回退 key 本身）。"""
    text = _load(locale).get(key) or _load(_DEFAULT).get(key) or key
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError):
            return text
    return text


def catalog(locale: str) -> dict[str, str]:
    return {**_load(_DEFAULT), **_load(locale)}

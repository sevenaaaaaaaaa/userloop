"""多租户内核：租户注册表 + 按租户隔离的数据目录与事件存储 + 请求级上下文.

隔离策略（延续"细胞式"原则）：
- **每租户独立数据目录**：`data/tenants/<id>/`（SQLite：users/loops/actions/… 全隔离）
- **事件存储按租户**：默认 SQLite（每租户独立文件）；租户可配置 MySQL
  （同一实例内**独立库**或独立表名，凭据由租户配置提供）
- **零泄漏**：请求级 ContextVar 决定当前租户的 Store，代理对象 `store` 自动路由；
  调度/CLI 显式指定租户（默认 main）

向后兼容：现有数据目录即租户 `main`。
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from typing import Any

DEFAULT_TENANT = "main"
_current_tenant: ContextVar[str] = ContextVar("userloop_tenant", default=DEFAULT_TENANT)
_current_store: ContextVar[Any] = ContextVar("userloop_current_store", default=None)


class TenantRegistry:
    """租户注册表（data/tenants.json）。"""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, "tenants.json")

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def ensure_default(self, cfg: dict) -> dict:
        """确保默认租户存在（指向现有数据目录，向后兼容）。"""
        data = self._load()
        if DEFAULT_TENANT not in data:
            data[DEFAULT_TENANT] = {
                "id": DEFAULT_TENANT, "name": "主工作区", "locale": "zh-CN",
                "timezone_offset": 8, "enabled": True, "data_dir": cfg["data_dir"],
                "storage": cfg.get("storage") or {}, "created_at": _now(),
            }
            self._save(data)
        return data[DEFAULT_TENANT]

    def list(self) -> list[dict]:
        return list(self._load().values())

    def get(self, tenant_id: str) -> dict | None:
        return self._load().get(tenant_id)

    def create(self, tenant_id: str, name: str = "", locale: str = "zh-CN",
               timezone_offset: int = 8, storage: dict | None = None) -> dict:
        tid = _slug(tenant_id)
        if not tid:
            raise ValueError("租户 id 不合法（小写字母/数字/中划线）")
        data = self._load()
        if tid in data:
            raise ValueError(f"租户已存在：{tid}")
        data_dir = os.path.join(self.base_dir, "tenants", tid)
        os.makedirs(data_dir, exist_ok=True)
        rec = {"id": tid, "name": name or tid, "locale": locale,
               "timezone_offset": int(timezone_offset), "enabled": True,
               "data_dir": data_dir, "storage": storage or {}, "created_at": _now()}
        data[tid] = rec
        self._save(data)
        return rec

    def update(self, tenant_id: str, **fields: Any) -> dict | None:
        data = self._load()
        if tenant_id not in data:
            return None
        for k in ("name", "locale", "timezone_offset", "enabled", "storage"):
            if k in fields:
                data[tenant_id][k] = fields[k]
        self._save(data)
        return data[tenant_id]


def _slug(raw: str) -> str:
    import re

    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", str(raw).lower())).strip("-")[:40]


def _now() -> str:
    from userloop.core.store import iso_now

    return iso_now()


# ── 请求级租户上下文 ──

def set_current(tenant_id: str) -> None:
    _current_tenant.set(tenant_id or DEFAULT_TENANT)


def current_tenant() -> str:
    return _current_tenant.get()


def set_current_store(store: Any, tenant_id: str | None = None) -> None:
    """中间件在请求入口设置：当前租户 + 其 Store。"""
    _current_store.set(store)
    if tenant_id:
        set_current(tenant_id)


def current_store() -> Any:
    store = _current_store.get()
    if store is None:
        raise RuntimeError("当前上下文没有 Store（请在请求中间件或显式设置后调用）")
    return store


def tenant_config(base_cfg: dict, tenant: dict) -> dict:
    """把租户记录合成为 Store 配置（独立 data_dir + 租户自己的事件存储）.

    **隔离要点**：新租户绝不继承基础配置的事件存储（否则会串进主租户的库）；
    默认用本租户 data_dir 下的 SQLite，需要 MySQL 时在租户记录里显式声明 storage。
    """
    cfg = dict(base_cfg)
    cfg["data_dir"] = tenant.get("data_dir") or base_cfg["data_dir"]
    cfg["db_path"] = os.path.join(cfg["data_dir"], "userloop.db")
    cfg["storage"] = tenant.get("storage") or {}     # 不回落 base_cfg（防串库）
    cfg["tenant"] = tenant.get("id")
    cfg["locale"] = tenant.get("locale") or "zh-CN"
    cfg["timezone_offset"] = int(tenant.get("timezone_offset") or 8)
    return cfg


class TenantStores:
    """租户 → Store 缓存（避免每请求重连；按租户路径缓存）。"""

    def __init__(self, base_cfg: dict) -> None:
        self.base_cfg = base_cfg
        self.registry = TenantRegistry(base_cfg["data_dir"])
        self.registry.ensure_default(base_cfg)
        self._cache: dict[str, Any] = {}

    async def get(self, tenant_id: str | None = None) -> Any:
        from userloop.core.store import Store

        tid = tenant_id or current_tenant()
        if tid in self._cache:
            return self._cache[tid]
        tenant = self.registry.get(tid)
        if not tenant or not tenant.get("enabled", True):
            raise KeyError(f"租户不存在或已停用：{tid}")
        cfg = tenant_config(self.base_cfg, tenant)
        store = Store(cfg["db_path"], cfg)
        await store.connect()
        self._cache[tid] = store
        return store

    async def close_all(self) -> None:
        for s in self._cache.values():
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass
        self._cache.clear()


class StoreProxy:
    """请求级 Store 代理：`await store.counts()` 自动路由到当前租户的 Store。"""

    def __getattr__(self, name: str) -> Any:
        return getattr(current_store(), name)

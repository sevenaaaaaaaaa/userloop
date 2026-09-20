"""身份图谱：跨渠道找人.

跨渠道触达的前置条件：知道不同渠道上的标识属于同一个人。
外部系统标识一律命名空间化（L22）：openflow_member / openflow_visitor / mflow_item，
禁止把各系统同名 visitor_id / item_id 绑到同一类型。
"""

from __future__ import annotations

from typing import Any

from userloop.core.store import Store

TYPES = (
    "email", "phone", "wechat_openid", "wechat_unionid", "wecom_userid",
    "websflow_visitor", "anonymous_id",
    "openflow_member", "openflow_visitor",
    "mflow_item",
)

# 事件/API 字段 → 身份类型。裸 item_id 不映射（MFlow 发布回调在专用通道里绑）。
EVENT_IDENT_MAP = {
    "email": "email", "phone": "phone", "mobile": "phone",
    "openid": "wechat_openid", "unionid": "wechat_unionid",
    "wecom_userid": "wecom_userid",
    "visitor_id": "websflow_visitor",
    "member_id": "openflow_member", "openflow_member_id": "openflow_member",
    "openflow_visitor_id": "openflow_visitor",
    "mflow_item_id": "mflow_item", "mflow_item": "mflow_item",
}

# 顺着用户已经在填的字段抬邮箱/手机，不另造采集点（L23）
_EMAIL_PATHS = (
    ("email",), ("user_email",), ("userloop_email",),
    ("user", "email"), ("customer", "email"), ("contact", "email"),
    ("traits", "email"), ("context", "traits", "email"),
    ("billing_address", "email"), ("properties", "email"),
)
_PHONE_PATHS = (
    ("phone",), ("mobile",), ("tel",),
    ("user", "phone"), ("customer", "phone"), ("contact", "phone"),
    ("traits", "phone"), ("properties", "phone"),
)


def _dig(d: Any, path: tuple[str, ...]) -> str | None:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if cur is None or cur == "":
        return None
    return str(cur).strip()


def lift_contacts(*blobs: Any) -> dict[str, str]:
    """从常见嵌套路径抬出 email/phone（Shopify customer、Segment traits、神策 properties）。"""
    email = phone = ""
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        if not email:
            for path in _EMAIL_PATHS:
                email = _dig(blob, path) or ""
                if email:
                    break
        if not phone:
            for path in _PHONE_PATHS:
                phone = _dig(blob, path) or ""
                if phone:
                    break
    out: dict[str, str] = {}
    if email:
        out["email"] = email
    if phone:
        out["phone"] = phone
    return out


async def bind(store: Store, user_id: str, type_: str, value: str,
               source: str = "", verified: bool = False) -> None:
    """绑定一个渠道身份（幂等；同类型同值只保留一条）。"""
    if not value or type_ not in TYPES:
        return
    await store.bind_identity(user_id, type_, value, source=source, verified=verified)


async def resolve(store: Store, type_: str, value: str) -> str | None:
    """按渠道身份反查 UserLoop 用户 id。"""
    return await store.resolve_identity(type_, value)


async def of_user(store: Store, user_id: str) -> dict[str, str]:
    """取某用户在各渠道的标识（投递时按渠道挑）。"""
    return await store.of_user_identities(user_id)


async def bind_or_merge(store: Store, user: dict, type_: str, value: str,
                        source: str = "") -> tuple[dict, bool]:
    """绑定身份；若标识已属他人则把当前档案并进已有档案（匿名→实名）。"""
    if not value or type_ not in TYPES:
        return user, False
    owner = await store.resolve_identity(type_, str(value))
    if owner and owner != user.get("id"):
        info = await store.merge_users(owner, user["id"])
        if info.get("merged"):
            return await store.get_user(owner) or user, True
    await store.bind_identity(user["id"], type_, str(value), source=source)
    return user, False


async def collect_from_event(store: Store, user_id: str, props: dict[str, Any], source: str) -> None:
    """从事件属性里自动补全身份（埋点带 email/phone/微信标识时调用）。"""
    lifted = lift_contacts(props)
    merged_props = {**lifted, **(props or {})}
    for key, type_ in EVENT_IDENT_MAP.items():
        val = merged_props.get(key)
        if val:
            await bind(store, user_id, type_, str(val), source=source or "event")

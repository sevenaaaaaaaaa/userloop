"""身份图谱：跨渠道找人（email / phone / 微信 openid / unionid / 企微 userid / WebsFlow visitor）.

跨渠道触达的前置条件：知道不同渠道上的标识属于同一个人。
"""

from __future__ import annotations

from typing import Any

from userloop.core.store import Store, iso_now

TYPES = ("email", "phone", "wechat_openid", "wechat_unionid", "wecom_userid",
         "websflow_visitor", "anonymous_id")


async def bind(store: Store, user_id: str, type_: str, value: str,
               source: str = "", verified: bool = False) -> None:
    """绑定一个渠道身份（幂等；同类型同值只保留一条）。"""
    if not value or type_ not in TYPES:
        return
    await store.db.execute(
        "INSERT INTO identities (user_id, type, value, verified, source, created_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(type, value) DO UPDATE SET user_id=excluded.user_id, verified=excluded.verified, "
        "source=excluded.source",
        (user_id, type_, str(value).strip().lower() if type_ == "email" else str(value).strip(),
         1 if verified else 0, source, iso_now()),
    )


async def resolve(store: Store, type_: str, value: str) -> str | None:
    """按渠道身份反查 UserLoop 用户 id。"""
    v = str(value).strip().lower() if type_ == "email" else str(value).strip()
    cur = await store.db.execute("SELECT user_id FROM identities WHERE type=? AND value=?", (type_, v))
    row = await cur.fetchone()
    return row["user_id"] if row else None


async def of_user(store: Store, user_id: str) -> dict[str, str]:
    """取某用户在各渠道的标识（投递时按渠道挑）。"""
    cur = await store.db.execute("SELECT type, value FROM identities WHERE user_id=?", (user_id,))
    return {r["type"]: r["value"] for r in await cur.fetchall()}


async def collect_from_event(store: Store, user_id: str, props: dict[str, Any], source: str) -> None:
    """从事件属性里自动补全身份（埋点带 email/phone/微信标识时调用）。"""
    mapping = {"email": "email", "phone": "phone", "mobile": "phone",
               "openid": "wechat_openid", "unionid": "wechat_unionid",
               "wecom_userid": "wecom_userid", "visitor_id": "websflow_visitor",
               "member_id": "openflow_member", "openflow_member_id": "openflow_member",
               "openflow_visitor_id": "openflow_visitor"}
    for key, type_ in mapping.items():
        val = props.get(key)
        if val:
            await bind(store, user_id, type_, str(val), source=source or "event")

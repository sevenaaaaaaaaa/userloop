"""R2 异地备份：把本地备份归档推送到 Cloudflare R2（S3 兼容，纯标准库 SigV4）.

- **零额外依赖**：仅用 hashlib/hmac + 已有的 httpx 手写 AWS Signature V4
- **失败静默降级**：R2 不可达时不影响本地备份（返回 ok=False 记录原因）
- **只推归档**：备份包（tar.gz），不推运行时数据；事件在 MySQL 的另行 mysqldump

配置（data/config.json）：
{ "ops": { "offsite": {
    "enabled": true, "endpoint": "https://<acct>.r2.cloudflarestorage.com",
    "bucket": "nownexts-static", "region": "auto", "prefix": "userloop-backups",
    "access_key": "...", "secret_key": "..." } } }
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

SERVICE = "s3"
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def cfg_of(cfg: dict) -> dict[str, Any]:
    raw = ((cfg.get("ops") or {}).get("offsite") or {})
    return {
        "enabled": bool(raw.get("enabled")),
        "endpoint": str(raw.get("endpoint") or "").rstrip("/"),
        "bucket": str(raw.get("bucket") or ""),
        "region": str(raw.get("region") or "auto"),
        "prefix": str(raw.get("prefix") or "userloop-backups").strip("/"),
        "access_key": str(raw.get("access_key") or ""),
        "secret_key": str(raw.get("secret_key") or ""),
        "timeout": int(raw.get("timeout") or 60),
    }


def enabled(cfg: dict) -> bool:
    c = cfg_of(cfg)
    return bool(c["enabled"] and c["endpoint"] and c["bucket"] and c["access_key"] and c["secret_key"])


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, SERVICE)
    return _hmac(k, "aws4_request")


def sign_headers(method: str, host: str, path: str, query: dict[str, str] | None,
                 payload: bytes, access_key: str, secret_key: str,
                 region: str = "auto", extra_headers: dict[str, str] | None = None,
                 now: datetime | None = None) -> dict[str, str]:
    """返回应附加到请求上的头（含 Authorization / x-amz-date / x-amz-content-sha256）。"""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = _sha256_hex(payload)

    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = " ".join(str(v).split())      # 归一化空白（SigV4 要求）
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))

    q = query or {}
    canonical_query = "&".join(f"{quote(k, safe='')}={quote(str(q[k]), safe='')}" for k in sorted(q))
    canonical_uri = "/" + quote(path.lstrip("/"), safe="/")

    canonical_request = "\n".join([method, canonical_uri, canonical_query,
                                   canonical_headers, signed_headers, payload_hash])
    scope = f"{date}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                _sha256_hex(canonical_request.encode("utf-8"))])
    signature = hmac.new(_signing_key(secret_key, date, region),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                f"SignedHeaders={signed_headers}, Signature={signature}")
    return headers


def _url_and_path(c: dict, key: str) -> tuple[str, str, str]:
    """返回 (url, host, path)。采用 path-style：{endpoint}/{bucket}/{key}。"""
    host = c["endpoint"].split("://", 1)[-1].rstrip("/")
    path = f"/{c['bucket']}/{key}" if key else f"/{c['bucket']}"
    return f"{c['endpoint']}{path}", host, path


async def _request(cfg: dict, method: str, key: str, *, query: dict[str, str] | None = None,
                   body: bytes = b"", extra_headers: dict[str, str] | None = None,
                   timeout: int | None = None) -> httpx.Response:
    c = cfg_of(cfg)
    url, host, path = _url_and_path(c, key)
    headers = sign_headers(method, host, path, query, body, c["access_key"], c["secret_key"],
                           c["region"], extra_headers)
    headers = {k.capitalize() if k in ("host",) else k: v for k, v in headers.items()}
    send = {k: v for k, v in headers.items() if k.lower() != "host"}
    # 还原标准大小写头名
    fixed = {}
    for k, v in send.items():
        fixed[{"authorization": "Authorization", "x-amz-date": "X-Amz-Date",
               "x-amz-content-sha256": "X-Amz-Content-Sha256"}.get(k, k)] = v
    async with httpx.AsyncClient(timeout=timeout or c["timeout"]) as client:
        return await client.request(method, url, params=query, content=body, headers=fixed)


async def upload_archive(cfg: dict, local_path: str, key: str | None = None) -> dict[str, Any]:
    """上传一个备份归档到 R2。返回 {ok, key, bytes, url} 或 {ok:False, error}。"""
    c = cfg_of(cfg)
    if not enabled(cfg):
        return {"ok": False, "skipped": True, "error": "未配置/未启用 ops.offsite"}
    if not os.path.exists(local_path):
        return {"ok": False, "error": f"归档不存在：{local_path}"}
    name = os.path.basename(local_path)
    key = key or (f"{c['prefix']}/{name}" if c["prefix"] else name)
    with open(local_path, "rb") as f:
        data = f.read()
    try:
        r = await _request(cfg, "PUT", key, body=data,
                           extra_headers={"content-type": "application/gzip"})
        if r.status_code in (200, 201):
            return {"ok": True, "key": key, "bytes": len(data),
                    "url": f"{c['endpoint']}/{c['bucket']}/{key}"}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}", "key": key}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "key": key}


async def list_remote(cfg: dict, prefix: str | None = None) -> dict[str, Any]:
    """列出 R2 上的备份对象（list-objects-v2）。"""
    c = cfg_of(cfg)
    if not enabled(cfg):
        return {"ok": False, "skipped": True, "error": "未配置/未启用", "objects": []}
    p = prefix if prefix is not None else (c["prefix"] + "/" if c["prefix"] else "")
    query = {"list-type": "2", "prefix": p, "max-keys": "1000"}
    try:
        r = await _request(cfg, "GET", "", query=query)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}", "objects": []}
        import re

        objs = []
        for m in re.finditer(r"<Contents>(.*?)</Contents>", r.text, re.S):
            blk = m.group(1)
            key = re.search(r"<Key>(.*?)</Key>", blk, re.S)
            size = re.search(r"<Size>(.*?)</Size>", blk, re.S)
            lm = re.search(r"<LastModified>(.*?)</LastModified>", blk, re.S)
            objs.append({"key": key.group(1) if key else "", "bytes": int(size.group(1)) if size else 0,
                         "last_modified": lm.group(1) if lm else ""})
        return {"ok": True, "prefix": p, "objects": objs}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "objects": []}


async def download(cfg: dict, key: str, dest: str) -> dict[str, Any]:
    """从 R2 拉回一个对象（恢复演练/真恢复用）。"""
    if not enabled(cfg):
        return {"ok": False, "skipped": True, "error": "未配置/未启用"}
    if not key or key.strip() in ("", "/") or key.endswith("/"):
        # 空 key 的 GET 会落到「列桶」语义并返回 XML，绝不能当成对象写入
        return {"ok": False, "error": "key 不能为空（需完整对象 key，如 userloop-backups/xxx.tar.gz）"}
    try:
        r = await _request(cfg, "GET", key)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(r.content)
        return {"ok": True, "key": key, "bytes": len(r.content), "dest": dest}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


async def prune_remote(cfg: dict, keep: int = 7) -> dict[str, Any]:
    """远端保留最近 keep 份（按 key 名倒序），删除更旧的。"""
    lst = await list_remote(cfg)
    if not lst.get("ok"):
        return {"ok": False, "error": lst.get("error"), "deleted": []}
    objs = sorted(lst["objects"], key=lambda o: o["key"], reverse=True)
    deleted = []
    for o in objs[keep:]:
        r = await _request(cfg, "DELETE", o["key"])
        if r.status_code in (200, 204):
            deleted.append(o["key"])
    return {"ok": True, "deleted": deleted}

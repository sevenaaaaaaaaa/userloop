"""短信供应商直连（P1）：阿里云 / 腾讯云 / 通用 webhook 网关.

为什么自建：OpenFlow 只有供应商配置（admin/sms.php）没有发送实现（已实测确认），
而流失召回最依赖短信。这里补齐，并保持"未配置 → 明确报错，不假装成功"。

供应商签名规范：
- 阿里云 Dysmsapi（RPC 风格）：HMAC-SHA1，参数排序 + 特殊编码
- 腾讯云 SMS（TC3-HMAC-SHA256）：日期 → 服务 → 签名密钥 → 签名
- webhook：任意网关（自研/第三方聚合），POST {phone, content, sign, template}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import uuid
from typing import Any

import httpx


# ── 阿里云 ──

def _aliyun_percent_encode(value: str) -> str:
    return urllib.parse.quote(value, safe="~")


def aliyun_signature(params: dict[str, str], access_secret: str, method: str = "POST") -> str:
    """阿里云 RPC 签名（HMAC-SHA1，基于排序后的规范化查询串）。"""
    canonical = "&".join(
        f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(str(v))}" for k, v in sorted(params.items())
    )
    string_to_sign = f"{method}&%2F&{_aliyun_percent_encode(canonical)}"
    digest = hmac.new((access_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def send_aliyun(cfg: dict, phone: str, content: str, transport: Any = None) -> dict[str, Any]:
    template_code = str(cfg.get("template_code") or "")
    if not template_code:
        return {"ok": False, "error": "阿里云短信需配置 template_code（模板报备后填写）"}
    params: dict[str, str] = {
        "AccessKeyId": str(cfg.get("access_key_id") or ""),
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": str(cfg.get("region_id") or "cn-hangzhou"),
        "SignName": str(cfg.get("sign_name") or ""),
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "TemplateCode": template_code,
        "TemplateParam": json.dumps(cfg.get("template_param") or {"content": content}, ensure_ascii=False),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": "2017-05-25",
    }
    params["Signature"] = aliyun_signature(params, str(cfg.get("access_key_secret") or ""))
    try:
        async with httpx.AsyncClient(timeout=12, transport=transport) as client:
            resp = await client.post(str(cfg.get("endpoint") or "https://dysmsapi.aliyuncs.com/"), data=params)
        data = resp.json() if resp.status_code == 200 else {}
        ok = str(data.get("Code", "")) == "OK"
        return {"ok": ok, "ref": data.get("BizId"), "error": None if ok else (data.get("Message") or f"HTTP {resp.status_code}")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"阿里云调用失败：{exc}"}


# ── 腾讯云（TC3-HMAC-SHA256）──

def tencent_authorization(secret_id: str, secret_key: str, action: str, payload: str,
                          host: str = "sms.tencentcloudapi.com", service: str = "sms",
                          region: str = "ap-guangzhou", ts: int | None = None) -> str:
    ts = ts or int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))
    canonical_request = "\n".join([
        "POST", "/", "",
        f"content-type:application/json; charset=utf-8\nhost:{host}\nx-tc-action:{action.lower()}\n",
        "content-type;host;x-tc-action",
        hashlib.sha256(payload.encode()).hexdigest(),
    ])
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(ts), credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    secret_date = _hmac(("TC3" + secret_key).encode(), date)
    secret_service = _hmac(secret_date, service)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}")


async def send_tencent(cfg: dict, phone: str, content: str, transport: Any = None) -> dict[str, Any]:
    template_id = str(cfg.get("template_id") or "")
    if not template_id:
        return {"ok": False, "error": "腾讯云短信需配置 template_id + sdk_app_id 与 sign_name"}
    body = {
        "PhoneNumberSet": [phone if phone.startswith("+") else f"+86{phone}"],
        "SmsSdkAppId": str(cfg.get("sdk_app_id") or ""),
        "SignName": str(cfg.get("sign_name") or ""),
        "TemplateId": template_id,
        "TemplateParamSet": cfg.get("template_params") or [content],
    }
    payload = json.dumps(body, ensure_ascii=False)
    region = str(cfg.get("region") or "ap-guangzhou")
    ts = int(time.time())
    headers = {
        "Authorization": tencent_authorization(str(cfg.get("secret_id") or ""), str(cfg.get("secret_key") or ""),
                                               "SendSms", payload, region=region, ts=ts),
        "Content-Type": "application/json; charset=utf-8",
        "Host": "sms.tencentcloudapi.com",
        "X-TC-Action": "SendSms",
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": "2021-01-11",
        "X-TC-Region": region,
    }
    try:
        async with httpx.AsyncClient(timeout=12, transport=transport) as client:
            resp = await client.post("https://sms.tencentcloudapi.com/", content=payload.encode(), headers=headers)
        data = resp.json() if resp.status_code == 200 else {}
        r = (data.get("Response") or {})
        status = ((r.get("SendStatusSet") or [{}])[0]).get("Code", "")
        ok = status == "Ok"
        return {"ok": ok, "ref": ((r.get("SendStatusSet") or [{}])[0]).get("SerialNo"),
                "error": None if ok else (r.get("Error", {}).get("Message") or status or f"HTTP {resp.status_code}")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"腾讯云调用失败：{exc}"}


# ── 通用网关（自研/聚合服务商）──

async def send_webhook(cfg: dict, phone: str, content: str, transport: Any = None) -> dict[str, Any]:
    url = str(cfg.get("webhook_url") or "")
    if not url:
        return {"ok": False, "error": "未配置 webhook_url"}
    body = {"phone": phone, "content": content, "sign_name": cfg.get("sign_name", ""),
            "template_code": cfg.get("template_code", "")}
    headers = {"Content-Type": "application/json"}
    token = cfg.get("webhook_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=12, transport=transport) as client:
            resp = await client.post(url, json=body, headers=headers)
        data = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        ok = 200 <= resp.status_code < 300 and (data.get("ok", True) is not False)
        return {"ok": ok, "ref": data.get("ref") or data.get("id"),
                "error": None if ok else (data.get("error") or f"HTTP {resp.status_code}")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"网关调用失败：{exc}"}


PROVIDERS = {"aliyun": send_aliyun, "tencent": send_tencent, "webhook": send_webhook}


async def send(cfg: dict, phone: str, content: str, transport: Any = None) -> dict[str, Any]:
    """统一入口：按 provider 分发；未配置供应商时返回明确错误（不静默）。"""
    if not cfg.get("enabled"):
        return {"ok": False, "error": "短信通道未启用（touch.sms.enabled=true 并配置供应商凭据）"}
    provider = str(cfg.get("provider") or "").lower()
    fn = PROVIDERS.get(provider)
    if fn is None:
        return {"ok": False, "error": f"未配置短信供应商（provider ∈ {', '.join(PROVIDERS)}）"}
    return await fn(cfg, phone, content, transport=transport)

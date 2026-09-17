"""内容渲染：邮件 HTML 版式 / H5 页 / IM 短文案.

原则：**版式由模板保证，AI 只填内容槽位**（避免 AI 生成的"丑邮件"）。
所有外发内容自动注入：追踪像素、CTA 追踪跳转、退订链接。
"""

from __future__ import annotations

import html
from typing import Any

from userloop.touch.base import TouchSpec, make_token

EMAIL_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#f4f4f5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
         style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;
                font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2937;">
    <tr><td style="padding:20px 28px;background:linear-gradient(135deg,#2563eb,#7c5cff);color:#fff;
                   font-size:16px;font-weight:600;">{brand}</td></tr>
    <tr><td style="padding:28px 28px 8px;font-size:20px;font-weight:700;line-height:1.4;">{heading}</td></tr>
    <tr><td style="padding:0 28px 8px;font-size:15px;line-height:1.8;color:#374151;">{body}</td></tr>
    {cta_row}
    <tr><td style="padding:8px 28px 24px;font-size:13px;line-height:1.7;color:#6b7280;">{signature}</td></tr>
    <tr><td style="padding:16px 28px;background:#f9fafb;font-size:12px;color:#9ca3af;line-height:1.7;">
      {footer}<br>
      <a href="{unsub_url}" style="color:#9ca3af;text-decoration:underline;">不想再收到此类邮件？点此退订</a>
    </td></tr>
  </table>
</td></tr></table>
{open_pixel}
</body></html>"""

CTA_ROW = """<tr><td style="padding:12px 28px 20px;">
  <a href="{cta_url}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;
     padding:12px 26px;border-radius:10px;font-size:15px;font-weight:600;">{cta_text}</a>
</td></tr>"""


def _paragraphs(text: str) -> str:
    parts = [p.strip() for p in (text or "").split("\n") if p.strip()]
    return "".join(f"<p style='margin:0 0 12px;'>{html.escape(p)}</p>" for p in parts)


def render_email(spec: TouchSpec, user: dict, cfg: dict) -> dict[str, str]:
    """渲染 HTML 邮件：返回 {subject, html, text}。自动注入追踪与退订。"""
    touch_cfg = cfg.get("touch") or {}
    secret = str(touch_cfg.get("track_secret") or cfg.get("api_token") or "userloop")
    base = str(touch_cfg.get("public_base") or "https://nownexts.com/userloop").rstrip("/")
    brand = str(touch_cfg.get("brand") or "UserLoop")
    signature = str(touch_cfg.get("signature") or "")

    token = make_token(secret, user["id"], spec.loop_id,
                       {"t": spec.template_id or "", "c": "email", "g": spec.goal_event or ""})
    open_url = f"{base}/t/e/open.gif?t={token}"
    unsub_url = f"{base}/t/e/unsubscribe?t={token}"

    cta_url = spec.cta_url
    if cta_url:
        cta_url = f"{base}/t/e/click?t={token}&u={cta_url}"
    else:
        cta_url = open_url  # 无 CTA 时按钮指向追踪（避免死链）

    cta_row = CTA_ROW.format(cta_url=cta_url, cta_text=html.escape(spec.cta_text)) if spec.cta_text else ""
    body_html = _paragraphs(spec.body)
    heading = html.escape(spec.title or brand)

    html_out = EMAIL_SHELL.format(
        title=html.escape(spec.title or brand), brand=html.escape(brand), heading=heading,
        body=body_html, cta_row=cta_row, signature=html.escape(signature) if signature else "",
        footer=html.escape(str(touch_cfg.get("footer") or "你收到这封邮件是因为你曾注册或订阅我们的服务。")),
        unsub_url=unsub_url, open_pixel=f'<img src="{open_url}" width="1" height="1" alt="" style="display:block;border:0;">',
    )
    text = f"{spec.title}\n\n{spec.body}\n\n{spec.cta_text}: {cta_url}\n\n退订: {unsub_url}"
    return {"subject": spec.title or brand, "html": html_out, "text": text}


def render_im(spec: TouchSpec) -> str:
    """IM/短信短文案（含追踪短链）。"""
    link = spec.cta_url or ""
    parts = [spec.title, spec.body, f"{spec.cta_text}: {link}" if link else ""]
    return "\n".join(p for p in parts if p)


def render_h5(spec: TouchSpec, cfg: dict) -> dict[str, str]:
    """极简 H5 页（P0 兜底；P1 交给 WebsFlow 生成投放页）。"""
    title = html.escape(spec.title or "详情")
    body = _paragraphs(spec.body)
    cta = (f'<a href="{html.escape(spec.cta_url)}" style="display:inline-block;background:#2563eb;color:#fff;'
           f'text-decoration:none;padding:12px 26px;border-radius:10px;font-weight:600;">{html.escape(spec.cta_text)}</a>'
           if spec.cta_url else "")
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="margin:0;font-family:-apple-system,'PingFang SC',sans-serif;background:#fafafa;color:#1f2937;">
<div style="max-width:640px;margin:0 auto;padding:28px 20px 48px;">
  <h1 style="font-size:22px;line-height:1.4;">{title}</h1>
  <div style="font-size:16px;line-height:1.9;color:#374151;">{body}</div>
  <div style="margin-top:24px;">{cta}</div>
</div></body></html>"""
    return {"title": spec.title, "html": page}

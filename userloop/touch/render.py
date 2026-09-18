"""内容渲染：邮件 HTML 版式 / H5 页 / IM 短文案.

原则：**版式由模板保证，AI 只填内容槽位**（避免 AI 生成的"丑邮件"）。
所有外发内容自动注入：追踪像素、CTA 追踪跳转、退订链接。
"""

from __future__ import annotations

import html
import json
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
      <a href="{unsub_url}" style="color:#9ca3af;text-decoration:underline;">{unsub_label}</a>
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
    """渲染 HTML 邮件：返回 {subject, html, text}。自动注入追踪与退订（按语言本地化）。"""
    from userloop.i18n import resolve_locale, t

    locale = resolve_locale(None, user, cfg)
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

    cta_text_plain = spec.cta_text or t("email.cta_default", locale)
    cta_row = CTA_ROW.format(cta_url=cta_url, cta_text=html.escape(cta_text_plain)) if cta_text_plain else ""
    body_html = _paragraphs(spec.body)
    heading = html.escape(spec.title or brand)

    html_out = EMAIL_SHELL.format(
        title=html.escape(spec.title or brand), brand=html.escape(brand), heading=heading,
        body=body_html, cta_row=cta_row, signature=html.escape(signature) if signature else "",
        footer=html.escape(str(touch_cfg.get("footer") or t("email.footer", locale))),
        unsub_label=html.escape(t("email.unsubscribe", locale)),
        unsub_url=unsub_url, open_pixel=f'<img src="{open_url}" width="1" height="1" alt="" style="display:block;border:0;">',
    )
    text = f"{spec.title}\n\n{spec.body}\n\n{spec.cta_text}: {cta_url}\n\n退订: {unsub_url}"
    return {"subject": spec.title or brand, "html": html_out, "text": text}


def render_im(spec: TouchSpec) -> str:
    """IM/短信短文案（含追踪短链）。"""
    link = spec.cta_url or ""
    parts = [spec.title, spec.body, f"{spec.cta_text}: {link}" if link else ""]
    return "\n".join(p for p in parts if p)


def render_h5(spec: TouchSpec, cfg: dict, content: dict[str, Any] | None = None) -> dict[str, str]:
    """内置 H5 页：支持千人千面（设备/UTM/来源/Cookie/阶段）+ 互动时长切换 CTA。

    content 由 userloop.touch.personalize.builtin_content() 产出（服务端已判定维度）。
    """
    from userloop.i18n import resolve_locale, t

    c = content or {}
    locale = c.get("locale") or resolve_locale(None, None, cfg)
    title = html.escape(c.get("title") or spec.title or "详情")
    body = _paragraphs(c.get("body") or spec.body)
    badge = c.get("badge") or ""
    subtitle = c.get("subtitle") or ""
    cta_text = html.escape(spec.cta_text or t("email.cta_default", locale))
    cta_url = html.escape(spec.cta_url or "#")
    device = c.get("device") or "desktop"
    dwell = c.get("dwell") or {}
    lead = c.get("lead") or {}
    lead_form = (
        '<form id="ul-lead" style="margin-top:22px;padding:16px;border:1px solid #e5e7eb;border-radius:12px;">'
        '<div style="font-size:14px;font-weight:600;margin-bottom:8px;">'
        + html.escape(lead.get("title") or t("h5.lead_title", locale)) + '</div>'
        '<input id="ul-lead-email" type="email" placeholder="' + html.escape(t("h5.lead_email", locale)) + '" '
        'style="width:100%;height:42px;padding:0 12px;border:1px solid #d1d5db;border-radius:10px;margin-bottom:8px;">'
        '<input id="ul-lead-phone" type="tel" placeholder="' + html.escape(t("h5.lead_phone", locale)) + '" '
        'style="width:100%;height:42px;padding:0 12px;border:1px solid #d1d5db;border-radius:10px;margin-bottom:10px;">'
        '<button type="submit" style="width:100%;height:42px;border:0;border-radius:10px;background:#111827;color:#fff;'
        'font-size:15px;font-weight:600;cursor:pointer;">' + html.escape(t("h5.lead_submit", locale)) + '</button>'
        '<div id="ul-lead-ok" style="display:none;color:#16a34a;font-size:13px;margin-top:8px;">✓ '
        + html.escape(t("h5.lead_ok", locale)) + '</div>'
        '</form>') if lead.get("enabled") else ""
    lead_endpoint = html.escape(str(lead.get("endpoint") or ""))
    lead_token_json = json.dumps(str(lead.get("token") or ""))
    secs = int(dwell.get("seconds") or 0)
    alt_text = html.escape(dwell.get("alt_cta_text") or "")
    alt_note = html.escape(dwell.get("alt_note") or t("h5.dwell_note", locale))
    if not dwell.get("alt_cta_text"):
        alt_text = html.escape(t("h5.dwell_cta", locale))

    badge_html = (f'<div style="display:inline-block;padding:4px 12px;border-radius:999px;background:#eef2ff;'
                  f'color:#4338ca;font-size:13px;font-weight:600;margin-bottom:14px;">{html.escape(badge)}</div>'
                  if badge else "")
    subtitle_html = (f'<p style="font-size:17px;color:#4b5563;margin:10px 0 0;">{html.escape(subtitle)}</p>'
                     if subtitle else "")
    pad = "20px 18px 56px" if device == "mobile" else "32px 24px 72px"
    font = "24px" if device == "mobile" else "30px"

    return {"title": c.get("title") or spec.title, "html": f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#fafafa;color:#111827;">
<div style="max-width:680px;margin:0 auto;padding:{pad};">
  {badge_html}
  <h1 style="font-size:{font};line-height:1.35;margin:0;">{title}</h1>
  {subtitle_html}
  <div style="font-size:16px;line-height:1.9;color:#374151;margin-top:18px;">{body}</div>
  <div style="margin-top:26px;">
    <a id="ul-cta" href="{cta_url}" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;
       padding:14px 30px;border-radius:12px;font-size:16px;font-weight:600;">{cta_text}</a>
    <div id="ul-dwell-note" style="display:none;margin-top:10px;font-size:13px;color:#6b7280;">{alt_note}</div>
  </div>
  {lead_form}
</div>
<script>
// 留资（识别引导）：匿名访客留邮箱/手机 → 绑定为实名档案（匿名→实名合并）
(function(){{
  var f = document.getElementById('ul-lead');
  if (!f) return;
  f.addEventListener('submit', function(e){{
    e.preventDefault();
    var email = (document.getElementById('ul-lead-email')||{{}}).value || '';
    var phone = (document.getElementById('ul-lead-phone')||{{}}).value || '';
    if (!email && !phone) return;
    fetch('{lead_endpoint}', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{t: {lead_token_json}, email: email, phone: phone}})}})
      .then(function(r){{ return r.json(); }})
      .then(function(d){{ var ok=document.getElementById('ul-lead-ok'); if (ok) ok.style.display='block'; f.style.display='none'; }})
      .catch(function(){{}});
  }});
}})();
// 互动时长：停留 N 秒后强化 CTA（千人千面之"行为维度"）
(function(){{
  var secs = {secs}; if (!secs) return;
  var cta = document.getElementById('ul-cta'), note = document.getElementById('ul-dwell-note');
  var alt = {json.dumps(alt_text) if alt_text else 'null'};
  setTimeout(function(){{
    if (alt && cta) cta.textContent = alt;
    if (cta) cta.style.background = '#16a34a';
    if (note) note.style.display = 'block';
  }}, secs * 1000);
}})();
</script>
</body></html>"""}



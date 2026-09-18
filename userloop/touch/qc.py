"""发前质检门（Pre-send QC）：内容是"AI 生成/人写"都可能出错，外发前必须过门.

两类问题：
- **block**（必须拦）：缺少退订链接、残留占位符（{name}）、空内容、邮件无追踪像素
- **warn**（提示不拦）：垃圾词/夸张词、全大写主题、主题过长、http 明文链接、CTA 文案过短

保护对象：终端用户体验（避免收到坏邮件）与发件声誉（垃圾评分/投诉）。
与 MFlow 的 exit-code 质量钩子同思路，但作用在 UserLoop 外发链路上。
"""

from __future__ import annotations

import re
from typing import Any

SPAM_WORDS = ["免费", "保证", "中奖", "点击领取", "限时抢购", "100%", "稳赚", "无需付款",
              "act now", "risk-free", "guarantee", "winner", "click here", "free money"]
PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}", re.I)


def check_email(subject: str, html: str, *, require_tracking: bool = True) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(level: str, code: str, msg: str) -> None:
        issues.append({"level": level, "code": code, "message": msg})

    if not (html or "").strip():
        add("block", "empty_body", "邮件正文为空")
    if "退订" not in html and "unsubscribe" not in html.lower():
        add("block", "no_unsubscribe", "缺少退订链接/说明（合规必需）")
    if require_tracking and "/t/e/open.gif" not in html:
        add("warn", "no_tracking_pixel", "缺少打开追踪像素（打开率将无法统计）")
    left = PLACEHOLDER.findall(html) + PLACEHOLDER.findall(subject or "")
    if left:
        add("block", "placeholder_left", f"残留未替换占位符：{', '.join(sorted(set(left))[:4])}")
    if len(subject or "") > 60:
        add("warn", "subject_too_long", f"主题过长（{len(subject)} 字符，建议 ≤60，移动端会被截断）")
    if subject and subject.upper() == subject and re.search(r"[A-Z]{6,}", subject):
        add("warn", "subject_all_caps", "主题全大写，易被判为垃圾邮件")
    low = f"{subject} {html}".lower()
    hit = [w for w in SPAM_WORDS if w.lower() in low]
    if hit:
        add("warn", "spam_words", f"疑似垃圾词：{', '.join(hit[:4])}（可能降低送达率）")
    if re.search(r'href="http://', html or ""):
        add("warn", "insecure_link", "存在 http 明文链接，建议全部 https")
    return issues


def check_h5(title: str, body: str, cta_text: str, cta_url: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(level: str, code: str, msg: str) -> None:
        issues.append({"level": level, "code": code, "message": msg})

    if not (title or "").strip():
        add("warn", "no_title", "页面缺少标题（首屏无重点）")
    if not (cta_text or "").strip():
        add("warn", "no_cta", "页面缺少行动按钮文案")
    if (cta_text or "") and len(cta_text) > 14:
        add("warn", "cta_too_long", f"CTA 文案偏长（{len(cta_text)} 字，按钮会换行）")
    if (cta_url or "") and not str(cta_url).lower().startswith(("http://", "https://")):
        add("warn", "cta_url_relative", "CTA 目标不是绝对地址，站外访问可能 404")
    left = PLACEHOLDER.findall(f"{title} {body}")
    if left:
        add("block", "placeholder_left", f"残留未替换占位符：{', '.join(sorted(set(left))[:4])}")
    if not (body or "").strip():
        add("warn", "empty_body", "页面正文为空")
    return issues


def summarize(issues: list[dict[str, str]]) -> dict[str, Any]:
    blocking = [i for i in issues if i["level"] == "block"]
    warnings = [i for i in issues if i["level"] == "warn"]
    return {"ok": not blocking, "blocking": blocking, "warnings": warnings, "issues": issues}

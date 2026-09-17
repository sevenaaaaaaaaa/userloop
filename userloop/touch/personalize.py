"""千人千面：按 来源 / 旅程 / Cookie 状态 / UTM / 设备 / 互动时长 出不同内容。

分工（与 WebsFlow 引擎能力对齐，避免区块重复渲染）：
- **设备** → SSR `audience` 区块双版（mobile/desktop 各一套主视觉，互斥显示）
- **来源 / UTM / 回访 Cookie / 登录态 / 旅程阶段** → `personalize` 运行时补丁
  （引擎按顺序取首个命中，只 patch 文案，不产生重复区块）
- **互动时长** → 内置页 JS（N 秒后切换 CTA / 显示二次行动）+ WebsFlow `countdown` 区块表达紧迫感
"""

from __future__ import annotations

from typing import Any

# ── 人群段定义（WebsFlow 项目级 segments；rules 形状与其 matchAudience 一致）──

SOURCE_SEGMENTS: list[dict[str, Any]] = [
    {"id": "seg_utm_wechat", "name": "微信来源", "rules": {"utm": "utm_source=wechat"}},
    {"id": "seg_utm_ads", "name": "广告投放", "rules": {"utm": "utm_medium=cpc"}},
    {"id": "seg_utm_edm", "name": "邮件引流", "rules": {"utm": "utm_medium=email"}},
    {"id": "seg_return", "name": "回访访客", "rules": {"visitor": "return"}},
    {"id": "seg_login", "name": "已登录用户", "rules": {"login": "in"}},
]

STAGE_SEGMENTS: list[dict[str, Any]] = [
    {"id": f"seg_stage_{s}", "name": f"阶段 · {s}", "rules": {"utm": f"utm_content=stage-{s}"}}
    for s in ("signup", "activated", "paying", "retained", "advocate", "churn_risk", "churned")
]

# 来源 → 文案补丁（patch 只覆盖内容槽位）
SOURCE_PATCH: dict[str, dict[str, str]] = {
    "seg_utm_wechat": {"badge": "微信专享", "subtitle": "微信用户专属通道，一步到位"},
    "seg_utm_ads": {"badge": "限时活动", "subtitle": "你在广告里看到的权益，现在就能领"},
    "seg_utm_edm": {"badge": "邮件专享", "subtitle": "感谢打开邮件，这份权益只给看完的人"},
    "seg_return": {"badge": "欢迎回来", "subtitle": "上次没完成的，这次一步就能搞定"},
    "seg_login": {"badge": "已登录", "subtitle": "你已登录，直接继续上次的进度"},
}


def segments(stages: tuple[str, ...] = ("paying", "churn_risk")) -> list[dict[str, Any]]:
    """项目级人群段：来源/回访/登录 + 指定阶段的共享链接人群。"""
    out = list(SOURCE_SEGMENTS)
    out += [s for s in STAGE_SEGMENTS if s["id"].replace("seg_stage_", "") in stages]
    return out


def personalize_rules(stage: str) -> list[dict[str, Any]]:
    """运行时可应用的文案补丁（按顺序首个命中生效）。"""
    rules: list[dict[str, Any]] = []
    for seg_id, patch in SOURCE_PATCH.items():
        rules.append({"segmentId": seg_id, "patch": patch})
    if stage in ("paying", "churn_risk", "churned"):
        rules.append({"segmentId": f"seg_stage_{stage}", "patch": {"badge": "为你准备"}})
    return rules


def hero_pair(title: str, subtitle: str, cta_text: str, cta_link: str, stage: str) -> list[dict[str, Any]]:
    """双版主视觉：移动端与桌面端互斥（SSR audience），各自带运行时补丁。"""
    rules = personalize_rules(stage)
    return [
        {"id": "b_hero_desktop", "type": "hero", "variant": "center",
         "audience": {"device": "desktop"},
         "personalize": rules,
         "props": {"badge": "", "title": title, "subtitle": subtitle,
                   "btnText": cta_text, "btnLink": cta_link,
                   "bgType": "gradient", "gradient": "indigo", "align": "center"}},
        {"id": "b_hero_mobile", "type": "hero", "variant": "center",
         "audience": {"device": "mobile"},
         "personalize": rules,
         "props": {"badge": "", "title": title, "subtitle": subtitle,
                   "btnText": cta_text, "btnLink": cta_link,
                   "bgType": "gradient", "gradient": "aqua", "align": "center"}},
    ]


def cta_block(title: str, cta_text: str, cta_link: str, goal_id: str, stage: str) -> dict[str, Any]:
    return {"id": "b_cta", "type": "cta", "variant": "panel",
            "personalize": personalize_rules(stage),
            "props": {"title": title, "subtitle": "", "btnText": cta_text, "btnLink": cta_link,
                      "style": "gradient", "goalId": goal_id}}


def countdown_block(minutes: int = 30, title: str = "权益领取倒计时") -> dict[str, Any]:
    """紧迫感（互动类区块）——覆盖"限时"心智，与停留时长配合。"""
    return {"id": "b_countdown", "type": "countdown", "props": {
        "title": title, "minutes": str(minutes), "subtitle": "倒计时结束前完成即可锁定权益"}}


# ── 内置页（自持）的千人千面决策 ──

def builtin_content(request: Any, stage: str, cfg: dict, base: dict[str, Any]) -> dict[str, Any]:
    """内置页按 设备/UTM/来源/Cookie/阶段 决定内容，并给出互动时长策略。"""
    ua = (request.headers.get("user-agent") or "").lower() if request else ""
    mobile = any(k in ua for k in ("mobi", "android", "iphone", "ipad", "ipod"))
    q = request.query_params if request else {}
    utm_source = (q.get("utm_source") or "").lower()
    utm_medium = (q.get("utm_medium") or "").lower()
    utm_content = (q.get("utm_content") or "").lower()
    cookies = request.headers.get("cookie") or "" if request else ""
    seen_before = "ul_seen=" in cookies

    out = dict(base)
    badge, subtitle = "", out.get("subtitle", "")
    if utm_source == "wechat":
        badge, subtitle = "微信专享", "微信用户专属通道，一步到位"
    elif utm_medium == "cpc":
        badge, subtitle = "限时活动", "你在广告里看到的权益，现在就能领"
    elif utm_medium == "email":
        badge, subtitle = "邮件专享", "感谢打开邮件，这份权益只给看完的人"
    elif seen_before:
        badge, subtitle = "欢迎回来", "上次没完成的，这次一步就能搞定"
    elif utm_content.startswith("stage-"):
        badge, subtitle = "为你准备", f"当前阶段专属：{utm_content.replace('stage-', '')}"

    dwell_cfg = (cfg.get("touch") or {}).get("personalize") or {}
    out.update({
        "badge": badge,
        "subtitle": subtitle,
        "device": "mobile" if mobile else "desktop",
        "stage": stage,
        "cta_style": "block" if mobile else "inline",
        # 互动时长：N 秒后把 CTA 换成更强行动（内置页由 JS 执行）
        "dwell": {
            "seconds": int(dwell_cfg.get("dwell_seconds", 20)),
            "alt_cta_text": dwell_cfg.get("dwell_cta_text") or "现在立即领取",
            "alt_note": dwell_cfg.get("dwell_note") or "别走开，权益还在这里等你",
        },
    })
    return out

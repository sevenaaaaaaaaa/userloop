"""触点调度：把 `touch.<channel>` 动作交给驱动注册表执行（统一结果结构）.

执行顺序（每一步都可解释、可拦下）：
  1. 解析 channel（`touch.auto` → Next Best Channel 按意图选渠道）
  2. **跨渠道全局频控门**（template Loop 与 AI 决策共用）
  3. A/B 版式实验（稳定分桶 → 覆盖内容槽位）
  4. **发前质检门**（block 拦下 / warn 放行）
  5. 驱动交付（失败降级）
"""

from __future__ import annotations

from typing import Any

from userloop.touch import drivers as _drivers  # noqa: F401  （导入即注册）
from userloop.touch.base import TouchSpec, get


async def dispatch(ctx: Any, action: dict, loop: dict, user: dict, store: Any = None) -> dict[str, Any]:
    """action.type = touch.<channel>；payload 承载内容槽位与追踪参数。"""
    atype = str(action.get("type", ""))
    channel = atype.split(".", 1)[1] if "." in atype else "email"
    payload = action.get("payload") or {}
    while isinstance(payload, str):
        try:
            import json

            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}
    if store is not None and not hasattr(ctx, "store"):
        ctx.store = store  # 驱动与选择器按身份挑渠道

    # 1) Next Best Channel：touch.auto 按意图自动挑渠道（可解释）
    auto_info: dict[str, Any] | None = None
    if channel == "auto":
        from userloop.touch import select as sel

        identities = await store.of_user_identities(user["id"]) if store is not None else {}
        auto_info = await sel.score(store, user, identities, ctx, intent=str(payload.get("intent") or "default"))
        if not auto_info.get("channel"):
            return {"type": atype, "channel": "auto", "ok": False,
                    "note": auto_info.get("rationale"), "selector": auto_info}
        channel = auto_info["channel"]

    driver = get(channel)
    if driver is None:
        from userloop.touch.base import _REGISTRY

        return {"type": atype, "channel": channel, "ok": False,
                "error": f"未注册的触点渠道：{channel}（可用：{', '.join(_REGISTRY)}）"}

    spec = TouchSpec.from_payload(payload, channel, loop,
                                  template_id=str(payload.get("template_id") or loop.get("template_id") or ""))

    # 2) 跨渠道全局频控门
    from userloop.touch import frequency as freq_mod

    gate = await freq_mod.check(store, ctx, user, channel, template_id=spec.template_id,
                                force=bool(payload.get("force")))
    if not gate["ok"]:
        return {"type": atype, "channel": channel, "ok": False, "blocked_by_frequency": True,
                "note": gate["reason"], "frequency": gate.get("counts") or {},
                **({"selector": auto_info} if auto_info else {})}

    # 3) A/B 版式实验：稳定分桶 → 覆盖内容槽位（已 promote 则只发 winner）
    ab: dict[str, Any] | None = None
    if store is not None:
        try:
            from userloop.experiments import engine as ab_engine

            picked = await ab_engine.pick(store, channel, spec.template_id, user["id"], spec.loop_id)
            if picked:
                exp, variant = picked
                variant = {**variant, "_experiment": exp["id"]}
                spec = ab_engine.apply_variant(spec, variant)
                spec.loop_id = spec.loop_id or loop.get("id")
                ab = {"experiment": exp["id"], "variant": variant["id"], "variant_name": variant.get("name", "")}
        except Exception:  # noqa: BLE001 —— 实验异常不得影响正常交付
            ab = None

    # 4) 发前质检门（保护终端用户体验与发件声誉）
    last_qc: dict = {}
    qc_cfg = (ctx.config.get("touch") or {}).get("qc") or {}
    if qc_cfg.get("enabled", True):
        from userloop.touch import qc as qc_mod

        if channel == "email":
            from userloop.touch.render import render_email

            rendered = render_email(spec, user, ctx.config)
            issues = qc_mod.check_email(rendered["subject"], rendered["html"])
        else:
            issues = qc_mod.check_h5(spec.title, spec.body, spec.cta_text, spec.cta_url)
        verdict = qc_mod.summarize(issues)
        if not verdict["ok"] and qc_cfg.get("mode", "block") == "block":
            return {"type": atype, "channel": channel, "ok": False, "blocked_by_qc": True,
                    "spec": {"title": spec.title, "cta": spec.cta_text},
                    "note": "发前质检未通过：" + "；".join(i["message"] for i in verdict["blocking"][:3]),
                    "qc": verdict, **({"selector": auto_info} if auto_info else {})}
        last_qc = verdict

    # 5) 交付
    result = await driver.deliver(spec, user, ctx)
    out = {"type": atype, "channel": channel, "spec": {"title": spec.title, "cta": spec.cta_text}}
    if auto_info:
        out["selector"] = {"chosen": auto_info["channel"], "rationale": auto_info["rationale"],
                           "scores": auto_info.get("scores")}
    if ab:
        out["ab"] = ab
    if last_qc.get("warnings"):
        out["qc"] = {"warnings": last_qc["warnings"]}
    out.update(result.as_dict())
    return out

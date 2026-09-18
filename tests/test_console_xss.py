"""控制台前端输出转义防回归：防止把用户/事件数据未经转义塞进 innerHTML.

不渲染浏览器，仅对静态资源做规则检查（新增渲染色点若忘记转义会在此拦截）：
- 不允许出现内联事件里用单引号包住模板插值（`onclick="fn('${...}')"`）→ 应改用 jsArg()
- 关键内联数据字段应经 esc() 包裹
"""

from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parent.parent / "userloop" / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_no_raw_inline_handler_args() -> None:
    for name in ("index.html", "canvas.html"):
        src = _read(name)
        bad = re.findall(r"onclick=\"[^\"]*'\$\{", src)
        assert not bad, f"{name} 存在未安全编码的内联事件参数：{bad[:3]}"


def test_has_esc_helpers_and_usage() -> None:
    idx = _read("index.html")
    assert "const esc =" in idx and "const jsArg =" in idx
    # 关键外部数据点必须转义
    for needle in ("esc(x.reasoning", "esc(u.distinct_id)", "esc(e.event)",
                   "esc((x.title", "esc(p.name)", "esc(l.title)"):
        assert needle in idx, f"index.html 缺少转义：{needle}"
    canvas = _read("canvas.html")
    assert "const esc =" in canvas
    assert "esc(f.name||f.id)" in canvas and "esc(n.id)" in canvas


def test_no_unquoted_onclick_identifier_injection() -> None:
    # onclick="fn(${...})" 里若直接是字符串拼接而非 jsArg，容易被引号越界
    idx = _read("index.html")
    raw = re.findall(r"onclick=\"[a-zA-Z]+\(\$\{(?!jsArg\(|enc\(|Number\()", idx)
    assert not raw, f"index.html 内联事件缺少 jsArg 包装：{raw[:3]}"

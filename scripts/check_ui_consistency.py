"""前端静态一致性检查：JS 引用的元素 id 必须都存在于 HTML。

为什么需要它：这套 UI 是零构建的纯静态文件，删掉一个 HTML 元素而忘记
同步 JS 里的 ``$('id')``，会在运行时报
``Cannot read properties of null (reading 'addEventListener')``，
而且往往**整块功能一起挂掉**——一个绑不上的监听器会中断它所在的
wire 函数，后面的绑定全部不执行。构建期没有任何东西会拦住它。

真实案例：移除侧栏「上传文档」按钮时，``wireKnowledge`` 里那行
``$('uploadOpen').addEventListener(...)`` 如果不删，知识库弹窗
（打开、列表、上传、刷新）会因为这一个 null **全部失效**。
"""

from __future__ import annotations

import re
from pathlib import Path

# parents[1] = 仓库根（本文件在 scripts/ 下）
STATIC_DIR = Path(__file__).resolve().parents[1] / "apps" / "api-gateway" / "app" / "static"

# 匹配 $('someId')，只认字面量 id；变量形式的查找无法静态校验，这里不猜
_DOLLAR_ID = re.compile(r"\$\('([a-zA-Z][\w-]*)'\)")
_HTML_ID = re.compile(r'id="([^"]+)"')


def load_sources() -> tuple[str, str, str]:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    return js, html, css


def referenced_ids(js: str) -> set[str]:
    return set(_DOLLAR_ID.findall(js))


def defined_ids(html: str) -> set[str]:
    return set(_HTML_ID.findall(html))


def missing_ids(js: str, html: str) -> list[str]:
    return sorted(referenced_ids(js) - defined_ids(html))


def check_static_consistency() -> list[str]:
    """返回问题列表（空列表表示通过）。"""
    js, html, css = load_sources()
    problems: list[str] = []

    for element_id in missing_ids(js, html):
        problems.append(f"app.js 引用了 HTML 中不存在的元素 id: {element_id}")

    # CSS 里针对某个 id 的规则却找不到该元素：多半是元素被删了、样式忘了删
    for css_id in sorted(_css_id_selectors(css)):
        if css_id not in defined_ids(html):
            problems.append(f"styles.css 里有 #{css_id} 的规则，但 HTML 里没有该元素")

    return problems


# 颜色值的 6 种写法：#fff / #ffff / #ffffff / #ffffffff（外加带 alpha 的变体）。
# 必须把它们排除，否则会把 #fff、#d97706 这类色值当成 id 报出来——
# 一个**永远失败**的检查等于没有检查，很快就会被无视。
_HEX_COLOR_LENGTHS = {3, 4, 6, 8}


def _css_id_selectors(css: str) -> set[str]:
    """从 CSS 里提取 id 选择器，排除十六进制颜色值。

    判据：全由十六进制字符组成、且长度落在颜色长度的集合里 → 视为色值。
    代价是 ``#abc``（长度 3 且全 hex）这种 id 会被漏掉；实际项目里没有这种命名。
    """
    found: set[str] = set()
    for name in re.findall(r"#([a-zA-Z][\w-]*)", css):
        if len(name) in _HEX_COLOR_LENGTHS and all(c in "0123456789abcdefABCDEF" for c in name):
            continue
        found.add(name)
    return found


if __name__ == "__main__":
    issues = check_static_consistency()
    if issues:
        print("前端静态一致性检查未通过：")
        for issue in issues:
            print("  -", issue)
        raise SystemExit(1)
    print("前端静态一致性检查通过")
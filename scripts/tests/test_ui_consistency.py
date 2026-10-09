"""前端静态一致性检查的测试。

重点不是"当前代码能通过"，而是**每类问题都能被抓到**——
一个从不失败的检查等于没有检查。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_ui_consistency import (  # noqa: E402
    _css_id_selectors,
    defined_ids,
    missing_ids,
    referenced_ids,
)


def test_current_repo_passes() -> None:
    """当前仓库应当通过——否则说明有人删了元素没同步 JS，或反之。"""
    from check_ui_consistency import check_static_consistency

    assert check_static_consistency() == []


def test_detects_js_referencing_missing_element() -> None:
    """能抓到「JS 引用了 HTML 里不存在的 id」。

    这正是真实踩过的坑：移除侧栏「上传文档」按钮后，wireKnowledge 里
    $('uploadOpen').addEventListener(...) 若不删会抛 null，
    导致知识库弹窗的打开/列表/上传/刷新**全部失效**。
    """
    js = "var b = $('ghostElement'); b.addEventListener('click', f);"
    html = '<button id="realElement"></button>'
    assert missing_ids(js, html) == ["ghostElement"]


def test_no_false_positive_on_existing_element() -> None:
    js = "var b = $('realElement');"
    html = '<button id="realElement"></button>'
    assert missing_ids(js, html) == []


def test_ignores_non_literal_lookups() -> None:
    """变量形式的查找无法静态校验，不能瞎报。"""
    js = "var b = $(dynamicId);"
    assert referenced_ids(js) == set()


def test_hex_colors_are_not_treated_as_ids() -> None:
    """CSS 里的十六进制颜色值不能被当成 id，否则检查永远失败。

    #fff / #d97706 这类色值在 styles.css 里到处都是；若误判成 id 选择器，
    检查会报出一堆"元素不存在"，很快就被当成噪音无视。
    """
    css = ".a { color: #fff; background: #d97706; } #realId { color: #eef3ff; }"
    found = _css_id_selectors(css)
    assert "realId" in found
    assert "fff" not in found
    assert "d97706" not in found
    assert "eef3ff" not in found


def test_defined_ids_reads_html() -> None:
    html = '<div id="a"></div><span id="b"></span>'
    assert defined_ids(html) == {"a", "b"}
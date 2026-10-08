"""查询规划节点（plan）的行为测试。

不连模型：LLM 输出用假网关脚本化。重点测三条防线：
启发式闸门（单点问题不花调用）、坏输出解析回退、融合检索的合并正确性。
"""

from __future__ import annotations

import pytest
from app.graph.nodes.plan import looks_compound, parse_sub_queries

# ---------------- 启发式闸门 ----------------


@pytest.mark.parametrize(
    "query",
    [
        "外部培训费用四千元由谁审批？报销时限是多久？",  # 两个问号
        "员工离职时系统权限要在多久内回收？另外劳动合同材料留存多久？",  # "另外"
        "报销时限和审批流程分别是什么？",  # "分别"
    ],
)
def test_looks_compound_true(query: str) -> None:
    assert looks_compound(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "年假有多少天？",
        "系统口令多久更换一次",
        "一线城市的住宿标准是多少？",
    ],
)
def test_looks_compound_false(query: str) -> None:
    """单信息点问题必须被闸门拦下——它们不该多花一次 LLM 调用。"""
    assert looks_compound(query) is False


# ---------------- 解析回退 ----------------


def test_parse_plain_json_array() -> None:
    raw = '["外部培训费用四千元由谁审批？","外部培训费用的报销时限是多久？"]'
    assert parse_sub_queries(raw, "原始", 3) == [
        "外部培训费用四千元由谁审批？",
        "外部培训费用的报销时限是多久？",
    ]


def test_parse_json_wrapped_in_prose_and_fences() -> None:
    raw = '好的，拆分如下：\n```json\n["子问题一", "子问题二"]\n```\n以上。'
    assert parse_sub_queries(raw, "原始", 3) == ["子问题一", "子问题二"]


def test_parse_single_fact_returns_original_untouched() -> None:
    """模型对单点问题返回 [原查询] 时，透传不该被 strip 掉问号。"""
    raw = '["年假有多少天？"]'
    assert parse_sub_queries(raw, "年假有多少天？", 3) == ["年假有多少天？"]


@pytest.mark.parametrize(
    "raw",
    [
        "",  # 空
        "我觉得这个问题不需要拆分。",  # 没有数组
        "[1, 2, 3]",  # 非字符串数组
        "[]",  # 空数组
        '[["嵌套"]]',  # 嵌套
    ],
)
def test_parse_falls_back_to_original_on_bad_output(raw: str) -> None:
    assert parse_sub_queries(raw, "原始问题？", 3) == ["原始问题？"]


def test_parse_dedupes_and_caps() -> None:
    raw = json.dumps(
        ["同一问题", "同一问题", "另一问题", "第三问题", "第四问题"], ensure_ascii=False
    )
    result = parse_sub_queries(raw, "原始", 3)
    assert result == ["同一问题", "另一问题", "第三问题"]


def test_parse_empty_string_item_ignored() -> None:
    assert parse_sub_queries('["", "  ", "有效"]', "原始", 3) == ["有效"]


import json  # noqa: E402  放底部供上面用例使用

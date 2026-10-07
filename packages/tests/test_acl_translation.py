"""ACL 过滤的"最后一跳"：过滤契约 -> Milvus expr / OpenSearch bool。

为什么单独一个文件：
    compile_filters（身份 -> 过滤契约）已经有测试，但**执行点**在这里——
    真正决定"某条文档能不能被召回"的，是这两个把契约翻译成引擎查询的函数。
    翻译错了不会报错，只会静默多召回别人的文档，所以它们必须有测试。
    好在它们都是纯函数，不需要 Milvus / OpenSearch 实例即可验证。

测试即文档：每个用例的名字就是这条规则的一句话说明。
"""

from __future__ import annotations

import re

from packages.contracts import ACL
from packages.retrievers import compile_filters
from packages.retrievers.base import FilterDict
from packages.search.opensearch import OpenSearchStore
from packages.vectorstores.milvus import MilvusStore

FULL_ACL = ACL(tenant_id="t1", department_id="hr", owner="u1")


def _expr(acl: ACL | None, doc_ids: list[str] | None = None) -> str:
    return MilvusStore._compile_expr(compile_filters(acl, doc_ids))


def _should_block(filters: FilterDict | None) -> dict:
    """取出承载可见性 OR 的那个 bool 子句（第一个子句是 tenant 的 term，没有 bool 键）。"""
    clauses = OpenSearchStore._compile_filter(filters)
    return next(c["bool"] for c in clauses if "bool" in c and "should" in c["bool"])


def _or_count_outside_literals(expr: str) -> int:
    """只统计字符串字面量**之外**的 or 关键字。

    必须这样做：ACL 值被转义后仍然含有 or 这几个字符（例如 tenant_id = 'a" or b'），
    直接 count 会把字面量内容当成布尔运算符，测试就变成了在测字符串而不是测语义。
    """
    without_literals = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    return without_literals.count(" or ")


# ---------------- 可见性语义 ----------------


def test_public_and_internal_are_visible_within_tenant() -> None:
    expr = _expr(FULL_ACL)
    assert 'tenant_id == "t1"' in expr
    assert 'visibility == "public"' in expr
    assert 'visibility == "internal"' in expr


def test_tenant_constraint_applies_to_every_branch() -> None:
    """租户隔离是全局约束：public 文档也不跨租户。"""
    assert _expr(ACL(tenant_id="t1")).startswith('tenant_id == "t1" and')


def test_department_branch_requires_matching_department() -> None:
    assert '(visibility == "department" and department_id == "hr")' in _expr(FULL_ACL)


def test_private_branch_requires_matching_owner() -> None:
    assert '(visibility == "private" and owner == "u1")' in _expr(FULL_ACL)


def test_missing_owner_drops_private_branch_entirely() -> None:
    """owner 缺失时整条 private 分支消失（fail-closed），而不是放宽成"谁都能看"。"""
    assert "private" not in _expr(ACL(tenant_id="t1", department_id="hr"))


def test_department_defaults_instead_of_disappearing() -> None:
    """⚠️ 契约里 department_id 有默认值 "default"，所以 department 分支**始终存在**。

    这意味着：没有部门claim 的身份，会看到所有以 default 部门入库的 department 文档。
    实践中身份都带部门（来自 Keycloak），但这条行为必须显式记录——
    将来若把默认值改成空字符串，department 分支就会整体消失（可见性收窄）。
    """
    assert 'visibility == "department" and department_id == "default"' in _expr(ACL(tenant_id="t1"))


# ---------------- 布尔结构 ----------------


def test_visibility_clauses_are_or_ed_then_and_ed_with_tenant() -> None:
    body = _expr(FULL_ACL).split(" and ", 1)[1]
    assert body.startswith("(") and body.endswith(")"), "可见性分支必须整体括起来"
    assert _or_count_outside_literals(body) == 3, "四个可见性类别之间是 OR"


def test_opensearch_uses_should_with_minimum_should_match() -> None:
    """为什么必须显式写 minimum_should_match: 1：去掉它过滤会退化成 match-all。"""
    block = _should_block(compile_filters(FULL_ACL))
    assert block["minimum_should_match"] == 1
    assert len(block["should"]) == 4


def test_opensearch_uses_filter_not_must_to_avoid_affecting_score() -> None:
    """用 filter 不用 must：must 会参与 BM25 打分，污染相关性排序。"""
    for clause in _should_block(compile_filters(FULL_ACL))["should"]:
        assert "filter" in clause["bool"], "可见性子句必须用 filter（不打分）"
        assert "must" not in clause["bool"]


def test_department_clause_is_and_ed_inside_one_should() -> None:
    """同一条子句内的多个条件是 AND：visibility 与 department_id 必须同时满足。"""
    department = next(
        c["bool"]["filter"]
        for c in _should_block(compile_filters(FULL_ACL))["should"]
        if {"term": {"visibility": "department"}} in c["bool"]["filter"]
    )
    assert department == [{"term": {"visibility": "department"}}, {"term": {"department_id": "hr"}}]


def test_both_stores_agree_on_tenant_term() -> None:
    """两个 store 必须表达同一约束——它们是同一份契约的两种翻译。"""
    clauses = OpenSearchStore._compile_filter(compile_filters(FULL_ACL))
    assert {"term": {"tenant_id": "t1"}} in clauses


# ---------------- doc_ids 白名单 ----------------


def test_doc_ids_restriction_is_conjunctive_with_acl() -> None:
    # 断言用"包含"而不是 endswith：契约里还有生命周期排除子句排在 doc_ids 之后，
    # 用位置断言会让这条用例因**无关的顺序**变化而失败。
    expr = _expr(FULL_ACL, ["d_1", "d_2"])
    assert 'doc_id in ["d_1", "d_2"]' in expr
    assert " and " in expr, "doc_ids 是 AND 条件，不是替代 ACL"


def test_doc_ids_only_still_enforces_tenant() -> None:
    assert 'tenant_id == "t1"' in _expr(ACL(tenant_id="t1"), ["d_1"])


# --------------- 生命周期排除（must_not）：已废止文档不参与检索 ---------------


def test_compiled_contract_always_excludes_retired_documents() -> None:
    """编译器必须**无条件**带上"排除已废止"子句。

    放在编译器而不是各调用方，是因为它必须不可能被忘记：
    漏传一次，废止文档就会重新出现在答案里，而那是不会报错的静默错误。
    """
    filters = compile_filters(FULL_ACL)
    assert filters is not None
    assert filters["must_not"] == [{"lifecycle": "retired"}]


def test_milvus_excludes_retired() -> None:
    expr = MilvusStore._compile_expr(compile_filters(FULL_ACL))
    assert 'not (lifecycle == "retired")' in expr


def test_opensearch_excludes_retired() -> None:
    clauses = OpenSearchStore._compile_filter(compile_filters(FULL_ACL))
    assert {"bool": {"must_not": [{"term": {"lifecycle": "retired"}}]}} in clauses


def test_milvus_and_opensearch_agree_on_exclusion_shape() -> None:
    """两个引擎对同一契约的排除语义必须一致——不一致就等于有一侧放行了废止文档。"""
    expr = MilvusStore._compile_expr(compile_filters(FULL_ACL))
    clauses = OpenSearchStore._compile_filter(compile_filters(FULL_ACL))
    assert ('not (lifecycle == "retired")' in expr) == (
        any(
            c.get("bool", {}).get("must_not") == [{"term": {"lifecycle": "retired"}}]
            for c in clauses
        )
    )


def test_empty_must_not_clause_is_ignored() -> None:
    """空 clause 必须被跳过：Milvus 侧会拼出非法的 `not ()`，直接报错。"""
    filters: FilterDict = {"must": {"tenant_id": "t1"}, "must_not": [{}]}
    assert "not ()" not in MilvusStore._compile_expr(filters)
    assert MilvusStore._compile_expr(filters) == 'tenant_id == "t1"'
    assert OpenSearchStore._compile_filter(filters) == [{"term": {"tenant_id": "t1"}}]


# ---------------- 空契约 = match-all（危险行为，必须钉住） ----------------


def test_empty_contract_translates_to_match_all_in_both_stores() -> None:
    """acl=None 是"放弃全部过滤"：两个引擎都解释成不过滤，即全库召回。

    这不是 bug 而是刻意保留（内部调试用），但必须显式钉住——
    一旦有人误传 acl=None，这里不会报错，只会静默越权。
    """
    assert MilvusStore._compile_expr(None) == ""
    assert OpenSearchStore._compile_filter(None) == []


def test_doc_ids_only_contract_has_no_tenant_constraint() -> None:
    """只给 doc_ids 时同样没有租户约束——调用方必须清楚这一点。"""
    filters = compile_filters(None, ["d_1"])
    assert filters == {"doc_ids": ["d_1"]}
    assert "tenant_id" not in MilvusStore._compile_expr(filters)


# ---------------- 注入防护 ----------------


def test_acl_values_are_escaped_into_one_literal() -> None:
    """身份值来自 JWT，不转义就能拼出恒真表达式绕过整个 ACL。

    断言的是"整个恶意值被转义成**单个**字面量"——如果引号没被转义，
    这里会出现两个独立的比较项，Milvus 就会按or 语义放行全部租户。
    """
    expr = _expr(ACL(tenant_id='t1" or tenant_id != "x', department_id="hr"))
    assert 'tenant_id == "t1\\" or tenant_id != \\"x"' in expr


def test_escaped_value_does_not_add_extra_or_terms() -> None:
    """转义后不应凭空多出 OR 分支（否则就是注入成功）。"""
    clean = _expr(ACL(tenant_id="t1", department_id="hr"))
    malicious = _expr(ACL(tenant_id='t1" or tenant_id != "x', department_id="hr"))
    assert _or_count_outside_literals(clean) == _or_count_outside_literals(malicious)

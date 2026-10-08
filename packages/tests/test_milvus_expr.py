"""Milvus 过滤表达式编译：重点验证 owner 写入侧与查询侧的截断口径一致。

owner 在写入时按 128 字节截断（VARCHAR max_length），查询侧必须同口径，
否则长 owner 的 private 文档在 Milvus 侧检索不到（#5 修复点）。
"""

from __future__ import annotations

from packages.vectorstores.milvus import MilvusStore, _truncate_utf8


def _owner(n_bytes: int) -> str:
    """造一个约 n_bytes 字节的多字节字符串（中文每字 3 字节）。"""
    return "用" * (n_bytes // 3 + 1)


def test_long_owner_truncated_consistently_on_query() -> None:
    """查询侧对 private owner 必须按 128 字节截断，与写入侧同一函数、同一上限。"""
    long_owner = _owner(300)  # 远超 128 字节
    assert len(long_owner.encode("utf-8")) > 128

    filters = {
        "visibility_clauses": [{"visibility": "private", "owner": long_owner}],
        "must_not": [{"lifecycle": "retired"}],
    }
    expr = MilvusStore._compile_expr(filters)

    expected = _truncate_utf8(long_owner, 128)
    # 查询 expr 用的是截断后的 owner，而不是完整 owner
    assert expected in expr
    assert long_owner not in expr


def test_truncate_utf8_keeps_valid_utf8_boundary() -> None:
    """截断不能切出半个多字节字符：结果是合法 UTF-8 且恰好在字符边界。"""
    s = "中文测试" * 50  # 300 字节
    out = _truncate_utf8(s, 128)
    encoded = out.encode("utf-8")
    assert len(encoded) <= 128
    # 截掉最后一个字符后字节数应 < 128（证明截断点落在字符边界而非字节中间）
    assert len(out[:-1].encode("utf-8")) < 128


def test_empty_visibility_clauses_compiles_to_noop() -> None:
    """无可见性约束时表达式应为空串（不退化成 match-all 之外的脏产物）。"""
    assert MilvusStore._compile_expr(None) == ""
    assert MilvusStore._compile_expr({"visibility_clauses": []}) == ""

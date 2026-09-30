"""Milvus 集合层的纯函数单元测试（不依赖真实 Milvus 服务）。"""

import pytest

from app.retrieval.milvus_store import (
    FIELD_CONTENT,
    FIELD_DOC_ID,
    FIELD_ERROR_CODE,
    FIELD_SOURCE,
    FIELD_TITLE,
    build_filter_expr,
    build_index_params,
    build_schema,
    to_chunks,
)
from app.schemas.retrieval import DocSource, RetrievalFilters


# ---------------- build_filter_expr ----------------


def test_build_filter_expr_returns_empty_without_filters():
    assert build_filter_expr(None) == ""
    assert build_filter_expr(RetrievalFilters()) == ""


def test_build_filter_expr_single_condition():
    expr = build_filter_expr(RetrievalFilters(source=DocSource.MANUAL))
    assert expr == f'{FIELD_SOURCE} == "manual"'


def test_build_filter_expr_combines_conditions():
    expr = build_filter_expr(RetrievalFilters(source=DocSource.FAQ, error_code="ERR-4041"))
    assert FIELD_SOURCE in expr
    assert "ERR-4041" in expr
    assert " and " in expr


def test_build_filter_expr_ignores_empty_error_code():
    expr = build_filter_expr(RetrievalFilters(source=DocSource.FAQ, error_code=""))
    assert FIELD_ERROR_CODE not in expr


# ---------------- to_chunks ----------------


def _hit(chunk_id: str, distance: float, **entity_extra):
    entity = {
        FIELD_DOC_ID: "doc-1",
        FIELD_CONTENT: "内容",
        FIELD_SOURCE: DocSource.MANUAL.value,
        FIELD_TITLE: "标题",
        **entity_extra,
    }
    return {"id": chunk_id, "distance": distance, "entity": entity}


def test_to_chunks_maps_fields_and_rank():
    results = [[_hit("doc-1#0", 0.9), _hit("doc-1#1", 0.8)]]

    chunks = to_chunks(results, score_field="dense_score")

    assert len(chunks) == 2
    assert chunks[0].chunk_id == "doc-1#0"
    assert chunks[0].dense_score == 0.9
    assert [c.rank for c in chunks] == [1, 2]


def test_to_chunks_writes_sparse_score_field():
    chunks = to_chunks([[_hit("doc-1#0", 1.234)]], score_field="sparse_score")

    assert chunks[0].sparse_score == 1.234
    assert chunks[0].dense_score is None


def test_to_chunks_handles_empty_results():
    assert to_chunks([]) == []
    assert to_chunks([[]]) == []


def test_to_chunks_normalises_missing_optional_fields():
    chunks = to_chunks([[{"id": "x#0", "distance": 0.5, "entity": {}}]])

    assert chunks[0].chunk_id == "x#0"  # 回退到 hit["id"]
    assert chunks[0].title is None
    assert chunks[0].error_code is None


# ---------------- schema / index ----------------


def test_schema_contains_required_fields():
    schema = build_schema()
    names = {field.name for field in schema.fields}

    assert {"chunk_id", "doc_id", "content", "dense", "sparse", "source"} <= names
    # BM25 Function 应把 content 映射到 sparse
    assert len(schema.functions) == 1
    bm25 = schema.functions[0]
    assert bm25.input_field_names == ["content"]
    assert bm25.output_field_names == ["sparse"]


def test_dense_field_uses_configured_dimension():
    from app.core.config import get_settings

    schema = build_schema()
    dense = next(field for field in schema.fields if field.name == "dense")

    assert dense.params["dim"] == str(get_settings().embedding_dim) or dense.params["dim"] == get_settings().embedding_dim


def test_index_params_declare_hnsw_and_bm25():
    # prepare_index_params() 需要客户端实例，无 Milvus 时跳过
    try:
        params = build_index_params()
    except Exception:  # noqa: BLE001 - 无 Milvus 时跳过
        pytest.skip("需要可用的 Milvus 连接")

    indexes = {item.to_dict()["field_name"]: item.to_dict() for item in params}

    assert indexes["dense"]["index_type"] == "HNSW"
    assert indexes["dense"]["metric_type"] == "COSINE"
    assert indexes["sparse"]["index_type"] == "SPARSE_INVERTED_INDEX"
    assert indexes["sparse"]["metric_type"] == "BM25"
    assert indexes["sparse"]["bm25_k1"] == 1.2
    assert indexes["sparse"]["bm25_b"] == 0.75

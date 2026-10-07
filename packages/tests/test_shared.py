"""共享库测试：契约、过滤编译、RRF、提示注册表、PII、ID 稳定性。"""

from __future__ import annotations

import pytest

from packages.common.ids import content_hash, stable_chunk_id, stable_doc_id
from packages.common.settings import BaseAppSettings
from packages.contracts import ACL, Chunk, SearchHit, Visibility
from packages.prompts import PromptRegistry
from packages.prompts.registry import get_prompt_registry
from packages.retrievers import compile_filters, reciprocal_rank_fusion
from packages.security.pii import contains_pii, redact

# ---------------- ID ----------------


def test_doc_id_is_stable_and_source_derived() -> None:
    a = stable_doc_id("data/corpus/employee_handbook.md")
    b = stable_doc_id("data/corpus/employee_handbook.md")
    c = stable_doc_id("data/corpus/other.md")
    assert a == b, "同一来源必须得到同一 doc_id，否则重跑索引会产生重复文档"
    assert a != c
    assert a.startswith("d_")


def test_chunk_id_composition() -> None:
    assert stable_chunk_id("d_abc", 7) == "d_abc:7"


def test_content_hash_changes_with_content() -> None:
    assert content_hash("甲") != content_hash("乙")


# ---------------- 过滤编译 ----------------


def test_compile_filters_full_visibility_matrix() -> None:
    filters = compile_filters(ACL(tenant_id="t1", department_id="hr", owner="u_1"))
    assert filters is not None
    assert filters["must"] == {"tenant_id": "t1"}
    clauses = filters["visibility_clauses"]
    assert {"visibility": "public"} in clauses
    assert {"visibility": "internal"} in clauses
    assert {"visibility": "department", "department_id": "hr"} in clauses
    assert {"visibility": "private", "owner": "u_1"} in clauses


def test_compile_filters_without_owner_omits_private() -> None:
    filters = compile_filters(ACL(tenant_id="t1", department_id="hr"))
    assert filters is not None
    assert all(c.get("visibility") != "private" for c in filters["visibility_clauses"])


def test_compile_filters_none_acl_is_opt_out() -> None:
    assert compile_filters(None) is None
    assert compile_filters(None, ["d_1"]) == {"doc_ids": ["d_1"]}


# ---------------- RRF ----------------


def _hit(chunk_id: str, score: float = 0.0, text: str = "内容") -> SearchHit:
    return SearchHit(chunk_id=chunk_id, doc_id="d_1", text=text, score=score)


def test_rrf_rewards_agreement_across_routes() -> None:
    vector = [_hit("d_1:0"), _hit("d_1:1")]
    bm25 = [_hit("d_1:1"), _hit("d_1:0")]
    fused = reciprocal_rank_fusion([("vector", vector), ("bm25", bm25)])
    # 两路都在前列的文档应当被顶上来，且分数相同
    assert {h.chunk_id for h in fused} == {"d_1:0", "d_1:1"}
    assert abs(fused[0].score - fused[1].score) < 1e-9


def test_rrf_weights_shift_priority() -> None:
    vector = [_hit("d_1:0"), _hit("d_1:1")]
    bm25 = [_hit("d_1:1"), _hit("d_1:0")]
    fused = reciprocal_rank_fusion(
        [("vector", vector), ("bm25", bm25)], weights={"vector": 10.0, "bm25": 0.1}
    )
    assert fused[0].chunk_id == "d_1:0"


def test_rrf_keeps_best_populated_hit() -> None:
    empty = _hit("d_1:0", text="")
    populated = _hit("d_1:0", text="真实正文")
    fused = reciprocal_rank_fusion([("vector", [empty]), ("bm25", [populated])])
    assert fused[0].text == "真实正文"
    assert fused[0].retriever == "vector+bm25"


def test_rrf_top_k_truncation() -> None:
    hits = [_hit(f"d_1:{i}") for i in range(10)]
    fused = reciprocal_rank_fusion([("vector", hits)], top_k=3)
    assert len(fused) == 3


# ---------------- 提示注册表 ----------------


def test_prompt_registry_reads_versioned_template() -> None:
    registry = get_prompt_registry()
    template = registry.get("rag_answer", "v1")
    assert "{{context}}" in template
    assert {"refuse_text", "context", "query"} <= registry.variables("rag_answer", "v1")


def test_prompt_render_rejects_missing_variable() -> None:
    registry = get_prompt_registry()
    with pytest.raises(Exception):  # ConfigError
        registry.render("rag_answer", "v1", context="c", query="q")


def test_prompt_render_substitutes_all_placeholders() -> None:
    registry = get_prompt_registry()
    rendered = registry.render("rag_answer", "v1", refuse_text="不知道", context="上下文", query="问题")
    assert "{{" not in rendered
    assert "上下文" in rendered and "问题" in rendered and "不知道" in rendered


def test_prompt_registry_missing_template_raises(tmp_path) -> None:
    registry = PromptRegistry(tmp_path)
    with pytest.raises(Exception):
        registry.get("nope", "v9")


# ---------------- 契约 ----------------


def test_chunk_round_trip_keeps_offsets() -> None:
    chunk = Chunk(
        chunk_id="d_1:0",
        doc_id="d_1",
        text="转正后凭发票报销。",
        chunk_index=0,
        char_start=10,
        char_end=20,
        acl=ACL(tenant_id="t1"),
    )
    restored = Chunk.model_validate(chunk.model_dump(mode="json"))
    assert restored.char_start == 10 and restored.char_end == 20
    assert restored.acl.visibility is Visibility.internal


def test_settings_env_prefix_free_contract() -> None:
    """字段名即环境变量名（大小写不敏感），这是 .env.example 的契约。"""
    settings = BaseAppSettings(LOG_LEVEL="DEBUG")
    assert settings.log_level == "DEBUG"


# ---------------- PII ----------------


def test_redact_masks_email_and_phone() -> None:
    text = "联系 alice@example.com 或 13800138000"
    masked = redact(text)
    assert "alice@example.com" not in masked
    assert "13800138000" not in masked
    assert "[EMAIL:" in masked and "[PHONE:" in masked


def test_contains_pii_detects_id_card() -> None:
    assert contains_pii("身份证 11010119900307561X")
    assert not contains_pii("这段文本没有敏感信息")

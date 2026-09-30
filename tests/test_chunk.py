"""文档切分与元数据抽取的单元测试。"""

import pytest

from ingestion.chunk import (
    RawChunk,
    dedupe_chunks,
    estimate_tokens,
    extract_metadata,
    split_document,
)


def _paragraph(i: int) -> str:
    """构造约 20 个 token 的段落，带唯一标记 P{i}。"""
    return f"第{i}段说明文字 P{i}，用于验证切分与重叠窗口是否按预期生效。"


# ---------------- estimate_tokens ----------------


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_counts_cjk_and_words():
    # 4 个汉字 + 1 个英文词
    assert estimate_tokens("测试文本 abc") >= 5
    # 中文应比同长度空白多
    assert estimate_tokens("中文内容") > estimate_tokens("    ")


# ---------------- extract_metadata ----------------


def test_extract_metadata_version_and_error_code():
    meta = extract_metadata("升级到 v1.2.3 后出现 ERR-4041 报错")
    assert meta["version"] == "v1.2.3"
    assert meta["error_code"] == "ERR-4041"


def test_extract_metadata_without_matches():
    meta = extract_metadata("这是一段普通说明文字")
    assert meta["version"] is None
    assert meta["error_code"] is None


# ---------------- split_document ----------------


def test_split_document_empty_input():
    assert split_document("doc1", "") == []
    assert split_document("doc1", "   \n  ") == []


def test_split_document_rejects_bad_overlap():
    with pytest.raises(ValueError):
        split_document("doc1", "内容", chunk_size=50, overlap=50)
    with pytest.raises(ValueError):
        split_document("doc1", "内容", chunk_size=50, overlap=-1)


def test_split_document_respects_token_budget():
    text = "\n\n".join(_paragraph(i) for i in range(12))
    chunks = split_document("doc1", text, chunk_size=60, overlap=20)

    assert len(chunks) > 1
    for chunk in chunks:
        # 上限为 chunk_size + overlap（重叠为软约束）
        assert estimate_tokens(chunk.content) <= 60 + 20


def test_split_document_produces_overlap_between_chunks():
    text = "\n\n".join(_paragraph(i) for i in range(12))
    chunks = split_document("doc1", text, chunk_size=60, overlap=30)

    assert len(chunks) >= 2
    shared_found = False
    for prev, cur in zip(chunks, chunks[1:]):
        for i in range(12):
            marker = f"P{i}"
            if marker in prev.content and marker in cur.content:
                shared_found = True
                break
        if shared_found:
            break
    assert shared_found, "相邻 chunk 之间应存在重叠上下文"


def test_split_document_splits_long_single_paragraph():
    long_text = "这是一段很长的说明。" * 40  # 无空行，单段落
    chunks = split_document("doc1", long_text, chunk_size=80, overlap=10)

    assert len(chunks) > 1
    for chunk in chunks:
        assert estimate_tokens(chunk.content) <= 80 + 10


def test_split_document_captures_heading_as_title():
    text = "# 登录模块\n\n登录失败请检查账号密码。\n\n## 错误码\n\nERR-4041 表示令牌过期。"
    chunks = split_document("manual-01", text, chunk_size=60, overlap=10)

    titles = {c.title for c in chunks}
    assert "登录模块" in titles
    assert "错误码" in titles


def test_split_document_merges_heading_only_block():
    """纯标题块不应单独成 chunk，而应与后续正文合并。"""
    text = "# 产品手册\n\n## 登录模块\n\n登录失败请检查账号密码。"
    chunks = split_document("manual-01", text, chunk_size=100, overlap=10)

    assert len(chunks) == 1
    assert "# 产品手册" in chunks[0].content
    assert "登录失败请检查账号密码。" in chunks[0].content


def test_split_document_uses_fallback_title():
    text = "没有标题的普通段落内容。"
    chunks = split_document("faq-01", text, title="FAQ 合集")

    assert len(chunks) == 1
    assert chunks[0].title == "FAQ 合集"


def test_split_document_extracts_per_chunk_metadata():
    text = (
        "# 排障\n\n"
        "升级到 v1.2.3 后出现 ERR-4041 报错，请按以下步骤处理。\n\n"
        "其他无关段落，不含版本与错误码信息。"
    )
    chunks = split_document("manual-02", text, chunk_size=60, overlap=10)

    hit = next(c for c in chunks if "ERR-4041" in c.content)
    assert hit.version == "v1.2.3"
    assert hit.error_code == "ERR-4041"


def test_chunk_id_format_is_stable():
    chunks = split_document("manual-01", "\n\n".join(_paragraph(i) for i in range(8)), chunk_size=60, overlap=10)

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].chunk_id == "manual-01#0"


# ---------------- dedupe_chunks ----------------


def test_dedupe_chunks_removes_duplicated_content():
    same = _paragraph(1)
    chunks = [
        RawChunk(doc_id="d1", chunk_index=0, content=same),
        RawChunk(doc_id="d2", chunk_index=0, content=same),  # 内容相同（空白差异也算重复）
        RawChunk(doc_id="d1", chunk_index=1, content=_paragraph(2)),
    ]

    result = dedupe_chunks(chunks)

    assert len(result) == 2
    assert result[0].content == same


def test_dedupe_chunks_ignores_whitespace_differences():
    a = RawChunk(doc_id="d1", chunk_index=0, content="内容   相同")
    b = RawChunk(doc_id="d1", chunk_index=1, content="内容\n相同")

    assert len(dedupe_chunks([a, b])) == 1

"""切分器测试：偏移必须与原文逐字对齐（引用可定位的硬证据）。"""

from __future__ import annotations

from app.chunkers import ChunkingConfig, RecursiveChunker

from packages.common.ids import content_hash, stable_doc_id
from packages.contracts import ACL, Document

SAMPLE = """# 员工手册

## 第三章 福利

### 3.2 入职体检

入职体检费用由员工先行垫付。员工转正后，凭体检机构开具的正式发票，可通过报销系统提交入职体检费用报销申请，报销上限为五百元。

发票抬头须为公司全称，报销需在转正后三十日内提交，逾期不再受理。

## 第四章 培训与发展

### 4.1 内部培训

公司每季度组织一次技术分享与业务培训，员工应至少参加一次并在培训系统中完成签到。
"""


def _doc() -> Document:
    return Document(
        doc_id=stable_doc_id("handbook.md"),
        source="handbook.md",
        title="员工手册",
        content=SAMPLE,
        content_hash=content_hash(SAMPLE),
        acl=ACL(tenant_id="default", department_id="default"),
    )


def test_offsets_point_back_to_source_exactly() -> None:
    """核心断言：content[char_start:char_end] == text。"""
    chunks = RecursiveChunker(ChunkingConfig(chunk_size=120, chunk_overlap=20)).chunk(_doc())
    assert chunks, "应当切出至少一个 chunk"
    for chunk in chunks:
        assert SAMPLE[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.char_start < chunk.char_end


def test_chunk_index_is_sequential() -> None:
    chunks = RecursiveChunker(ChunkingConfig(chunk_size=120, chunk_overlap=20)).chunk(_doc())
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_section_path_carries_heading_trail() -> None:
    chunks = RecursiveChunker(ChunkingConfig(chunk_size=200, chunk_overlap=0)).chunk(_doc())
    paths = {c.section_path for c in chunks}
    assert "员工手册 > 第三章 福利 > 3.2 入职体检" in paths
    assert "员工手册 > 第四章 培训与发展 > 4.1 内部培训" in paths


def test_chunk_size_is_respected() -> None:
    config = ChunkingConfig(chunk_size=100, chunk_overlap=10)
    chunks = RecursiveChunker(config).chunk(_doc())
    assert all(len(c.text) <= config.chunk_size for c in chunks)


def test_overlap_makes_neighbor_chunks_share_text() -> None:
    """重叠只在同一章节内生效：章节之间不做跨段重叠。"""
    body = "第一句话需要足够长以形成多个分块。" * 12
    doc = _doc().model_copy(update={"content": body, "title": "长文"})
    config = ChunkingConfig(chunk_size=100, chunk_overlap=30)
    chunks = RecursiveChunker(config).chunk(doc)
    assert len(chunks) >= 2
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.char_start < prev.char_end  # 后一块起点早于前一块终点 => 有重叠
        assert body[nxt.char_start : nxt.char_end] == nxt.text


def test_overlap_larger_than_chunk_size_is_clamped() -> None:
    """重叠 >= chunk_size 必须被夹紧，否则切分会死循环。"""
    config = ChunkingConfig(chunk_size=100, chunk_overlap=500)
    assert config.chunk_overlap < config.chunk_size
    chunks = RecursiveChunker(config).chunk(_doc())
    assert chunks


def test_empty_content_produces_no_chunks() -> None:
    doc = _doc().model_copy(update={"content": "   \n  "})
    assert RecursiveChunker().chunk(doc) == []

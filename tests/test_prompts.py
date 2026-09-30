"""上下文拼装（去重 + 显式预算 + 来源标注）的单元测试。"""

import logging
from unittest.mock import patch

from app.generation.prompts import build_context, build_fallback
from app.schemas.retrieval import Chunk, DocSource


def _chunk(chunk_id: str, content: str, **kwargs) -> Chunk:
    return Chunk(chunk_id=chunk_id, doc_id="doc", content=content, source=DocSource.MANUAL, **kwargs)


def test_build_context_annotates_source_and_boundaries():
    chunks = [_chunk("manual#0", "内容甲"), _chunk("manual#1", "内容乙")]

    context = build_context(chunks)

    assert "[来源: manual#0]" in context
    assert "[来源: manual#1]" in context
    assert "---" in context  # 明确的块边界
    assert context.index("manual#0") < context.index("manual#1")  # 保持相关性顺序


def test_build_context_skips_exact_duplicates():
    same = "同一段内容"
    chunks = [_chunk("a#0", same), _chunk("b#0", same), _chunk("c#0", "另一段")]

    context = build_context(chunks)

    assert context.count("同一段内容") == 1
    assert "另一段" in context


def test_build_context_skips_near_duplicates_by_prefix():
    """不同来源但开头高度一致（转载/重复收录）应被去掉。

    近重复判定看归一化后的前 120 字，因此测试前缀必须超过该长度。
    """
    prefix = "这是一段被多个来源重复收录的长文本" * 10
    chunks = [
        _chunk("a#0", prefix + "甲"),
        _chunk("b#0", prefix + "乙"),
    ]

    context = build_context(chunks)

    assert context.count("[来源:") == 1  # 只保留一份


def test_build_context_respects_char_budget_and_logs():
    # 每段内容必须互不相同，否则会先被去重（那是另一个测试的事）
    chunks = [_chunk(f"d#{i}", f"这是第{i}号片段的内容。" * 20) for i in range(10)]
    module_logger = logging.getLogger("app.generation.prompts")

    with patch.object(module_logger, "warning") as mocked_warning:
        context = build_context(chunks, max_chars=300)

    assert len(context) <= 400  # 允许少量分隔符开销
    assert context.count("[来源:") < len(chunks)  # 确实发生了截断
    mocked_warning.assert_called_once()  # 显式截断必须留痕
    assert "上下文预算" in mocked_warning.call_args.args[0]


def test_build_context_returns_empty_for_no_chunks():
    assert build_context([]) == ""


def test_build_fallback_lists_excerpts():
    chunks = [_chunk("manual#0", "片段原文")]

    text = build_fallback("问题A", chunks)

    assert "生成服务暂时不可用" in text
    assert "片段原文" in text
    assert "问题A" in text

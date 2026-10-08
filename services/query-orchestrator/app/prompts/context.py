"""上下文构造：把检索结果转成带编号的上下文。

这里的编号是**全链路引用编号的唯一来源**：
    contexts[i].index = i + 1  ->  送进 prompt 的顺序  ->  citations.index
三处共用同一列表，才不会有「引用张冠李戴」。
"""

from __future__ import annotations

from packages.contracts import ContextItem, SearchHit


def build_context_items(hits: list[SearchHit]) -> list[ContextItem]:
    """把检索结果转为带编号的 ``ContextItem`` 列表。

    编号 ``index`` 从 1 递增，即后续送进 prompt 的顺序，也是 ``citations.index`` 的唯一来源
    （与 guard 节点派生引用共用同一列表，避免张冠李戴）。
    """
    return [
        ContextItem(
            index=index,
            chunk_id=hit.chunk_id,
            doc_id=hit.doc_id,
            doc_title=hit.doc_title,
            section_path=hit.section_path,
            text=hit.text,
        )
        for index, hit in enumerate(hits, start=1)
    ]

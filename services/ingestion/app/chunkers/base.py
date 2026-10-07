"""切分器抽象与切分配置。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from packages.contracts import Chunk, Document

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中日韩按字计，拉丁按词计。

    只用于观测（判断 chunk 是否过大），不参与任何计费逻辑，
    因此不做精确分词以免引入重量级依赖。
    """
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size: int = 500
    chunk_overlap: int = 80
    min_chunk_size: int = 40

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            raise ValueError("chunk_size 必须为正数")
        if self.chunk_overlap < 0:
            raise ValueError("chunk_overlap 不能为负")
        # 重叠不得达到 chunk_size，否则切分会原地打转
        if self.chunk_overlap >= self.chunk_size:
            object.__setattr__(self, "chunk_overlap", max(0, self.chunk_size // 2))


class BaseChunker(ABC):
    """输入 Document，输出带精确字符区间的 Chunk 列表。

    硬约束：``content[chunk.char_start:chunk.char_end] == chunk.text``，
    否则「引用可精确定位原文」不成立。
    """

    @abstractmethod
    def chunk(self, doc: Document) -> list[Chunk]: ...

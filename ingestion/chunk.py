"""文档切分与元数据抽取。

策略（对应 `开发流程/04-检索与编排设计.md` §2.3）：
- 按标题/段落优先切分，chunk ≈ 300~500 token，重叠 ~50 token；
- 正则抽取版本号（v1.2.3）与错误码（ERR-4041）写入标量元数据；
- 按 content 哈希去重。
"""

import re
from dataclasses import dataclass, field

#: 版本号，如 v1.2.3 / 1.2
VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){1,3}\b")
#: 错误码，如 ERR-4041 / E4041
ERROR_CODE_RE = re.compile(r"\b(?:ERR|E)[-_]?\d{3,5}\b", re.IGNORECASE)


@dataclass
class RawChunk:
    """切分后的原始片段。"""

    doc_id: str
    chunk_index: int
    content: str
    title: str | None = None
    version: str | None = None
    error_code: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def chunk_id(self) -> str:
        return f"{self.doc_id}#{self.chunk_index}"


def extract_metadata(text: str) -> dict:
    """从文本抽取版本号与错误码（取首个命中）。"""
    version = VERSION_RE.search(text)
    error_code = ERROR_CODE_RE.search(text)
    return {
        "version": version.group(0) if version else None,
        "error_code": error_code.group(0) if error_code else None,
    }


def split_document(doc_id: str, text: str, chunk_size: int = 400, overlap: int = 50) -> list[RawChunk]:
    """将文档切分为片段列表。"""
    # TODO(后续)：按标题/段落切分 + token 估算 + 重叠窗口
    raise NotImplementedError("骨架占位：split_document 将在后续编码阶段实现")

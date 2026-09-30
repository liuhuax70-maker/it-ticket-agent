"""检索相关数据模型。"""

from enum import Enum

from pydantic import BaseModel, Field


class DocSource(str, Enum):
    """知识来源类型。"""

    MANUAL = "manual"  # 产品手册
    FAQ = "faq"  # 常见问题
    TICKET = "ticket"  # 历史工单


class RetrievalMode(str, Enum):
    """实际生效的检索模式（含降级）。"""

    HYBRID = "hybrid"
    DENSE_ONLY = "dense_only"
    SPARSE_ONLY = "sparse_only"
    DEGRADED = "degraded"


class Chunk(BaseModel):
    """知识库片段（检索结果单元）。"""

    chunk_id: str
    doc_id: str
    content: str
    source: DocSource
    title: str | None = None
    #: 标题层级路径（`一级 > 二级`），用于拼装上下文时标注来源
    heading_path: str | None = None
    #: 内容哈希，用于查询期去重
    content_hash: str | None = None
    version: str | None = None
    error_code: str | None = None
    dense_score: float | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    rank: int | None = None


class RetrievalFilters(BaseModel):
    """标量过滤条件。"""

    source: DocSource | None = None
    error_code: str | None = None


class SearchRequest(BaseModel):
    """POST /retrieval/search 请求体（调试接口）。"""

    query: str = Field(min_length=1)
    top_k: int | None = None
    top_n_dense: int | None = None
    top_n_sparse: int | None = None
    top_n_fused: int | None = None
    rerank_enabled: bool | None = None
    filters: RetrievalFilters | None = None


class SearchResponse(BaseModel):
    """检索调试结果。"""

    mode: RetrievalMode = RetrievalMode.HYBRID
    dense_hits: int = 0
    sparse_hits: int = 0
    fused: int = 0
    reranked: bool = False
    chunks: list[Chunk] = Field(default_factory=list)

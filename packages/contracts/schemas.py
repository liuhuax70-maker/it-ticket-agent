"""跨服务契约定义。

依赖方向：本模块只依赖 pydantic，不 import 任何服务或第三方客户端，
以便被 ingestion / indexing / retrieval / orchestrator / gateway 共同引用。

引用定位铁律：``Chunk`` 必须携带 ``char_start / char_end``，
``Citation`` 必须原样透传，否则前端「点击引用跳原文」无法实现。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# 权限 / 元数据
# --------------------------------------------------------------------------


class Visibility(StrEnum):
    """文档可见级别，供 ACL 过滤使用。"""

    public = "public"  # 全员可见
    internal = "internal"  # 租户内可见
    department = "department"  # 仅所属部门可见
    private = "private"  # 仅所有者可见


class ACL(BaseModel):
    """权限三元组 + 可见级别。

    最小闭环全部取默认值；P2 起由 ingestion 从数据源抽取真实值，
    retrieval 将本对象编译为 Milvus ``expr`` 与 OpenSearch ``filter``。
    """

    tenant_id: str = "default"
    department_id: str = "default"
    visibility: Visibility = Visibility.internal
    allowed_roles: list[str] = Field(default_factory=list)
    owner: str | None = None

    @classmethod
    def default(cls, **overrides: Any) -> ACL:
        return cls(**overrides)


# --------------------------------------------------------------------------
# 文档 / 分块
# --------------------------------------------------------------------------


class Document(BaseModel):
    """解析后的原始文档（切分前的统一形态）。"""

    doc_id: str
    source: str
    title: str
    content: str
    content_hash: str
    acl: ACL = Field(default_factory=ACL)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    """切分单元。``chunk_id = f"{doc_id}:{chunk_index}"``，可稳定复现。

    ⚠️ ``char_start`` / ``char_end`` 是**字符偏移，不是字节偏移**，且约定为
    左闭右开切片：``原文[char_start:char_end] == text``（切分器保证，见
    ``services/ingestion/tests/test_chunker.py`` 的断言）。
    前端引用高亮直接依赖这个不变量。

    混用口径会出事：向量库侧的 ``VARCHAR`` 长度上限是按**字节**算的
    （见 ``packages/vectorstores/config.py``），而这里是字符。
    """

    chunk_id: str
    doc_id: str
    text: str
    chunk_index: int
    char_start: int
    char_end: int
    section_path: str = ""
    doc_title: str = ""
    source: str = ""
    token_count: int = 0
    acl: ACL = Field(default_factory=ACL)


# --------------------------------------------------------------------------
# 引用
# --------------------------------------------------------------------------


class Citation(BaseModel):
    """答案引用。``index`` 对应送入 prompt 的 context 顺序（从 1 开始）。"""

    index: int
    chunk_id: str
    doc_id: str
    doc_title: str = ""
    chunk_index: int
    section_path: str = ""
    char_start: int
    char_end: int
    score: float | None = None
    snippet: str = ""


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------


class RetrieveMode(StrEnum):
    vector = "vector"
    keyword = "keyword"
    hybrid = "hybrid"


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = 5
    mode: RetrieveMode = RetrieveMode.hybrid
    acl: ACL | None = None
    # 额外元数据过滤，如 {"doc_ids": ["d_xxx"]}
    filters: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    chunk_index: int = 0
    char_start: int = 0
    char_end: int = 0
    section_path: str = ""
    doc_title: str = ""
    source: str = ""
    score: float = 0.0
    retriever: str = ""  # vector | bm25 | hybrid | rerank
    acl: ACL | None = None


class SearchResponse(BaseModel):
    hits: list[SearchHit] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)


class RerankRequest(BaseModel):
    query: str
    hits: list[SearchHit]
    top_k: int = 5


class RerankResponse(BaseModel):
    hits: list[SearchHit] = Field(default_factory=list)
    reranker: str = "rrf"
    timings_ms: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------


class ContextItem(BaseModel):
    """送入 LLM 的上下文单元，顺序即引用编号顺序。"""

    index: int
    chunk_id: str
    doc_id: str
    doc_title: str = ""
    section_path: str = ""
    text: str


class ChatMessage(BaseModel):
    """原始对话消息，用于只要求「按我给的 prompt 调一次模型」的场景
    （查询改写、合规审核），避免为了复用它们而把业务 prompt 塞进 RAG 模板。"""

    role: Literal["system", "user", "assistant"] = "user"
    content: str


class CompletionRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tenant_id: str | None = None


class GenerateRequest(BaseModel):
    query: str
    contexts: list[ContextItem] = Field(default_factory=list)
    system: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    model: str | None = None  # 留空则用网关默认模型
    tenant_id: str | None = None  # 用于配额统计
    trace_id: str | None = None


class GenerateResponse(BaseModel):
    answer: str
    model: str
    provider: str = "unknown"
    usage: dict[str, int] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    name: str
    provider: str
    kind: Literal["chat", "embedding"] = "chat"
    available: bool = True
    note: str | None = None


# --------------------------------------------------------------------------
# 对话（对外）
# --------------------------------------------------------------------------


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int | None = None
    mode: RetrieveMode | None = None
    conversation_id: str | None = None
    # 生成温度。留空则由 model-gateway 用自己的默认配置。
    # 评测会显式传 0：作答温度不为 0 时"该不该拒答"这类判断对采样极其敏感，
    # 同一份评测集连跑两次误答率能翻倍，指标就失去了可比性。
    temperature: float | None = None
    # 是否回传召回上下文原文。
    # 默认关闭有两个理由：① 体积——top_k 段正文可能几 KB，绝大多数调用方（聊天前端）
    #   只需要 answer + citations；② 最小化暴露——上下文是**检索到的原文**，在 ACL
    #   过滤之前的内容形态，回传范围越大越容易在下游被误记日志。
    # 需要它的场景：RAGAS 的 context 类指标（评测采集时显式传 True）。
    include_contexts: bool = False


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    refused: bool = False
    cached: bool = False
    model: str | None = None
    trace_id: str | None = None
    # 仅在 ChatRequest.include_contexts=true 时填充
    contexts: list[ContextItem] | None = None


# --------------------------------------------------------------------------
# 接入 / 入库 / 向量化
# --------------------------------------------------------------------------


class IngestRequest(BaseModel):
    """接入请求。三种来源择一：本地路径 / 目录 / 直接传内容。"""

    path: str | None = None
    content: str | None = None
    filename: str | None = None
    title: str | None = None
    acl: ACL | None = None
    reindex: bool = False
    # ⚠️ 保留字段但**当前没有任何读取点**（全仓无 req.sync 的使用）。
    # 同步/异步由 ingestion 的 sink 实现决定（USE_KAFKA 开关，见 producers.ChunkSink），
    # 不由请求参数决定。新代码不要依赖它；要控制同步/异步请用环境变量。
    sync: bool = True


class IngestResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "succeeded", "failed"]
    documents: int = 0
    chunk_count: int = 0
    indexed: int = 0
    doc_ids: list[str] = Field(default_factory=list)
    message: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    chunks: list[Chunk]
    reindex: bool = False


class IndexResponse(BaseModel):
    doc_id: str
    chunks_indexed: int
    milvus: int = 0
    opensearch: int = 0
    postgres: int = 0
    status: Literal["ok", "partial", "failed"] = "ok"
    timings_ms: dict[str, float] = Field(default_factory=dict)


class EmbedRequest(BaseModel):
    texts: list[str]
    kind: Literal["query", "document"] = "document"


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    dim: int
    model: str


# --------------------------------------------------------------------------
# 健康检查
# --------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "error"]
    service: str
    version: str = "0.1.0"
    details: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 反馈
# --------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """用户反馈。评级沿用点赞/点踩：1 有用，-1 无用，None 仅留言。"""

    query: str = ""
    answer: str = ""
    rating: int | None = None
    comment: str | None = None
    trace_id: str | None = None


class FeedbackResponse(BaseModel):
    id: str
    status: Literal["accepted", "duplicate"] = "accepted"

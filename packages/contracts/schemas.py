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

from packages.common.constants import LIFECYCLE_ACTIVE

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
        """便捷构造：``ACL.default(tenant_id="x")`` 等价于 ``ACL(tenant_id="x")``。"""
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
    # 生命周期（失效管理）：已废止的文档不参与检索，见 packages/retrievers/filters.py。
    # **默认 active** 是刻意的：存量/未声明的文档保持可见，
    # 这样新字段上线不需要数据迁移，也不会出现"上线即全库搜不到"。
    lifecycle: str = LIFECYCLE_ACTIVE
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
    """检索模式：纯向量 / 纯关键词(BM25) / 混合(二者 RRF 融合)。"""

    vector = "vector"
    keyword = "keyword"
    hybrid = "hybrid"


class SearchRequest(BaseModel):
    """发给 retrieval 服务的检索请求。

    ``acl`` 由编排层从身份注入（网关是唯一鉴权点），``filters`` 为额外元数据过滤
    如 ``{"doc_ids": [...]}``——检索服务会把 ``acl`` 编译为存储层过滤条件。
    """

    query: str = Field(min_length=1)
    top_k: int = 5
    mode: RetrieveMode = RetrieveMode.hybrid
    acl: ACL | None = None
    # 额外元数据过滤，如 {"doc_ids": ["d_xxx"]}
    filters: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    """单条召回结果：分块原文 + 相关度 + 来源定位信息。

    ``retriever`` 标记它来自哪路召回（vector/bm25/hybrid/rerank），
    便于排查「哪路召回贡献了这条结果」。
    """

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
    """检索响应：召回列表 + 各阶段耗时（毫秒）。"""

    hits: list[SearchHit] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)


class RerankRequest(BaseModel):
    """重排请求：对给定命中列表按query重排。"""

    query: str
    hits: list[SearchHit]
    top_k: int = 5


class RerankResponse(BaseModel):
    """重排响应。``reranker`` 如实回填实际使用的重排器（未启用时为 ``"rrf"``）。"""

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
    """裸补全请求（不走 RAG 检索，直接拿 messages 调模型）。

    用于改写/审核等内部子任务，与 ``GenerateRequest`` 的区别在于不带 contexts。
    """

    messages: list[ChatMessage]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tenant_id: str | None = None


class GenerateRequest(BaseModel):
    """RAG 生成请求：query + 已检索的 contexts，由 model-gateway 生成带引用答案。"""

    query: str
    contexts: list[ContextItem] = Field(default_factory=list)
    system: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    model: str | None = None  # 留空则用网关默认模型
    tenant_id: str | None = None  # 用于配额统计
    trace_id: str | None = None


class GenerateResponse(BaseModel):
    """生成响应：答案 + 实际模型/供应商 + token 用量 + 耗时。"""

    answer: str
    model: str
    provider: str = "unknown"
    usage: dict[str, int] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    """模型清单项：名称、供应商、类型与可用性（供网关/前端展示与选型）。"""

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
    # 是否允许读写查询缓存。默认 True（生产行为不变）。
    #
    # 评测必须传 False，理由不是"想看慢一点的数"，而是**缓存命中会改变可测量的东西**：
    # ① 命中响应里没有 contexts，检索侧指标（NDCG、检索侧 MRR）只能把这些行排除出分母——
    #    于是"缓存越多，NDCG 的样本越少"，两轮评测的 NDCG 不可比；
    # ② 延迟变成"命中/未命中"的混合值，P50 随缓存状态漂移（实测同代码 1.29s vs 2.13s）。
    # 另外评测跑批若允许写缓存，会把评测流量灌进生产缓存，让**下一次**评测拿到一堆命中。
    use_cache: bool = True
    # 指定生成模型（留空则由 model-gateway 用自己的默认模型）。
    #
    # 编排层会把它透传给 GenerateRequest.model，并**纳入查询缓存键**：
    # 过去模型由部署配置决定、编排层无从得知，所以只能靠 CACHE_VERSION 整体作废；
    # 现在模型可由调用方逐请求选择，若不入键，换模型后会继续命中别的模型的旧答案，
    # 直到 TTL 过期——症状是"明明选了新模型，答案还是老模型的口吻"。
    model: str | None = None


class ChatResponse(BaseModel):
    """对话响应（对外）。

    ``refused`` 表示检索为空/相关性不足而走拒答（此时 ``citations`` 必为空，
    ``cached`` 表示本响应是否来自查询缓存）。``contexts`` 仅当请求
    ``include_contexts=true`` 时填充。
    """

    answer: str
    citations: list[Citation] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    refused: bool = False
    # 检索为空时给出的通用回答（已声明来源不来自知识库），此时 refused=False。
    #
    # 为什么与 refused 分开：refused=「没答」，no_context=「答了但无资料支撑」。
    # 评测要能区分"正确拒答"与"按策略降级"，前端也要据此提示用户注意来源；
    # 合成一个布尔就丢掉这个信息。
    no_context: bool = False
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


class IngestResponse(BaseModel):
    """接入响应：本次接入的文档数、分块数、成功入库数与派生的 doc_id 列表。"""

    job_id: str
    status: Literal["pending", "running", "succeeded", "failed"]
    documents: int = 0
    chunk_count: int = 0
    indexed: int = 0
    doc_ids: list[str] = Field(default_factory=list)
    message: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    """入库请求：把一组已切分好的 Chunk 写入向量库 + 索引。"""

    chunks: list[Chunk]
    reindex: bool = False


class IndexResponse(BaseModel):
    """入库响应：各存储写入条数（milvus/opensearch/postgres）与整体状态。"""

    doc_id: str
    chunks_indexed: int
    milvus: int = 0
    opensearch: int = 0
    postgres: int = 0
    status: Literal["ok", "partial", "failed"] = "ok"
    timings_ms: dict[str, float] = Field(default_factory=dict)


class EmbedRequest(BaseModel):
    """向量化请求：``kind`` 区分 query / document 两路（部分后端两路用词不同）。"""

    texts: list[str]
    kind: Literal["query", "document"] = "document"


class EmbedResponse(BaseModel):
    """向量化响应：向量列表 + 维度 + 实际模型名。"""

    vectors: list[list[float]]
    dim: int
    model: str


# --------------------------------------------------------------------------
# 健康检查
# --------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """健康检查响应。``status`` 为 ok/degraded/error，``details`` 携带各依赖明细。"""

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
    """反馈响应。``status=duplicate`` 表示该 trace_id 的反馈已存在，未重复入库。"""

    id: str
    status: Literal["accepted", "duplicate"] = "accepted"

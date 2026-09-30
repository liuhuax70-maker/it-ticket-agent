"""Milvus 集合定义与客户端管理。

对应 `开发流程/04-检索与编排设计.md` §2.2，按 Milvus 3.x / pymilvus 3.0.2 API 落地。

集合 `kb_chunks` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| chunk_id | VARCHAR(128) | 主键，`{doc_id}#{index}` |
| doc_id | VARCHAR(128) | 文档标识 |
| content | VARCHAR(**8192**) | 片段正文，`enable_analyzer=True` 供 BM25 |
| dense | FLOAT_VECTOR(1024) | qwen3-embedding 向量，COSINE |
| sparse | SPARSE_FLOAT_VECTOR | 由 BM25 Function 自动生成 |
| source | VARCHAR(32) | manual / faq / ticket |
| title | VARCHAR(512) | 章节标题 |
| version | VARCHAR(64) | 版本号（可空） |
| error_code | VARCHAR(64) | 错误码（可空） |
| updated_at | INT64 | 更新时间戳（秒） |
"""

from functools import lru_cache

from pymilvus import CollectionSchema, DataType, Function, FunctionType, MilvusClient

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.retrieval import Chunk, DocSource, RetrievalFilters

logger = get_logger(__name__)

# ---- 字段名常量（避免各处拼字符串） ----
FIELD_CHUNK_ID = "chunk_id"
FIELD_DOC_ID = "doc_id"
FIELD_CONTENT = "content"
FIELD_DENSE = "dense"
FIELD_SPARSE = "sparse"
FIELD_SOURCE = "source"
FIELD_TITLE = "title"
FIELD_VERSION = "version"
FIELD_ERROR_CODE = "error_code"
FIELD_UPDATED_AT = "updated_at"

#: BM25 函数名（Milvus 由此在 content 上自动生成 sparse 向量）
BM25_FUNCTION_NAME = "bm25"

# 字段长度上限
MAX_ID_LENGTH = 128
MAX_CONTENT_LENGTH = 8192
MAX_TITLE_LENGTH = 512
MAX_SHORT_LENGTH = 64

#: 中文分析器；jieba 分词对错误码与专有名词的切分更友好
ANALYZER_PARAMS: dict = {"tokenizer": "jieba"}

#: 检索时返回的字段
OUTPUT_FIELDS = [
    FIELD_CHUNK_ID,
    FIELD_DOC_ID,
    FIELD_CONTENT,
    FIELD_SOURCE,
    FIELD_TITLE,
    FIELD_VERSION,
    FIELD_ERROR_CODE,
]


@lru_cache
def get_client() -> MilvusClient:
    """返回 Milvus 客户端单例。"""
    settings = get_settings()
    logger.info("连接 Milvus: %s", settings.milvus_uri)
    return MilvusClient(uri=settings.milvus_uri)


def build_schema() -> CollectionSchema:
    """构建集合 Schema（含 BM25 Function）。"""
    settings = get_settings()

    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)

    schema.add_field(
        field_name=FIELD_CHUNK_ID,
        datatype=DataType.VARCHAR,
        is_primary=True,
        max_length=MAX_ID_LENGTH,
    )
    schema.add_field(field_name=FIELD_DOC_ID, datatype=DataType.VARCHAR, max_length=MAX_ID_LENGTH)
    schema.add_field(
        field_name=FIELD_CONTENT,
        datatype=DataType.VARCHAR,
        max_length=MAX_CONTENT_LENGTH,
        enable_analyzer=True,  # BM25 全文检索需要
        analyzer_params=ANALYZER_PARAMS,
    )
    schema.add_field(
        field_name=FIELD_DENSE,
        datatype=DataType.FLOAT_VECTOR,
        dim=settings.embedding_dim,
    )
    schema.add_field(field_name=FIELD_SPARSE, datatype=DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field(field_name=FIELD_SOURCE, datatype=DataType.VARCHAR, max_length=MAX_SHORT_LENGTH)
    schema.add_field(field_name=FIELD_TITLE, datatype=DataType.VARCHAR, max_length=MAX_TITLE_LENGTH)
    schema.add_field(field_name=FIELD_VERSION, datatype=DataType.VARCHAR, max_length=MAX_SHORT_LENGTH)
    schema.add_field(field_name=FIELD_ERROR_CODE, datatype=DataType.VARCHAR, max_length=MAX_SHORT_LENGTH)
    schema.add_field(field_name=FIELD_UPDATED_AT, datatype=DataType.INT64)

    # 声明 BM25：content → sparse 的转换由 Milvus 自动完成，插入时无需提供 sparse
    schema.add_function(
        Function(
            name=BM25_FUNCTION_NAME,
            function_type=FunctionType.BM25,
            input_field_names=[FIELD_CONTENT],
            output_field_names=[FIELD_SPARSE],
        )
    )
    return schema


def build_index_params():
    """构建索引参数：稠密 HNSW + 稀疏 BM25 倒排 + 标量 INVERTED。"""
    client = get_client()
    index_params = client.prepare_index_params()

    index_params.add_index(
        field_name=FIELD_DENSE,
        index_name="idx_dense",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )
    index_params.add_index(
        field_name=FIELD_SPARSE,
        index_name="idx_sparse_bm25",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={"bm25_k1": 1.2, "bm25_b": 0.75},
    )
    index_params.add_index(
        field_name=FIELD_SOURCE,
        index_name="idx_source",
        index_type="INVERTED",
    )
    index_params.add_index(
        field_name=FIELD_ERROR_CODE,
        index_name="idx_error_code",
        index_type="INVERTED",
    )
    return index_params


def ensure_collection(client: MilvusClient | None = None, *, recreate: bool = False) -> str:
    """确保集合存在（可选重建），返回集合名。"""
    settings = get_settings()
    client = client or get_client()
    name = settings.milvus_collection

    if client.has_collection(name):
        if not recreate:
            logger.info("集合已存在，跳过创建: %s", name)
            return name
        logger.warning("重建集合（将删除已有数据）: %s", name)
        client.drop_collection(name)

    client.create_collection(
        collection_name=name,
        schema=build_schema(),
        index_params=build_index_params(),
    )
    logger.info("集合创建完成: %s", name)
    return name


def collection_stats(client: MilvusClient | None = None) -> dict:
    """返回集合统计信息（用于验证）。"""
    settings = get_settings()
    client = client or get_client()
    name = settings.milvus_collection

    if not client.has_collection(name):
        return {"exists": False, "collection": name, "row_count": 0}

    stats = client.get_collection_stats(name)
    return {
        "exists": True,
        "collection": name,
        "row_count": int(stats.get("row_count", 0)),
    }


def build_filter_expr(filters: RetrievalFilters | None) -> str:
    """把过滤条件转成 Milvus 表达式字符串（空条件返回 ""）。"""
    if filters is None:
        return ""

    clauses: list[str] = []
    if filters.source is not None:
        clauses.append(f'{FIELD_SOURCE} == "{filters.source.value}"')
    if filters.error_code:
        clauses.append(f'{FIELD_ERROR_CODE} == "{filters.error_code}"')
    return " and ".join(clauses)


def to_chunks(results, score_field: str | None = None) -> list[Chunk]:
    """把 Milvus 检索结果转成 Chunk 列表。

    :param results: `client.search()` 的返回值（List[List[dict]]），只取第一个 query 的结果。
    :param score_field: 写回哪个分数字段（如 dense_score / sparse_score）。
    """
    hits = results[0] if results else []
    chunks: list[Chunk] = []

    for rank, hit in enumerate(hits, start=1):
        entity = hit.get("entity", {}) or {}
        source = entity.get(FIELD_SOURCE) or DocSource.FAQ.value
        extra = {}
        if score_field:
            extra[score_field] = float(hit.get("distance", 0.0))
        chunks.append(
            Chunk(
                chunk_id=entity.get(FIELD_CHUNK_ID, str(hit.get("id", ""))),
                doc_id=entity.get(FIELD_DOC_ID, ""),
                content=entity.get(FIELD_CONTENT, ""),
                source=DocSource(source),
                title=entity.get(FIELD_TITLE) or None,
                version=entity.get(FIELD_VERSION) or None,
                error_code=entity.get(FIELD_ERROR_CODE) or None,
                rank=rank,
                **extra,
            )
        )
    return chunks

"""Milvus 集合定义与客户端管理。

对应 `开发流程/04-检索与编排设计.md` §2.2，按 Milvus 3.x / pymilvus 3.0.2 API 落地。

集合 `kb_chunks` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| chunk_id | VARCHAR(128) | 主键，`{doc_id}#{index}` |
| doc_id | VARCHAR(128) | 文档标识 |
| content | VARCHAR(**8192**) | 片段正文（含标题层级路径前缀），`enable_analyzer=True` 供 BM25 |
| dense | FLOAT_VECTOR(1024) | qwen3-embedding 向量，COSINE |
| sparse | SPARSE_FLOAT_VECTOR | 由 BM25 Function 自动生成 |
| source | VARCHAR(32) | manual / faq / ticket，支持按来源删除 |
| title | VARCHAR(512) | 章节标题 |
| heading_path | VARCHAR(512) | 标题层级路径（`一级 > 二级`），用于来源标注 |
| content_hash | VARCHAR(64) | 内容哈希，用于**入库幂等**与查询期去重 |
| embedding_model | VARCHAR(128) | 生成该向量时所用的 embedding 模型，用于一致性断言 |
| version | VARCHAR(64) | 版本号（可空） |
| error_code | VARCHAR(64) | 错误码（可空） |
| updated_at | INT64 | 更新时间戳（秒） |

## 为什么要记录 embedding 模型

换 embedding 模型却不重建索引，是 RAG 里最隐蔽的故障：新旧向量落在**不同向量空间**，
代码不报错、维度甚至可能一致，但召回质量会**静默下降**且可持续数周。

因此：集合创建时把「embedding 模型名 + 维度 + schema 版本」写进集合 description，
每次入库/启动都 `verify_index_consistency()` 断言一致，不一致直接拒绝服务。
"""

import json
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
FIELD_HEADING_PATH = "heading_path"
FIELD_CONTENT_HASH = "content_hash"
FIELD_EMBEDDING_MODEL = "embedding_model"
FIELD_VERSION = "version"
FIELD_ERROR_CODE = "error_code"
FIELD_UPDATED_AT = "updated_at"

#: BM25 函数名（Milvus 由此在 content 上自动生成 sparse 向量）
BM25_FUNCTION_NAME = "bm25"

# 字段长度上限
MAX_ID_LENGTH = 128
MAX_CONTENT_LENGTH = 8192
MAX_TITLE_LENGTH = 512
MAX_PATH_LENGTH = 512
MAX_HASH_LENGTH = 64
MAX_MODEL_LENGTH = 128
MAX_SHORT_LENGTH = 64

#: 中文分析器；jieba 分词对错误码与专有名词的切分更友好
ANALYZER_PARAMS: dict = {"tokenizer": "jieba"}

#: Schema 版本；字段结构变化时必须递增，用于识别「旧索引需要重建」
SCHEMA_VERSION = 2

#: 集合 description 里存放索引元数据的键
INDEX_META_KEY = "kb_index_meta"

#: 检索时返回的字段
OUTPUT_FIELDS = [
    FIELD_CHUNK_ID,
    FIELD_DOC_ID,
    FIELD_CONTENT,
    FIELD_SOURCE,
    FIELD_TITLE,
    FIELD_HEADING_PATH,
    FIELD_CONTENT_HASH,
    FIELD_VERSION,
    FIELD_ERROR_CODE,
]


@lru_cache
def get_client() -> MilvusClient:
    """返回 Milvus 客户端单例。"""
    settings = get_settings()
    logger.info("连接 Milvus: %s", settings.milvus_uri)
    return MilvusClient(uri=settings.milvus_uri)


def build_index_meta() -> dict:
    """当前代码期望的索引元数据（写入集合 description）。"""
    settings = get_settings()
    return {
        "schema_version": SCHEMA_VERSION,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "analyzer": ANALYZER_PARAMS,
    }


def _description(meta: dict) -> str:
    return json.dumps({INDEX_META_KEY: meta}, ensure_ascii=False)


def build_schema() -> CollectionSchema:
    """构建集合 Schema（含 BM25 Function）。"""
    settings = get_settings()

    # 说明：description 必须通过 create_schema 传入；
    # 直接给 create_collection(description=...) 不会持久化（读回为空字符串）。
    schema = MilvusClient.create_schema(
        auto_id=False,
        enable_dynamic_field=False,
        description=_description(build_index_meta()),
    )

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
    schema.add_field(field_name=FIELD_HEADING_PATH, datatype=DataType.VARCHAR, max_length=MAX_PATH_LENGTH)
    schema.add_field(field_name=FIELD_CONTENT_HASH, datatype=DataType.VARCHAR, max_length=MAX_HASH_LENGTH)
    schema.add_field(field_name=FIELD_EMBEDDING_MODEL, datatype=DataType.VARCHAR, max_length=MAX_MODEL_LENGTH)
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
    for field_name, index_name in (
        (FIELD_SOURCE, "idx_source"),
        (FIELD_ERROR_CODE, "idx_error_code"),
        (FIELD_CONTENT_HASH, "idx_content_hash"),
    ):
        index_params.add_index(field_name=field_name, index_name=index_name, index_type="INVERTED")
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
        schema=build_schema(),  # description 已写入 schema
        index_params=build_index_params(),
    )
    logger.info("集合创建完成: %s（schema v%d）", name, SCHEMA_VERSION)
    return name


def read_index_meta(client: MilvusClient | None = None) -> dict | None:
    """读取集合 description 中的索引元数据。"""
    settings = get_settings()
    client = client or get_client()
    name = settings.milvus_collection

    if not client.has_collection(name):
        return None

    description = client.describe_collection(name).get("description") or ""
    try:
        payload = json.loads(description)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload.get(INDEX_META_KEY) if isinstance(payload, dict) else None


def verify_index_consistency(
    client: MilvusClient | None = None, *, raise_on_mismatch: bool = True
) -> dict:
    """断言「索引里的向量」与「当前 embedding 模型」属于同一向量空间。

    这是防「换模型不重建索引」这类**静默故障**的闸门。

    :raises RuntimeError: 不一致且 `raise_on_mismatch=True`。
    """
    expected = build_index_meta()
    stored = read_index_meta(client)

    mismatches: list[str] = []
    if stored is None:
        mismatches.append("索引未记录 embedding 元数据（可能是旧版本建的集合）")
    else:
        for key in ("embedding_model", "embedding_dim", "schema_version"):
            if stored.get(key) != expected.get(key):
                mismatches.append(f"{key}: 索引={stored.get(key)!r} 当前={expected.get(key)!r}")

    result = {"ok": not mismatches, "stored": stored, "expected": expected, "mismatches": mismatches}

    if mismatches:
        message = (
            "索引与当前 embedding 配置不一致："
            + "；".join(mismatches)
            + "。请用 `python -m ingestion.ingest --recreate` 重建索引。"
        )
        if raise_on_mismatch:
            raise RuntimeError(message)
        logger.error(message)
    return result


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


def upsert_rows(rows: list[dict], client: MilvusClient | None = None) -> int:
    """按主键 upsert 写入（**幂等**：重复执行不会产生重复片段）。"""
    if not rows:
        return 0
    settings = get_settings()
    client = client or get_client()

    result = client.upsert(collection_name=settings.milvus_collection, data=rows)
    return int(result.get("upsert_count", len(rows)))


def delete_by_source(source: str, client: MilvusClient | None = None) -> int:
    """按来源删除全部片段（支持「删文档」场景）。"""
    settings = get_settings()
    client = client or get_client()

    result = client.delete(
        collection_name=settings.milvus_collection,
        filter=f'{FIELD_SOURCE} == "{source}"',
    )
    deleted = int(result.get("delete_count", 0))
    logger.info("按来源删除: source=%s count=%d", source, deleted)
    return deleted


def count_by_source(client: MilvusClient | None = None) -> dict[str, int]:
    """统计各来源的片段数（用于验证幂等与删除效果）。"""
    settings = get_settings()
    client = client or get_client()
    name = settings.milvus_collection

    if not client.has_collection(name):
        return {}

    client.flush(name)
    counts: dict[str, int] = {}
    for source in DocSource:
        rows = client.query(
            collection_name=name,
            filter=f'{FIELD_SOURCE} == "{source.value}"',
            output_fields=["count(*)"],
        )
        total = rows[0].get("count(*)", 0) if rows else 0
        counts[source.value] = int(total)
    return counts


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
                heading_path=entity.get(FIELD_HEADING_PATH) or None,
                content_hash=entity.get(FIELD_CONTENT_HASH) or None,
                version=entity.get(FIELD_VERSION) or None,
                error_code=entity.get(FIELD_ERROR_CODE) or None,
                rank=rank,
                **extra,
            )
        )
    return chunks

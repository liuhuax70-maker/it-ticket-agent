"""向量化配置。"""

from __future__ import annotations

from packages.common.settings import BaseAppSettings


class EmbedSettings(BaseAppSettings):
    """向量化配置。

    ``embed_dim`` 的真实语义（与早期注释相反）：它是**期望值**，不是硬校验。
    后端启动时会探测模型实际维度，与 ``embed_dim`` 不一致只打 warning 并按模型维度运行
    （见 ``fastembed_backend``）；indexing 又改成"以 embedder 实际维度建 collection"。
    所以真正会报错的场景只有一个：**collection 已存在且 dim 与当前模型不同**——
    此时写入被 Milvus 拒绝。改模型维度后必须重建 collection（或换 collection 名）。
    """

    service_name: str = "indexing"

    # fastembed = 本地 ONNX（默认，无需外部服务）
    # litellm  = 远端 OpenAI 兼容 /v1/embeddings
    # sentence_transformers = 本地 torch（体积大）
    embed_backend: str = "fastembed"
    embed_model: str = "BAAI/bge-small-zh-v1.5"
    embed_dim: int = 512
    embed_batch_size: int = 32
    # BGE 中文系列：**仅查询侧**加指令，文档侧不加（否则召回变差且无报错）
    embed_query_instruction: str = "为这个句子生成表示以用于检索相关文章："

    # litellm 后端专用
    embed_api_base: str = ""
    embed_api_key: str = ""

    # 缓存
    embed_cache_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 3600

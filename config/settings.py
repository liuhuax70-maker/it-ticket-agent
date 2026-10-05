"""全局配置：唯一的配置入口。

约定（开发流程文档 §7 S0）：
    - 所有"拍脑袋的常数"（chunk 大小、top_k、模型名、超时、开关）都在这里声明；
    - 业务代码只从 ``settings`` 读取，不允许在 .py 里硬编码字面量；
    - 读取顺序：真实环境变量 > .env 文件 > 此处默认值。

环境变量名与字段名一一对应（大小写不敏感），例如 ``MILVUS_URI`` -> ``milvus_uri``。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings", "settings"]


class Settings(BaseSettings):
    """应用配置。字段即环境变量契约，改动需同步更新 .env.example。"""

    model_config = SettingsConfigDict(
        env_file=".env",          # 相对当前工作目录，请在仓库根目录启动服务
        env_file_encoding="utf-8",
        extra="ignore",           # .env 里的多余键不报错
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_env: str = "dev"
    log_level: str = "INFO"

    # ---------- Milvus ----------
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "chunks_p0"
    milvus_timeout: float = 5.0

    # ---------- 向量化（BGE）----------
    embed_model: str = "BAAI/bge-small-zh-v1.5"
    embed_dim: int = 512                 # 与 milvus collection 的 dim 必须一致
    embed_batch_size: int = 32

    # ---------- LLM（OpenAI 兼容端点）----------
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 60.0

    # ---------- Redis ----------
    redis_url: str = "redis://localhost:6379/0"
    use_redis_cache: bool = False        # P0 只用于 embedding 缓存，默认关闭

    # ---------- 数据与检索 ----------
    data_dir: str = "./data"
    docs_dir: str = "./docs_corpus"
    chunk_size: int = 500
    chunk_overlap: int = 80
    top_k: int = 5

    # ---------- LangFuse（留空即不启用）----------
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例，避免每次调用都重新解析 .env。"""
    return Settings()


settings = get_settings()

"""应用配置。

所有可调参数集中于此，通过环境变量 / `.env` 注入（pydantic-settings）。
配置项与 `开发流程/07-部署与运维方案.md` 的 `.env.example` 一一对应。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 应用 ----
    app_env: str = "dev"
    app_name: str = "it-ticket-assistant"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # ---- Milvus ----
    # 用 127.0.0.1 而非 localhost：后者可能优先解析到 IPv6 ::1 导致连接挂起
    milvus_host: str = "127.0.0.1"
    milvus_port: int = 19530
    milvus_collection: str = "kb_chunks"

    # ---- Ollama / 生成 ----
    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3.5:9b"
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dim: int = 1024
    reranker_model: str = "dengcao/Qwen3-Reranker-4B:Q5_K_M"

    # ---- 检索参数 ----
    top_n_dense: int = 20
    top_n_sparse: int = 20
    top_n_fused: int = 50
    top_k: int = 5
    rrf_k: int = 60
    rerank_enabled: bool = False

    # ---- 编排 / 会话 ----
    checkpointer_backend: str = "sqlite"
    checkpointer_path: str = "./data/checkpoints.sqlite"
    session_ttl_days: int = 7

    # ---- 可观测 ----
    langsmith_enabled: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "it-ticket-assistant"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # ---- 安全 ----
    idempotency_ttl_hours: int = 24

    @property
    def milvus_uri(self) -> str:
        """pymilvus 连接地址。"""
        return f"http://{self.milvus_host}:{self.milvus_port}"


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例，避免重复读取环境变量。"""
    return Settings()

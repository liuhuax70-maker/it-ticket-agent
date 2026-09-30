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
    #: 单通道超时（秒）；超时则该通道降级。
    #: 设计文档给的是 2s，但本地 embedding 首次调用含模型加载，故放宽到 5s。
    channel_timeout_seconds: float = 5.0
    #: 参与重排的候选上限（重排逐条调用大模型，必须限流）
    rerank_top_n: int = 20
    #: 重排并发度
    rerank_max_workers: int = 4
    #: 重排单条超时（秒）
    rerank_timeout_seconds: float = 60.0

    # ---- 编排 / 会话 ----
    checkpointer_backend: str = "sqlite"  # sqlite | memory
    checkpointer_path: str = "./data/checkpoints.sqlite"
    session_ttl_days: int = 7
    #: 敏感关键词（英文逗号分隔）；命中即判为敏感工单并转人工审核
    sensitive_keywords: str = "投诉,举报,起诉,法律,赔偿,泄露,数据丢失,安全事件,监管,停机事故,账号被盗"
    #: 是否所有工单都需人工审核（默认仅敏感工单需要）
    require_review_for_all: bool = False

    # ---- 生成 ----
    #: 单次生成超时（秒）；本地模型长文本生成较慢，给足时间
    llm_timeout_seconds: float = 300.0

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

    @property
    def sensitive_keyword_list(self) -> list[str]:
        """敏感关键词列表（去掉空项）。"""
        return [kw.strip() for kw in self.sensitive_keywords.split(",") if kw.strip()]


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例，避免重复读取环境变量。"""
    return Settings()

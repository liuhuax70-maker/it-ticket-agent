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
    #: 进入生成上下文的片段数。由 `evaluation/run_param_sweep.py` 扫出：
    #: k=3→Recall 85.0%，k=5→90.8%，**k=8→95.8%**，k=8~10 为平台期，k=15→100%。
    #: 取拐点 k=8（再往上收益递减但上下文成本线性增长）。
    top_k: int = 8
    rrf_k: int = 60
    rerank_enabled: bool = False
    #: 相关性门槛：Top-K 中最高稠密分低于此值 → 判定「无可用资料」，
    #: 直接走拒答/转人工路径，不再把无关片段喂给生成层（省一次生成，也避免诱导编造）。
    #:
    #: 由 `evaluation/calibrate_threshold.py` 在 200 条样本上校准（5 折 CV）后的权衡：
    #:   0.42 → 漏判 0 / 误收 7 of 36（**保守点，不误杀真问题**，拦截 81%）
    #:   0.45 → 漏判 1 / 误收 2
    #:   0.50 → 漏判 1 / 误收 0（激进点）
    #: F1 最优值 0.513 会误杀 2 条「gold 已被召回」的问题，故**不取 F1 最优点**：
    #: 两种误判代价不对称 —— 漏判=本可答对却被拒答（代价高），误收=无关片段放行（代价低，
    #: 生成层还有一道拒答闸）。
    #:
    #: 只用稠密分，不用 BM25 分：BM25 原始分受查询长度/词频影响，**不可跨查询比较**
    #: （实测正负分布重叠：正样本 min 6.2 < 负样本 max 16.5）；RRF 是排名分，无相关性含义。
    #: 设为 0 可关闭门槛。
    relevance_min_dense: float = 0.42
    #: 单通道超时（秒）；超时则该通道降级。
    #: 实测：本地 embedding 在模型冷启动/负载高时会超过 5s，导致稠密通道**静默降级**为纯 BM25
    #: （实测出现过一次）。这类「静默劣化」比报错更危险，故放宽到 10s。
    #: 代价是真正卡死时多等 5s，可接受。
    channel_timeout_seconds: float = 10.0
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
    #: 输出 token 上限；0 表示不限制。
    #: 本地生成延迟 ≈ 输出 token 数 ÷ 生成速率，因此限制输出长度是最直接的一刀（见 编码过程/11）。
    llm_num_predict: int = 0
    #: 模型保活时长。本地模型**冷加载**耗时可达十几秒，保活可避免每次请求重复加载。
    llm_keep_alive: str = "30m"
    #: 拼装上下文的最大字符预算；超出时**显式丢弃**低排名片段并记录日志，
    #: 而不是让模型 API 静默截断（被截掉的可能正是答案所在片段）
    context_max_chars: int = 6000

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

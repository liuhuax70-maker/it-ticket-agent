"""eval 配置。

同时继承两套配置：
    LLMSettings      评测要调用 LLM 当裁判，复用 DeepSeek/本地双模解析，不另维护一套模型配置
    SecuritySettings 采集要用**真实身份**取 Keycloak 令牌（否则权限类样本全是假阳性）

不继承就会踩的坑：设置里没有 keycloak_* 字段，TokenProvider 一取令牌就 AttributeError。
"""

from __future__ import annotations

from packages.common.constants import SERVICE_EVAL
from packages.llms.config import LLMSettings
from packages.security.config import SecuritySettings


class Settings(LLMSettings, SecuritySettings):
    service_name: str = SERVICE_EVAL
    host: str = "0.0.0.0"
    port: int = 8006

    # 被测系统入口
    api_gateway_url: str = "http://localhost:8000"
    request_timeout: float = 300.0

    # 数据集与报告
    dataset_path: str = "configs/eval/golden.jsonl"
    reports_dir: str = "eval_data/reports"
    max_samples: int = 100

    # ---- 采集 ----
    # 评测时向被测系统请求的 top_k，直接决定 hit@k 里的 k
    top_k: int = 5
    # 拉文档台账用的身份（需要 documents:read；默认取有写权限的 carol）
    ledger_username: str = "carol"
    ledger_limit: int = 500
    # 语料缺失时是否仍然继续：默认 False——缺语料跑出来的 hit@k 没有意义，
    # 不如直接失败并把补齐命令打出来
    allow_missing_corpus: bool = False

    # ---- L2（RAGAS）----
    ragas_enabled: bool = True
    # 裁判每样本要发多次请求，本地小模型很慢，默认只跑一小部分
    ragas_max_samples: int = 12

    # 裁判模型（留空=用 LLM_PROVIDER 的默认模型）
    judge_model: str = ""
    # 默认只启用**不需要 embedding** 的指标：
    # answer_relevancy 需要 embedding 模型，接入自建 embedding 服务后再打开
    metrics: str = "faithfulness,context_precision,context_recall"

    def metric_list(self) -> list[str]:
        return [m.strip() for m in self.metrics.split(",") if m.strip()]

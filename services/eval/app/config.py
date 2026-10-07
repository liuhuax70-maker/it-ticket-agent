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
    # 评测固定作答温度（默认 0）。理由见 docs/adr/0005：
    # 温度不为 0 时"该不该拒答"对采样极其敏感，同一份评测集连跑两次误答率能翻倍，
    # 指标就失去了可比性。置为 null 则沿用 model-gateway 的默认温度
    # （仅在故意观察采样波动时使用）。
    answer_temperature: float | None = 0.0
    ledger_limit: int = 500
    # 语料缺失时是否仍然继续：默认 False——缺语料跑出来的 hit@k 没有意义，
    # 不如直接失败并把补齐命令打出来
    allow_missing_corpus: bool = False

    # ---- L2（RAGAS）----
    ragas_enabled: bool = True
    # 裁判每样本要发多次请求，本地小模型很慢，默认只跑一小部分
    ragas_max_samples: int = 12
    # 单个评测任务的超时与并发上限。
    # ragas 默认 timeout=180s：本地小模型下 context_precision 这类"对每条上下文分别
    # 调用裁判"的指标会整批超时（表现为全 NaN，很容易被误读成"裁判不会解析"）。
    judge_timeout: float = 900.0
    judge_workers: int = 4

    # 裁判模型（留空=用 LLM_PROVIDER 的默认模型）
    judge_model: str = ""
    # 默认只启用**不需要 embedding** 的指标：
    # answer_relevancy 需要 embedding 模型，接入自建 embedding 服务后再打开
    metrics: str = "faithfulness,context_precision,context_recall"

    def metric_list(self) -> list[str]:
        return [m.strip() for m in self.metrics.split(",") if m.strip()]

"""eval 配置。

评测要调用 LLM 当裁判（judge），因此直接继承 LLMSettings 复用 DeepSeek 双模解析，
不额外维护一套模型配置。
"""

from __future__ import annotations

from packages.common.constants import SERVICE_EVAL
from packages.llms.config import LLMSettings


class Settings(LLMSettings):
    service_name: str = SERVICE_EVAL
    host: str = "0.0.0.0"
    port: int = 8006

    # 被测系统入口
    api_gateway_url: str = "http://localhost:8000"
    request_timeout: float = 300.0

    # 数据集与报告
    dataset_path: str = "eval_data/golden.jsonl"
    reports_dir: str = "eval_data/reports"
    max_samples: int = 100

    # 评测身份（沿用默认租户/部门；若开启鉴权需换成真实令牌）
    tenant_id: str = "default"
    department_id: str = "default"
    user_id: str = "u_eval"

    # 裁判模型（留空=用 LLM_PROVIDER 的默认模型）
    judge_model: str = ""
    # 默认只启用**不需要 embedding** 的指标：
    # answer_relevancy 需要 embedding 模型，接入自建 embedding 服务后再打开
    metrics: str = "faithfulness,context_precision,context_recall"

    def metric_list(self) -> list[str]:
        return [m.strip() for m in self.metrics.split(",") if m.strip()]

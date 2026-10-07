"""跨服务共享常量。"""

from __future__ import annotations

# 检索为空或资料不足时的统一拒答话术。
# 铁律：一个在检索为空时仍然编答案的 RAG 接口，比不可用更危险。
REFUSE_TEXT = "抱歉，我没有在当前知识库中找到与你的问题相关的资料，无法给出准确回答。"

SERVICE_API_GATEWAY = "api-gateway"
SERVICE_QUERY_ORCHESTRATOR = "query-orchestrator"
SERVICE_RETRIEVAL = "retrieval"
SERVICE_INGESTION = "ingestion"
SERVICE_INDEXING = "indexing"
SERVICE_MODEL_GATEWAY = "model-gateway"
SERVICE_EVAL = "eval"
SERVICE_FEEDBACK = "feedback"
SERVICE_AUTHZ = "authz"

VERSION = "0.1.0"

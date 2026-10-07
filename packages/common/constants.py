"""跨服务共享常量。"""

from __future__ import annotations

# 检索为空或资料不足时的统一拒答话术（**面向用户**的文案）。
# 铁律：一个在检索为空时仍然编答案的 RAG 接口，比不可用更危险。
REFUSE_TEXT = "抱歉，我没有在当前知识库中找到与你的问题相关的资料，无法给出准确回答。"

# 面向**模型**的拒答标记：让模型输出一个机器可识别的哨兵，而不是复述上面的文案。
# 原因：模型复述话术时会顺手加上引用、或用同义改写（「资料中未提及」），
# 靠字符串匹配话术去判断拒答既不可靠也易误判。
REFUSE_MARKER = "NO_ANSWER"

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

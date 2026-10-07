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

# ---------------- 文档生命周期（失效管理）----------------
#
# 为什么要有它：制度文档被取代后，旧版本**仍会被检索、仍会被引用**——
# 用户拿到的是一份已经作废的规定，而系统无从知道它已失效。
# 越权有 ACL 兜着，"过期"没有，只能靠显式的生命周期状态。
#
# 取值刻意只有两个：
#   active   —— 现行有效
#   retired  —— 已废止（被取代、或已过有效期），不参与检索
# 不再细分 superseded/draft/expired：区分它们对检索过滤没有区别，
# 而"多一个状态"就多一处需要同步维护的语义。区分细节放进元数据里的
# ``lifecycle_reason`` / ``superseded_by``，供人查看，不参与过滤。
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_RETIRED = "retired"
LIFECYCLE_VALUES: tuple[str, ...] = (LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED)

# 不参与检索的取值。用**排除**语义（must_not）而不是"要求 active"：
# 存量文档没有这个字段，Milvus 未赋值为 ""、OpenSearch 不参与 term 匹配，
# 两者都天然通过排除，因此上线不需要数据迁移；反过来会让全库搜不到东西。
RETIRED_LIFECYCLES: tuple[str, ...] = (LIFECYCLE_RETIRED,)
EXCLUDE_LIFECYCLE: list[dict[str, str]] = [{"lifecycle": value} for value in RETIRED_LIFECYCLES]

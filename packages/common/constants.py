"""跨服务共享常量。"""

from __future__ import annotations

# 检索为空或资料不足时的统一拒答话术（**面向用户**的文案）。
#
# ⚠️ 使用前提：ANSWER_FALLBACK_ENABLED=false 且提示词为 v4 及更早。
# 默认配置下（v6 + 降级开启）**已经不走拒答**了：知识库是可选信息源，
# 查不到时改为「声明来源 + 通用知识」（见 NO_CONTEXT_NOTICE），寒暄类问题直接正常回答。
# 保留本常量是因为旧行为仍可通过配置回退，且评测与历史数据按它对齐口径。
#
# 历史铁律（已作废，勿据此"修复"）：曾规定"检索为空仍编答案比不可用更危险"。
# 它成立的前提是"这套系统只用于查公司制度"；当用户也会问寒暄、通用的编程问题时，
# 一律拒答的代价（用户问什么都得不到回应）超过了它要防的风险。
# 真正的防线不是"不许答"，而是**不许伪装成有依据**：降级回答必须声明来源、不得带引用。
REFUSE_TEXT = "抱歉，我没有在当前知识库中找到与你的问题相关的资料，无法给出准确回答。"

# 面向**模型**的拒答标记：让模型输出一个机器可识别的哨兵，而不是复述上面的文案。
# 原因：模型复述话术时会顺手加上引用、或用同义改写（「资料中未提及」），
# 靠字符串匹配话术去判断拒答既不可靠也易误判。
REFUSE_MARKER = "NO_ANSWER"

# ---------------- 无资料降级回答 ----------------
#
# 检索为空时不再直接拒答，而是输出「声明 + 通用知识简答」。
# 降级答案的第一句固定为这段话——**必须**明确告知用户答案不来自知识库，
# 否则模型常识与内部制度混在一起，用户无从分辨哪句能照着做。
#
# 为什么不做成「悄悄降级」：本项目是制度问答，用户可能把答案当作公司规定执行。
# 一旦无法分辨来源，幻觉的代价就从"信息不准"升级为"照着错误制度操作"。
NO_CONTEXT_NOTICE = "知识库中没有检索到与该问题相关的内容。以下回答基于通用知识，不来自本知识库，请勿直接当作公司规定执行："

# 提示词要求模型在「不该带引用」的两类回答末尾输出本标记，守卫节点据此跳过引用兜底。
#
# 为什么需要它：guard 的"答案没标引用就兜底附 top1"是为**正常问答**准备的兜底，
# 但它无法区分「模型漏标」和「模型在回答一个与资料无关的问题」。不加这个标记时，
# 会出现两种荒谬结果：
#   * 用户问"你好"，回答后面挂着一条员工手册的制度引用；
#   * 答案写着"资料中没有检索到相关内容"，脚上却挂着一条引用（自相矛盾，
#     而且会让用户以为那段通用建议是有依据的）。
# 靠"答案里有没有 [n]"判断不了——而引用一旦挂上，用户的信任成本极高。
NO_CITE_MARKER = "[[NO_CITE]]"


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

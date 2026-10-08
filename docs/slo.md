# SLO：书面目标值

## 为什么需要它

验收清单里"核心业务指标达标"没有阈值就无法判定——"达标"必须先有"标"。
本文件就是那个"标"。每条 SLO 都注明**在哪里被度量**（指标 / 评测 / 告警），
保证它不是愿望清单：任何一条都能指着具体的查询或门禁说"它被这么检查着"。

原则：SLO 比告警阈值更严格没关系（告警是快速信号，SLO 是月度承诺），
但**绝不能比度量手段宽松**——那样的 SLO 只会被用来事后解释，不会驱动任何改进。

## 服务等级目标（2026-10 基线：70 条评测 / P50 688ms / P95 1065ms）

| 维度 | 目标 | 度量手段 | 违约时的信号 |
| --- | --- | --- | --- |
| 可用性（月度） | /chat 成功率 >= 99.5% | `http_requests_total`，Prometheus | `RagServiceDown`（critical）、`RagHighErrorRate`（critical，5xx > 1% 持续 10m） |
| 端到端延迟（月度） | /chat P95 <= 3.5s | `http_request_duration_seconds`（网关直方图） | `RagHighP95Latency`（warning，P95 > 5s 持续 10m——比 SLO 宽松，作快速信号） |
| 越权泄露 | **0 条（硬不变量）** | 评测门禁 `leak_count` + `RagAclMissing`（critical，for: 0m） | 触发即视为事故：权限下推链路断裂 |
| 注入得逞 | **0 次（硬不变量）** | 评测门禁 `forbidden_count`（`must_not_contain`）+ `RagPromptInjectionInContext`（warning，来源排查） | 得逞 = 答案出现标记串；资料疑似投毒 = 告警人工排查 |
| 检索质量 | hit@k >= 95%，片段召回 >= 95% | 评测回归门禁（每 PR 全量跑，`configs/eval/baseline.json`） | 门禁 fail（检索类指标容差 0） |
| 回答质量 | 漏答率 <= 5%，误答率 <= 5% | 评测回归门禁（容差 0.05 = 1 条样本翻转） | 门禁 fail |
| L2 参考值 | faithfulness >= 0.90 | 评测门禁（容差 0.10 = **实测裁判噪声**，见 ADR 0005） | 门禁 fail（注意：L2 是弱信号，仅参考） |
| 拒答率（线上） | < 30%（滚动 30 分钟） | `rag_answer_total{outcome}` | `RagRefusalRateHigh`（warning）——拒答飙升多半是检索/语料坏了 |
| 缓存健康 | 命中率 >= 5%（开启时） | `rag_cache_lookups_total` | `RagCacheHitRateLow`（warning，1h） |

## 明确不设 SLO 的项（以及为什么）

- **NDCG/MRR 的绝对值**：它们是回归信号（相对基线不许降），不是绝对质量承诺——
  "NDCG 必须 >= 0.99"没有意义，评测集做难之后它会自然下降而系统更好。
- **成本**：见 model-gateway 的 `rag_llm_cost_usd_total`；
  单价随供应商变，预算应由运营侧按月核定，不在工程 SLO 里定死。
- **首 token 延迟**：当前链路不做流式，P95 覆盖整请求即可。

## SLO 违约后的动作

1. **安全不变量（越权/注入）**：立即停下发布、回滚最近的语料/配置/代码变更，
   按 `docs/adr/0003` 与 `verify_permissions.py` 排查权限下推链路；这是唯一"触发即事故"的类别。
2. **可用性/延迟**：先看 `http_request_duration_seconds` 按服务分位，找最慢的一段
   （模型网关通常是长头）；再对账各阶段计时与端到端的缺口（README 有方法）。
3. **质量（评测门禁）**：门禁 fail 时先跑 `--rescore` 排除裁判噪声（faithfulness 极差 0.052），
   再看失败明细（报告已带 `per_sample_by_id`）定位到样本。
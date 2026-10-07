# ADR 0004：`private` 可见性必须显式携带 owner，缺失时大声失败

- 状态：已采纳
- 日期：2026-10-07

## 背景

`private` 可见性的判定依赖 `owner` 字段：过滤表达式里是
`visibility == "private" and owner == "<调用方 user_id>"`。

S7 权限闭环验收时发现：**通过界面上传的 private 文档，上传者本人也检索不到**。
追查 Milvus 实际落库数据：

```
{'chunk_id': 'd_653ea5f0:0', 'visibility': 'private', 'owner': ''}
```

`owner` 是空字符串。根因是 multipart 上传链路：

1. `api-gateway` 已经算好了 `ACL(owner=identity.user_id)`；
2. 但 `IngestionClient.upload()` 组装 form 字段时**漏传了 `owner`**（只传了
   tenant_id / department_id / visibility / reindex）；
3. `services/ingestion` 的 `/ingest/upload` 用默认值构造 ACL，`owner=None` → 落库为 `""`。

结果：文档**上传成功（HTTP 200、`status=succeeded`）**，但之后任何人都检索不到它。
这是权限系统最危险的失败模式——**静默失效**：接口一片绿，功能其实不成立。

对比：`POST /documents/ingest`（JSON 路径）传的是完整 ACL 对象，所以没有这个问题；
两个入口的字段集不一致，才让这个漏洞藏在了"另一条路能跑通"的假象后面。

## 决策

1. **`owner` 与 `tenant_id` / `department_id` 同规则：只能由网关从身份注入**，
   客户端不得指定。multipart 上传补齐 `owner` 字段。
2. **`visibility=private` 且未提供 `owner` 时返回 422**，不设默认值兜底。
   宁可在入口报错，也不要产生"看起来成功、实际不可见"的数据。
3. **禁止权限相关的静默降级**：凡是"过滤条件缺字段"的情形，一律走显式失败或显式告警
   （另见 `compile_filters` 在 ACL 为 None 时的 `logger.warning`）。

## 理由

- 权限数据的错误**不会自己暴露**：没有异常、没有 5xx，只是"搜不到"或"多搜到了"。
  唯一可靠的发现方式是端到端验收（本 ADR 就是 S7 的产物，而不是单测的产物）。
- 单测断言的是"过滤契约的形状"，无法覆盖"网关→ingestion 字段是否传全"这条链路。
  **跨服务的字段契约只能靠端到端验证兜住。**
- fail-loud 会让调用方立刻修正，而默认值兜底会把 bug 推迟到"用户投诉搜不到文档"。

## 代价

- 上传接口多了一个条件校验（仅 private 场景触发）。
- `allowed_roles` 目前仍未参与过滤（`compile_filters` 只看
  tenant / department / visibility / owner），multipart 入口也不接收它。
  若要支持"角色白名单可见性"，需同时改 ACL 契约、存储层 schema/expr 与两个入口，
  属于后续阶段，已记入 README「当前边界」。

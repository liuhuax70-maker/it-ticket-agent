# ADR 0002：权限过滤下沉到存储层，不在应用层裁剪结果

- 状态：已采纳
- 日期：2026-10-07

## 背景

「权限感知 RAG」的核心难点不是鉴权，而是**检索阶段的权限过滤**。
一个常见的实现是：先按相似度召回 top_k，再在应用层过滤掉无权访问的文档。

这个做法有一个隐蔽且严重的后果：**越权的文档会先占据 top_k 名额**，
用户越权范围越大，能看到的有权文档反而越少——表现为「明明有权限却检索不到」。

## 决策

过滤条件编译为**存储层的原生过滤**：

- Milvus：`expr = tenant_id == "t1" and (visibility == "public" or ... )`
- OpenSearch：`bool.filter` 下的 `term` / `terms` 组合

编译只有一处实现：`packages/retrievers/filters.py::compile_filters(acl, doc_ids)`。
各存储适配层只做**机械翻译**，不参与语义判断。

可见性语义（`must.tenant_id` 对所有分支生效）：

| visibility | 可见范围 |
| --- | --- |
| `public` | 租户内所有人 |
| `internal` | 租户内所有人 |
| `department` | 同部门 |
| `private` | 仅 `owner` |

## 理由

1. **正确性**：top_k 名额不会被越权文档挤占，召回规模与权限无关。
2. **性能**：过滤在存储层与向量检索同时完成，避免「召回 100 条再砍掉 90 条」的浪费。
3. **可测试**：编译结果是纯函数，可以用单元测试覆盖可见性矩阵；过滤效果可以用
   「同一 query 换不同身份，召回集合应为包含关系」来断言。

## 代价与缓解

- 需要在写入时把 ACL 字段冗余进检索索引（Milvus 标量字段 + OpenSearch keyword），
  存储放大可忽略（每条 chunk 增加约 100 字节）。
- schema 与过滤表达式耦合：ACL 字段必须**在建表时就写进 schema**，
  因此最小闭环虽不启用权限，字段与 `INVERTED` 索引已预置，启用时无需重建 collection。
- 语义分散风险：通过「编译器唯一实现 + 适配层只翻译」的结构规避。

## 影响

- `Chunk` 契约必须携带 `acl`；`ChatRequest` 不携带身份，身份由网关注入并编码为
  `SearchRequest.acl` 一路下传。
- 新增检索后端（如 pgvector）时，只需实现同一份 `FilterDict` 的翻译。

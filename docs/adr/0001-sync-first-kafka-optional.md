# ADR 0001：接入链路先同步直连，Kafka 只预留接口

- 状态：已采纳
- 日期：2026-10-07

## 背景

栈里包含 Kafka，参考结构也把 `raw-documents → chunk-events` 的异步拓扑写死了。
但整个平台当前**还没有可用的评测集**，也就无法判断「异步化」带来的是收益还是新的故障面。

## 决策

最小闭环里 `ingestion → indexing` 走**同步 HTTP 直连**，Kafka 只保留拓扑与接口：

```python
class ChunkSink(Protocol):
    async def send(self, chunks, *, reindex) -> IndexResponse: ...
    async def delete_document(self, doc_id) -> dict[str, int]: ...
```

- `HttpChunkSink`：默认实现，一次 `/ingest` 返回即代表索引已写入；
- `KafkaChunkSink`：占位实现，接口一致，投递成功但此时尚未入库（返回体的计数如实为 0）。

切换方式：`USE_KAFKA=true` + `--profile streaming`，业务代码零改动。

## 理由

1. **一致性更容易守**：同步模式下「元数据写库 → 索引写入 → 改状态」是一条直线，
   任何时刻 `status=indexed` 都意味着索引真的写了；异步模式下要额外处理
   「事件重复、乱序、消费者滞后」三类问题，而这些问题的排查成本远高于当时的收益。
2. **首次调试成本**：异步链路一旦出问题，需要同时排查 Producer、Topic、Consumer、
   消费者并发与幂等，而最小闭环的目标是验证**检索质量与引用可靠性**。
3. **接口先定型**：`ChunkSink` 已经把「谁负责投递、投递什么结构」固定下来，
   后补 Kafka 属于替换实现，不是重构。

## 代价与缓解

- 大文件接入会占用一次长连接：网关与 ingestion 的超时已放宽到 600s，并且
  单文件大小上限 32MB（`MAX_FILE_SIZE_MB`）。
- 接入吞吐受单请求限制：需要大批量导入时用 `pipelines/ingestion_dag/sync.py`
  批量驱动，或切 `USE_KAFKA=true`。

## 影响

- ingestion 的 `metadata.py` 负责文档/分块落库，indexing 只负责检索索引；
  两个存储不会被同一个服务同时持有事务语义。
- 删除路径同样保持一致顺序：**先删索引、再删元数据**（反序会留下孤儿向量）。

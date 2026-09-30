"""知识库入库脚本（手动执行）。

流程：读取源文档 → 切分 → 向量化（qwen3-embedding）→ 写入 Milvus
（同时写入 content 以启用内置 BM25）。

运行：`python -m ingestion.ingest`
"""


def ensure_collection() -> None:
    """创建 Collection（dense FLOAT_VECTOR + sparse SPARSE_FLOAT_VECTOR）与索引。"""
    # TODO(后续)：按 `开发流程/04-检索与编排设计.md` §2.2 定义 schema
    raise NotImplementedError("骨架占位：ensure_collection 将在后续编码阶段实现")


def ingest(path: str) -> None:
    """执行一次全量/增量导入。"""
    # TODO(后续)：遍历源文档 → split_document → 向量化 → upsert
    raise NotImplementedError("骨架占位：ingest 将在后续编码阶段实现")


def main() -> None:
    """命令行入口。"""
    raise NotImplementedError("骨架占位：main 将在后续编码阶段实现")


if __name__ == "__main__":
    main()

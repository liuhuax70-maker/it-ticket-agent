"""RAGAS 兼容层与指标装配。

## 为什么需要兼容层

`ragas 0.4.3` 在 `ragas/llms/base.py` 里**无条件**导入
`langchain_community.chat_models.vertexai.ChatVertexAI`，
而本机 `langchain_community 0.4.2` 已移除该模块，导致 `import ragas` 直接失败：

    ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'

本项目使用本地 Ollama，完全不涉及 VertexAI。为「一个用不到的类」去降级整条
langchain 依赖链风险更大，因此这里在导入 ragas **之前**注入一个占位模块绕过该导入。

## 指标

指标选取与 `开发流程/06-测试与验收方案.md` §6 一致（六个）：

| 指标 | 含义 |
| --- | --- |
| Faithfulness | 回答是否忠实于检索上下文（无幻觉） |
| Answer Relevancy | 回答与问题的相关程度 |
| Context Precision | 检索上下文的精确率 |
| Context Recall | 检索上下文的召回率 |
| Answer Correctness | 回答与参考答案的一致度 |
| Context Entity Recall | 关键实体（错误码/版本号等）的召回情况 |
"""

import sys
import types


def _install_vertexai_shim() -> bool:
    """按需注入 `langchain_community.chat_models.vertexai` 占位模块。

    :return: 是否注入了 shim（False 表示环境本身已可用）。
    """
    if "langchain_community.chat_models.vertexai" in sys.modules:
        return False
    try:
        import langchain_community.chat_models.vertexai  # noqa: F401
        return False
    except ModuleNotFoundError:
        pass

    module = types.ModuleType("langchain_community.chat_models.vertexai")

    class ChatVertexAI:  # pragma: no cover - 仅满足 ragas 的导入，不会被实例化
        """占位类：本项目使用本地 Ollama，不使用 VertexAI。"""

    module.ChatVertexAI = ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = module
    return True


VERTEXAI_SHIM_INSTALLED = _install_vertexai_shim()

# --- 以下导入必须晚于 shim ---
from ragas.metrics import (  # noqa: E402
    AnswerCorrectness,
    AnswerRelevancy,
    ContextEntityRecall,
    ContextPrecision,
    ContextRecall,
    Faithfulness,
)

from app.core.config import get_settings  # noqa: E402

#: 指标名 → 中文说明（报告里用）
METRIC_LABELS: dict[str, str] = {
    "faithfulness": "忠实度（无幻觉）",
    "answer_relevancy": "回答相关性",
    "context_precision": "上下文精确率",
    "context_recall": "上下文召回率",
    "answer_correctness": "答案正确性",
    "context_entity_recall": "实体召回（错误码/版本号）",
}


#: 裁判上下文窗口；RAGAS 的 prompt 要装下多个检索片段，默认 2048 会被截断
JUDGE_NUM_CTX = 8192


def build_judge_llm(model: str | None = None, *, reasoning: bool = False):
    """构建 LLM 裁判（本地 Ollama，temperature=0 保证可复现）。

    :param reasoning: 是否允许思考模式。默认关闭 —— 实测 `qwen3.5:9b` 关闭后
        单次裁判调用从 **9.04s 降到 0.27s（约 33 倍）**，否则 6 个指标并行打分
        会全部撞上 RAGAS 的 180s 超时并返回 NaN。
    """
    from langchain_ollama import ChatOllama
    from ragas.llms import LangchainLLMWrapper

    settings = get_settings()
    llm = ChatOllama(
        model=model or settings.llm_model,
        base_url=settings.ollama_base_url,
        temperature=0,
        reasoning=reasoning,
        num_ctx=JUDGE_NUM_CTX,
    )
    return LangchainLLMWrapper(llm)


def build_judge_embeddings(model: str | None = None):
    """构建评估用向量模型（Answer Relevancy 需要）。"""
    from langchain_ollama import OllamaEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper

    settings = get_settings()
    embeddings = OllamaEmbeddings(
        model=model or settings.embedding_model,
        base_url=settings.ollama_base_url,
    )
    return LangchainEmbeddingsWrapper(embeddings)


def build_metrics(llm, embeddings) -> list:
    """装配六个指标（各自的裁判与向量模型均显式注入）。"""
    return [
        Faithfulness(llm=llm),
        AnswerRelevancy(llm=llm, embeddings=embeddings),
        ContextPrecision(llm=llm),
        ContextRecall(llm=llm),
        AnswerCorrectness(llm=llm, embeddings=embeddings),
        ContextEntityRecall(llm=llm),
    ]

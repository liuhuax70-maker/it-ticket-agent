"""Ollama 调用封装：本地 Qwen2.5，支持流式输出。

基础地址与模型名来自配置（OLLAMA_BASE_URL / LLM_MODEL）。
"""

from collections.abc import Iterator


def generate_stream(system: str, user: str) -> Iterator[str]:
    """流式生成，逐段 yield 文本增量（对接 SSE 的 token 事件）。"""
    # TODO(后续)：调用 ollama.chat(stream=True)，并处理重试与超时降级
    raise NotImplementedError("骨架占位：generate_stream 将在后续编码阶段实现")


def generate(system: str, user: str) -> str:
    """非流式生成，返回完整文本。"""
    # TODO(后续)：聚合流式结果或调用非流式接口
    raise NotImplementedError("骨架占位：generate 将在后续编码阶段实现")

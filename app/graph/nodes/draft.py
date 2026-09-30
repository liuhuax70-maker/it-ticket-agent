"""草稿生成节点：基于检索上下文调用 Qwen2.5 生成回复草稿（流式）。"""

from app.graph.state import TicketState


def draft_node(state: TicketState) -> dict:
    """返回 draft 与 citations。

    流式：token 通过 SSE 边生成边推送（见 `开发流程/04-检索与编排设计.md` §3.6）。
    """
    # TODO(后续)：调用 app.generation.ollama 生成，并回填引用
    raise NotImplementedError("骨架占位：draft_node 将在后续编码阶段实现")

"""SSE 事件编码。

事件契约见 `开发流程/05-接口与数据契约设计.md` §5：

    event: <type>
    id: <seq>
    data: <json>

每个事件以空行结束（`\\n\\n`）。
"""

import json

#: SSE 响应头（禁用缓冲，保证逐 token 实时到达）
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 反向代理（如 nginx）不缓冲
}


def format_sse(event: str, data: dict, event_id: int | None = None) -> str:
    """把一次事件编码为 SSE 文本块。"""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


class SSEWriter:
    """带自增 id 的事件编码器（仅供事件流生成器内部使用）。"""

    def __init__(self) -> None:
        self._seq = 0

    def event(self, event: str, data: dict) -> str:
        self._seq += 1
        return format_sse(event, data, self._seq)

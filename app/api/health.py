"""健康检查接口。

对依赖服务（Milvus / Ollama / Checkpointer）做短超时探测，
用于容器探活与运维排障（见 `开发流程/07-部署与运维方案.md`）。
"""

import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.schemas.common import ApiResponse

router = APIRouter(tags=["health"])

_PROBE_TIMEOUT = 1.0


async def _check_tcp(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> str:
    """TCP 连通性探测，返回 up / down。"""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - 关闭异常不影响判定
            pass
        return "up"
    except Exception:  # noqa: BLE001 - 任何异常都视为不可用
        return "down"


@router.get("/health", response_model=ApiResponse[dict], summary="健康检查")
async def health(request: Request) -> ApiResponse[dict]:
    settings = get_settings()
    ollama = urlparse(settings.ollama_base_url)
    ollama_port = ollama.port or (443 if ollama.scheme == "https" else 80)

    milvus_status, ollama_status = await asyncio.gather(
        _check_tcp(settings.milvus_host, settings.milvus_port),
        _check_tcp(ollama.hostname or "localhost", ollama_port),
    )

    deps = {
        "milvus": milvus_status,
        "ollama": ollama_status,
        "checkpointer": "up",  # TODO(后续)：随 Checkpointer 实现改为真实探测
    }

    if all(v == "up" for v in deps.values()):
        status = "healthy"
    elif any(v == "up" for v in deps.values()):
        status = "degraded"
    else:
        status = "unhealthy"

    return ApiResponse(
        code=0,
        message="ok",
        trace_id=getattr(request.state, "trace_id", None),
        data={"status": status, "dependencies": deps},
    )

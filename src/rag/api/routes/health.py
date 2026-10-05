"""健康检查：聚合 Milvus / LLM / Redis 三项依赖状态。

S0 的验收闸门就是它——依赖不通就不许往下写业务代码。
设计取舍：使用同步 def（FastAPI 会放进线程池执行），因为 pymilvus / redis 都是阻塞
SDK，写成 async 反而会阻塞事件循环；LLM 端点同理用同步 httpx。
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter
from pymilvus import MilvusClient
from redis import Redis

from config.settings import settings

router = APIRouter(tags=["health"])

OK = "ok"
ERROR = "error"
SKIP = "skip"


def _check_milvus() -> tuple[str, str]:
    """探测 Milvus 是否可连，返回 (状态, 说明)。"""
    client = None
    try:
        client = MilvusClient(uri=settings.milvus_uri, timeout=settings.milvus_timeout)
        return OK, f"uri={settings.milvus_uri} server={client.get_server_version()}"
    except Exception as exc:  # noqa: BLE001 - 健康检查需吞掉异常并如实上报
        return ERROR, f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响检查结论
                pass


def _check_llm() -> tuple[str, str]:
    """探测 OpenAI 兼容端点；未配置则跳过（不算故障）。"""
    if not settings.llm_base_url:
        return SKIP, "LLM_BASE_URL 未配置"
    url = settings.llm_base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
        return OK, f"endpoint={settings.llm_base_url} model={settings.llm_model or '-'}"
    except Exception as exc:  # noqa: BLE001
        return ERROR, f"{type(exc).__name__}: {exc}"


def _check_redis() -> tuple[str, str]:
    """探测 Redis；未启用缓存则跳过。"""
    if not settings.use_redis_cache:
        return SKIP, "USE_REDIS_CACHE=false"
    client = None
    try:
        client = Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        client.ping()
        return OK, f"url={settings.redis_url}"
    except Exception as exc:  # noqa: BLE001
        return ERROR, f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


@router.get("/health", summary="依赖健康检查")
def health() -> dict[str, Any]:
    """返回各依赖状态。

    状态取值：``ok`` / ``error`` / ``skip``（skip = 未启用，不算故障）。
    只要没有 error，整体即为 ``ok``。
    """
    checks = {
        "milvus": _check_milvus(),
        "llm": _check_llm(),
        "redis": _check_redis(),
    }
    return {
        "status": OK if all(status != ERROR for status, _ in checks.values()) else "degraded",
        "app_env": settings.app_env,
        "milvus": checks["milvus"][0],
        "llm": checks["llm"][0],
        "redis": checks["redis"][0],
        "details": {name: detail for name, (_, detail) in checks.items()},
    }

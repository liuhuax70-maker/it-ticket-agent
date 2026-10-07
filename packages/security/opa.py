"""OPA 策略决策客户端（fail-closed）。

**在用**：api-gateway 通过 ``require_action(...)`` 在 chat / documents / feedback
上调用它（见 ``apps/api-gateway/app/middleware/identity.py``）。
只有 ``AUTHZ_ENABLED=false`` 时才整体跳过（此时 ``allow()`` 直接返回放行）。

安全取向：以下三种情况**一律拒绝**，而不是放行——
    1. OPA 不可达或超时（连接失败、5xx）；
    2. 响应不是预期结构（result 既不是 bool 也不是对象）；
    3. result 是对象但没有 allow 字段。

宁可误拒也不要误放：拒绝的代价是一次失败请求，放行的代价是一次越权。
策略本身在 ``services/authz/policies/rag.rego``。
"""

from __future__ import annotations

from typing import Any

from packages.common.logging import get_logger
from packages.security.config import SecuritySettings

logger = get_logger("security.opa")


class OpaClient:
    def __init__(self, settings: SecuritySettings, *, enabled: bool | None = None) -> None:
        self._settings = settings
        self.enabled = settings.authz_enabled if enabled is None else enabled
        self._client: Any = None  # httpx.AsyncClient，懒创建（enabled=False 时不建）

    def _http(self) -> Any:
        """复用同一个 AsyncClient。

        曾经每次决策都 ``async with httpx.AsyncClient(...)`` 新建连接。在本机
        （服务间 URL 都写成 ``localhost``，而服务实际绑在 127.0.0.1），
        Windows 会先尝试 ::1 再回退，**每次新建连接都要付这一笔**——
        实测每个经过网关的请求因此固定多花约 0.65~1.1s，且观测手段完全看不到：
        编排器自己的 total 正常，只有端到端延迟虚高。
        """
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._settings.opa_timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def allow(self, input_doc: dict[str, Any]) -> tuple[bool, str]:
        """返回 ``(是否允许, 原因)``。"""
        if not self.enabled:
            return True, "authz disabled"

        url = f"{self._settings.opa_url.rstrip('/')}/{self._settings.opa_decision_path.lstrip('/')}"
        try:
            resp = await self._http().post(url, json={"input": input_doc})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("OPA 决策失败，按 fail-closed 拒绝: %s", exc)
            return False, f"opa unavailable: {exc}"

        result = payload.get("result")
        if isinstance(result, bool):
            return result, "opa allow" if result else "opa deny"
        if isinstance(result, dict):
            allowed = bool(result.get("allow", False))
            return allowed, str(result.get("reason", "opa decision"))
        logger.warning("OPA 返回了非预期结构: %r", result)
        return False, "opa malformed response"

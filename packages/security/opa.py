"""OPA 策略决策客户端（fail-closed）。

最小闭环不启用（AUTHZ_ENABLED=false，网关不会调用）；策略本身已写入
``services/authz/policies/rag.rego``，启用时无需改代码。

安全取向：OPA 不可达时**拒绝**而不是放行（fail-closed），
只有显式 ``enabled=False`` 才跳过决策。
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

    async def allow(self, input_doc: dict[str, Any]) -> tuple[bool, str]:
        """返回 ``(是否允许, 原因)``。"""
        if not self.enabled:
            return True, "authz disabled"

        import httpx

        url = f"{self._settings.opa_url.rstrip('/')}/{self._settings.opa_decision_path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self._settings.opa_timeout) as client:
                resp = await client.post(url, json={"input": input_doc})
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

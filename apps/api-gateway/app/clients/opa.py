"""OPA 客户端适配（网关侧接缝）。

实现位于 ``packages.security.opa``（fail-closed）；
此处只做一次再导出，让网关的依赖清单集中在本目录，
后续若需要给网关单独加缓存/批量决策，改动点也只在这一处。
"""

from __future__ import annotations

from packages.security.opa import OpaClient

__all__ = ["OpaClient"]

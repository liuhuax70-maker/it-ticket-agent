"""前端运行时配置端点。

存在的理由：OIDC 的 issuer 与浏览器 client_id 属于**部署环境**，不是代码。
前端早期把 ``http://localhost:8180/realms/rag`` 和 ``rag-ui`` 硬编码在 app.js 里，
于是换任何非 localhost 环境（容器、K8s、内网域名）部署，登录都会静默失败——
浏览器跳到一个根本不存在的 Keycloak。前端没有任何途径拿到真实值。

现在改成由后端下发，避免"改了 KEYCLOAK_URL 却忘了改 JS"这类漂移。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.config import Settings

router = APIRouter(tags=["ui"])


@router.get("/ui-config", summary="前端运行时配置（OIDC 端点与开关）")
async def ui_config(request: Request) -> dict[str, Any]:
    """GET /ui-config：下发前端启动所需的部署相关配置。

    ⚠️ 本端点**必须**在 ``authz_exempt()`` 的免鉴权名单里，否则形成死锁：
    未登录 → 前端拿不到 issuer → 无法发起登录 → 永远拿不到配置。
    与 ``/`` 同理（根路径是 302 到 /ui/，放行不泄露任何数据）。

    只下发前端**必须**知道的值。不要把 ``keycloak_client_secret``、数据库地址等
    任何服务端机密放进来——这个响应是匿名可读的。
    """
    settings: Settings = request.app.state.settings
    return {
        "authz_enabled": settings.authz_enabled,
        "oidc": {
            # 未开启鉴权时前端不需要走登录流程，给空串让它隐藏登录入口
            "issuer": settings.issuer() if settings.authz_enabled else "",
            "client_id": settings.keycloak_ui_client_id if settings.authz_enabled else "",
        },
        "max_upload_mb": settings.max_upload_mb,
    }
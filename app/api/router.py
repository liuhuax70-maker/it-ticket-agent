"""路由汇总：所有子路由在此聚合，由 main.py 挂载统一前缀。"""

from fastapi import APIRouter

from app.api import health, retrieval, session, ticket

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(ticket.router)
api_router.include_router(session.router)
api_router.include_router(retrieval.router)

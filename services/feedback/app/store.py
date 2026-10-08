"""反馈存储（Postgres）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from packages.common.db import session_scope
from packages.common.logging import get_logger
from packages.common.models import Feedback

logger = get_logger("feedback.store")


class FeedbackStore:
    """反馈落库与坏例查询：Postgres 单表，按租户/时间排序；坏例即点踩记录。"""

    def __init__(self, database_url: str) -> None:
        self._url = database_url

    async def add(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        answer: str,
        rating: int | None,
        comment: str | None,
        trace_id: str | None,
    ) -> tuple[str, bool]:
        """写入一条反馈，返回 ``(主键 id, 是否重复)``。

        去重：同一 ``trace_id`` 只保留第一条，后续返回 ``duplicate``。
        这与 ``FeedbackResponse.status`` 的取值一致——契约早就声明了
        ``duplicate``，但这里此前是**无条件插入**，于是契约承诺的去重从未生效，
        连点两次反馈会落两行（实测确认）。

        ⚠️ 只在 ``trace_id`` 非空时去重：它是"同一次问答"的标识。
        trace_id 为空（调用方没带）时无法判断是否同一次问答，此时必须各存一条——
        否则所有匿名反馈会互相挤掉，只剩第一条。

        这是**应用层**去重，并发提交同一 trace_id 仍有极小窗口会写两行。
        真要严格保证需要 ``trace_id`` 上的唯一约束（alembic 迁移）。
        反馈是低频人工操作，且前端已做按钮防重，这个窗口可以接受；
        之所以不在此处上约束：迁移会给已有表加索引，而这条路径的收益并不匹配代价。
        """
        async with session_scope(self._url) as session:
            if trace_id:
                existing = await session.scalar(
                    select(Feedback.id).where(Feedback.trace_id == trace_id).limit(1)
                )
                if existing is not None:
                    existing_id = str(existing)
                    logger.info("反馈重复，沿用已有记录 trace=%s id=%s", trace_id, existing_id)
                    return existing_id, True

            row = Feedback(
                tenant_id=tenant_id,
                user_id=user_id,
                query=query,
                answer=answer,
                rating=rating,
                comment=comment,
                trace_id=trace_id,
            )
            session.add(row)
            await session.flush()
            feedback_id = str(row.id)
        logger.info("收到反馈 tenant=%s rating=%s id=%s", tenant_id, rating, feedback_id)
        return feedback_id, False

    async def list_recent(
        self, *, tenant_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """最近反馈（按时间倒序），可选按租户过滤。"""
        async with session_scope(self._url) as session:
            stmt = select(Feedback).order_by(Feedback.created_at.desc()).limit(limit)
            if tenant_id:
                stmt = stmt.where(Feedback.tenant_id == tenant_id)
            rows = (await session.scalars(stmt)).all()
            return [self._to_dict(row) for row in rows]

    async def list_bad_cases(
        self, *, limit: int = 100, max_rating: int = -1, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """坏例 = 点踩（rating <= max_rating）的记录。"""
        async with session_scope(self._url) as session:
            stmt = (
                select(Feedback)
                .where(Feedback.rating.is_not(None))
                .where(Feedback.rating <= max_rating)
                .order_by(Feedback.created_at.desc())
                .limit(limit)
            )
            if tenant_id:
                stmt = stmt.where(Feedback.tenant_id == tenant_id)
            rows = (await session.scalars(stmt)).all()
            return [self._to_dict(row) for row in rows]

    @staticmethod
    def _to_dict(row: Feedback) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "tenant_id": row.tenant_id,
            "user_id": row.user_id,
            "query": row.query,
            "answer": row.answer,
            "rating": row.rating,
            "comment": row.comment,
            "trace_id": row.trace_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    async def health(self) -> tuple[bool, str]:
        """Postgres 健康探测（取 1 条样本确认可达）。"""
        try:
            rows = await self.list_recent(limit=1)
            return True, f"reachable (sample={len(rows)})"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

"""反馈存储（Postgres）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from packages.common.db import session_scope
from packages.common.logging import get_logger
from packages.common.models import Feedback

logger = get_logger("feedback.store")


class FeedbackStore:
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
    ) -> str:
        async with session_scope(self._url) as session:
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
        return feedback_id

    async def list_recent(
        self, *, tenant_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
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
        try:
            rows = await self.list_recent(limit=1)
            return True, f"reachable (sample={len(rows)})"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

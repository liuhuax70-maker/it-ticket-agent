"""坏例沉淀：把点踩记录导出为评测数据集种子。

为什么要把坏例导出成 jsonl：反馈留在数据库里只是「记录」，
进入 eval 数据集才是「回归防线」——同一类错误不应该出现第二次。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.store import FeedbackStore
from packages.common.logging import get_logger

logger = get_logger("feedback.bad_cases")


class BadCaseCollector:
    def __init__(self, store: FeedbackStore, output_dir: str) -> None:
        self._store = store
        self._dir = Path(output_dir)

    async def collect(self, *, limit: int = 100, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return await self._store.list_bad_cases(limit=limit, tenant_id=tenant_id)

    async def export(
        self, *, limit: int = 100, tenant_id: str | None = None, filename: str = "bad_cases.jsonl"
    ) -> Path:
        cases = await self.collect(limit=limit, tenant_id=tenant_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / filename
        with target.open("w", encoding="utf-8") as handle:
            for case in cases:
                # 只导出评测所需的字段，不带 user_id（避免把个人标识带进数据集）
                handle.write(
                    json.dumps(
                        {
                            "question": case["query"],
                            "answer": case["answer"],
                            "reference": None,
                            "comment": case["comment"],
                            "trace_id": case["trace_id"],
                            "source": "user_feedback",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        logger.info("已导出坏例 %s 条 -> %s", len(cases), target)
        return target

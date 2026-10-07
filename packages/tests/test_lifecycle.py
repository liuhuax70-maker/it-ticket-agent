"""文档生命周期判定测试。

重点是**边界**：生效当天/失效当天算不算有效、显式废止与日期谁优先、
以及"未声明"绝不能改变既有行为（存量文档不做迁移就能继续工作）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from packages.common.constants import LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED
from packages.common.lifecycle import LifecycleDeclaration, load_declarations, resolve

TODAY = dt.date(2026, 6, 1)


def test_undeclared_documents_stay_active() -> None:
    """未声明 = 现行有效。存量文档不做任何迁移就能继续工作。"""
    state = resolve("anything.md", None, today=TODAY)
    assert state.lifecycle == LIFECYCLE_ACTIVE
    assert resolve("other.md", {}, today=TODAY).lifecycle == LIFECYCLE_ACTIVE


def test_explicitly_retired_is_excluded() -> None:
    declarations = {"old.md": LifecycleDeclaration(status=LIFECYCLE_RETIRED, note="被新版取代")}
    state = resolve("old.md", declarations, today=TODAY)
    assert state.lifecycle == LIFECYCLE_RETIRED
    assert "被新版取代" in state.reason


def test_past_effective_to_is_retired() -> None:
    declarations = {"old.md": LifecycleDeclaration(effective_to="2025-12-31")}
    assert resolve("old.md", declarations, today=TODAY).lifecycle == LIFECYCLE_RETIRED


def test_future_effective_from_is_retired() -> None:
    """未生效的文档不应被引用——提前写进语料是常态，但检索不能提前放出。"""
    declarations = {"soon.md": LifecycleDeclaration(effective_from="2027-01-01")}
    assert resolve("soon.md", declarations, today=TODAY).lifecycle == LIFECYCLE_RETIRED


def test_boundary_dates_are_inclusive() -> None:
    """生效日当天生效、失效日当天仍有效——边界取闭区间。

    取闭区间是为了少一个"差一天"的坑：制度通常写"自 X 日起施行""至 Y 日止有效"，
    按中文语义两端都含。
    """
    declarations = {
        "start.md": LifecycleDeclaration(effective_from="2026-06-01"),
        "end.md": LifecycleDeclaration(effective_to="2026-06-01"),
    }
    assert resolve("start.md", declarations, today=TODAY).lifecycle == LIFECYCLE_ACTIVE
    assert resolve("end.md", declarations, today=TODAY).lifecycle == LIFECYCLE_ACTIVE


def test_within_window_is_active() -> None:
    declarations = {
        "a.md": LifecycleDeclaration(effective_from="2026-01-01", effective_to="2026-12-31")
    }
    assert resolve("a.md", declarations, today=TODAY).lifecycle == LIFECYCLE_ACTIVE


def test_explicit_retired_beats_date_calculation() -> None:
    """显式废止优先于日期推算：它是人的明确决定。"""
    declarations = {
        "a.md": LifecycleDeclaration(
            status=LIFECYCLE_RETIRED, effective_from="2026-01-01", effective_to="2030-01-01"
        )
    }
    assert resolve("a.md", declarations, today=TODAY).lifecycle == LIFECYCLE_RETIRED


def test_details_are_kept_for_the_ledger() -> None:
    declarations = {
        "old.md": LifecycleDeclaration(
            status=LIFECYCLE_RETIRED, effective_to="2025-12-31", superseded_by="差旅管理制度"
        )
    }
    state = resolve("old.md", declarations, today=TODAY)
    assert state.details["superseded_by"] == "差旅管理制度"
    assert state.details["effective_to"] == "2025-12-31"


def test_invalid_date_fails_loudly() -> None:
    """日期写错必须报错：静默忽略会让"声明了失效日期却永不生效"。"""
    declarations = {"a.md": LifecycleDeclaration(effective_to="2025/12/31")}
    with pytest.raises(ValueError, match="无效的日期"):
        resolve("a.md", declarations, today=TODAY)


def test_load_declarations_missing_file_is_empty() -> None:
    assert load_declarations("configs/corpus/does-not-exist.yaml") == {}


def test_load_declarations_reads_repo_config(tmp_path) -> None:
    path = tmp_path / "lifecycle.yaml"
    path.write_text(
        "documents:\n"
        "  old.md:\n"
        "    status: retired\n"
        "    effective_to: '2025-12-31'\n"
        "    superseded_by: 差旅管理制度\n"
        "    note: 已被取代\n",
        encoding="utf-8",
    )
    declarations = load_declarations(path)
    assert "old.md" in declarations
    assert declarations["old.md"].status == LIFECYCLE_RETIRED
    assert declarations["old.md"].superseded_by == "差旅管理制度"
    assert resolve("old.md", declarations, today=TODAY).lifecycle == LIFECYCLE_RETIRED

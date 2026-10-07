"""文档生命周期（失效管理）的声明与判定。

存在理由：制度文档被取代后，旧版本**仍会被检索、仍会被引用**——
用户拿到的是一份已经作废的规定，而系统无从知道它已失效。
越权有 ACL 兜着，"过期"没有人兜，只能靠显式的生命周期状态。

设计取舍：

* **声明式**（`configs/corpus/lifecycle.yaml`）而不是"调接口改状态"：
  语料生命周期跟语料本身一样是**配置**，放进版本库才能被 review、
  才能被一致性检查发现"这份文档的失效日期已经过了"。
  只有声明式才能同时满足"可见"与"可治理"。
* **状态只有 active / retired 两种**：区分 superseded / expired / draft
  对检索过滤没有任何区别，多一个状态就多一处需要同步维护的语义。
  细分的理由放进 ``superseded_by`` / ``note``，供人查看，不参与过滤。
* **按文件名匹配**而不是完整路径：上传入库的文件其 source 会被改写
  （`data/uploads/<原名>`），按路径匹配必然对不上。
  代价：同名文件会共享声明——语料里刻意没有同名文件，且有检查兜着。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from packages.common.constants import LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED


@dataclass(frozen=True)
class LifecycleDeclaration:
    """一份文档的生命周期声明。日期用 ISO 格式（YYYY-MM-DD）。"""

    status: str = LIFECYCLE_ACTIVE
    effective_from: str | None = None
    effective_to: str | None = None
    superseded_by: str | None = None
    note: str = ""


@dataclass(frozen=True)
class LifecycleState:
    """声明在今天这一天的判定结果。"""

    lifecycle: str
    reason: str = ""  # 为什么是这个状态（入库日志与台账里都要能看出原因）
    details: dict[str, str] = field(default_factory=dict)


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        # 日期写错必须显式失败：静默忽略会让"声明了失效日期却永不生效"，
        # 那样这份文档会被当成永久有效——正是本机制要防止的事故。
        raise ValueError(f"无效的日期：{value!r}（应为 YYYY-MM-DD）") from None


def load_declarations(path: str | Path) -> dict[str, LifecycleDeclaration]:
    """读取声明文件，返回 {文件名: 声明}。文件不存在时返回空字典。

    空字典表示"全部按现行有效处理"——不加声明不应改变任何既有行为。
    """
    target = Path(path)
    if not target.exists():
        return {}
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    entries = raw.get("documents") or {}
    out: dict[str, LifecycleDeclaration] = {}
    for filename, value in entries.items():
        value = value or {}
        out[str(filename)] = LifecycleDeclaration(
            status=str(value.get("status", LIFECYCLE_ACTIVE)).strip() or LIFECYCLE_ACTIVE,
            effective_from=value.get("effective_from"),
            effective_to=value.get("effective_to"),
            superseded_by=value.get("superseded_by"),
            note=str(value.get("note", "")),
        )
    return out


def resolve(
    filename: str,
    declarations: dict[str, LifecycleDeclaration] | None,
    *,
    today: dt.date | None = None,
) -> LifecycleState:
    """判定某份文档今天的状态。

    判定顺序：显式废止 > 已过有效期 > 尚未生效 > 现行有效。
    显式废止放最前：它是人的明确决定，优先于日期推算。
    """
    today = today or dt.date.today()
    declaration = (declarations or {}).get(filename)
    if declaration is None:
        # 未声明 = 现行有效。存量文档不做任何迁移就能继续工作。
        return LifecycleState(LIFECYCLE_ACTIVE, reason="未声明，按现行有效处理")

    details = {
        key: str(value)
        for key, value in (
            ("effective_from", declaration.effective_from),
            ("effective_to", declaration.effective_to),
            ("superseded_by", declaration.superseded_by),
        )
        if value
    }

    if declaration.status == LIFECYCLE_RETIRED:
        return LifecycleState(
            LIFECYCLE_RETIRED,
            reason=f"已废止：{declaration.note or '声明为 retired'}",
            details=details,
        )

    effective_from = _parse_date(declaration.effective_from)
    effective_to = _parse_date(declaration.effective_to)

    if effective_to is not None and today > effective_to:
        return LifecycleState(
            LIFECYCLE_RETIRED,
            reason=f"已过失效日期 {effective_to}（今天 {today}）",
            details=details,
        )
    if effective_from is not None and today < effective_from:
        return LifecycleState(
            LIFECYCLE_RETIRED,
            reason=f"尚未生效（生效日期 {effective_from}，今天 {today}）",
            details=details,
        )

    return LifecycleState(LIFECYCLE_ACTIVE, reason="现行有效", details=details)


__all__ = ["LifecycleDeclaration", "LifecycleState", "load_declarations", "resolve"]

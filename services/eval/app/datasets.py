"""评测数据集：golden set（黄金集）与坏例回归集。

格式（jsonl，一行一个样本）：
    {"question": "...", "reference": "...", "tags": ["hr", "福利"]}

``reference``（标准答案）用于 context_recall 等需要参考答案的指标；
没有参考答案的坏例样本 ``reference`` 为 null，只参与不需要参考答案的指标。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from packages.common.errors import ConfigError, NotFoundError
from packages.common.logging import get_logger

logger = get_logger("eval.datasets")


class GoldenSample(BaseModel):
    question: str
    reference: str | None = None
    tags: list[str] = Field(default_factory=list)


# 种子集：与 data/corpus/employee_handbook.md 对齐，verify_loop 的正样本同源。
# 首次运行时会落盘成 eval_data/golden.jsonl，后续直接编辑该文件即可。
SEED_SAMPLES: list[GoldenSample] = [
    GoldenSample(
        question="入职体检费用怎么报销？",
        reference="员工转正后凭体检机构开具的正式发票，通过报销系统提交，报销上限为五百元。",
        tags=["hr", "福利"],
    ),
    GoldenSample(
        question="入职体检报销需要在多久内提交？",
        reference="需在转正后三十日内提交，逾期不再受理。",
        tags=["hr", "福利"],
    ),
    GoldenSample(
        question="年假有多少天？",
        reference="司龄一至三年每年五天，三至五年每年十天，五年以上每年十五天。",
        tags=["hr", "休假"],
    ),
    GoldenSample(
        question="年假用不完可以结转到明年吗？",
        reference="因工作原因无法休完的，经部门负责人批准可结转至次年第一季度。",
        tags=["hr", "休假"],
    ),
    GoldenSample(
        question="外部培训费用超过三千元需要谁审批？",
        reference="须由部门负责人及人力资源部共同审批。",
        tags=["hr", "培训"],
    ),
    GoldenSample(
        question="离职后保密义务还有效吗？",
        reference="离职后保密义务继续有效，期限为离职后三年。",
        tags=["hr", "保密"],
    ),
]


def ensure_dataset(path: str | Path) -> Path:
    """数据集不存在时落盘种子集，避免第一次跑评测就以「文件不存在」失败。"""
    target = Path(path)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for sample in SEED_SAMPLES:
            handle.write(sample.model_dump_json() + "\n")
    logger.info("已生成种子评测集（%s 条）-> %s", len(SEED_SAMPLES), target)
    return target


def load_samples(path: str | Path, *, limit: int | None = None) -> list[GoldenSample]:
    target = ensure_dataset(path)
    if not target.exists():
        raise NotFoundError(f"评测集不存在: {target}")

    samples: list[GoldenSample] = []
    with target.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                samples.append(GoldenSample.model_validate(json.loads(line)))
            except Exception as exc:  # noqa: BLE001
                raise ConfigError(f"{target}:{lineno} 样本格式非法: {exc}") from exc

    if not samples:
        raise ConfigError(f"评测集为空: {target}")
    if limit:
        samples = samples[:limit]
    return samples

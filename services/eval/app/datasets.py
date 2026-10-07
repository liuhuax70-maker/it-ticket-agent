"""评测数据集：golden set（黄金集）与坏例回归集。

为什么要比"问题 + 参考答案"多三组字段：

    ``expected_sources``   期望命中的来源文件。用它算 hit@k / MRR，**确定性**、不花裁判模型的钱。
                           注意存的是**文件路径**而不是 doc_id：doc_id 是路径的哈希，
                           写死在数据集里既不可读，也会在换语料时静默失效。
    ``expected_snippets``  期望出现在召回上下文里的原文片段。比按 doc_id 判定更宽容，
                           用来算不依赖 LLM 的 context recall；片段是原文，不依赖切分参数，
                           改 chunk_size 后仍然有效。
    ``identity``           以什么身份提问。**这是本项目评测集与普通 RAG 评测集的根本差别**：
                           同一个问题在不同身份下的正确行为不同（有权限→作答，无权限→拒答），
                           没有身份维度的评测集无法发现越权。

字段 ``forbidden_sources`` 是显式的越权断言；此外采集器还会按台账里的真实 ACL
自动推导每个身份的可见集合，做一次兜底的全量泄露检查。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from packages.common.errors import ConfigError, NotFoundError
from packages.common.ids import stable_doc_id
from packages.common.logging import get_logger

logger = get_logger("eval.datasets")

# 默认身份：default 租户的 HR 员工（可见全部内部文档 + HR 部门文档）
DEFAULT_IDENTITY = {"username": "alice", "tenant_id": "default", "department_id": "hr"}


class EvalIdentity(BaseModel):
    """提问身份画像。``username`` 对应 Keycloak 账号，开启鉴权时用它换令牌。"""

    username: str = DEFAULT_IDENTITY["username"]
    tenant_id: str = DEFAULT_IDENTITY["tenant_id"]
    department_id: str = DEFAULT_IDENTITY["department_id"]
    user_id: str = "u_eval"  # 仅无鉴权模式回退用

    def describe(self) -> str:
        return f"{self.username}@{self.tenant_id}/{self.department_id}"


class GoldenSample(BaseModel):
    id: str
    question: str
    reference: str | None = None
    expected_sources: list[str] = Field(default_factory=list)
    expected_snippets: list[str] = Field(default_factory=list)
    forbidden_sources: list[str] = Field(default_factory=list)
    # 答案里**绝不能出现**的字符串。用于"必须挡住了什么"这类断言——
    # 目前主要给提示注入用：投毒文档/提问里埋一个标记串，答案一旦出现它就说明模型照做了。
    # 与 forbidden_sources 是两件事：那个管"不该引用的文档"，这个管"不该出现的内容"。
    must_not_contain: list[str] = Field(default_factory=list)
    should_refuse: bool = False
    identity: EvalIdentity = Field(default_factory=EvalIdentity)
    tags: list[str] = Field(default_factory=list)

    def expected_doc_ids(self) -> set[str]:
        """按来源路径**直接**派生 doc_id。

        注意：这只是"路径恰好等于入库 source"时的快捷算法，**不是权威来源**。
        通过上传接口入库的文件，其 source 会被改写成上传目录下的路径，
        因此采集器实际用的是 ``collector.resolve_sources``（按台账解析，完整路径优先、
        文件名兜底）。这里保留该方法是为了离线推导与单测方便。
        """
        return {stable_doc_id(source) for source in self.expected_sources}

    def forbidden_doc_ids(self) -> set[str]:
        """同上：非权威，采集器会按台账重新解析并叠加 ACL 推导结果。"""
        return {stable_doc_id(source) for source in self.forbidden_sources}


def load_samples(path: str | Path, *, limit: int | None = None) -> list[GoldenSample]:
    import json

    target = Path(path)
    if not target.exists():
        raise NotFoundError(f"评测集不存在: {target}（格式参考仓库内的 configs/eval/golden.jsonl）")

    samples: list[GoldenSample] = []
    seen: set[str] = set()
    with target.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                sample = GoldenSample.model_validate(json.loads(line))
            except Exception as exc:  # noqa: BLE001
                raise ConfigError(f"{target}:{lineno} 样本格式非法: {exc}") from exc
            if sample.id in seen:
                raise ConfigError(f"{target}:{lineno} 样本 id 重复: {sample.id}")
            seen.add(sample.id)
            samples.append(sample)

    if not samples:
        raise ConfigError(f"评测集为空: {target}")
    pos = sum(1 for s in samples if not s.should_refuse)
    neg = len(samples) - pos
    logger.info("已加载评测集 %s 条（正样本 %s / 负样本 %s）<- %s", len(samples), pos, neg, target)
    if limit:
        samples = samples[:limit]
    return samples


__all__ = ["DEFAULT_IDENTITY", "EvalIdentity", "GoldenSample", "load_samples"]

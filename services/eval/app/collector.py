"""评测采集：把被测系统的真实响应（答案 + 上下文 + 引用）抓回来。

这是评测里最容易被做错的一环，三个坑都在这里堵掉：

1. **身份**：逐条样本用各自的 Keycloak 身份取令牌。如果整轮评测都用同一个"管理员"身份，
   权限相关的样本就全是假阳性——越权检索在管理员身份下本来就不会发生。
2. **越权禁止集合**：不能只靠数据集里手写的 ``forbidden_sources``（写漏一条就漏检），
   还要按台账里的真实 ACL 自动推导：**租户不符**、**部门不符**、以及**别人的 private 文档**
   一律计入禁止集合。
3. **前置检查**：语料没入库就直接跑，会得到一片 hit@k=0，容易被误读成"检索退化了"。
   所以先核对期望来源是否都在台账里，缺了就报错并给出修复命令。
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import Any

import httpx

from app.config import Settings
from app.datasets import EvalIdentity, GoldenSample
from packages.common.errors import ConfigError
from packages.common.logging import get_logger
from packages.contracts import ChatRequest
from packages.security import TokenProvider

logger = get_logger("eval.collector")


async def _fetch_ledger(settings: Settings, tokens: TokenProvider) -> dict[str, dict[str, Any]]:
    """拉取文档台账（doc_id -> 元数据），失败返回空表并告警。"""
    headers: dict[str, str] = {}
    token = await tokens.token(settings.ledger_username)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{settings.api_gateway_url.rstrip('/')}/documents",
                params={"limit": settings.ledger_limit},
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉取文档台账失败，将只依赖数据集里显式声明的禁止来源: %s", exc)
        return {}

    return {str(item["doc_id"]): item for item in items}


def resolve_sources(
    sources: list[str] | set[str], ledger: dict[str, dict[str, Any]]
) -> tuple[dict[str, str], list[str]]:
    """把评测集里声明的来源路径解析成 doc_id。

    为什么不能直接 ``stable_doc_id(路径)``：
    通过**上传接口**入库的文件，其 ``source`` 会被改写成上传目录下的路径
    （``data/uploads/hr_policy.md``），而评测集里写的是语料夹具路径
    （``data/corpus_permissions/hr_policy.md``）。两者派生出的 doc_id 不同，
    直接算就会把所有权限样本判成"没命中 + 越权"。

    解析规则：
        1. 先按完整路径精确匹配；
        2. 匹配不到再按**文件名**兜底（唯一同名才算，同名的多份会报错要求写全路径）。

    宁可报错也不要猜：一条来源指错文档，越权断言就会静默失效。
    """
    by_source = {str(meta.get("source", "")): doc_id for doc_id, meta in ledger.items()}
    by_basename: dict[str, list[str]] = {}
    for doc_id, meta in ledger.items():
        name = PurePosixPath(str(meta.get("source", ""))).name
        if name:
            by_basename.setdefault(name, []).append(doc_id)

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for source in sorted(sources):
        if source in by_source:
            resolved[source] = by_source[source]
            continue
        candidates = by_basename.get(PurePosixPath(source).name, [])
        if len(candidates) == 1:
            resolved[source] = candidates[0]
            continue
        if len(candidates) > 1:
            logger.error("来源 %s 在台账里匹配到多份同名文档，请在评测集中改用完整路径", source)
        missing.append(source)
    return resolved, missing


def _acl_forbidden_doc_ids(
    ledger: dict[str, dict[str, Any]], identity: EvalIdentity, expected_doc_ids: set[str]
) -> set[str]:
    """按真实 ACL 推导该身份**不该看到**的文档集合。

    private 文档的 owner 不在台账里，因此采用保守规则：
    除非这条样本明确把它写进 ``expected_sources``，否则一律视为不可见。
    这样"别人的私有文档"不会因为信息缺失而漏检。
    """
    forbidden: set[str] = set()
    for doc_id, meta in ledger.items():
        visibility = str(meta.get("visibility", ""))
        if str(meta.get("tenant_id", "")) != identity.tenant_id:
            forbidden.add(doc_id)
            continue
        if visibility == "department" and str(meta.get("department_id", "")) != identity.department_id:
            forbidden.add(doc_id)
            continue
        if visibility == "private" and doc_id not in expected_doc_ids:
            forbidden.add(doc_id)
    return forbidden


async def preflight(samples: list[GoldenSample], settings: Settings) -> dict[str, Any]:
    """采集前置检查：语料是否入库、鉴权是否可用。"""
    tokens = TokenProvider(settings)
    try:
        ledger = await _fetch_ledger(settings, tokens)
        authz_mode = "token" if await tokens.token(settings.ledger_username) else "fixed-identity"

        declared = {
            source
            for sample in samples
            for source in [*sample.expected_sources, *sample.forbidden_sources]
        }
        _resolved, missing = resolve_sources(declared, ledger)
        missing = sorted(missing)

        # 同一份来源可能被多个身份期望命中，这里按来源汇总，便于一次性补齐
        summary = {
            "authz_mode": authz_mode,
            "ledger_size": len(ledger),
            "declared_sources": sorted(declared),
            "missing_sources": missing,
            "sample_count": len(samples),
            "identities": sorted({s.identity.describe() for s in samples}),
        }
        if missing:
            logger.error(
                "有 %s 份期望来源不在知识库中，请先执行语料准备：%s",
                len(missing),
                missing,
            )
        return summary
    finally:
        await tokens.aclose()


def _missing_sources_error(summary: dict[str, Any]) -> str:
    lines = [
        "评测语料未就绪，以下期望来源不在知识库台账中：",
        *(f"  - {source}" for source in summary["missing_sources"]),
        "",
        "先准备语料，再重跑评测：",
        "  python scripts/prepare_corpus.py          # 通用语料 + 权限语料",
        "  python scripts/verify_permissions.py      # 仅准备权限语料（含身份与 ACL）",
    ]
    return "\n".join(lines)


async def collect(samples: list[GoldenSample], settings: Settings) -> list[dict[str, Any]]:
    """逐条采集。返回的行同时携带 L1 判定所需的全部信息。"""
    tokens = TokenProvider(settings)
    ledger = await _fetch_ledger(settings, tokens)
    authz_available = bool(await tokens.token(settings.ledger_username))
    if not authz_available:
        logger.warning(
            "取不到 Keycloak 令牌，回退为固定身份请求头——"
            "此时**权限类样本不具备验证意义**，请不要据此判断越权行为"
        )

    declared = {
        source
        for sample in samples
        for source in [*sample.expected_sources, *sample.forbidden_sources]
    }
    resolved, unresolved = resolve_sources(declared, ledger)
    if unresolved:
        raise ConfigError(_missing_sources_error({"missing_sources": sorted(unresolved)}))

    rows: list[dict[str, Any]] = []
    gateway = settings.api_gateway_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        for index, sample in enumerate(samples, start=1):
            headers: dict[str, str] = {}
            token = await tokens.token(sample.identity.username)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers.update(
                    {
                        "x-tenant-id": sample.identity.tenant_id,
                        "x-department-id": sample.identity.department_id,
                        "x-user-id": sample.identity.user_id,
                    }
                )

            payload = ChatRequest(
                query=sample.question,
                include_contexts=True,
                top_k=settings.top_k,
            ).model_dump(mode="json")

            started = time.perf_counter()
            error: str | None = None
            body: dict[str, Any] = {}
            try:
                resp = await client.post(f"{gateway}/chat", json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                error = f"{exc.__class__.__name__}: {exc}"
                logger.error("采集失败 sample=%s err=%s", sample.id, error)

            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            citations = body.get("citations") or []
            contexts = [str(item.get("text", "")) for item in (body.get("contexts") or [])]
            if not contexts:
                # 网关未返回上下文时（如拒答路径）用引用片段兜底，
                # 保证片段召回仍有可判定的依据
                contexts = [str(c.get("snippet", "")) for c in citations]

            expected_doc_ids = {resolved[s] for s in sample.expected_sources if s in resolved}
            forbidden = {
                resolved[s] for s in sample.forbidden_sources if s in resolved
            } | _acl_forbidden_doc_ids(ledger, sample.identity, expected_doc_ids)

            rows.append(
                {
                    "sample_id": sample.id,
                    "question": sample.question,
                    "reference": sample.reference,
                    "identity": sample.identity.describe(),
                    "tags": sample.tags,
                    "should_refuse": sample.should_refuse,
                    "expected_doc_ids": sorted(expected_doc_ids),
                    "expected_snippets": sample.expected_snippets,
                    "forbidden_doc_ids": sorted(forbidden),
                    "answer": body.get("answer", ""),
                    "refused": bool(body.get("refused")),
                    "cached": bool(body.get("cached")),
                    "citations": citations,
                    "contexts": contexts,
                    "latency_ms": latency_ms,
                    "timings_ms": body.get("timings_ms") or {},
                    "error": error,
                }
            )
            logger.info(
                "[%s/%s] %s identity=%s citations=%s refused=%s %sms",
                index,
                len(samples),
                sample.id,
                sample.identity.username,
                len(citations),
                bool(body.get("refused")),
                latency_ms,
            )

    await tokens.aclose()
    if not ledger:
        raise ConfigError(
            "无法读取文档台账（/documents 不可用），拒绝继续："
            "缺少台账就无法做越权兜底检查，此时评测结果不可信"
        )
    return rows


__all__ = ["_acl_forbidden_doc_ids", "_missing_sources_error", "collect", "preflight"]

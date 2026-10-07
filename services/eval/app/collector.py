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
from typing import Any, NamedTuple

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
        if (
            visibility == "department"
            and str(meta.get("department_id", "")) != identity.department_id
        ):
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


async def _identity_headers(tokens: TokenProvider, identity: EvalIdentity) -> dict[str, str]:
    """按样本身份构造请求头。

    取不到令牌时回退为固定身份头——注意这会让**权限类样本失去验证意义**
    （``preflight`` 与报告里的 ``authz_mode`` 会如实标出这种降级）。
    """
    token = await tokens.token(identity.username)
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {
        "x-tenant-id": identity.tenant_id,
        "x-department-id": identity.department_id,
        "x-user-id": identity.user_id,
    }


def _chat_payload(sample: GoldenSample, settings: Settings) -> dict[str, Any]:
    return ChatRequest(
        query=sample.question,
        include_contexts=True,
        top_k=settings.top_k,
        temperature=settings.answer_temperature,
        # 必须绕过缓存，两个理由都不是"想看慢一点的数"：
        # ① 命中响应里没有 contexts -> 检索侧指标（NDCG / 检索侧 MRR）只能把这些行
        #    排除出分母，缓存越多分母越小，两轮评测的 NDCG 就不可比；
        # ② 评测若允许写缓存，会把评测流量灌进生产缓存，让**下一次**评测拿到一堆命中。
        use_cache=False,
    ).model_dump(mode="json")


async def _ask(
    client: httpx.AsyncClient,
    gateway: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    sample_id: str,
) -> tuple[dict[str, Any], str | None, float]:
    """发一次 /chat。返回 (响应体, 错误信息, 耗时毫秒)。

    单条失败不中断整轮：失败信息带进该行，由指标层统计成 ``error_count``。
    """
    started = time.perf_counter()
    try:
        resp = await client.post(f"{gateway}/chat", json=payload, headers=headers)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        error = None
    except Exception as exc:  # noqa: BLE001
        body = {}
        error = f"{exc.__class__.__name__}: {exc}"
        logger.error("采集失败 sample=%s err=%s", sample_id, error)
    return body, error, round((time.perf_counter() - started) * 1000, 1)


class _Extracted(NamedTuple):
    """一次采集里"能看到什么"的四份数据。

    ``contexts`` 与 ``chunk_doc_ids`` **等长且同序**——正因如此才能把每个召回分块
    归属到它的文档，而不必把分块文本再抄一遍（报告里文本已经有 ``contexts``）。
    """

    contexts: list[str]
    citations: list[dict[str, Any]]
    # 检索侧 doc 名次（去重、保持首次出现位置）；缓存命中/无 contexts 时为空
    retrieved_doc_ids: list[str]
    # 与 contexts 等长同序：该分块属于哪篇文档
    chunk_doc_ids: list[str]


def _extract_contexts(body: dict[str, Any]) -> _Extracted:
    """取出上下文文本、引用列表，以及**检索侧**的 doc 名次。

    检索名次与引用顺序**刻意不同源**，这是本函数存在的理由：

    ``citations`` 的顺序是**生成侧**的选择——模型挑哪几条引用、按什么顺序排列。
    而 NDCG 与检索侧 MRR 要量的是**检索侧**的名次（RRF 融合 / 重排后的顺序，
    由重排节点的 ``build_context_items(hits)`` 按 hits 顺序构建）。
    两者混用会把"模型引用顺序"当成"检索排序质量"，指标含义就错了。

    所以 ``retrieved_doc_ids`` 只从**真正的** ``contexts`` 取，且**不做引用兜底**：
    缓存命中的请求根本不返回 contexts，那种行应该被排除在检索侧指标之外，
    而不是拿引用顺序顶替——否则缓存越多，检索侧指标越"好看"。
    """
    citations = body.get("citations") or []
    raw_contexts = body.get("contexts") or []

    contexts = [str(item.get("text", "")) for item in raw_contexts]
    chunk_doc_ids = [str(item.get("doc_id", "")) for item in raw_contexts]

    retrieved_doc_ids: list[str] = []
    seen: set[str] = set()
    for doc_id in chunk_doc_ids:
        # 同一篇文档常因多个分块重复出现；只保留**首次**出现的位置，那就是它的名次
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            retrieved_doc_ids.append(doc_id)

    if not contexts:
        # 兜底只保证"片段召回"仍有可判定依据（拒答路径与缓存命中都没有 contexts）
        contexts = [str(citation.get("snippet", "")) for citation in citations]

    return _Extracted(contexts, citations, retrieved_doc_ids, chunk_doc_ids)


def _expected_and_forbidden(
    sample: GoldenSample,
    ledger: dict[str, dict[str, Any]],
    resolved: dict[str, str],
) -> tuple[set[str], set[str]]:
    """该样本的 (期望来源, 禁止来源) doc_id 集合。

    禁止集合 = 数据集显式声明 + 按台账 ACL 推导，两者都要——
    只靠手写声明会漏检，只靠自动推导会漏掉 private 文档。
    """
    expected = {resolved[source] for source in sample.expected_sources if source in resolved}
    forbidden = {resolved[source] for source in sample.forbidden_sources if source in resolved}
    return expected, forbidden | _acl_forbidden_doc_ids(ledger, sample.identity, expected)


def _build_row(
    sample: GoldenSample,
    body: dict[str, Any],
    error: str | None,
    latency_ms: float,
    expected: set[str],
    forbidden: set[str],
) -> dict[str, Any]:
    """把一次采集结果整理成指标层可直接消费的行。"""
    extracted = _extract_contexts(body)
    return {
        "sample_id": sample.id,
        "question": sample.question,
        "reference": sample.reference,
        "identity": sample.identity.describe(),
        "tags": sample.tags,
        "should_refuse": sample.should_refuse,
        "expected_doc_ids": sorted(expected),
        "expected_snippets": sample.expected_snippets,
        "forbidden_doc_ids": sorted(forbidden),
        "must_not_contain": list(sample.must_not_contain),
        "answer": body.get("answer", ""),
        # 作答用的模型名。必须落盘：否则"误答率从 44.4% 降到 x%"无法归因到模型切换，
        # 后人会以为那是提示词或检索的功劳。
        "answer_model": body.get("model"),
        "refused": bool(body.get("refused")),
        "cached": bool(body.get("cached")),
        "citations": extracted.citations,
        "contexts": extracted.contexts,
        # 检索侧信息：NDCG / 检索侧 MRR 的输入。缓存命中时为空（响应里没有 contexts），
        # 指标层据此把这些行排除在检索侧分母之外。
        "retrieved_doc_ids": extracted.retrieved_doc_ids,
        "chunk_doc_ids": extracted.chunk_doc_ids,
        "latency_ms": latency_ms,
        "timings_ms": body.get("timings_ms") or {},
        "error": error,
    }


async def collect(samples: list[GoldenSample], settings: Settings) -> list[dict[str, Any]]:
    """逐条采集，返回的行携带 L1 判定所需的全部信息。

    只做编排：前置检查 → 逐条请求 → 组装行。单个样本的细节见各自的 ``_`` 函数。
    """
    tokens = TokenProvider(settings)
    try:
        ledger = await _fetch_ledger(settings, tokens)
        # 台账要在**采集之前**校验：没有台账就无法做越权兜底检查，
        # 等采完 41 条样本再报错等于整轮白跑。
        if not ledger:
            raise ConfigError(
                "无法读取文档台账（/documents 不可用），拒绝继续："
                "缺少台账就无法做越权兜底检查，此时评测结果不可信"
            )
        if not await tokens.token(settings.ledger_username):
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
                headers = await _identity_headers(tokens, sample.identity)
                body, error, latency_ms = await _ask(
                    client, gateway, _chat_payload(sample, settings), headers, sample.id
                )
                expected, forbidden = _expected_and_forbidden(sample, ledger, resolved)
                rows.append(_build_row(sample, body, error, latency_ms, expected, forbidden))
                logger.info(
                    "[%s/%s] %s identity=%s citations=%s refused=%s %sms",
                    index,
                    len(samples),
                    sample.id,
                    sample.identity.username,
                    len(body.get("citations") or []),
                    bool(body.get("refused")),
                    latency_ms,
                )
        return rows
    finally:
        # 放在 finally：请求中途抛异常时也要释放连接池
        await tokens.aclose()


__all__ = ["_acl_forbidden_doc_ids", "_missing_sources_error", "collect", "preflight"]

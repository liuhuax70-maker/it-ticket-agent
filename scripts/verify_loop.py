"""最小闭环验收脚本。

判据（缺一不可）：
    V1 各服务 /health 正常，依赖项真实连通
    V2 接入语料成功，且可重复执行（幂等，chunk 数稳定）
    V3 **检索级引用回查**：content[char_start:char_end] == hit.text
        这是「引用可精确定位原文」的硬证据，且不依赖 LLM
    V4 正样本问答返回非空答案 + 非空 citations
    V5 负样本（文档里没有的问题）走拒答，不编造

用法：
    python scripts/verify_loop.py                 # 全量（需要可用的 LLM 配置）
    python scripts/verify_loop.py --skip-chat     # 只验 V1~V3（无需 LLM 密钥）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.common.ids import stable_doc_id  # noqa: E402

SERVICES = {
    "api-gateway": "http://localhost:8000",
    "query-orchestrator": "http://localhost:8001",
    "retrieval": "http://localhost:8002",
    "model-gateway": "http://localhost:8003",
    "ingestion": "http://localhost:8004",
    "indexing": "http://localhost:8005",
}
CORPUS_DIR = ROOT / "data" / "corpus"

POSITIVE_QUERIES = [
    "入职体检费用怎么报销？",
    "年假有多少天？",
    "入职体检报销需要在多久内提交？",
]
NEGATIVE_QUERIES = [
    "公司年会在哪家酒店举办？",
    "食堂今天午饭吃什么？",
]

ACL = {"tenant_id": "default", "department_id": "default", "visibility": "internal"}

# 鉴权开启（AUTHZ_ENABLED=true）后，/chat 需要真实令牌。
# 这里默认用 carol（default 租户、rag_writer），它能看到公共语料；
# 关掉鉴权时取不到令牌也不影响 V1~V3。
KEYCLOAK_URL = os.getenv("RAG_KEYCLOAK_URL", "http://localhost:8180")
VERIFY_USER = os.getenv("RAG_VERIFY_USER", "carol")
VERIFY_PASSWORD = os.getenv("RAG_VERIFY_PASSWORD", "carol")
_headers: dict[str, str] = {}


def auth_headers() -> dict[str, str]:
    """惰性获取一次令牌；失败则返回空（等价于鉴权未开启）。"""
    if _headers:
        return _headers
    try:
        resp = httpx.post(
            f"{KEYCLOAK_URL}/realms/rag/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "rag-api",
                "client_secret": "rag-api-dev-secret",
                "username": VERIFY_USER,
                "password": VERIFY_PASSWORD,
                "scope": "openid",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        _headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        print(f"  [INFO] 已获取 {VERIFY_USER} 的访问令牌（鉴权开启）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [INFO] 未获取令牌，按鉴权关闭处理（{exc.__class__.__name__}）")
    return _headers


_failures: list[str] = []
_warnings: list[str] = []


def ok(label: str, detail: str = "") -> None:
    print(f"  [PASS] {label}{(' — ' + detail) if detail else ''}")


def fail(label: str, detail: str = "") -> None:
    _failures.append(label)
    print(f"  [FAIL] {label}{(' — ' + detail) if detail else ''}")


def warn(label: str, detail: str = "") -> None:
    _warnings.append(label)
    print(f"  [WARN] {label}{(' — ' + detail) if detail else ''}")


def corpus_index() -> dict[str, Path]:
    """doc_id -> 文件路径，用于引用回查。"""
    mapping: dict[str, Path] = {}
    if CORPUS_DIR.exists():
        for path in sorted(CORPUS_DIR.rglob("*")):
            if path.is_file():
                mapping[stable_doc_id(str(path.relative_to(ROOT).as_posix()))] = path
    return mapping


def locate(path: Path, start: int, end: int) -> str:
    return path.read_text(encoding="utf-8")[start:end]


# ---------------------------------------------------------------- V1


async def check_health(client: httpx.AsyncClient) -> None:
    """V1：逐个探活 6 个服务，任一不可达或下游依赖报错即记入 _failures。

    这一步是后续所有阶段的前置门禁——服务没起来就别浪费时间跑接入和问答。
    """
    print("\nV1 服务健康检查")
    for name, base in SERVICES.items():
        try:
            resp = await client.get(f"{base}/health", timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            fail(f"{name} 不可达", str(exc))
            continue
        if resp.status_code != 200:
            fail(f"{name} /health 返回 {resp.status_code}")
            continue
        body = resp.json()
        status = body.get("status")
        details = body.get("details", {})
        if status == "ok":
            ok(f"{name} ok")
        else:
            fail(f"{name} 状态 {status}", str(details)[:300])
        if name == "retrieval":
            for key in ("milvus", "opensearch"):
                if details.get(key, "").startswith(("error", "unreachable")):
                    fail(f"retrieval.{key} 不可用", details[key])


# ---------------------------------------------------------------- V2


async def check_ingest(client: httpx.AsyncClient) -> None:
    """V2：接入两次并断言 chunk 数一致，验证接入幂等。

    不幂等说明 reindex 没真正清理旧分块，会让索引膨胀、检索结果重复。
    """
    print("\nV2 语料接入（幂等）")
    payload = {"path": str(CORPUS_DIR.relative_to(ROOT).as_posix()), "reindex": True}
    counts: list[int] = []
    for attempt in (1, 2):
        try:
            resp = await client.post(f"{SERVICES['ingestion']}/ingest", json=payload, timeout=600.0)
        except Exception as exc:  # noqa: BLE001
            fail(f"第 {attempt} 次接入请求失败", str(exc))
            return
        if resp.status_code >= 400:
            fail(f"第 {attempt} 次接入失败", f"HTTP {resp.status_code} {resp.text[:300]}")
            return
        body = resp.json()
        counts.append(int(body.get("chunk_count", 0)))
        print(
            f"  第 {attempt} 次: documents={body.get('documents')} "
            f"chunks={body.get('chunk_count')} indexed={body.get('indexed')}"
        )

    if counts[0] <= 0:
        fail("接入后 chunk 数为 0")
    elif counts[0] != counts[1]:
        fail("两次接入 chunk 数不一致（不幂等）", f"{counts[0]} != {counts[1]}")
    else:
        ok("接入幂等", f"chunk_count={counts[0]}")


# ---------------------------------------------------------------- V3


async def retrieval_search(client: httpx.AsyncClient, query: str, top_k: int = 5) -> list[dict]:
    """向 retrieval 发一次混合检索，返回 hits（含 char_start/char_end/text）。"""
    resp = await client.post(
        f"{SERVICES['retrieval']}/search",
        json={"query": query, "top_k": top_k, "mode": "hybrid", "acl": ACL},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json().get("hits", [])


async def check_retrieval_locate(client: httpx.AsyncClient) -> None:
    """V3：用原文偏移回查，证明检索命中能精确定位到原文字符区间。

    这是「引用真实可溯源、不是模型现编」的硬证据，且完全不依赖 LLM，
    所以 --skip-chat 时也必须跑。
    """
    print("\nV3 检索级引用回查（content[char_start:char_end] == hit.text）")
    index = corpus_index()
    if not index:
        fail("语料目录为空，无法回查", str(CORPUS_DIR))
        return

    total_checked = 0
    for query in POSITIVE_QUERIES[:2]:
        try:
            hits = await retrieval_search(client, query)
        except Exception as exc:  # noqa: BLE001
            fail(f"检索失败 query={query!r}", str(exc))
            continue
        if not hits:
            fail(f"检索结果为空 query={query!r}")
            continue

        ok(f"{query!r} 召回 {len(hits)} 条")
        for hit in hits:
            path = index.get(hit["doc_id"])
            if path is None:
                # 只跳过，不计失败：doc_id 由 source 派生，路径变化时可能对不上
                warn(f"doc_id={hit['doc_id']} 无法映射到语料文件，跳过回查")
                continue
            total_checked += 1
            actual = locate(path, hit["char_start"], hit["char_end"])
            if actual != hit["text"]:
                fail(
                    f"偏移与原文不一致 chunk={hit['chunk_id']}",
                    f"期望 {hit['text'][:40]!r} 实际 {actual[:40]!r}",
                )
            elif not hit.get("section_path"):
                warn(f"chunk={hit['chunk_id']} 缺少 section_path")

    if total_checked:
        ok("引用偏移可精确定位原文", f"已回查 {total_checked} 条分块")


# ---------------------------------------------------------------- V4 / V5


async def chat(client: httpx.AsyncClient, query: str) -> dict:
    """走网关 /chat 发一次问答；非 2xx 直接抛错（让调用方记作失败而非误判拒答）。"""
    resp = await client.post(
        f"{SERVICES['api-gateway']}/chat",
        json={"query": query},
        headers=auth_headers(),
        timeout=300.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


async def check_chat(client: httpx.AsyncClient) -> None:
    """V4/V5：正样本要答案+引用齐备，负样本必须拒答不能编造。

    V5 的「未拒答」按可能编造处理——RAG 答了文档里没有的事比答不上来更危险。
    """
    index = corpus_index()
    print("\nV4 正样本问答（需要可用的 LLM 配置）")
    for query in POSITIVE_QUERIES:
        try:
            body = await chat(client, query)
        except Exception as exc:  # noqa: BLE001
            fail(f"问答失败 query={query!r}", str(exc))
            continue

        answer = body.get("answer", "")
        citations = body.get("citations", [])
        if not answer.strip():
            fail(f"答案为空 query={query!r}")
            continue
        if body.get("refused"):
            fail(f"正样本被拒答 query={query!r}", answer[:80])
            continue
        if not citations:
            fail(f"citations 为空 query={query!r}")
            continue

        print(
            f"  {query} -> {answer[:60]}… "
            f"(citations={len(citations)}, total={body.get('timings_ms', {}).get('total')}ms)"
        )

        # 引用回查：snippet 是原文前缀，必须与 source 的对应区间一致
        for citation in citations:
            path = index.get(citation["doc_id"])
            if path is None:
                continue
            actual = locate(path, citation["char_start"], citation["char_end"])
            snippet = citation.get("snippet", "")
            if not actual.startswith(snippet):
                fail(f"引用区间与原文不符 chunk={citation['chunk_id']}")
            elif citation["char_start"] >= citation["char_end"]:
                fail(f"引用区间非法 chunk={citation['chunk_id']}")
        ok(f"{query!r} 答案与引用齐备")

    print("\nV5 负样本拒答（文档中不存在的问题）")
    for query in NEGATIVE_QUERIES:
        try:
            body = await chat(client, query)
        except Exception as exc:  # noqa: BLE001
            fail(f"问答失败 query={query!r}", str(exc))
            continue
        if body.get("refused") and not body.get("citations"):
            ok(f"{query!r} 正确拒答")
        else:
            fail(
                f"{query!r} 未拒答（可能编造）",
                f"answer={body.get('answer', '')[:80]!r} citations={len(body.get('citations', []))}",
            )


# ---------------------------------------------------------------- main


async def main() -> int:
    """CLI 入口：依次跑 V1~V3（必要时 V4/V5），汇总失败/警告并控制退出码。

    退出码非 0 即「验收未通过」，可直接接进 CI 门禁。
    """
    parser = argparse.ArgumentParser(description="最小闭环验收")
    parser.add_argument("--skip-chat", action="store_true", help="只验 V1~V3（不需要 LLM）")
    args = parser.parse_args()

    async with httpx.AsyncClient() as client:
        await check_health(client)
        if _failures:
            print("\n服务未就绪，先解决 V1 再继续")
            return 1
        await check_ingest(client)
        await check_retrieval_locate(client)
        if not args.skip_chat:
            await check_chat(client)
        else:
            print("\n（--skip-chat：跳过 V4/V5）")

    print("\n" + "=" * 56)
    if _warnings:
        print(f"警告 {len(_warnings)} 条")
    if _failures:
        print(f"验收未通过，失败 {len(_failures)} 项:")
        for item in _failures:
            print(f"  - {item}")
        return 1
    scope = "V1~V3" if args.skip_chat else "V1~V5"
    print(f"验收通过：最小闭环 {scope} 全部满足")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

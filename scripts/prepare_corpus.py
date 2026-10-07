"""准备知识库语料：通用语料 + 权限语料。

单一入口，被评测（services/eval）与权限验收（scripts/verify_permissions.py）共用——
两份代码各写一套"哪份文件该以什么身份、什么可见性入库"，是最容易悄悄漂移的地方。

用法：
    python scripts/prepare_corpus.py                 # 两份语料都准备
    python scripts/prepare_corpus.py --only general  # 只准备 data/corpus
    python scripts/prepare_corpus.py --only permissions

前提：api-gateway 已启动；开启鉴权（AUTHZ_ENABLED=true）时需要 Keycloak 可访问。
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

from packages.security import SecuritySettings, TokenProvider  # noqa: E402

GATEWAY = os.getenv("RAG_GATEWAY_URL", "http://127.0.0.1:8000")

GENERAL_DIR = "data/corpus"
PERMISSION_DIR = "data/corpus_permissions"

# 权限语料 -> (上传者, 可见性, 期望落库部门)
# 上传者决定 tenant/department（网关强制用身份覆盖客户端传值），
# 所以这份映射同时是"权限切片的预期结果"，评测试集会据此断言。
PERMISSION_FILES: dict[str, tuple[str, str, str]] = {
    "hr_policy.md": ("carol", "department", "hr"),
    "eng_runbook.md": ("erin", "department", "engineering"),
    "carol_note.md": ("carol", "private", "hr"),
}

# 通用语料由谁入库：需要有 documents:write 身份
GENERAL_WRITER = "carol"


async def _headers(tokens: TokenProvider, username: str) -> dict[str, str]:
    token = await tokens.token(username)
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def prepare_general(tokens: TokenProvider, *, reindex: bool) -> dict:
    path = ROOT / GENERAL_DIR
    count = len(list(path.glob("*.md")))
    async with httpx.AsyncClient(timeout=1800.0) as client:
        resp = await client.post(
            f"{GATEWAY}/documents/ingest",
            json={"path": GENERAL_DIR, "reindex": reindex},
            headers=await _headers(tokens, GENERAL_WRITER),
        )
        resp.raise_for_status()
        body = resp.json()
    print(
        f"[corpus] 通用语料 {GENERAL_DIR}（{count} 个文件）"
        f" -> 文档 {body.get('documents')} 篇 / 分块 {body.get('chunk_count')} 个"
        f" / {round((body.get('timings_ms') or {}).get('total', 0) / 1000, 1)}s"
    )
    return body


async def prepare_permissions(tokens: TokenProvider, *, reindex: bool) -> dict[str, str]:
    """按身份上传权限语料，返回 文件名 -> doc_id。"""
    doc_ids: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=1800.0) as client:
        for filename, (uploader, visibility, expected_department) in PERMISSION_FILES.items():
            target = ROOT / PERMISSION_DIR / filename
            content = target.read_bytes()
            resp = await client.post(
                f"{GATEWAY}/documents/upload",
                headers=await _headers(tokens, uploader),
                files={"file": (filename, content, "text/markdown")},
                data={"visibility": visibility, "reindex": "true" if reindex else "false"},
            )
            resp.raise_for_status()
            doc_id = (resp.json().get("doc_ids") or [""])[0]
            doc_ids[filename] = doc_id
            print(
                f"[corpus] {filename:<18} 上传者={uploader:<6} 可见性={visibility:<11} "
                f"期望部门={expected_department:<12} doc_id={doc_id}"
            )
    return doc_ids


async def verify_acl(tokens: TokenProvider, doc_ids: dict[str, str]) -> bool:
    """回读台账，确认 ACL 真的是身份决定的（而不是我们以为的那样）。"""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(
            f"{GATEWAY}/documents",
            params={"limit": 500},
            headers=await _headers(tokens, GENERAL_WRITER),
        )
        resp.raise_for_status()
        by_id = {item["doc_id"]: item for item in resp.json().get("items", [])}

    healthy = True
    for filename, (uploader, visibility, expected_department) in PERMISSION_FILES.items():
        meta = by_id.get(doc_ids.get(filename, ""))
        if meta is None:
            print(f"[corpus] !! {filename} 未出现在台账中")
            healthy = False
            continue
        actual = (meta.get("tenant_id"), meta.get("department_id"), meta.get("visibility"))
        expected = (meta.get("tenant_id"), expected_department, visibility)
        marker = "OK " if actual == expected else "!! "
        if actual != expected:
            healthy = False
        print(
            f"[corpus] {marker}{filename:<18} 上传者={uploader:<6} 落库 tenant/dept/vis = {actual}"
        )
    return healthy


async def main() -> int:
    parser = argparse.ArgumentParser(description="准备知识库语料")
    parser.add_argument("--only", choices=["general", "permissions"], default=None)
    parser.add_argument("--no-reindex", action="store_true", help="不清理旧分块（内容未变时更快）")
    args = parser.parse_args()

    settings = SecuritySettings()
    tokens = TokenProvider(settings, passwords={})
    reindex = not args.no_reindex

    try:
        if await tokens.token(GENERAL_WRITER) is None:
            print("[corpus] 取不到令牌，将以固定身份请求（AUTHZ_ENABLED=false 时正常）")

        if args.only in (None, "general"):
            await prepare_general(tokens, reindex=reindex)
        if args.only in (None, "permissions"):
            doc_ids = await prepare_permissions(tokens, reindex=reindex)
            healthy = await verify_acl(tokens, doc_ids)
            if not healthy:
                print("[corpus] 权限语料的 ACL 与预期不符，请检查网关的身份注入逻辑")
                return 1
    finally:
        await tokens.aclose()

    print("[corpus] 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

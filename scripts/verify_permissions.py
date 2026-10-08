"""权限闭环验收（S7）：用真身份证明「检索结果被权限裁剪」。

与 verify_loop.py 的区别：
    verify_loop.py 验证「一条链路能跑通」（单租户单部门）；
    本脚本验证**本项目区别于普通 RAG 的那件事**——不同身份看到的知识边界不同，
    且越权文档不是"排在后面"，而是**根本不会进入候选集**。

设计原则：
    1. 身份来自真实 Keycloak（密码模式取令牌），不是伪造的请求头；
    2. 断言只依赖确定性事实（doc_id 集合），不依赖模型措辞；
    3. 每个判据都能独立失败并打印证据，便于定位是"过滤没生效"还是"策略没生效"。

前置：AUTHZ_ENABLED=true，Keycloak/OPA 已启动（docker compose --profile authz up -d）。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "corpus_permissions"

GATEWAY = "http://127.0.0.1:8000"
RETRIEVAL = "http://127.0.0.1:8002"
# 必须与服务端 KEYCLOAK_URL 同主机：Keycloak 的 iss 声明跟随请求主机
KEYCLOAK = "http://localhost:8180/realms/rag"
CLIENT_ID = "rag-api"
CLIENT_SECRET = "rag-api-dev-secret"

# 账号：用户名 -> (密码, 租户, 部门, 角色)
ACCOUNTS = {
    "alice": ("alice", "default", "hr", ["rag_user"]),
    "carol": ("carol", "default", "hr", ["rag_user", "rag_writer"]),
    "bob": ("bob", "default", "engineering", ["rag_user"]),
    "erin": ("erin", "default", "engineering", ["rag_user", "rag_writer"]),
    "dave": ("dave", "tenant-b", "hr", ["rag_user"]),
}

# 语料 -> 上传者 / 可见性 / 期望落库的部门
# 与 scripts/prepare_corpus.py 共用同一份定义，避免两处各写一套而悄悄漂移
from prepare_corpus import PERMISSION_FILES as CORPUS_FILES  # noqa: E402

# default 租户里所有身份都能看到的公共文档（internal 可见性）
GENERAL_DOC_TITLES = {
    "员工手册",
    "费用报销管理制度",
    "差旅管理制度",
    "考勤与休假制度",
    "信息安全管理制度",
    "采购与供应商管理制度",
    "研发规范",
    "薪酬与晋升制度",
    "培训管理制度",
    "数据合规与留存制度",
    # 注入回归夹具（故意投毒，仍是一份 internal 文档，可被合法引用）
    "会议室预定管理规定",
}

# 每个身份**有权看到**的文档标题片段（由 ACL 推导，用作硬断言的上界）。
# 断言形式：任意一次回答的 citations 必须 ⊆ 该集合。
# 这比「只允许出现期望文档」正确得多——公共基线文档被引用是合法的。
VISIBLE_DOCS = {
    "alice": GENERAL_DOC_TITLES | {"人力资源部内部制度"},
    "carol": GENERAL_DOC_TITLES | {"人力资源部内部制度", "入职交接清单"},
    "bob": GENERAL_DOC_TITLES | {"工程部运行手册"},
    "erin": GENERAL_DOC_TITLES | {"工程部运行手册"},
    "dave": set(),  # 其他租户：一份都看不到
}

# 查询 -> (问题, 该问题的"专属文档"标题片段, 应当命中它的账号集合)
#
# 探针问题刻意选**只在该受控文档里出现**的事实：扩语料后，"调薪窗口在四月"
# 这类事实在公共文档里也有，用它做权限探针就不再能区分"看到了"和"没看到"。
QUERIES = {
    "hr": (
        "招聘需求审批中，编制核对需要在几个工作日内完成？",
        "人力资源部内部制度",
        {"alice", "carol"},
    ),
    "eng": ("P1 告警需要在几分钟内响应？", "工程部运行手册", {"bob", "erin"}),
    "priv": ("入职交接清单的第 3 项是什么？", "入职交接清单", {"carol"}),
    # 公共基线：租户内所有身份都应命中；tenant-b 因租户隔离拿不到
    "pub": ("年假有多少天？", "员工手册", {"alice", "carol", "bob", "erin"}),
}

_failures: list[str] = []
_warnings: list[str] = []


def ok(message: str) -> None:
    print(f"  [PASS] {message}")


def fail(message: str) -> None:
    print(f"  [FAIL] {message}")
    _failures.append(message)


def warn(message: str) -> None:
    print(f"  [WARN] {message}")
    _warnings.append(message)


def check(condition: bool, message: str) -> bool:
    ok(message) if condition else fail(message)
    return condition


def wait_keycloak(timeout: int = 180) -> None:
    """轮询 Keycloak 的 .well-known 配置直到就绪；超时则直接退出。

    权限验收全程依赖真实令牌，Keycloak 没起来后面每步都会假失败，
    所以宁可阻塞等它，也不要带着不可达依赖硬跑。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if (
                httpx.get(f"{KEYCLOAK}/.well-known/openid-configuration", timeout=5).status_code
                == 200
            ):
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    raise SystemExit("Keycloak 未就绪，请先执行 docker compose --profile authz up -d")


def token(username: str) -> str:
    """用密码模式向 Keycloak 取某账号的 access_token（权限验收只用真身份）。"""
    resp = httpx.post(
        f"{KEYCLOAK}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": ACCOUNTS[username][0],
            "scope": "openid",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def decode_claims(access_token: str) -> dict:
    """不校验签名地解出 JWT payload，仅用于回显 tenant/department/roles 做断言。

    注意：这里不验签，因为本脚本本身就是用同一套 Keycloak 取的令牌，
    断言的是「网关侧解析出的声明是否符合预期」，而非令牌真伪。
    """
    part = access_token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def chat(access_token: str | None, query: str) -> dict:
    """以某身份走网关 /chat；非 2xx 抛错（权限探针要的是确定性结果，不应静默吞）。"""
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    resp = httpx.post(f"{GATEWAY}/chat", json={"query": query}, headers=headers, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def upload(access_token: str, filename: str, visibility: str, reindex: bool = True) -> dict:
    """以某身份上传权限语料；可见性由参数给定，但 tenant/department 由网关按身份覆盖。"""
    path = CORPUS / filename
    with path.open("rb") as handle:
        resp = httpx.post(
            f"{GATEWAY}/documents/upload",
            headers={"Authorization": f"Bearer {access_token}"},
            files={"file": (filename, handle.read(), "text/markdown")},
            data={"visibility": visibility, "reindex": "true" if reindex else "false"},
            timeout=600,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"上传 {filename} 失败 HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def list_documents(access_token: str) -> dict:
    """拉取当前身份可见的文档台账，用于核对落库 ACL 与隔离矩阵。"""
    resp = httpx.get(
        f"{GATEWAY}/documents",
        params={"limit": 200},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def cited_titles(body: dict) -> list[str]:
    """从回答里抽出被引用的文档标题列表，作为越权对比的素材。"""
    return [c.get("doc_title") or "" for c in body.get("citations", [])]


# ---------------------------------------------------------------- 阶段


def phase_auth_gate() -> dict[str, str]:
    """阶段 0：鉴权开关是否真的生效。"""
    print("P0 鉴权门禁")
    health = httpx.get(f"{GATEWAY}/health", timeout=10)
    check(health.status_code == 200, f"探针 /health 免鉴权可访问（HTTP {health.status_code}）")

    no_token = httpx.post(f"{GATEWAY}/chat", json={"query": "x"}, timeout=10)
    check(no_token.status_code == 401, f"无令牌访问 /chat 被拒（HTTP {no_token.status_code}）")

    bad_token = httpx.post(
        f"{GATEWAY}/chat",
        json={"query": "x"},
        headers={"Authorization": "Bearer not-a-jwt"},
        timeout=10,
    )
    check(bad_token.status_code == 401, f"伪造令牌被拒（HTTP {bad_token.status_code}）")

    tokens = {name: token(name) for name in ACCOUNTS}
    claims = decode_claims(tokens["carol"])
    roles = claims.get("realm_access", {}).get("roles", [])
    check(
        claims.get("tenant_id") == "default" and claims.get("department_id") == "hr",
        f"令牌声明被正确解析（tenant={claims.get('tenant_id')} dept={claims.get('department_id')}）",
    )
    check("rag_writer" in roles, f"carol 具备写角色（roles={roles}）")
    return tokens


def phase_clean(tokens: dict[str, str]) -> None:
    """清理历史上传产物，保证可重复执行。

    注意：这会删掉所有 `data/uploads/` 下的文档（含界面手工上传的），
    因此只在准备阶段执行；``--skip-prepare`` 不动现有数据。
    """
    print("\nP1a 清理历史上传产物")
    listing = list_documents(tokens["carol"])
    stale = [
        item for item in listing.get("items", []) if item["source"].startswith("data/uploads/")
    ]
    if not stale:
        ok("没有需要清理的文档")
        return
    for item in stale:
        httpx.delete(
            f"{GATEWAY}/documents/{item['doc_id']}",
            headers={"Authorization": f"Bearer {tokens['carol']}"},
            timeout=120,
        )
    ok(f"已清理 {len(stale)} 篇历史文档：{[i['title'] for i in stale]}")


def phase_prepare(tokens: dict[str, str]) -> dict[str, str]:
    """阶段 1：按真实身份上传语料，验证 ACL 由身份决定。"""
    print("\nP1 语料准备（ACL 由身份决定）")
    doc_ids: dict[str, str] = {}
    for filename, (uploader, visibility, _) in CORPUS_FILES.items():
        result = upload(tokens[uploader], filename, visibility)
        doc_id = (result.get("doc_ids") or [""])[0]
        doc_ids[filename] = doc_id
        print(f"  {filename:<18} 由 {uploader:<5} 上传 visibility={visibility:<10} doc_id={doc_id}")

    listing = list_documents(tokens["carol"])
    by_id = {item["doc_id"]: item for item in listing.get("items", [])}
    for filename, (_, visibility, expected_dept) in CORPUS_FILES.items():
        item = by_id.get(doc_ids[filename])
        if item is None:
            fail(f"{filename} 未出现在台账中")
            continue
        check(
            item["department_id"] == expected_dept and item["visibility"] == visibility,
            f"{filename} 落库 ACL 正确（dept={item['department_id']} visibility={item['visibility']}）",
        )
    return doc_ids


def phase_matrix(tokens: dict[str, str], doc_ids: dict[str, str]) -> None:
    """阶段 2：隔离矩阵——谁该看到什么。"""
    print("\nP2 隔离矩阵（4 个查询 × 5 个身份）")

    for key, (query, expect_title, should_hit) in QUERIES.items():
        print(f"\n  ── {key}: {query}")
        for name in ACCOUNTS:
            body = chat(tokens[name], query)
            titles = cited_titles(body)
            refused = bool(body.get("refused"))
            hit = any(expect_title in t for t in titles)

            # 强断言：本次回答引用的每一份文档，都必须在该身份的可见集合内。
            # 「不越权」指的是不出现在别人的文档，而不是"只能出现期望的那一份"。
            visible = VISIBLE_DOCS[name]
            leaked = [t for t in titles if t and not any(v in t for v in visible)]
            if leaked:
                fail(f"{name:<6} 引用了越权文档 {leaked}")
            else:
                ok(
                    f"{name:<6} 引用未越权（{len(titles)} 条引用，可见集合 {sorted(visible) or '空'}）"
                )

            if name in should_hit:
                check(hit and not refused, f"{name:<6} 命中专属文档「{expect_title}」")
            else:
                check(
                    not hit,
                    f"{name:<6} 未获得「{expect_title}」的任何引用（citations={titles or '空'}）",
                )


def phase_storage_filter(tokens: dict[str, str]) -> None:
    """阶段 3：过滤必须在存储层生效（应用层裁剪会导致越权文档挤占 top_k）。"""
    print("\nP3 存储层过滤硬证据")
    # 用 hr_policy 的原句去查，且以工程部身份请求：
    # 若过滤没下沉到 Milvus/OpenSearch，这句话会稳居 top1。
    probe = "年度调薪窗口固定在每年四月启动，由人力资源部统一发起"
    payload = {
        "query": probe,
        "top_k": 10,
        "mode": "hybrid",
        "acl": {"tenant_id": "default", "department_id": "engineering", "visibility": "internal"},
    }
    resp = httpx.post(f"{RETRIEVAL}/search", json=payload, timeout=60)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    hr_hits = [
        h
        for h in hits
        if "hr_policy" in h.get("source", "") or "人力资源部内部制度" in h.get("doc_title", "")
    ]
    check(
        not hr_hits,
        f"以 engineering 身份检索 HR 原句，结果不含 HR 文档（命中 {len(hits)} 条，其中 HR {len(hr_hits)} 条）",
    )

    # 反向对照：同一句话以 hr 身份检索必须能命中，证明上面不是因为"索引里没有"
    payload_hr = dict(
        payload, acl={"tenant_id": "default", "department_id": "hr", "visibility": "internal"}
    )
    hit_hr = httpx.post(f"{RETRIEVAL}/search", json=payload_hr, timeout=60).json().get("hits", [])
    check(
        any(
            "人力资源部内部制度" in h.get("doc_title", "") or "hr_policy" in h.get("source", "")
            for h in hit_hr
        ),
        f"同一句话以 hr 身份检索可以命中（命中 {len(hit_hr)} 条）— 对照组成立",
    )


def phase_opa(tokens: dict[str, str]) -> None:
    """阶段 4：OPA 策略对写动作的约束。"""
    print("\nP4 OPA 写权限")

    resp = httpx.post(
        f"{GATEWAY}/documents/ingest",
        json={"content": "# 越权写入尝试\n\n不应成功。", "filename": "denied.md"},
        headers={"Authorization": f"Bearer {tokens['alice']}"},
        timeout=60,
    )
    check(resp.status_code == 403, f"alice（普通用户）写入被 OPA 拒绝（HTTP {resp.status_code}）")

    resp_ok = httpx.post(
        f"{GATEWAY}/documents/ingest",
        json={"content": "# 合法写入\n\n由 carol（rag_writer）发起。", "filename": "writer_ok.md"},
        headers={"Authorization": f"Bearer {tokens['carol']}"},
        timeout=600,
    )
    check(
        resp_ok.status_code == 200, f"carol（rag_writer）写入被放行（HTTP {resp_ok.status_code}）"
    )
    if resp_ok.status_code == 200:
        doc_id = (resp_ok.json().get("doc_ids") or [""])[0]
        httpx.delete(
            f"{GATEWAY}/documents/{doc_id}",
            headers={"Authorization": f"Bearer {tokens['carol']}"},
            timeout=120,
        )

    tenant_b = httpx.get(
        f"{GATEWAY}/documents",
        headers={"Authorization": f"Bearer {tokens['dave']}"},
        timeout=30,
    )
    check(
        tenant_b.status_code == 200 and tenant_b.json().get("total", 0) == 0,
        f"tenant-b 身份看不到 default 租户的任何文档（total={tenant_b.json().get('total') if tenant_b.status_code == 200 else '-'}）",
    )


LOCK_FILE = ROOT / "data" / "verify_permissions.lock"


class AlreadyRunning(RuntimeError):
    """表明已有另一份验证进程持有锁在跑，本进程应当直接退出而非抢占。"""


def acquire_lock() -> None:
    """禁止并发运行。

    两个验证进程同时跑会互相抢 Ollama/索引写入，还会在网关重启窗口里
    互相制造假失败——我本人就踩过一次，所以用锁把它挡在入口。
    """
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            pid = 0
        if pid and pid != _current_pid() and _pid_alive(pid):
            raise AlreadyRunning(f"已有验证进程在运行（pid={pid}），请先等待其结束")
        print(f"  [WARN] 清理到期的锁文件（pid={pid} 已不存在）")
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(_current_pid()), encoding="utf-8")


def release_lock() -> None:
    """进程退出（无论成功/失败）时删锁，避免残留锁文件挡住下一次运行。"""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _current_pid() -> int:
    import os

    return os.getpid()


def _pid_alive(pid: int) -> bool:
    """跨平台判断 pid 是否仍存活（Windows 用 tasklist，POSIX 用 kill(pid,0)）。"""
    import os

    if os.name == "nt":
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
            return str(pid) in out
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    """入口加锁防并发，再编排各阶段；退出码非 0 即验收未通过。"""
    parser = argparse.ArgumentParser(description="权限闭环验收")
    parser.add_argument(
        "--skip-prepare",
        action="store_true",
        help="跳过清理与语料上传（沿用现有数据，不改动知识库）",
    )
    args = parser.parse_args()

    try:
        acquire_lock()
    except AlreadyRunning as exc:
        print(f"跳过：{exc}")
        return 2

    try:
        return _run(args)
    finally:
        release_lock()


def _run(args: argparse.Namespace) -> int:
    """实际验收流程：门禁→准备→隔离矩阵→存储层过滤→OPA 写权限，最后汇总结论。"""
    wait_keycloak()

    print("=" * 60)
    tokens = phase_auth_gate()
    if not args.skip_prepare:
        phase_clean(tokens)
        doc_ids = phase_prepare(tokens)
    else:
        print("\n（--skip-prepare：沿用现有语料，跳过清理与上传）")
        doc_ids = {}
    phase_matrix(tokens, doc_ids)
    phase_storage_filter(tokens)
    phase_opa(tokens)

    print("\n" + "=" * 60)
    if _warnings:
        print(f"警告 {len(_warnings)} 条")
    if _failures:
        print(f"权限验收未通过，失败 {len(_failures)} 项：")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("权限验收通过：身份隔离、存储层过滤、OPA 策略均符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())

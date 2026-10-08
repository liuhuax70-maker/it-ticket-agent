"""共享库测试：契约、过滤编译、RRF、提示注册表、PII、ID 稳定性。"""

from __future__ import annotations

import pytest

from packages.common.constants import NO_CITE_MARKER, NO_CONTEXT_NOTICE, REFUSE_MARKER
from packages.common.errors import ConfigError
from packages.common.ids import content_hash, stable_chunk_id, stable_doc_id
from packages.common.settings import BaseAppSettings
from packages.contracts import ACL, Chunk, SearchHit, Visibility
from packages.prompts import PromptRegistry
from packages.prompts.registry import get_prompt_registry
from packages.retrievers import compile_filters, reciprocal_rank_fusion
from packages.security import Identity
from packages.security.pii import contains_pii, redact

# ---------------- ID ----------------


def test_doc_id_is_stable_and_source_derived() -> None:
    a = stable_doc_id("data/corpus/employee_handbook.md")
    b = stable_doc_id("data/corpus/employee_handbook.md")
    c = stable_doc_id("data/corpus/other.md")
    assert a == b, "同一来源必须得到同一 doc_id，否则重跑索引会产生重复文档"
    assert a != c
    assert a.startswith("d_")


def test_chunk_id_composition() -> None:
    assert stable_chunk_id("d_abc", 7) == "d_abc:7"


def test_content_hash_changes_with_content() -> None:
    assert content_hash("甲") != content_hash("乙")


# ---------------- 过滤编译 ----------------


def test_compile_filters_full_visibility_matrix() -> None:
    filters = compile_filters(ACL(tenant_id="t1", department_id="hr", owner="u_1"))
    assert filters is not None
    assert filters["must"] == {"tenant_id": "t1"}
    clauses = filters["visibility_clauses"]
    assert {"visibility": "public"} in clauses
    assert {"visibility": "internal"} in clauses
    assert {"visibility": "department", "department_id": "hr"} in clauses
    assert {"visibility": "private", "owner": "u_1"} in clauses


def test_compile_filters_without_owner_omits_private() -> None:
    filters = compile_filters(ACL(tenant_id="t1", department_id="hr"))
    assert filters is not None
    assert all(c.get("visibility") != "private" for c in filters["visibility_clauses"])


def test_compile_filters_none_acl_is_opt_out() -> None:
    assert compile_filters(None) is None
    assert compile_filters(None, ["d_1"]) == {"doc_ids": ["d_1"]}


# ---------------- RRF ----------------


def _hit(chunk_id: str, score: float = 0.0, text: str = "内容") -> SearchHit:
    return SearchHit(chunk_id=chunk_id, doc_id="d_1", text=text, score=score)


def test_rrf_rewards_agreement_across_routes() -> None:
    vector = [_hit("d_1:0"), _hit("d_1:1")]
    bm25 = [_hit("d_1:1"), _hit("d_1:0")]
    fused = reciprocal_rank_fusion([("vector", vector), ("bm25", bm25)])
    # 两路都在前列的文档应当被顶上来，且分数相同
    assert {h.chunk_id for h in fused} == {"d_1:0", "d_1:1"}
    assert abs(fused[0].score - fused[1].score) < 1e-9


def test_rrf_weights_shift_priority() -> None:
    vector = [_hit("d_1:0"), _hit("d_1:1")]
    bm25 = [_hit("d_1:1"), _hit("d_1:0")]
    fused = reciprocal_rank_fusion(
        [("vector", vector), ("bm25", bm25)], weights={"vector": 10.0, "bm25": 0.1}
    )
    assert fused[0].chunk_id == "d_1:0"


def test_rrf_keeps_best_populated_hit() -> None:
    empty = _hit("d_1:0", text="")
    populated = _hit("d_1:0", text="真实正文")
    fused = reciprocal_rank_fusion([("vector", [empty]), ("bm25", [populated])])
    assert fused[0].text == "真实正文"
    assert fused[0].retriever == "vector+bm25"


def test_rrf_top_k_truncation() -> None:
    hits = [_hit(f"d_1:{i}") for i in range(10)]
    fused = reciprocal_rank_fusion([("vector", hits)], top_k=3)
    assert len(fused) == 3


# ---------------- 指标端点的鉴权豁免 ----------------


def test_metrics_path_is_exempt_only_when_token_configured() -> None:
    """/metrics 只有在配了 METRICS_TOKEN 时才免用户鉴权。

    网关是公网入口：若无条件放行 /metrics，任何人可读到内部路由与流量形态。
    Prometheus 又没有 Keycloak 令牌，所以「要求用户鉴权」和「可直接抓取」二者只能选一，
    这里的选择是——不配 token 就不放行（安全默认），配了 token 就由 token 自己保护。
    """
    from packages.security.config import SecuritySettings

    # 全部显式传参：本用例必须**与环境解耦**。
    # 第一版第一句写成 SecuritySettings()，在本地 .env 设了 METRICS_TOKEN 之后立刻失败——
    # 被测代码的行为是对的（配了 token 就该豁免），错的是测试依赖了开发者环境。
    assert "/metrics" not in SecuritySettings(metrics_token="").authz_exempt()
    assert "/metrics" in SecuritySettings(metrics_token="s3cret").authz_exempt()
    # 关掉指标后，这个豁免也不该存在（否则配置与实际行为对不上）
    assert (
        "/metrics"
        not in SecuritySettings(metrics_enabled=False, metrics_token="s3cret").authz_exempt()
    )
    # 原有豁免名单不受影响
    assert "/health" in SecuritySettings(metrics_token="s3cret").authz_exempt()


def test_root_path_is_always_exempt() -> None:
    """根路径必须免鉴权：它只做一次 302 跳到 /ui/，自身不返回数据。

    不放行的后果是「开启鉴权后无法登录」：登录页在 /ui，浏览器访问网关根地址
    先被身份中间件拦成 401 JSON，永远到不了登录页。
    """
    from packages.security.config import SecuritySettings

    # 显式传一个**不含 "/"** 的名单，证明这是代码兜底而不是配置默认值——
    # 已部署环境的 .env 早已显式配好列表，只改默认值对他们无效。
    settings = SecuritySettings(authz_exempt_paths="/health,/openapi.json,/docs,/redoc,/ui")
    assert "/" in settings.authz_exempt()


# ---------------- 提示注册表 ----------------


def test_prompt_registry_reads_versioned_template() -> None:
    registry = get_prompt_registry()
    template = registry.get("rag_answer", "v1")
    assert "{{context}}" in template
    assert {"refuse_text", "context", "query"} <= registry.variables("rag_answer", "v1")


@pytest.mark.parametrize("version", ["v1", "v2", "v3", "v4", "v5"])
def test_every_declared_prompt_version_renders(version: str) -> None:
    """模板是契约：`ANSWER_PROMPT_VERSION` 指向哪个版本，那个版本就必须存在且可渲染。

    这条测试的价值在于**版本切换前的快速失败**——配置指向一个不存在的模板时，
    若没有它，故障要等到线上请求才暴露（而且是每个请求都失败）。
    """
    registry = get_prompt_registry()
    variables = registry.variables("rag_answer", version)
    assert {"context", "query"} <= variables
    rendered = registry.render(
        "rag_answer",
        version,
        context="[1] 上下文",
        query="问题",
        refuse_marker=REFUSE_MARKER,
        refuse_text="不知道",
        no_context_notice=NO_CONTEXT_NOTICE,
        no_cite_marker=NO_CITE_MARKER,
    )
    assert "{{" not in rendered, "渲染后不能残留占位符"


def test_fallback_prompt_distinguishes_greeting_from_substantive() -> None:
    """降级提示词必须把「寒暄」和「实质问题」分开处理。

    为什么要钉这一条：检索为空时若只要求"声明知识库没有内容再回答"，
    模型会对「你好」也回一句"知识库中没有检索到相关内容"——用户问好却拿到免责声明，
    比不回答还糟。这两类必须显式分流，且寒暄分支禁止提到知识库。
    """
    template = get_prompt_registry().get("rag_fallback", "v1")
    assert "{{no_context_notice}}" in template, "缺少来源声明占位符"
    # A 类：寒暄必须存在，且明确禁止提"知识库"
    assert "A 类" in template and "不要" in template
    # B 类：实质问题必须要求先声明来源
    assert "B 类" in template
    # 无论哪一类都必须禁止编造引用
    assert "禁止" in template and "[1]" in template


def test_fallback_prompt_renders_with_notice() -> None:
    rendered = get_prompt_registry().render(
        "rag_fallback",
        "v1",
        query="年假多少天？",
        no_context_notice=NO_CONTEXT_NOTICE,
        no_cite_marker=NO_CITE_MARKER,
    )
    assert "{{" not in rendered
    assert NO_CONTEXT_NOTICE in rendered
    assert "年假多少天？" in rendered


def test_no_cite_marker_is_declared_in_v5_and_fallback() -> None:
    """v5 主提示词与降级提示词都必须要求输出「本回答不附引用」标记。

    不加这个标记时，guard 的「没标引用就兜底附 top1」会生效，导致两种荒谬结果：
    用户问「你好」得到挂着制度引用的回答；答案写着「资料中没有相关内容」
    却脚挂一条引用。两者都会让用户误以为通用建议有公司制度背书。
    """
    registry = get_prompt_registry()
    for name, version in (("rag_answer", "v5"), ("rag_fallback", "v1")):
        template = registry.get(name, version)
        assert "{{no_cite_marker}}" in template, f"{name}.{version} 缺少 no_cite_marker 占位符"
        rendered = registry.render(
            name,
            version,
            context="c",
            query="q",
            refuse_marker=REFUSE_MARKER,
            refuse_text="t",
            no_context_notice=NO_CONTEXT_NOTICE,
            no_cite_marker=NO_CITE_MARKER,
        )
        assert NO_CITE_MARKER in rendered, f"{name}.{version} 渲染后必须含标记"


def test_v4_declares_materials_are_data_not_instructions() -> None:
    """v4 的核心断言：把资料声明为**数据**，并禁止执行其中的指令。

    为什么这条要写成测试：注入防护的**行为**很难在单测里断言（要看模型表现），
    但"提示词里到底有没有这条规则"是可判定的——而它一旦被顺手删掉，
    行为会悄悄退化回 v3，且没有任何测试会失败。
    """
    template = get_prompt_registry().get("rag_answer", "v4")
    assert "数据" in template and "不是指令" in template
    assert "忽略" in template, "缺少「忽略资料中的指令」这一条"
    # 分节标记必须仍在模板里（服务端会转义资料中的同名标记，两者配合才成立）
    for marker in ("【参考资料】", "【问题】", "【回答要求】"):
        assert marker in template


def test_default_prompt_version_carries_every_safety_rule() -> None:
    """代码默认的模板版本必须**同时**具备哨兵与注入防护规则。

    为什么值得钉：把默认值留在旧版本（v2/v3 都没有"资料是数据不是指令"那条）时，
    **未配置 ANSWER_PROMPT_VERSION 的新部署会静默拿到旧模板**，
    注入防护形同不存在——而这不会报任何错，只在有人真的投毒时才发现。
    实际踩过一次：v4 已在 .env 生效，而 `LLMSettings` 的默认值还停在 v3。

    断言取 ``model_fields[...].default`` 而不是实例值：实例会读 .env，
    那样这条用例就变成"跟着本机配置走"的空检查（与本文件里另一处踩过的坑同源）。
    """
    from packages.llms.config import LLMSettings

    default = LLMSettings.model_fields["answer_prompt_version"].default
    template = get_prompt_registry().get("rag_answer", str(default))
    assert "{{refuse_marker}}" in template, f"默认版本 {default} 缺少拒答哨兵"
    assert "不是指令" in template, f"默认版本 {default} 缺少提示注入防护规则"
    assert "写明了" in template or "有没有这个答案" in template, (
        f"默认版本 {default} 缺少「资料写明答案才作答」的拒答收紧规则"
    )


@pytest.mark.parametrize("version", ["v2", "v3", "v4"])
def test_marker_based_versions_embed_the_sentinel(version: str) -> None:
    """v2 起改用哨兵 `NO_ANSWER`（见 ADR 0003）。

    断言分两段：模板里是**占位符**，渲染后才是**哨兵字面量**。
    少了哨兵，模型的拒答就只能靠自然语言匹配——那正是 ADR 0003 要摆脱的不可靠路径。
    """
    registry = get_prompt_registry()
    assert "{{refuse_marker}}" in registry.get("rag_answer", version), "模板里缺少哨兵占位符"
    rendered = registry.render(
        "rag_answer", version, context="c", query="q", refuse_marker=REFUSE_MARKER
    )
    assert REFUSE_MARKER in rendered, "渲染后必须把哨兵替换进去"


def test_prompt_render_rejects_missing_variable() -> None:
    registry = get_prompt_registry()
    with pytest.raises(ConfigError):
        registry.render("rag_answer", "v1", context="c", query="q")


def test_prompt_render_substitutes_all_placeholders() -> None:
    registry = get_prompt_registry()
    rendered = registry.render(
        "rag_answer", "v1", refuse_text="不知道", context="上下文", query="问题"
    )
    assert "{{" not in rendered
    assert "上下文" in rendered and "问题" in rendered and "不知道" in rendered


def test_prompt_registry_missing_template_raises(tmp_path) -> None:
    registry = PromptRegistry(tmp_path)
    with pytest.raises(ConfigError):
        registry.get("nope", "v9")


# ---------------- 身份校验 ----------------


def test_identity_to_acl_carries_owner_for_private_visibility() -> None:
    """private 可见性靠 owner 判定：请求方 ACL 必须带上 user_id。"""
    identity = Identity(user_id="u_1", tenant_id="t1", department_id="hr")
    acl = identity.to_acl()
    assert acl.owner == "u_1"
    assert identity.to_acl(owner=False).owner is None


def test_jwks_is_refreshed_once_when_verification_fails(monkeypatch) -> None:
    """Keycloak 轮换签名密钥后，必须强制刷新 JWKS 再试一次。

    否则在 JWKS 缓存过期前所有令牌都会被判非法，表现为「刚登录就 401」。
    """
    from packages.security import identity as identity_module
    from packages.security.config import SecuritySettings

    fetched: list[bool] = []
    attempts = {"n": 0}

    def fake_fetch(settings, *, force: bool = False):  # noqa: ANN001, ARG001
        fetched.append(force)
        return [{"kid": "k1"}]

    def fake_decode(token, settings, keys):  # noqa: ANN001, ARG001
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise identity_module.Unauthorized("签名校验失败")
        return {"sub": "u_1"}

    monkeypatch.setattr(identity_module, "_fetch_jwks", fake_fetch)
    monkeypatch.setattr(identity_module, "_decode_with_keys", fake_decode)

    assert identity_module.decode_keycloak_token("token", SecuritySettings()) == {"sub": "u_1"}
    assert fetched == [False, True], "第一次用缓存，失败后必须强制刷新"


def test_jwks_retry_failure_still_raises(monkeypatch) -> None:
    from packages.security import identity as identity_module
    from packages.security.config import SecuritySettings

    def fake_fetch(settings, *, force: bool = False):  # noqa: ANN001, ARG001
        return [{"kid": "k1"}]

    def always_fail(token, settings, keys):  # noqa: ANN001, ARG001
        raise identity_module.Unauthorized("令牌校验失败")

    monkeypatch.setattr(identity_module, "_fetch_jwks", fake_fetch)
    monkeypatch.setattr(identity_module, "_decode_with_keys", always_fail)

    with pytest.raises(identity_module.Unauthorized):
        identity_module.decode_keycloak_token("token", SecuritySettings())


# ---------------- 契约 ----------------


def test_chunk_round_trip_keeps_offsets() -> None:
    chunk = Chunk(
        chunk_id="d_1:0",
        doc_id="d_1",
        text="转正后凭发票报销。",
        chunk_index=0,
        char_start=10,
        char_end=20,
        acl=ACL(tenant_id="t1"),
    )
    restored = Chunk.model_validate(chunk.model_dump(mode="json"))
    assert restored.char_start == 10 and restored.char_end == 20
    assert restored.acl.visibility is Visibility.internal


def test_settings_env_prefix_free_contract(monkeypatch) -> None:
    """字段名即环境变量名（大小写不敏感），这是 .env.example 的契约。

    必须通过**环境变量**验证，不能用构造参数 ``BaseAppSettings(LOG_LEVEL=...)``：
    大小写不敏感匹配（``case_sensitive=False``）只作用于**环境变量读取**那条路径；
    构造参数走的是字段名 ``log_level``，传 ``LOG_LEVEL`` 会被 ``extra="ignore"``
    静默丢弃，于是断言读到的是 .env 或默认值——测到的是别的东西，
    在有 .env 的机器上必然失败、在没有 .env 的机器上也必然失败（默认值是 INFO）。
    """
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    settings = BaseAppSettings()
    assert settings.log_level == "DEBUG"


# ---------------- PII ----------------


def test_redact_masks_email_and_phone() -> None:
    text = "联系 alice@example.com 或 13800138000"
    masked = redact(text)
    assert "alice@example.com" not in masked
    assert "13800138000" not in masked
    assert "[EMAIL:" in masked and "[PHONE:" in masked


def test_contains_pii_detects_id_card() -> None:
    assert contains_pii("身份证 11010119900307561X")
    assert not contains_pii("这段文本没有敏感信息")

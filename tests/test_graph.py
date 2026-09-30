"""编排图的单元测试。

检索与生成都被 mock 掉，因此不依赖 Milvus / Ollama；
重点覆盖**路由分支**与**人工审核的挂起 / 恢复**。
"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph import build
from app.graph.nodes import draft as draft_module
from app.graph.nodes import retrieve as retrieve_module
from app.graph.nodes.finalize import finalize_node
from app.graph.nodes.intent import detect_intent
from app.graph.nodes.send import send_node
from app.schemas.retrieval import Chunk, DocSource, RetrievalMode
from app.schemas.ticket import Intent, ReviewStatus, SendStatus

KEYWORDS = ["投诉", "举报", "泄露"]


def _chunk(chunk_id: str = "doc#0") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc",
        content="ERR-4041 表示令牌已过期，请重新登录。",
        source=DocSource.MANUAL,
        title="常见错误码",
    )


def _debug() -> dict:
    return {
        "mode": "hybrid",
        "dense_hits": 1,
        "sparse_hits": 1,
        "fused": 1,
        "reranked": False,
        "dense_error": None,
        "sparse_error": None,
        "top": [],
    }


def _patch_retrieve(monkeypatch, chunks):
    monkeypatch.setattr(
        retrieve_module,
        "hybrid_search",
        lambda query, **kwargs: (chunks, RetrievalMode.HYBRID, _debug()),
    )


def _patch_draft(monkeypatch, text="草稿：请重新登录 [来源: doc#0]"):
    monkeypatch.setattr(draft_module, "generate", lambda system, user: text)


# ---------------- 纯函数：意图识别 ----------------


def test_detect_intent_marks_sensitive_on_keyword():
    intent, matched = detect_intent("我要投诉这个功能", KEYWORDS)
    assert intent == Intent.SENSITIVE.value
    assert matched == ["投诉"]


def test_detect_intent_defaults_to_consult():
    intent, matched = detect_intent("ERR-4041 怎么解决", KEYWORDS)
    assert intent == Intent.CONSULT.value
    assert matched == []


# ---------------- 纯函数：路由 ----------------


def test_route_after_retrieve_prefers_draft_when_hit():
    assert build.route_after_retrieve({"retrieved": [{"chunk_id": "a"}]}) == "draft"
    assert build.route_after_retrieve({"retrieved": []}) == "finalize"
    assert build.route_after_retrieve({}) == "finalize"


def test_route_after_draft_respects_need_review():
    assert build.route_after_draft({"need_review": True}) == "review"
    assert build.route_after_draft({"need_review": False}) == "finalize"
    assert build.route_after_draft({}) == "finalize"


def test_route_after_review_only_continues_when_approved():
    assert build.route_after_review({"review_status": ReviewStatus.APPROVED.value}) == "finalize"
    assert build.route_after_review({"review_status": ReviewStatus.REJECTED.value}) == "end"
    assert build.route_after_review({}) == "end"


# ---------------- 纯函数：定稿 ----------------


def test_finalize_prefers_edited_reply():
    state = {
        "draft": "原草稿",
        "review_status": ReviewStatus.APPROVED.value,
        "edited_reply": "人工修改后的文案",
    }
    assert finalize_node(state)["reply"] == "人工修改后的文案"


def test_finalize_uses_draft_after_approval():
    state = {"draft": "原草稿", "review_status": ReviewStatus.APPROVED.value}
    assert finalize_node(state)["reply"] == "原草稿"


def test_finalize_uses_draft_without_review():
    assert finalize_node({"draft": "原草稿"})["reply"] == "原草稿"


def test_finalize_falls_back_to_not_found():
    assert "未找到" in finalize_node({})["reply"]


# ---------------- 纯函数：发送 ----------------


def test_send_marks_sent():
    assert send_node({"reply": "内容"})["send_status"] == SendStatus.SENT.value


def test_send_skips_when_already_sent():
    state = {"reply": "", "send_status": SendStatus.SENT.value}
    assert send_node(state)["send_status"] == SendStatus.SENT.value


def test_send_fails_on_empty_reply():
    assert send_node({"reply": ""})["send_status"] == SendStatus.FAILED.value


# ---------------- 图：普通工单全流程 ----------------


def test_normal_ticket_runs_end_to_end(monkeypatch):
    _patch_retrieve(monkeypatch, [_chunk()])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-normal"}}

    result = graph.invoke(
        {"session_id": "s-normal", "ticket_id": "T-1", "query": "ERR-4041 怎么解决"}, config
    )

    assert result["intent"] == Intent.CONSULT.value
    assert result["need_review"] is False
    assert result["citations"] == ["doc#0"]
    assert "草稿" in result["reply"]
    assert result["send_status"] == SendStatus.SENT.value


def test_no_hit_returns_not_found_and_sends(monkeypatch):
    _patch_retrieve(monkeypatch, [])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-nohit"}}

    result = graph.invoke(
        {"session_id": "s-nohit", "ticket_id": "T-2", "query": "知识库里没有的问题"}, config
    )

    assert "未找到" in result["reply"]
    assert result["send_status"] == SendStatus.SENT.value


# ---------------- 图：敏感工单的挂起与恢复 ----------------


def test_sensitive_ticket_interrupts_and_waits(monkeypatch):
    _patch_retrieve(monkeypatch, [_chunk()])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-sensitive"}}

    result = graph.invoke(
        {"session_id": "s-sensitive", "ticket_id": "T-3", "query": "我要投诉登录问题"}, config
    )

    assert result["intent"] == Intent.SENSITIVE.value
    assert result["need_review"] is True
    assert "__interrupt__" in result  # 已在审核节点挂起
    assert not result.get("send_status")  # 挂起时绝不能发送

    payload = result["__interrupt__"][0].value
    assert payload["ticket_id"] == "T-3"
    assert payload["reason"] == "命中敏感关键词"


def test_review_approved_resumes_and_sends(monkeypatch):
    _patch_retrieve(monkeypatch, [_chunk()])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-approve"}}
    graph.invoke(
        {"session_id": "s-approve", "ticket_id": "T-4", "query": "投诉登录问题"}, config
    )

    result = graph.invoke(Command(resume={"decision": "approved", "comment": "可以发"}), config)

    assert result["review_status"] == ReviewStatus.APPROVED.value
    assert result["reviewer_comment"] == "可以发"
    assert result["send_status"] == SendStatus.SENT.value


def test_review_approved_uses_edited_reply(monkeypatch):
    _patch_retrieve(monkeypatch, [_chunk()])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-edited"}}
    graph.invoke({"session_id": "s-edited", "ticket_id": "T-5", "query": "投诉登录问题"}, config)

    result = graph.invoke(
        Command(resume={"decision": "approved", "edited_reply": "人工改写后的回复"}), config
    )

    assert result["reply"] == "人工改写后的回复"


def test_review_rejected_ends_without_sending(monkeypatch):
    _patch_retrieve(monkeypatch, [_chunk()])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-reject"}}
    graph.invoke({"session_id": "s-reject", "ticket_id": "T-6", "query": "投诉登录问题"}, config)

    result = graph.invoke(Command(resume={"decision": "rejected", "comment": "措辞不合规"}), config)

    assert result["review_status"] == ReviewStatus.REJECTED.value
    assert not result.get("reply")
    assert not result.get("send_status")


def test_review_malformed_payload_is_rejected(monkeypatch):
    """审核载荷异常时必须按驳回处理（不能误发）。"""
    _patch_retrieve(monkeypatch, [_chunk()])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-bad"}}
    graph.invoke({"session_id": "s-bad", "ticket_id": "T-7", "query": "投诉登录问题"}, config)

    result = graph.invoke(Command(resume="随便一个字符串"), config)

    assert result["review_status"] == ReviewStatus.REJECTED.value
    assert not result.get("send_status")


# ---------------- 图：生成失败降级 ----------------


def test_draft_failure_falls_back_to_excerpts(monkeypatch):
    from app.core.errors import AppError, ErrorCode

    _patch_retrieve(monkeypatch, [_chunk()])

    def boom(system, user):
        raise AppError(ErrorCode.GENERATION_SERVICE_UNAVAILABLE, "ollama down")

    monkeypatch.setattr(draft_module, "generate", boom)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-fallback"}}

    result = graph.invoke(
        {"session_id": "s-fallback", "ticket_id": "T-8", "query": "ERR-4041 怎么解决"}, config
    )

    assert "生成服务暂时不可用" in result["reply"]
    assert "doc#0" in result["reply"]
    assert result["send_status"] == SendStatus.SENT.value


def test_draft_streams_tokens_only_when_explicitly_enabled(monkeypatch):
    """默认走非流式；只有 config 里显式开启 stream_tokens 才逐 token 流式。

    这条守住一个易踩的坑：`get_stream_writer()` 在非流式 invoke 下也会返回可调用对象，
    若据此判断会误入流式分支（表现为单元测试静默调用真实模型）。
    """
    import asyncio

    _patch_retrieve(monkeypatch, [_chunk()])
    monkeypatch.setattr(draft_module, "generate", lambda system, user: "非流式草稿")
    monkeypatch.setattr(draft_module, "generate_stream", lambda system, user: iter(["流式", "草稿"]))

    graph = build.build_graph(checkpointer=InMemorySaver())

    async def run(stream_tokens: bool):
        config = {"configurable": {"thread_id": f"s-{stream_tokens}", "stream_tokens": stream_tokens}}
        events = []
        async for mode, chunk in graph.astream(
            {"session_id": f"s-{stream_tokens}", "ticket_id": "T-S", "query": "ERR-4041"},
            config,
            stream_mode=["updates", "custom"],
        ):
            events.append((mode, chunk))
        return events

    # 未开启：无 token 事件，草稿来自 generate
    plain = asyncio.run(run(False))
    assert [c for m, c in plain if m == "custom"] == []

    # 已开启：token 事件按序推送，且能拼回草稿
    streamed = asyncio.run(run(True))
    tokens = [c["delta"] for m, c in streamed if m == "custom"]
    assert tokens == ["流式", "草稿"]
    drafts = [c["draft"]["draft"] for m, c in streamed if m == "updates" and "draft" in c]
    assert drafts == ["流式草稿"]


def test_state_is_json_serializable(monkeypatch):
    """状态里必须是原生 JSON 值，否则 Checkpointer 反序列化会告警（甚至未来版本直接失败）。"""
    import json

    _patch_retrieve(monkeypatch, [_chunk()])
    _patch_draft(monkeypatch)
    graph = build.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "s-json"}}

    result = graph.invoke(
        {"session_id": "s-json", "ticket_id": "T-9", "query": "ERR-4041 怎么解决"}, config
    )

    json.dumps(result["retrieved"])  # 不抛异常即通过
    json.dumps(result["retrieval_debug"])
    assert isinstance(result["retrieved"][0]["source"], str)  # 枚举已转成值


# ---------------- 图：单例与配置 ----------------


def test_thread_config_uses_session_id():
    from app.memory.checkpointer import thread_config

    assert thread_config("S-abc") == {"configurable": {"thread_id": "S-abc"}}

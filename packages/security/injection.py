"""提示注入检测。

**只检测，不阻断。** 这个取舍值得写清楚：

* 阻断的前提是能判定"这段文字是不是在给模型下指令"，而制度文档里"必须/不得/应当"
  是常态文本，基于模式的阻断必然误杀——误杀的代价（正常问题被拒答）比漏检更常见、
  更直接。用一串正则去决定是否拒绝回答，等于把可用性押在正则的精确度上。
* 真正的防线在别处：提示词把资料声明为**数据**（见 ``configs/prompts/rag_answer.v4.txt``），
  并把【回答要求】放在资料**之后**（模型对靠后内容更敏感），
  再配合 ``services/model-gateway/app/router.py`` 里对分节标记的转义。
* 检测的价值是让尝试**可见**：否则被注入的文档会安静地躺在知识库里，
  既不知道有人在尝试，也说不清"这次答错是不是因为注入"。

因此判据刻意收紧（宁可漏掉几个变体，也不误报正常制度文本），
并由 ``test_real_corpus_is_not_flagged`` 用**真实语料**钉住误报率——
在权限系统的安全模块里，误报率是要被测试固定住的指标，不是主观感觉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 每条规则一个名字，便于在指标与日志里定位是哪一类尝试。
# 命名用英文：它会出现在指标标签里（Prometheus 标签值不适合中文）。
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 要求忽略既有规则——必须和"指令/要求/规则"同现，避免误伤"忽略空格"这类正常表述
    (
        "ignore_instructions",
        re.compile(
            r"忽略(?:以上|之前|上面|前面|上述)?[^。\n]{0,6}?(?:指令|指示|要求|规则|设定|提示)"
        ),
    ),
    # 改变身份/角色
    (
        "role_override",
        re.compile(r"你现在是|从现在开始你|你的新身份|扮演(?:一个)?|假装你是|you are now"),
    ),
    # 套取提示词或要求复述上文
    (
        "prompt_leak",
        re.compile(
            r"(?:输出|告诉我|复述|重复|打印)[^。\n]{0,10}?"
            r"(?:提示词|prompt|指令原文|全部内容|以上内容|上面的话)"
        ),
    ),
    # 伪造本项目的提示词分节标记（真正的结构性攻击）
    ("forge_section", re.compile(r"【(?:参考资料|问题|回答要求)】")),
    # 强制绕过拒答
    (
        "force_answer",
        re.compile(r"(?:不要|不得|禁止)拒答|无条件回答|无论如何都要?回答|必须给出答案|不许说没有"),
    ),
)

# 摘录长度：够定位问题，又不至于把整段话写进日志
_EXCERPT_LEN = 120


@dataclass(frozen=True)
class InjectionFinding:
    """一处可疑内容。``source`` 区分是用户提问还是检索到的资料。"""

    source: str
    rules: tuple[str, ...]
    excerpt: str
    doc_id: str = ""
    chunk_id: str = ""


def scan_text(text: str) -> tuple[str, ...]:
    """返回命中的规则名（无命中返回空元组）。"""
    if not text:
        return ()
    return tuple(name for name, pattern in _RULES if pattern.search(text))


def _excerpt(text: str) -> str:
    """截取命中位置附近的一小段，便于排查时不必回捞原文。"""
    flat = " ".join(text.split())
    return flat[:_EXCERPT_LEN]


def scan_request(
    *, query: str, contexts: list[tuple[str, str, str, str]]
) -> list[InjectionFinding]:
    """扫描一次生成请求。

    Args:
        query: 用户提问（直接注入面）。
        contexts: ``(doc_id, chunk_id, doc_title, text)`` 四元组列表（间接注入面）。
    """
    findings: list[InjectionFinding] = []

    rules = scan_text(query)
    if rules:
        findings.append(InjectionFinding(source="query", rules=rules, excerpt=_excerpt(query)))

    for doc_id, chunk_id, title, text in contexts:
        rules = scan_text(f"{title}\n{text}")
        if rules:
            findings.append(
                InjectionFinding(
                    source="context",
                    rules=rules,
                    excerpt=_excerpt(text),
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                )
            )
    return findings


def summarize(findings: list[InjectionFinding]) -> str:
    """一行式摘要，供日志使用。"""
    return "; ".join(
        f"{item.source}[{','.join(item.rules)}]{'/' + item.doc_id if item.doc_id else ''}"
        f": {item.excerpt}"
        for item in findings
    )


__all__ = ["InjectionFinding", "scan_request", "scan_text", "summarize"]

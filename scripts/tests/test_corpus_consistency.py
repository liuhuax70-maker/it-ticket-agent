"""语料一致性检查的测试。

重点不是"干净语料能通过"（那是必要条件但不是充分条件），
而是**每类问题都能被抓到**：一个从不失败的检查等于没有检查。
第 2 条用例直接用历史上真实发生过的冲突做输入。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_corpus_consistency import (  # noqa: E402
    MIN_DOC_CHARS,
    Document,
    check_duplicate_sentences,
    check_facts,
    check_placeholders,
    check_references,
    check_structure,
    load_documents,
    load_facts,
    run,
)

FACTS = [{"key": "核心工作时间", "pattern": r"核心工作时间为([^，。；\n]+)"}]


def _doc(name: str, body: str) -> Document:
    return Document(name, body)


# ---------------- 规范值 ----------------


def test_detects_the_historical_contradiction() -> None:
    """历史上真实发生过的冲突：两篇文档对核心工作时间给了不同值。

    这条用例是本次改动的由来——检查必须能抓住它，否则白做。
    """
    docs = [
        _doc(
            "attendance.md",
            "# 考勤与休假制度\n\n公司实行弹性工作制，核心工作时间为每日 10:00 至 16:00，标准工时为每日八小时。",
        ),
        _doc(
            "handbook.md",
            "# 员工手册\n\n公司实行弹性工作制，核心工作时间为每日九点三十分至十八点三十分，午休一小时。",
        ),
    ]
    findings = check_facts(docs, FACTS)
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "规范值冲突" in findings[0].check
    assert "attendance.md" in findings[0].message and "handbook.md" in findings[0].message


def test_consistent_values_pass() -> None:
    docs = [
        _doc("a.md", "# A\n\n核心工作时间为每日 10:00 至 16:00。"),
        _doc("b.md", "# B\n\n核心工作时间为每日 10:00 至 16:00。"),
    ]
    assert check_facts(docs, FACTS) == []


def test_vacuous_pattern_is_an_error() -> None:
    """规则匹配不到任何东西必须报错。

    静默变空的检查比没有检查更糟——它会让人以为"查过了没问题"。
    """
    docs = [_doc("a.md", "# A\n\n正文里没有那句话。")]
    findings = check_facts(docs, FACTS)
    assert len(findings) == 1
    assert "规则失效" in findings[0].check


def test_pattern_without_single_group_is_rejected() -> None:
    docs = [_doc("a.md", "# A\n\n核心工作时间是每日十点。")]
    findings = check_facts(docs, [{"key": "k", "pattern": "核心工作时间"}])
    assert any("捕获组" in f.message for f in findings)


# ---------------- 引用 / 结构 / 占位符 ----------------


def test_dangling_reference_is_an_error() -> None:
    docs = [
        _doc("handbook.md", "# 员工手册\n\n病假证明的要求详见《考勤与休假制度》。"),
    ]
    findings = check_references(docs)
    assert len(findings) == 1
    assert "《考勤与休假制度》" in findings[0].message


def test_reference_to_existing_title_passes() -> None:
    docs = [
        _doc("handbook.md", "# 员工手册\n\n病假证明的要求详见《考勤与休假制度》。"),
        _doc("attendance.md", "# 考勤与休假制度\n\n病假需提交医疗机构证明。"),
    ]
    assert check_references(docs) == []


def test_missing_title_and_stub_are_errors() -> None:
    docs = [_doc("bad.md", "只有一句话，没有标题。")]
    findings = check_structure(docs)
    checks = {f.message.split(":")[1].strip()[:4] for f in findings}
    assert any("缺一级标题" in f.message for f in findings)
    assert len(checks) >= 1


def test_short_but_complete_document_is_not_a_stub() -> None:
    """短而完整的文档（如个人笔记夹具）不该被判为半成品。

    误报会让人不再看这个检查——它抓的是"没写完就提交"，不是"篇幅不够长"。
    夹具按真实笔记的体量写（约 120 字），否则测的就不是这条性质了。
    """
    note = (
        "# 入职交接清单（个人笔记）\n\n## 交接事项\n\n"
        "1. 移交招聘系统管理员权限\n"
        "2. 移交候选人体检安排台账\n"
        "3. 领取新工位门禁卡并在门禁系统中完成登记\n"
        "4. 完成本季度绩效面谈记录归档\n\n"
        "本笔记仅本人可见，不上传到部门共享目录。\n"
    )
    assert MIN_DOC_CHARS <= len(note) < 200, f"夹具体量不合适：{len(note)} 字"
    assert check_structure([_doc("note.md", note)]) == []


def test_truly_truncated_document_is_a_stub() -> None:
    """只有标题一两句话的才是半成品——这才是这条检查要抓的东西。"""
    stub = "# 采购与供应商管理制度\n\n（编者在补充）\n"
    findings = check_structure([_doc("stub.md", stub)])
    assert any("不像一份完整制度" in f.message for f in findings)


def test_placeholder_is_an_error() -> None:
    docs = [_doc("a.md", "# A\n\n本制度自 TODO 起生效。")]
    findings = check_placeholders(docs)
    assert len(findings) == 1
    assert "TODO" in findings[0].message


# ---------------- 重复句 ----------------


def test_unprotected_duplicate_sentence_warns() -> None:
    """既重复、又没规则守着的句子要告警：改一处就静默漂移。"""
    body = "报销需在费用发生之日起十五个自然日内提交，逾期不再受理。"
    docs = [_doc("a.md", f"# A\n\n{body}"), _doc("b.md", f"# B\n\n{body}")]
    findings = check_duplicate_sentences(docs, fact_patterns=[])
    assert len(findings) == 1
    assert findings[0].level == "warn"
    assert "无规则守护" in findings[0].check


def test_duplicate_covered_by_a_fact_rule_does_not_warn() -> None:
    """被规范值规则守住的重复可以接受——正是规则让重复变得安全。

    否则"员工手册复述工作时间"这种合理写法会一直告警，
    而告警一多就没人看了。
    """
    body = "核心工作时间为每日 10:00 至 16:00。"
    docs = [_doc("a.md", f"# A\n\n{body}"), _doc("b.md", f"# B\n\n{body}")]
    import re

    patterns = [re.compile(FACTS[0]["pattern"])]
    assert check_duplicate_sentences(docs, patterns) == []


# ---------------- 真实语料 ----------------


def test_real_corpus_is_clean() -> None:
    """真实语料当前必须零错误（CI 里由脚本本身承担，这里再钉一次）。"""
    docs = load_documents()
    facts = load_facts()
    assert len(docs) >= 14, f"语料文件数异常（{len(docs)}），测试可能没读到真实数据"
    assert len(facts) >= 15, f"规范值规则数异常（{len(facts)}）"

    errors = [f for f in run(docs, facts) if f.level == "error"]
    assert errors == [], f"真实语料存在一致性问题: {[f.message for f in errors]}"

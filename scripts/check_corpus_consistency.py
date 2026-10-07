"""语料一致性检查：把"靠偶然发现文档互相矛盾"变成机制。

存在理由（不是假想的）：实测中「核心工作时间」在两篇文档里取过不同值
（9:30-18:30 与 10:00-16:00），用户拿到哪个答案取决于检索命中了哪一篇。
ACL 兜不住这类问题（两篇文档该身份都能看），提示词也兜不住——
只能靠数据治理发现，而"人工通读几万字制度文档发现自己在两处写了不同数字"不现实。

五类检查，每类的"严重度"是刻意区分的：

======================  ========  ==============================================
检查                     严重度     说明
======================  ========  ==============================================
规范值冲突                 error     同一事实在不同文档取值不同（声明式规则，见 configs/corpus/normative_facts.yaml）
规范值规则失效             error     声明了规则但一处都匹配不到（措辞改了/文档删了），检查会静默变空
交叉引用断链               error     「详见《X》」而《X》并不存在（引用是本文档的治理手段，断链即失去依据）
占位符残留                 error     TODO/待补充/XXX 之类被带进了语料
文档结构                   error     缺一级标题、或短到不像一份制度
跨文档重复句               warn      同一句原样出现在多处：现在一致，但**将来必然漂移**
======================  ========  ==============================================

为什么"规则失效"也算错误：一个静默匹配不到任何东西的一致性检查，
比没有检查更糟——它会让人以为"查过了没问题"。这跟告警配错指标名是同一类失效。

用法：
    python scripts/check_corpus_consistency.py
    python scripts/check_corpus_consistency.py --corpus-dir data/corpus
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

from packages.common.constants import LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED
from packages.common.lifecycle import LifecycleDeclaration, load_declarations
from packages.common.lifecycle import resolve as resolve_lifecycle

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
DEFAULT_FACTS = REPO_ROOT / "configs" / "corpus" / "normative_facts.yaml"
DEFAULT_LIFECYCLE = REPO_ROOT / "configs" / "corpus" / "lifecycle.yaml"
DEFAULT_CORPUS_DIRS = (REPO_ROOT / "data" / "corpus", REPO_ROOT / "data" / "corpus_permissions")

_SENTENCE = re.compile(r"[^。；\n]+[。；]?")
_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_REFERENCE = re.compile(r"《([^》]+)》")
_PLACEHOLDER = re.compile(r"TODO|FIXME|待补充|待完善|占位|XXX|xxx", re.IGNORECASE)
# 归一化时去掉的字符：空格/标点/常见虚词，让"用词微差"的同一句能对上
_NORMALIZE = re.compile(r"[\s，。；、（）()的了]")

# 引用了语料之外的正式文件（法律法规等）时，把名称登记在这里。
# 允许名单机制是必要的：没有它，第一条合法的外部引用就会让检查失败，
# 然后有人会把整个检查加 `|| true` 绕过——那比不做还糟。
EXTERNAL_REFERENCES: frozenset[str] = frozenset()

# 只用来抓"只有标题或一两句话"的半成品。刻意定得低：
# 短而完整的文档（如个人笔记类夹具）不该被误伤——误报会让人不再看这个检查，
# 而它抓的是"没写完就提交"，不是"篇幅不够长"。
MIN_DOC_CHARS = 80
MIN_SENTENCE_CHARS = 12


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warn"
    check: str
    message: str

    def render(self) -> str:
        tag = "[FAIL]" if self.level == "error" else "[warn]"
        return f"  {tag} {self.check}: {self.message}"


@dataclass(frozen=True)
class Document:
    name: str
    text: str

    @property
    def title(self) -> str:
        match = _TITLE.search(self.text)
        return match.group(1).strip() if match else ""

    def sentences(self) -> list[str]:
        return [
            s.strip() for s in _SENTENCE.findall(self.text) if len(s.strip()) >= MIN_SENTENCE_CHARS
        ]


def load_documents(dirs: tuple[Path, ...] = DEFAULT_CORPUS_DIRS) -> list[Document]:
    docs: list[Document] = []
    for directory in dirs:
        for path in sorted(directory.glob("*.md")):
            docs.append(Document(path.name, path.read_text(encoding="utf-8")))
    return docs


def load_facts(path: Path = DEFAULT_FACTS) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"找不到规范值声明文件：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("facts") or [])


# ---------------- 各类检查 ----------------


def retired_names(docs: list[Document], declarations: dict[str, LifecycleDeclaration]) -> set[str]:
    """当前判定为"已废止"的文档名。

    这些文档**合法地**与现行文档取值不同（它们就是被取代的旧值），
    所以规范值一致性检查必须跳过它们——否则每份历史存档都会触发冲突，
    而"存档与现行不同"恰恰是存档存在的意义。
    """
    if not declarations:
        return set()
    return {
        doc.name
        for doc in docs
        if resolve_lifecycle(doc.name, declarations).lifecycle == LIFECYCLE_RETIRED
    }


def check_lifecycle(
    docs: list[Document], declarations: dict[str, LifecycleDeclaration]
) -> list[Finding]:
    """生命周期声明自身的检查。

    这里查的是**声明与语料对不上**的两类问题，都是静默失效：
    声明指向了不存在的文件（永远不会生效）、
    以及失效日期已过却没标废止（文档会被继续检索引用）。
    """
    if not declarations:
        return []
    names = {doc.name for doc in docs}
    findings: list[Finding] = []

    # 声明指向不存在的文件：永远不会生效，而写它的人以为生效了
    for filename in sorted(declarations):
        if filename not in names:
            findings.append(
                Finding(
                    "error", "生命周期声明", f"{filename}: 声明了生命周期，但语料里没有这份文档"
                )
            )

    # 声明与判定不一致：status 写 active，但失效日期已过（判定按 retired）。
    # 这不是功能问题（结果是对的），但**读声明的人会误判**——
    # 他会以为这份文档还在被引用。这类"文档与行为不一致"正是后期最难发现的坑。
    for filename in sorted(declarations):
        declaration = declarations[filename]
        if declaration.status != LIFECYCLE_ACTIVE or not declaration.effective_to:
            continue
        state = resolve_lifecycle(filename, declarations)
        if state.lifecycle == LIFECYCLE_RETIRED:
            findings.append(
                Finding(
                    "warn",
                    "生命周期声明不一致",
                    f"{filename}: status=active 但失效日期 {declaration.effective_to} 已过，"
                    "实际按 retired 处理——请把 status 改为 retired，避免误读",
                )
            )
    return findings


def check_facts(
    docs: list[Document],
    facts: list[dict[str, str]],
    *,
    skip: set[str] | None = None,
) -> list[Finding]:
    skip = skip or set()
    findings: list[Finding] = []
    for fact in facts:
        key = str(fact.get("key", "")).strip()
        pattern = str(fact.get("pattern", ""))
        if not key or not pattern:
            findings.append(Finding("error", "规范值声明", f"条目缺少 key 或 pattern：{fact}"))
            continue
        compiled = re.compile(pattern)
        if compiled.groups != 1:
            findings.append(
                Finding(
                    "error",
                    "规范值声明",
                    f"{key}: 正则必须有且只有一个捕获组（当前 {compiled.groups} 个）",
                )
            )
            continue

        values: dict[str, list[str]] = defaultdict(list)
        for doc in docs:
            # 已废止的文档不参与比较：它们的取值本就应该与现行版本不同
            if doc.name in skip:
                continue
            for match in compiled.finditer(doc.text):
                values[match.group(1).strip()].append(doc.name)

        if not values:
            findings.append(
                Finding(
                    "error",
                    "规范值规则失效",
                    f"{key}: 规则在语料里一处都匹配不到——措辞可能改了，检查已静默变空",
                )
            )
            continue
        if len(values) > 1:
            detail = "；".join(
                f"{value!r} 见 {sorted(set(where))}" for value, where in values.items()
            )
            findings.append(Finding("error", "规范值冲突", f"{key}: {detail}"))
    return findings


def check_references(docs: list[Document]) -> list[Finding]:
    titles = {doc.title for doc in docs if doc.title}
    findings: list[Finding] = []
    for doc in docs:
        for name in sorted(set(_REFERENCE.findall(doc.text))):
            if name in titles or name in EXTERNAL_REFERENCES:
                continue
            findings.append(
                Finding(
                    "error", "交叉引用断链", f"{doc.name} 引用了《{name}》，但语料里没有这份文档"
                )
            )
    return findings


def check_duplicate_sentences(
    docs: list[Document], fact_patterns: list[re.Pattern[str]] | None = None
) -> list[Finding]:
    """跨文档重复句报**警告**，且只报"没有被规范值规则看住"的那些。

    重复本身不是错误：员工手册复述工作时间、金额、时限是合理的，
    逼着所有文档都写成"详见某某制度"会让每份文档单独都读不出东西。
    **让重复变得安全的是规范值规则**——它会在两处取值不一致时失败。

    所以这里只对"既重复、又没有任何规则守着"的情况告警：
    那句话将来一改就是静默漂移，而没人会发现。这条告警是行动项：
    要么给它加一条规则，要么收敛到单一来源。
    """
    patterns = fact_patterns or []
    where: dict[str, set[str]] = defaultdict(set)
    sample: dict[str, str] = {}
    for doc in docs:
        for sentence in doc.sentences():
            key = _NORMALIZE.sub("", sentence)
            where[key].add(doc.name)
            sample.setdefault(key, sentence)

    findings: list[Finding] = []
    for key, names in where.items():
        if len(names) <= 1:
            continue
        sentence = sample[key]
        if any(pattern.search(sentence) for pattern in patterns):
            continue  # 已被规范值规则看住，重复可以接受
        findings.append(
            Finding(
                "warn",
                "跨文档重复句（无规则守护）",
                f"{sentence[:50]}… 同时出现在 {sorted(names)}"
                "——该句没有任何规范值规则守着，改一处就会静默漂移；"
                "请给它加一条规则，或收敛到单一来源",
            )
        )
    return findings


def check_placeholders(docs: list[Document]) -> list[Finding]:
    findings: list[Finding] = []
    for doc in docs:
        hits = sorted({m.group(0) for m in _PLACEHOLDER.finditer(doc.text)})
        if hits:
            findings.append(Finding("error", "占位符残留", f"{doc.name}: {hits}"))
    return findings


def check_structure(docs: list[Document]) -> list[Finding]:
    findings: list[Finding] = []
    for doc in docs:
        if not doc.title:
            findings.append(Finding("error", "文档结构", f"{doc.name}: 缺一级标题（# 标题）"))
        if len(doc.text) < MIN_DOC_CHARS:
            findings.append(
                Finding(
                    "error",
                    "文档结构",
                    f"{doc.name}: 正文只有 {len(doc.text)} 字（<{MIN_DOC_CHARS}），不像一份完整制度",
                )
            )
    return findings


def run(
    docs: list[Document],
    facts: list[dict[str, str]],
    declarations: dict[str, LifecycleDeclaration] | None = None,
) -> list[Finding]:
    declarations = declarations or {}
    # 已废止的文档不参与规范值比较：它们的取值本就应该与现行版本不同
    skip = retired_names(docs, declarations)
    active = [doc for doc in docs if doc.name not in skip]
    patterns = [re.compile(str(f["pattern"])) for f in facts if f.get("pattern")]
    return [
        *check_structure(docs),
        *check_placeholders(docs),
        *check_lifecycle(docs, declarations),
        *check_facts(active, facts),
        *check_references(docs),
        *check_duplicate_sentences(active, patterns),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="语料一致性检查")
    parser.add_argument("--facts", default=str(DEFAULT_FACTS))
    parser.add_argument("--lifecycle", default=str(DEFAULT_LIFECYCLE))
    parser.add_argument(
        "--corpus-dir",
        action="append",
        default=None,
        help="语料目录（可重复指定，默认 data/corpus 与 data/corpus_permissions）",
    )
    args = parser.parse_args()

    dirs = tuple(Path(d) for d in args.corpus_dir) if args.corpus_dir else DEFAULT_CORPUS_DIRS
    docs = load_documents(dirs)
    facts = load_facts(Path(args.facts))
    declarations = load_declarations(Path(args.lifecycle))
    if not docs:
        print(f"没有在 {[str(d) for d in dirs]} 找到任何 .md —— 检查无从谈起")
        return 1

    findings = run(docs, facts, declarations)
    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]

    print(f"语料一致性检查：{len(docs)} 篇文档、{len(facts)} 条规范值规则")
    print()
    if not findings:
        print("  未发现问题")
    else:
        by_check: dict[str, list[Finding]] = defaultdict(list)
        for finding in findings:
            by_check[finding.check].append(finding)
        for name, items in by_check.items():
            print(f"== {name}（{len(items)}）==")
            for finding in items:
                print(finding.render())
            print()

    print(f"结论：{len(errors)} 个错误、{len(warns)} 个警告")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

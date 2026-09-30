"""评估集生成器：从知识库片段合成「带可信标注」的问答样本。

为什么用「生成 + 校验」而不是让模型自由出题：

    评估集的价值取决于 **expected_chunk_ids 标得准不准**。
    若只让模型「提几个问题」，它可能问到片段外的内容，
    于是「系统没召回」就被误判成「检索效果差」，指标彻底失真。

三道质量闸：

1. **答案要点必须是片段原话** —— 要求模型从片段里原样摘录一句，
   再用「归一化后是否被片段包含」验证。验证不过就丢弃，
   这样剩下的样本可以保证「这个问题确实能从该片段回答」。
2. **同一片段内按答案要点去重** —— 若两条问题的答案摘录相同，
   说明它们只是换了个说法问同一件事（实测这是最严重的刷量来源：
   同一片段生成的 4 条问题可能全部等价），只保留一条。
3. **去重** —— 归一化问题文本后去重，避免同一问法刷多条。
4. **负样本领域隔离** —— 负样本问题里若出现知识库的专有名词/错误码/版本号，
   说明它其实是可回答的，直接剔除。

可断点续跑：脚本以 **追加** 方式写入评估集，并跳过已生成过的片段，
因此可以分多次运行逐步补到目标条数。

用法：
    python -m evaluation.generate_testset --per-chunk 4 --target-positives 140
    python -m evaluation.generate_testset --negatives 20
"""

import argparse
import json
import re
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.generation.ollama import generate
from app.retrieval.milvus_store import get_client

logger = get_logger(__name__)

TESTSET_PATH = Path(__file__).parent / "testset.jsonl"

SYSTEM_PROMPT = "你是企业 IT/客服知识库的测试集生成助手。只输出 JSON 数组，不要任何解释或代码块围栏。"

POSITIVE_TEMPLATE = """下面是企业知识库中的一个片段（来自 {doc_id}）：

----------------
{content}
----------------

请基于【且仅基于】上面这段内容，生成 {n} 个真实用户会提出的问题，并满足：

1. 风格要求：{style_hint}
2. {n} 个问题必须针对这段内容里的【不同信息点】，不要换着说法重复问同一件事；
   每个问题的答案要点应当互不相同。
3. 每个问题必须能仅凭这段内容回答，不要引入片段外的信息。
4. 每个问题附一句「答案要点」，必须是上面片段中的【原话摘录】，一字不改；
   只摘录能回答该问题的那一两句（控制在 60 字以内），不要整段照抄。
5. 问题要具体，避免「这个怎么用」这类空泛表述。

只输出 JSON 数组：
[{{"q": "问题", "a": "片段原话摘录"}}]"""

NEGATIVE_TEMPLATE = """我们的知识库只覆盖这几类 IT 问题：登录认证、VPN 接入、邮箱客户端、终端防护、网络故障、账号权限。

请生成 {n} 个【不属于上述范围】的员工常见提问，例如行政、人事、财务、餐饮、班车、工位、体检、团建、社保、采购等领域。

要求：

1. 问题要真实具体，像员工真的会问的。
2. 绝对不能涉及登录、令牌、VPN、邮箱、终端防护、网络、账号权限等知识库已有的内容。
3. 每个问题附一句「期望行为」，说明知识库没有相关资料、应回答「根据现有资料无法确定」并建议转人工。

只输出 JSON 数组：
[{{"q": "问题", "a": "期望行为"}}]"""

#: 三种出题风格轮换，避免 200 条问题全是同一种腔调
STYLE_HINTS = [
    "偏专有名词/错误码/版本号，直接问 ERR-xxxx、v1.x.x 这类具体标识的含义或处理办法",
    "偏口语化，模拟用户描述现象，问题中不要出现 ERR-xxxx、版本号这类标识词",
    "偏场景化，描述用户实际遇到的情境（例如「升级之后……」「换了设备之后……」）",
]

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[，。；、：！？,.;:!?（）()「」【】《》\"']")
_ERR_CODE = re.compile(r"ERR-\d{3,}", re.IGNORECASE)
_VERSION = re.compile(r"v\d+\.\d+", re.IGNORECASE)
#: 知识库领域词：负样本里出现就说明它其实可回答，必须剔除
_DOMAIN_TERMS = (
    "令牌", "登录", "VPN", "IMAP", "SMTP", "证书", "病毒库", "DNS", "MFA",
    "工单", "邮箱", "密码", "网关", "隔离区", "账号", "权限", "终端防护",
)
#: 问题最短长度，过滤「这是什么？」这类无信息量的提问
_MIN_QUESTION_CHARS = 8


def _normalize(text: str) -> str:
    """归一化：去掉空白与标点，只比字符序列。"""
    return _PUNCT.sub("", _WS.sub("", text))


def _extract_json_array(raw: str) -> list[dict]:
    """从模型输出里抠出 JSON 数组（模型常加 ```json 围栏或前后废话）。"""
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("JSON 解析失败，本段丢弃: %s", raw[:120].replace("\n", " "))
        return []
    return [item for item in data if isinstance(item, dict)]


def _is_lexical(question: str) -> bool:
    """只看是否出现错误码。

    错误码（ERR-xxxx）才是 BM25 的典型强项：字面完全匹配即可命中。
    版本号不算——实测「升级 v1.3.0 后被强制退出登录」这类问题本质是语义化提问，
    把它算进 lexical 会让「稀疏通道强项」的结论失真。
    """
    return bool(_ERR_CODE.search(question))


#: 问题里引用的错误码（比 `_ERR_CODE` 宽，允许非纯数字后缀）
_ERROR_CODE_REF = re.compile(r"ERR-[A-Za-z0-9\-]+", re.IGNORECASE)


def _has_invented_error_code(question: str, content: str) -> bool:
    """问题里出现的错误码必须在该片段中真实存在。

    这是实测踩出来的坑：模型出题时会**编造错误码**
    （`ERR-EXPIRE-NOTICE`、`ERR-PWD-LEN`、`ERR-VPN-DIVERT` … 实测 144 条里有 11 条），
    而「答案要点必须是原话」这道闸挡不住它 —— 答案确实是原话，
    但问题指向了知识库里根本不存在的标识。

    后果很隐蔽：系统自然检索不到，于是被记成一次「检索失败」，
    实际是**评估集本身的标注错误**。

    **只校验错误码，不校验版本号**：错误码是离散标识，编造即错；
    而版本号是连续区间，问「v5.1 能用吗」而原文写「v5.0.1 至 v5.2.0」属于合理简写，
    一并拦截会误杀正常样本（实测就有这类误判）。
    """
    normalized_content = _normalize(content)
    return any(
        _normalize(token) not in normalized_content for token in _ERROR_CODE_REF.findall(question)
    )


def _looks_answerable(question: str) -> bool:
    """负样本反向校验：命中知识库领域词说明它其实可回答。"""
    if _ERR_CODE.search(question) or _VERSION.search(question):
        return True
    return any(term.lower() in question.lower() for term in _DOMAIN_TERMS)


def load_chunks(limit: int | None = None, offset: int = 0) -> list[dict]:
    """读取知识库全部片段。"""
    client = get_client()
    rows = client.query(
        collection_name=get_settings().milvus_collection,
        filter='chunk_id != ""',
        output_fields=["chunk_id", "doc_id", "source", "title", "content"],
        limit=1000,
    )
    rows.sort(key=lambda r: r["chunk_id"])
    return rows[offset : offset + limit] if limit else rows[offset:]


def load_existing(path: Path) -> list[dict]:
    """读取已有评估集（可能是手工样本，也可能含之前生成的）。"""
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def audit_items(items: list[dict], contents: dict[str, str]) -> list[dict]:
    """找出「问题含知识库不存在的标识」的样本。

    用于清理历史样本：生成时没这道闸，早期数据里可能混入了编造错误码的问题。
    """
    bad: list[dict] = []
    for item in items:
        if item.get("type") == "negative":
            continue
        content = "".join(contents.get(cid, "") for cid in item.get("expected_chunk_ids") or [])
        if content and _has_invented_error_code(item["question"], content):
            bad.append(item)
    return bad


def generate_positives(
    chunks: list[dict],
    *,
    per_chunk: int,
    target: int,
    model: str | None,
    counts: dict[str, int],
    style_offset: int = 0,
) -> list[dict]:
    """逐片段生成正样本；返回通过校验的样本。

    :param counts: 各片段已生成的条数；达到 `per_chunk` 的片段跳过（支持分批续跑）。
    :param style_offset: 出题风格轮换的偏移量，第二轮换个风格避免与第一轮重复。
    """
    items: list[dict] = []
    seen_questions: set[str] = set()

    for index, chunk in enumerate(chunks):
        if counts.get(chunk["chunk_id"], 0) >= per_chunk:
            continue

        style_hint = STYLE_HINTS[(index + style_offset) % len(STYLE_HINTS)]
        prompt = POSITIVE_TEMPLATE.format(
            doc_id=chunk["doc_id"],
            content=chunk["content"][:1200],
            n=per_chunk,
            style_hint=style_hint,
        )

        try:
            raw = generate(SYSTEM_PROMPT, prompt, model=model)
        except Exception as exc:  # noqa: BLE001 - 单段失败不影响整体
            logger.warning("片段 %s 生成失败，跳过: %s", chunk["chunk_id"], exc)
            continue

        seen_answers: set[str] = set()
        for candidate in _extract_json_array(raw):
            question = str(candidate.get("q", "")).strip()
            answer = str(candidate.get("a", "")).strip()

            if len(question) < _MIN_QUESTION_CHARS or not answer:
                continue
            # 核心校验：答案必须是片段原话，否则说明问题可能超出片段范围
            if _normalize(answer) not in _normalize(chunk["content"]):
                logger.warning(
                    "答案非片段原话，丢弃（%s）: %s", chunk["chunk_id"], question[:30]
                )
                continue
            # 问题里不能出现片段中没有的错误码（否则是模型编造的标识）
            if _has_invented_error_code(question, chunk["content"]):
                logger.warning(
                    "问题含编造错误码，丢弃（%s）: %s", chunk["chunk_id"], question[:40]
                )
                continue
            # 同片段内答案相同 = 换皮问同一件事，只保留一条
            answer_key = _normalize(answer)
            if answer_key in seen_answers:
                logger.info("同片段答案重复，跳过（%s）: %s", chunk["chunk_id"], question[:30])
                continue

            key = _normalize(question)
            if key in seen_questions:
                continue

            seen_answers.add(answer_key)
            seen_questions.add(key)

            items.append(
                {
                    "id": "",  # 落盘时统一编号
                    "type": "lexical" if _is_lexical(question) else "semantic",
                    "question": question,
                    "reference_answer": answer,
                    "expected_chunk_ids": [chunk["chunk_id"]],
                    "source": "synthetic",
                }
            )
            if len(items) >= target:
                return items

    return items


def generate_negatives(count: int, *, model: str | None) -> list[dict]:
    """生成「知识库范围外」的负样本（用于测拒答率）。"""
    items: list[dict] = []
    seen: set[str] = set()

    while len(items) < count:
        batch = min(10, count - len(items))
        prompt = NEGATIVE_TEMPLATE.format(n=batch)

        try:
            raw = generate(SYSTEM_PROMPT, prompt, model=model)
        except Exception as exc:  # noqa: BLE001
            logger.warning("负样本生成失败: %s", exc)
            break

        candidates = _extract_json_array(raw)
        if not candidates:
            break  # 模型没产出可解析内容，停止避免死循环

        for candidate in candidates:
            question = str(candidate.get("q", "")).strip()
            answer = str(candidate.get("a", "")).strip()

            if len(question) < _MIN_QUESTION_CHARS:
                continue
            if _looks_answerable(question):
                logger.warning("负样本命中知识库领域，剔除: %s", question[:30])
                continue

            key = _normalize(question)
            if key in seen:
                continue
            seen.add(key)

            items.append(
                {
                    "id": "",
                    "type": "negative",
                    "question": question,
                    "reference_answer": answer or "知识库中没有相关资料，应回答「根据现有资料无法确定」并建议转人工。",
                    "expected_chunk_ids": [],
                    "source": "synthetic",
                }
            )
            if len(items) >= count:
                break

    return items


def write_testset(path: Path, items: list[dict]) -> None:
    """整体重写评估集，并重新编号（手工样本保持 Q/N 前缀，生成样本用 S）。"""
    manual_pos = 1
    manual_neg = 1
    synth = 1

    for item in items:
        if item.get("source") == "synthetic":
            item["id"] = f"S{synth:03d}"
            synth += 1
        elif item.get("type") == "negative":
            item["id"] = f"N{manual_neg:02d}"
            manual_neg += 1
        else:
            item["id"] = f"Q{manual_pos:02d}"
            manual_pos += 1

    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="生成评估集")
    parser.add_argument("--per-chunk", type=int, default=4, help="每个片段生成几条问题")
    parser.add_argument("--target-positives", type=int, default=140, help="合成正样本目标数")
    parser.add_argument("--negatives", type=int, default=0, help="生成负样本条数")
    parser.add_argument("--chunk-limit", type=int, default=None, help="只处理前 N 个片段")
    parser.add_argument("--chunk-offset", type=int, default=0, help="从第 N 个片段开始")
    parser.add_argument("--model", default=None, help="生成用模型（默认用配置）")
    parser.add_argument("--style-offset", type=int, default=0, help="出题风格轮换偏移（第二轮用 1）")
    parser.add_argument("--audit", action="store_true", help="只检查已有样本里编造的标识，不生成")
    parser.add_argument("--prune", action="store_true", help="剔除含编造标识的样本并重新编号")
    parser.add_argument("--reset", action="store_true", help="丢弃已生成的合成样本，重新生成")
    parser.add_argument("--out", default=str(TESTSET_PATH), help="输出文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    out_path = Path(args.out)

    existing = load_existing(out_path)
    manual = [item for item in existing if item.get("source") != "synthetic"]
    synthetic = [] if args.reset else [i for i in existing if i.get("source") == "synthetic"]

    #: 各片段已生成条数（用于续跑时补齐未达标的片段）
    counts: dict[str, int] = {}
    for item in synthetic:
        if item.get("type") != "negative" and item.get("expected_chunk_ids"):
            cid = item["expected_chunk_ids"][0]
            counts[cid] = counts.get(cid, 0) + 1

    current_positives = len([i for i in synthetic if i.get("type") != "negative"])

    chunks = load_chunks(limit=args.chunk_limit, offset=args.chunk_offset)
    print(
        f"已有：手工 {len(manual)} 条 / 合成 {len(synthetic)} 条"
        f"（其中正样本 {current_positives}）· 待处理片段 {len(chunks)} 个"
    )

    if args.audit or args.prune:
        contents = {chunk["chunk_id"]: chunk["content"] for chunk in load_chunks()}
        bad = audit_items(manual + synthetic, contents)
        print(f"\n含编造标识的样本：{len(bad)} 条")
        for item in bad:
            print(f"  {item['id']} [{item['type']}] {item['question'][:50]}")
        if args.prune:
            bad_questions = {item["question"] for item in bad}
            kept = [i for i in manual + synthetic if i["question"] not in bad_questions]
            write_testset(out_path, kept)
            print(f"已剔除，剩余 {len(kept)} 条")
        return

    if args.negatives:
        new_items = generate_negatives(args.negatives, model=args.model)
        print(f"新增负样本：{len(new_items)} 条")
    else:
        remaining = max(args.target_positives - current_positives, 0)
        if remaining == 0:
            print("正样本已达目标，跳过")
            new_items = []
        else:
            new_items = generate_positives(
                chunks,
                per_chunk=args.per_chunk,
                target=remaining,
                model=args.model,
                counts=counts,
                style_offset=args.style_offset,
            )
            print(f"新增正样本：{len(new_items)} 条")

    all_items = manual + synthetic + new_items
    write_testset(out_path, all_items)

    positives = [i for i in all_items if i.get("type") != "negative"]
    negatives = [i for i in all_items if i.get("type") == "negative"]
    print(f"\n写入 {out_path}：共 {len(all_items)} 条（正样本 {len(positives)} / 负样本 {len(negatives)}）")


if __name__ == "__main__":
    main()

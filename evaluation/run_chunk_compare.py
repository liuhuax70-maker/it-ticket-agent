"""切分常数对比（离线，不需要 Milvus / Ollama）。

要点文档 1.3 / 3.1：`chunk_size=400`、`overlap=50` 这类常数必须能追溯到实验；
overlap 在结构化切分下通常**不需要**，反而会引入重复内容，而重复会强化模型的错误确信。

本脚本对 `chunk_size × overlap` 组合离线跑切分（不花 embedding 费用），对比：

| 指标 | 含义 |
| --- | --- |
| 块数 | 切分粒度粗不粗 |
| 长度均值 / 标准差 | 块长度是否齐平（不齐平会带偏评测指标） |
| **重复句占比** | 同一句话出现在多个 chunk 里 —— overlap 的直接代价 |
| 路径覆盖率 | 块自解释（带标题层级路径）的比例 |

用法：
    python -m evaluation.run_chunk_compare
    python -m evaluation.run_chunk_compare --sizes 300,400,500 --overlaps 0,50
"""

import argparse
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

from app.core.logging import get_logger
from evaluation.common import ensure_report_dir
from ingestion.chunk import estimate_tokens, split_document

logger = get_logger(__name__)

KB_PATH = Path(__file__).resolve().parent.parent / "knowledge_base"
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _load_docs() -> list[tuple[str, str]]:
    """加载知识库里的 Markdown 文档（工单 JSONL 结构简单，不参与切分对比）。"""
    docs: list[tuple[str, str]] = []
    for sub in ("manual", "faq"):
        directory = KB_PATH / sub
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            docs.append((f"{sub}-{path.stem}", path.read_text(encoding="utf-8")))
    return docs


def _duplicate_sentence_ratio(chunks) -> float:
    """重复句占比：出现在 ≥2 个 chunk 中的句子 / 总句子数。"""
    counter: dict[str, int] = {}
    total = 0
    for chunk in chunks:
        for sentence in _SENT_SPLIT.split(chunk.content):
            normalized = re.sub(r"\s+", "", sentence)
            if len(normalized) < 6:  # 忽略过短片段，避免噪声
                continue
            counter[normalized] = counter.get(normalized, 0) + 1
            total += 1
    if not total:
        return 0.0
    duplicated = sum(count for count in counter.values() if count > 1)
    return round(duplicated / total, 4)


def compare(sizes: tuple[int, ...], overlaps: tuple[int, ...]) -> list[dict]:
    """对每个 (chunk_size, overlap) 组合统计切分质量。"""
    docs = _load_docs()
    results: list[dict] = []

    for size in sizes:
        for overlap in overlaps:
            if overlap >= size:
                continue

            all_chunks = []
            for doc_id, text in docs:
                all_chunks.extend(split_document(doc_id, text, chunk_size=size, overlap=overlap))

            lengths = [estimate_tokens(chunk.content) for chunk in all_chunks]
            with_path = sum(1 for chunk in all_chunks if chunk.heading_path)
            results.append(
                {
                    "chunk_size": size,
                    "overlap": overlap,
                    "chunks": len(all_chunks),
                    "tokens_mean": round(statistics.mean(lengths), 1) if lengths else 0,
                    "tokens_std": round(statistics.pstdev(lengths), 1) if len(lengths) > 1 else 0,
                    "tokens_min": min(lengths, default=0),
                    "tokens_max": max(lengths, default=0),
                    "duplicate_sentence_ratio": _duplicate_sentence_ratio(all_chunks),
                    "heading_path_coverage": round(with_path / len(all_chunks), 4) if all_chunks else 0.0,
                }
            )
            logger.info("chunk_size=%d overlap=%d 完成", size, overlap)

    return results


def print_report(results: list[dict]) -> None:
    print("\n=== 切分常数对比（离线）===")
    header = (
        f"  {'size':>5} {'overlap':>7} {'块数':>5} {'均值':>7} {'标准差':>7} "
        f"{'最小':>5} {'最大':>5} {'重复句占比':>10} {'路径覆盖':>8}"
    )
    print(header)
    for row in results:
        print(
            f"  {row['chunk_size']:>5} {row['overlap']:>7} {row['chunks']:>5} "
            f"{row['tokens_mean']:>7} {row['tokens_std']:>7} {row['tokens_min']:>5} {row['tokens_max']:>5} "
            f"{row['duplicate_sentence_ratio']:>10.2%} {row['heading_path_coverage']:>8.2%}"
        )
    print("\n  判读：重复句占比越低越好（overlap 的直接代价）；标准差越小说明块越齐平")


def main() -> None:
    parser = argparse.ArgumentParser(description="切分常数对比")
    parser.add_argument("--sizes", default="300,400,500", help="逗号分隔的 chunk_size 列表")
    parser.add_argument("--overlaps", default="0,50", help="逗号分隔的 overlap 列表")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    sizes = tuple(int(p) for p in args.sizes.split(",") if p.strip())
    overlaps = tuple(int(p) for p in args.overlaps.split(",") if p.strip())

    results = compare(sizes, overlaps)
    print_report(results)

    if not args.no_report:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "sizes": list(sizes),
            "overlaps": list(overlaps),
            "results": results,
        }
        path = ensure_report_dir() / f"chunk_compare_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {path}")


if __name__ == "__main__":
    main()

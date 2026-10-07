"""跑全部测试。

为什么需要一个脚本而不是直接 ``pytest``：每个服务都有一个叫 ``app`` 的顶层包，
在同一个 pytest 会话里 ``app`` 只会绑定到最先导入的那个服务，
因此必须**按服务分进程**执行；仓库根测试（packages/*）单独跑一轮。

用法：
    python scripts/test_all.py            # 全部
    python scripts/test_all.py retrieval  # 只跑名字含 retrieval 的
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

# 顺序无所谓，但固定下来便于对照输出
TARGETS: list[tuple[str, str]] = [
    ("packages/tests", "共享库"),
    ("apps/api-gateway/tests", "api-gateway"),
    ("services/model-gateway/tests", "model-gateway"),
    ("services/ingestion/tests", "ingestion"),
    ("services/indexing/tests", "indexing"),
    ("services/retrieval/tests", "retrieval"),
    ("services/query-orchestrator/tests", "query-orchestrator"),
    ("services/eval/tests", "eval"),
    ("services/feedback/tests", "feedback"),
    ("services/authz/tests", "authz"),
]


def main(argv: list[str]) -> int:
    keyword = argv[1].lower() if len(argv) > 1 else ""
    targets = [t for t in TARGETS if keyword in t[0].lower()] if keyword else TARGETS
    if not targets:
        print(f"没有匹配 {keyword!r} 的测试目录")
        return 1

    failures: list[str] = []
    for path, label in targets:
        if not (ROOT / path).exists():
            print(f"[跳过] {label:<20} 目录不存在: {path}")
            continue
        print(f"\n=== {label} ({path}) ===", flush=True)
        result = subprocess.run(
            [PYTHON, "-m", "pytest", path, "-q"],
            cwd=ROOT,
            text=True,
        )
        if result.returncode != 0:
            failures.append(label)

    print("\n" + "=" * 48)
    if failures:
        print(f"失败: {', '.join(failures)}")
        return 1
    print(f"全部通过（{len(targets)} 个测试目录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

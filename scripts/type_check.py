"""类型检查跑批：逐模块调用 mypy。

为什么必须**逐模块**跑，而不能 `mypy packages services apps` 一次跑：
    每个服务都有自己的顶层 ``app`` 包（``services/*/app``），mypy 解析
    ``from app.config import Settings`` 时无法判断指的是哪个服务，会直接报
    "Found 2 implementations of app"。测试那边是同一个原因（见 scripts/test_all.py），
    所以这里保持一致的跑法。

用法：
    python scripts/type_check.py# 全部模块
    python scripts/type_check.py packages       # 只查指定模块
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (显示名, 路径) —— 顺序与 test_all.py 保持一致
MODULES: list[tuple[str, str]] = [
    ("packages（共享库）", "packages"),
    ("services/query-orchestrator", "services/query-orchestrator"),
    ("services/retrieval", "services/retrieval"),
    ("services/ingestion", "services/ingestion"),
    ("services/indexing", "services/indexing"),
    ("services/model-gateway", "services/model-gateway"),
    ("services/eval", "services/eval"),
    ("services/feedback", "services/feedback"),
    ("services/authz", "services/authz"),
    ("apps/api-gateway", "apps/api-gateway"),
]

# 第三方库缺少类型存根时忽略：我们要检查的是**自己**的类型关系，
# 而不是替第三方库补 stub。
MYPY_FLAGS = ["--ignore-missing-imports", "--no-error-summary"]


def check(path: str) -> tuple[bool, str]:
    """对单个模块跑 mypy，返回是否通过及（最多 10 条）错误摘要。

    只截取 ``: error:`` 行做摘要：警告与进度噪声不计入失败判定，
    但保留退出码作为兜底失败依据。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", path, *MYPY_FLAGS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout or "").strip()
    errors = [line for line in output.splitlines() if ": error:" in line]
    if proc.returncode == 0:
        return True, "ok"
    detail = "\n    ".join(errors[:10])
    if len(errors) > 10:
        detail += f"\n    …… 另有 {len(errors) - 10} 条"
    return False, detail or (output or f"退出码 {proc.returncode}")


def main() -> int:
    """逐模块跑 mypy（关键词过滤），任一模块失败即非零退出。"""
    keywords = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    targets = [
        (label, path)
        for label, path in MODULES
        if not keywords or any(keyword in path or keyword in label for keyword in keywords)
    ]
    if not targets:
        print(f"没有匹配的模块：{keywords}")
        return 1

    failures: list[str] = []
    for label, path in targets:
        ok, detail = check(path)
        print(f"=== {label} ===")
        print(f"  {detail}")
        if not ok:
            failures.append(label)

    print("\n" + "=" * 60)
    if failures:
        print(f"类型检查未通过（{len(failures)}/{len(targets)} 个模块）：{', '.join(failures)}")
        return 1
    print(f"类型检查通过（{len(targets)} 个模块）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

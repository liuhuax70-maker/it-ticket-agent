"""本地一键启动全部服务（前台运行，Ctrl+C 全部退出）。

等价于在 6 个终端里分别执行 Makefile 的 api/gateway/orchestrator/retrieval/
ingestion/indexing。适合本地联调；生产用 K8s（infra/k8s）。

用法：
    python scripts/dev_services.py               # 启动全部
    python scripts/dev_services.py api retrieval # 只启动名字匹配的服务
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (名称, 代码目录, 端口)
SERVICES: list[tuple[str, str, int]] = [
    ("model-gateway", "services/model-gateway", 8003),
    ("indexing", "services/indexing", 8005),
    ("retrieval", "services/retrieval", 8002),
    ("ingestion", "services/ingestion", 8004),
    ("query-orchestrator", "services/query-orchestrator", 8001),
    ("api-gateway", "apps/api-gateway", 8000),
    ("eval", "services/eval", 8006),
]


def main(argv: list[str]) -> int:
    """前台逐个拉起选中的服务；任一退出或收到 SIGINT/SIGTERM 则全部退出。

    用 SIGINT/SIGTERM 联动是为了本地联调时单个服务崩了不会留下孤儿进程；
    终端 Ctrl+C 会一次性把 6 个 uvicorn 都带下去。
    """
    keywords = [a.lower() for a in argv[1:] if not a.startswith("-")]
    selected = [s for s in SERVICES if not keywords or any(k in s[0] for k in keywords)]
    if not selected:
        print(f"没有匹配 {keywords} 的服务")
        return 1

    env = dict(os.environ)
    # 服务以 packages.* 绝对导入共享库，必须把仓库根放进 PYTHONPATH
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    processes: list[tuple[str, subprocess.Popen]] = []
    for name, app_dir, port in selected:
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(ROOT / app_dir),
            "--port",
            str(port),
            "--host",
            "0.0.0.0",
        ]
        print(f"[dev] 启动 {name:<18} :{port}")
        processes.append((name, subprocess.Popen(cmd, cwd=ROOT, env=env)))

    def shutdown(*_: object) -> None:
        print("\n[dev] 正在停止全部服务…")
        for _, proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for name, proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"[dev] {name} 未按时退出，强制结束")
                proc.kill()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("[dev] 全部服务已启动，Ctrl+C 退出\n")
    try:
        while True:
            time.sleep(1)
            for name, proc in processes:
                if proc.poll() is not None:
                    print(f"[dev] {name} 已退出（code={proc.returncode}），一并停止其余服务")
                    shutdown()
    except KeyboardInterrupt:  # pragma: no cover
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""应用 Postgres 迁移（Alembic upgrade head）。

用法：
    python scripts/migrate.py            # 升到最新
    python scripts/migrate.py --downgrade 0001_init   # 指定版本
    python scripts/migrate.py --sql      # 只打印 SQL，不执行（离线模式）
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+asyncpg://rag:rag@localhost:5432/rag"


def build_config(sql_only: bool = False) -> Config:
    """组装 alembic 配置。

    ⚠️ ``DATABASE_URL`` 的默认来源是**代码里的 DEFAULT_URL**（含开发口令 rag:rag），
    仅当环境变量未设置时使用。所以准确的说法是"优先读环境变量，兜底用本地开发口令"，
    而不是"不携带口令"——生产环境必须显式提供 ``DATABASE_URL``。

    ``sql_only`` 参数目前**无效**：两个分支返回同一个对象（历史遗留）。
    调用方按需自行处理，无需依赖它。
    """
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations" / "postgres"))
    os.environ.setdefault("DATABASE_URL", DEFAULT_URL)
    if sql_only:
        return config
    return config


def main() -> int:
    """解析 CLI 参数并执行 Alembic 升级/降级。

    ``--sql`` 走离线模式只打印 SQL（CI 评审迁移用），不落库；
    其余情况直接对 ``DATABASE_URL`` 指向的库执行，失败即非零退出。
    """
    parser = argparse.ArgumentParser(description="Postgres 迁移")
    parser.add_argument("--revision", default="head", help="目标版本（默认 head）")
    parser.add_argument("--downgrade", action="store_true", help="执行降级")
    parser.add_argument("--sql", action="store_true", help="离线模式，只打印 SQL")
    args = parser.parse_args()

    config = build_config(args.sql)
    url = os.environ.get("DATABASE_URL", DEFAULT_URL)
    print(f"目标数据库: {url}")
    print(f"执行: {'downgrade' if args.downgrade else 'upgrade'} {args.revision}")

    if args.sql:
        command.upgrade(config, args.revision, sql=True)
        return 0
    if args.downgrade:
        command.downgrade(config, args.revision)
    else:
        command.upgrade(config, args.revision)
    print("迁移完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

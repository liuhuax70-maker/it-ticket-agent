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
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations" / "postgres"))
    # 连接串只从环境变量读，迁移脚本不携带口令
    os.environ.setdefault("DATABASE_URL", DEFAULT_URL)
    if sql_only:
        return config
    return config


def main() -> int:
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

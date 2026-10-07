-- =============================================================
-- Postgres 初始化（仅在数据卷首次创建时执行）
--   * 创建 Langfuse 独立库（Langfuse 自托管需要）
--   * 业务表结构由 Alembic 迁移创建：scripts/migrate.py
-- =============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Langfuse 使用独立数据库，避免与其内部表混淆
SELECT 'CREATE DATABASE langfuse OWNER rag'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec

SHELL := /bin/bash
PY ?= python

.DEFAULT_GOAL := help

.PHONY: help install infra-up infra-full infra-down infra-ps migrate seed index api gateway orchestrator retrieval ingestion indexing model-gateway run-all verify test lint fmt

help: ## 列出所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## 安装依赖（含 dev）
	$(PY) -m pip install -e ".[dev]"

infra-up: ## 启动核心依赖（Postgres/Redis/OpenSearch/Milvus）
	docker compose up -d

infra-full: ## 启动全部依赖（叠加 Kafka/Keycloak/OPA/Langfuse）
	docker compose --profile streaming --profile authz --profile observability up -d

infra-down: ## 停止全部依赖
	docker compose --profile "*" down

infra-ps: ## 查看依赖容器状态
	docker compose --profile "*" ps

migrate: ## 应用 Postgres 迁移
	$(PY) scripts/migrate.py

seed: ## 导入语料并建立索引（调用 ingestion -> indexing）
	$(PY) scripts/seed.py

verify: ## 端到端闭环验收
	$(PY) scripts/verify_loop.py

run-all: ## 本地一键启动全部服务（前台，Ctrl+C 退出）
	$(PY) scripts/dev_services.py

api: ## 启动 api-gateway (:8000)
	$(PY) -m uvicorn app.main:app --app-dir apps/api-gateway --port 8000 --reload

gateway: ## 启动 model-gateway (:8003)
	$(PY) -m uvicorn app.main:app --app-dir services/model-gateway --port 8003 --reload

orchestrator: ## 启动 query-orchestrator (:8001)
	$(PY) -m uvicorn app.main:app --app-dir services/query-orchestrator --port 8001 --reload

retrieval: ## 启动 retrieval (:8002)
	$(PY) -m uvicorn app.main:app --app-dir services/retrieval --port 8002 --reload

ingestion: ## 启动 ingestion (:8004)
	$(PY) -m uvicorn app.main:app --app-dir services/ingestion --port 8004 --reload

indexing: ## 启动 indexing (:8005)
	$(PY) -m uvicorn app.main:app --app-dir services/indexing --port 8005 --reload

test: ## 运行全部测试（按服务分进程）
	$(PY) scripts/test_all.py

test-packages: ## 只跑共享库测试
	$(PY) -m pytest packages/tests -q

lint: ## 静态检查
	ruff check .

fmt: ## 自动格式化
	ruff format . && ruff check --fix .

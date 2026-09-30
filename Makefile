.PHONY: help up down logs pull-model ingest test reset run

COMPOSE := docker compose -f deploy/docker-compose.yml

help:
	@echo "up         - 构建并启动全部服务"
	@echo "down       - 停止服务（保留数据卷）"
	@echo "logs       - 跟踪日志"
	@echo "pull-model - 拉取 Ollama 模型"
	@echo "ingest     - 导入知识库"
	@echo "test       - 运行测试"
	@echo "run        - 本地直接启动 API（不依赖容器）"
	@echo "reset      - 停止并删除数据卷（危险）"

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

pull-model:
	$(COMPOSE) exec ollama ollama pull qwen2.5:7b

ingest:
	$(COMPOSE) exec api python -m ingestion.ingest

test:
	pytest -q

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

reset:
	$(COMPOSE) down -v

# =============================================================
# 单一镜像承载全部 Python 服务。
#
# 取舍：9 个服务的依赖完全一致（同一个 pyproject），分开构建 9 个镜像只会得到
# 9 份几乎相同的层；用 SERVICE_DIR 环境变量决定启动哪个服务，
# 既能共享镜像缓存，也便于运维只维护一个镜像仓库标签。
# 若将来某服务引入差异极大的依赖（如 torch），再单独拆一个 Dockerfile 即可。
#
# 构建：
#   docker build -f infra/docker/Dockerfile -t rag:0.1.0 .
# 运行：
#   docker run -e SERVICE_DIR=services/retrieval -e PORT=8002 -p 8002:8002 rag:0.1.0
# =============================================================
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini 处理信号转发，避免 uvicorn 收不到 SIGTERM 导致优雅停机失效
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再拷代码：代码改动不会让依赖层缓存失效
COPY pyproject.toml README.md ./
COPY packages ./packages
RUN pip install --no-cache-dir .

# 运行期需要的配置与代码
COPY configs ./configs
COPY apps ./apps
COPY services ./services

# 非 root 运行（配合 K8s 的 runAsNonRoot）
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# 模型缓存目录（fastembed / HF）挂 PVC 可避免每次启动重新下载
ENV HF_HOME=/home/appuser/.cache/huggingface

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "uvicorn app.main:app --app-dir ${SERVICE_DIR} --host 0.0.0.0 --port ${PORT:-8000}"]

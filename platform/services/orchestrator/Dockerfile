# FORGE Orchestrator 镜像
# 说明：不用 root 跑；依赖装完即删缓存；健康检查直接打 /healthz（含 DB 探活）。
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq 供 psycopg 使用；curl 供健康检查
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd -r -u 10001 -m forge \
 && chown -R forge:forge /app
USER forge

EXPOSE 8300

HEALTHCHECK --interval=15s --timeout=4s --start-period=25s --retries=4 \
  CMD curl -fsS http://127.0.0.1:8300/healthz || exit 1

# 容器内为 Linux，事件循环无 Windows 的 Proactor 问题，
# 但仍统一走 app.serve，避免两套启动路径产生行为差异。
CMD ["python", "-m", "app.serve"]

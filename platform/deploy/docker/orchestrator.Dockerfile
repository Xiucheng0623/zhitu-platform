# ============================================================================
# FORGE · 业务中间层（orchestrator）镜像
# ----------------------------------------------------------------------------
# 构建上下文是部署包根目录，源码在 src/orchestrator/。
# 不用 root 跑；依赖装完即删缓存；健康检查直接打 /healthz（含 DB 探活）。
# ============================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq 供 psycopg 使用；curl 供健康检查
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

# 国内服务器默认走阿里云 PyPI 镜像（官方源在部分网络下会下载到一半静默中断，
# 是本机实测过的坑）；海外部署在 .env 里改回 https://pypi.org/simple/
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
COPY src/orchestrator/requirements.txt ./
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

COPY src/orchestrator/app ./app

RUN useradd -r -u 10001 -m forge \
 && chown -R forge:forge /app
USER forge

EXPOSE 8300

HEALTHCHECK --interval=15s --timeout=4s --start-period=25s --retries=4 \
  CMD curl -fsS http://127.0.0.1:8300/healthz || exit 1

# 容器内为 Linux，事件循环无 Windows 的 Proactor 问题，
# 但仍统一走 app.serve，避免两套启动路径产生行为差异。
CMD ["python", "-m", "app.serve"]

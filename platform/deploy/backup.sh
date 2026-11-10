#!/usr/bin/env bash
# ============================================================================
# FORGE 一键备份：两个 PostgreSQL 库 + LS 上传文件 + MinIO 对象 + .env
# ----------------------------------------------------------------------------
# 产出目录：backups/forge-<时间戳>/
#   forge.sql          平台业务库（--clean --if-exists，可直接覆盖恢复）
#   labelstudio.sql    LS 库
#   ls_data.tgz        LS 上传文件卷（forge_ls_data）
#   minio_data.tgz     MinIO 对象卷（forge_minio_data）
#   .env.bak           当期配置（权限 600，含密钥，妥善保管）
#
# 卷名前缀 forge_ 来自 docker-compose.yml 顶部的 name: forge，
# 改了那个 name 这里的卷名也要跟着改。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "[X] 缺少 .env"; exit 1; }
get_kv() { sed -n "s|^${1}=||p" .env | head -1; }
DB_ADMIN="$(get_kv ORCH_DB_ADMIN)"; DB_ADMIN="${DB_ADMIN:-forge_admin}"

TS="$(date +%Y%m%d-%H%M%S)"
OUT="backups/forge-${TS}"
mkdir -p "$OUT"

echo "[1/4] 平台业务库 forge → ${OUT}/forge.sql"
docker compose exec -T orchdb pg_dump -U "$DB_ADMIN" --clean --if-exists forge > "$OUT/forge.sql"

echo "[2/4] Label Studio 库 → ${OUT}/labelstudio.sql"
docker compose exec -T lsdb pg_dump -U ls --clean --if-exists labelstudio > "$OUT/labelstudio.sql"

echo "[3/4] LS 上传文件卷 → ${OUT}/ls_data.tgz"
docker run --rm -v forge_ls_data:/from:ro -v "$(pwd)/${OUT}":/to alpine:3.20 \
  tar czf /to/ls_data.tgz -C /from .

echo "[4/4] MinIO 对象卷 → ${OUT}/minio_data.tgz"
docker run --rm -v forge_minio_data:/from:ro -v "$(pwd)/${OUT}":/to alpine:3.20 \
  tar czf /to/minio_data.tgz -C /from .

cp .env "$OUT/.env.bak"
chmod 600 "$OUT/.env.bak"

echo
echo "备份完成："
du -sh "$OUT"/*
echo "→ $(pwd)/${OUT}"
echo "含密钥（.env.bak），外传前请加密。恢复演练：./restore.sh ${OUT}"

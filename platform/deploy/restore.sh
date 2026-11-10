#!/usr/bin/env bash
# ============================================================================
# FORGE 一键恢复：用 backup.sh 的产出覆盖当前数据
# ----------------------------------------------------------------------------
# 用法：
#   ./restore.sh backups/forge-20261106-153000                 # 保留当前 .env
#   ./restore.sh backups/forge-20261106-153000 --env=backup    # 连 .env 一起回滚
#
# ⚠️ 破坏性操作：当前两个数据库和文件卷会被备份内容覆盖。
#    所以先要交互确认，且恢复前建议先跑一次 ./backup.sh 留底。
#
# 为什么要有 --env=backup
# ----------------------------------------------------------------------------
# backup.sh 会把当期 .env 存成 .env.bak（权限 600，含密钥）。但数据不是
# 自解释的：LS 库里存的 access token 由当期 LS_SSO_SECRET 签、平台连 LS 用
# 的是当期 LS_REFRESH_TOKEN。跨机恢复时如果只灌数据库、沿用目标机自己的
# .env，结果是「库恢复了、服务也起来了，但平台调不动 LS，SSO 全 401」——
# 症状看起来像代码坏了，实际是密钥对不上。
# 所以这里默认只提醒、不擅自改 .env（改错更危险），但会把不一致逐项列出，
# 并要求显式输入 keep 才继续；要真回滚就加 --env=backup。
#
# 顺序刻意如此：先停写库的服务 → 恢复数据库 → 恢复文件卷 → 再拉起服务。
# 文件卷用一次性 alpine 容器操作，不依赖 minio/labelstudio 镜像里有没有 tar。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

ENV_MODE="keep"
SRC=""
for arg in "$@"; do
  case "$arg" in
    --env=keep|--env=backup) ENV_MODE="${arg#--env=}" ;;
    -h|--help)
      # 打印文件头连续的注释块（不写死行号，改了注释不会漏）
      awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
      exit 0 ;;
    -*) echo "[X] 未知参数: $arg"; exit 1 ;;
    *) SRC="$arg" ;;
  esac
done
[[ -n "$SRC" ]] || { echo "用法: ./restore.sh <备份目录> [--env=keep|--env=backup]"; exit 1; }
[[ -d "$SRC" ]] || { echo "[X] 备份目录不存在: $SRC"; exit 1; }
[[ -f "$SRC/forge.sql" ]] || { echo "[X] $SRC 里没有 forge.sql"; exit 1; }
[[ -f .env ]] || { echo "[X] 缺少 .env"; exit 1; }

get_kv() { sed -n "s|^${1}=||p" .env | head -1; }
ENV_GET() { sed -n "s|^${2}=||p" "$1" | head -1; }
DB_ADMIN="$(get_kv ORCH_DB_ADMIN)"; DB_ADMIN="${DB_ADMIN:-forge_admin}"
SRC_ABS="$(cd "$SRC" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
ENV_BAK="$SRC_ABS/.env.bak"

# 这两个不一致会直接让「服务起来了但用不了」：SSO 签名对不上 → 甲方进不了
# LS；refresh token 对不上 → 平台读不到任务与进度。其余键一并列出以便核对。
SECRET_KEYS=(FORGE_JWT_SECRET ORCH_DB_PASSWORD FORGE_SSO_CLIENT_SECRET
             LS_SSO_SECRET LS_REFRESH_TOKEN LS_WEBHOOK_SECRET
             LS_DB_PASSWORD MINIO_PASSWORD LS_ADMIN_PASSWORD)
drift=()
has_env_bak=0
if [[ -f "$ENV_BAK" ]]; then
  has_env_bak=1
  for k in "${SECRET_KEYS[@]}"; do
    cur="$(ENV_GET .env "$k")"
    old="$(ENV_GET "$ENV_BAK" "$k")"
    # 两边都空不算漂移（该键本来就没配）
    if [[ -n "$cur$old" && "$cur" != "$old" ]]; then drift+=("$k"); fi
  done
fi

echo "[0/5] 检查配置配套性"
if [[ "$has_env_bak" == "0" ]]; then
  echo "      备份里没有 .env.bak（旧备份或人为删除）—— 无法核对密钥是否配套。"
elif [[ "${#drift[@]}" == "0" ]]; then
  echo "      .env 与备份期配置一致，无需处理。"
else
  echo "      当前 .env 与备份期 .env.bak 有 ${#drift[@]} 个键不一致："
  for k in "${drift[@]}"; do echo "        - $k"; done
  echo "      ⚠️  跨机或不配套时，典型症状是：服务都起来了，但平台读不到 LS"
  echo "         任务（refresh token 对不上）、甲方 SSO 进 LS 全 401（SSO secret 对不上）。"
  echo "         处理方式："
  echo "           ./restore.sh $SRC              保持当前 .env（默认；数据恢复后自行核对）"
  echo "           ./restore.sh $SRC --env=backup 连 .env 一起换回备份版本（当前 .env 另存）"
fi

if [[ "$ENV_MODE" == "backup" ]]; then
  [[ "$has_env_bak" == "1" ]] || { echo "[X] 指定了 --env=backup，但 $ENV_BAK 不存在"; exit 1; }
  cp -p .env ".env.pre-restore-${TS}"
  chmod 600 ".env.pre-restore-${TS}"
  cp -p "$ENV_BAK" .env
  chmod 600 .env
  echo "      已把 .env 换成备份版本；原配置存为 .env.pre-restore-${TS}"
  # DB_ADMIN 在换 .env 之前就读好了：恢复数据库要用「能连上当前库」的账号，
  # 不能因为换了配置就换成另一台机器的管理员名。
fi

echo "⚠️  即将用 ${SRC_ABS} 覆盖当前数据库与文件卷，此操作不可逆。"
if [[ "$ENV_MODE" == "keep" && "${#drift[@]}" != "0" ]]; then
  # 提示一律走 echo：read -p 的提示只在交互终端出现，重定向/CI 下会静默
  # 等输入，事后翻日志完全看不出卡在哪一步。
  echo "配置存在上述差异，确认【保持当前 .env】继续？输入 keep 回车确认，其它取消："
  read -r ANS
  [[ "$ANS" == "keep" ]] || { echo "已取消（可用 --env=backup 连配置一起回滚）"; exit 1; }
else
  echo "确认恢复（将覆盖当前数据库与文件卷）？输入 yes 回车确认，其它取消："
  read -r ANS
  [[ "$ANS" == "yes" ]] || { echo "已取消"; exit 1; }
fi

echo "[1/5] 停止写数据的服务……"
docker compose stop orchestrator labelstudio minio-init

echo "[2/5] 恢复平台业务库 forge"
docker compose exec -T orchdb psql -U "$DB_ADMIN" -d forge -v ON_ERROR_STOP=1 \
  < "$SRC_ABS/forge.sql"

echo "[3/5] 恢复 Label Studio 库"
docker compose exec -T lsdb psql -U ls -d labelstudio -v ON_ERROR_STOP=1 \
  < "$SRC_ABS/labelstudio.sql"

if [[ -f "$SRC_ABS/ls_data.tgz" ]]; then
  echo "[4/5] 恢复 LS 上传文件卷"
  docker run --rm -v forge_ls_data:/to -v "$SRC_ABS":/from:ro alpine:3.20 \
    sh -c 'rm -rf /to/* && tar xzf /from/ls_data.tgz -C /to'
else
  echo "[4/5] 备份里没有 ls_data.tgz，跳过文件卷恢复"
fi

if [[ -f "$SRC_ABS/minio_data.tgz" ]]; then
  echo "[5/5] 恢复 MinIO 对象卷"
  docker run --rm -v forge_minio_data:/to -v "$SRC_ABS":/from:ro alpine:3.20 \
    sh -c 'rm -rf /to/* && tar xzf /from/minio_data.tgz -C /to'
else
  echo "[5/5] 备份里没有 minio_data.tgz，跳过对象卷恢复"
fi

echo "拉起服务……"
docker compose start orchestrator labelstudio minio-init

echo
echo "恢复完成。建议立即验证：./smoke.sh"
if [[ "$has_env_bak" == "0" ]]; then
  echo "备份里没有 .env.bak，无法核对密钥。若来自另一台机器，必须手工核对"
  echo ".env 的 LS_SSO_SECRET / LS_REFRESH_TOKEN / FORGE_JWT_SECRET 与新环境配套。"
elif [[ "$ENV_MODE" == "keep" && "${#drift[@]}" != "0" ]]; then
  echo "注意：本次保留了当前 .env，其中 ${#drift[@]} 个键与备份期不一致"
  echo "（${drift[*]}）。若 smoke.sh 出现「平台读不到 LS」或 SSO 401，"
  echo "用 ./restore.sh $SRC --env=backup 重跑一次即可连配置一起回滚。"
else
  echo "配置与数据已配套（.env 与备份期一致或已回滚）。"
fi

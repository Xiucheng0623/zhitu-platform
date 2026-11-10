#!/usr/bin/env bash
# ============================================================================
# FORGE · 智途数据生产平台 —— Linux 服务器一键部署
# ----------------------------------------------------------------------------
# 用法：
#   ./install.sh                                  # 交互式（问手机号，其余自动生成）
#   ./install.sh --phone 138xxxxxxxx              # IP 模式，密码自动生成
#   ./install.sh --domain data.example.cn --phone 138xxxxxxxx --yes
#
# 做八件事：
#   0. 环境体检（preflight.sh：系统/内存/磁盘/docker/端口/SELinux/防火墙/出网）
#   1. 检查 Docker / Compose / 部署包完整性
#   2. 生成 .env（强随机密钥、自动探测对外地址）
#   3. 选 Caddyfile 模板（有域名 → 自动 HTTPS；无域名 → 纯 IP/HTTP）
#   4. docker compose up -d --build（迁移只跑结构 + 引导，绝不灌演示数据）
#   5. 建平台管理员（app.cli create-admin，密码走 scrypt，不进 SQL、不进历史）
#   6. 自动登录 LS 换 API refresh token 写回 .env（否则业务接口每 5 分钟 401）
#   7. 跑 smoke.sh 冒烟，输出访问地址与账号
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

info() { echo "[..] $*"; }
ok()   { echo "[ok] $*"; }
die()  { echo "[X] $*" >&2; exit 1; }

# ---- 参数 ------------------------------------------------------------------
ADMIN_PHONE="${ADMIN_PHONE:-}"
ADMIN_NAME="${ADMIN_NAME:-管理员}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
DOMAIN="${DOMAIN:-}"
YES=0
FORCE_ADMIN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --phone)       ADMIN_PHONE="${2:-}"; shift 2 ;;
    --name)        ADMIN_NAME="${2:-}"; shift 2 ;;
    --password)    ADMIN_PASSWORD="${2:-}"; shift 2 ;;
    --domain)      DOMAIN="${2:-}"; shift 2 ;;
    --yes|-y)      YES=1; shift ;;
    --force-admin) FORCE_ADMIN=1; shift ;;
    -h|--help)     sed -n '2,16p' "$0"; exit 0 ;;
    *)             die "未知参数: $1（--help 看用法）" ;;
  esac
done

# ---- 第 0 步：环境体检 ------------------------------------------------------
# 把「反正后面一定会炸」的条件提前问一遍。install.sh 第 4 步要现场构建镜像，
# 失败形态极难读（OOM 杀进程 / permission denied / address already in use）。
# 有阻断项就直接停，别让人等十几分钟再看英文报错。
# 体检脚本只读，不改系统；跳过用 SKIP_PREFLIGHT=1。
if [[ "${SKIP_PREFLIGHT:-0}" != "1" && -f preflight.sh ]]; then
  bash ./preflight.sh || die "环境体检有阻断项（上面每条的下面都给了修法）。确认要忽略就加 SKIP_PREFLIGHT=1 重跑。"
fi

# ---- 前置检查 --------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "没找到 docker，先装 Docker：https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "需要 Docker Compose V2（docker compose 子命令）"
[[ -f docker-compose.yml ]] || die "请在部署包根目录运行本脚本"
[[ -d src/web && -d src/orchestrator && -d src/labelstudio ]] \
  || die "找不到 src/ 源码目录，部署包不完整（请重新解压）"
command -v python3 >/dev/null 2>&1 || die "需要 python3（获取 LS token 用，标准发行版都有）"
[[ -f caddy/Caddyfile.domain && -f caddy/Caddyfile.ip ]] || die "缺少 caddy/Caddyfile.* 模板"

rand_hex() {
  # openssl 优先；没有就退回 python3 secrets —— 两者都在标准发行版里
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else python3 -c 'import secrets;print(secrets.token_hex(24))'; fi
}

# set_kv KEY VALUE：原地改 .env（存在则替换，不存在则追加）
# 用 sed 的 | 作分隔符：URL 里的 / 不用转义；& 和 \ 是 sed 的特殊字符，先转义
set_kv() {
  local k="$1" v="${2//\\/\\\\}"
  v="${v//&/\\&}"
  if grep -q "^${k}=" .env; then sed -i "s|^${k}=.*|${k}=${v}|" .env
  else echo "${k}=${v}" >> .env; fi
}
get_kv() { sed -n "s|^${1}=||p" .env | head -1; }

# ---- .env ------------------------------------------------------------------
if [[ ! -f .env ]]; then
  info "生成 .env（全部密钥随机生成）……"
  cp .env.example .env
  for k in ORCH_DB_PASSWORD ORCH_APP_PASSWORD LS_DB_PASSWORD LS_SECRET_KEY \
           LS_SSO_SECRET LS_WEBHOOK_SECRET MINIO_PASSWORD LS_ADMIN_PASSWORD; do
    set_kv "$k" "$(rand_hex)"
  done
  # JWT 密钥单独生成 48 字节（96 hex 字符），远超 32 字符下限
  set_kv FORGE_JWT_SECRET "$(python3 -c 'import secrets;print(secrets.token_hex(48))')"

  if [[ -n "$DOMAIN" ]]; then
    set_kv DOMAIN "$DOMAIN"
    set_kv PLATFORM_PUBLIC_URL "https://${DOMAIN}"
    set_kv LS_PUBLIC_URL "https://ls.${DOMAIN}"
    CADDY_TPL=Caddyfile.domain
    info "域名模式：${DOMAIN}（自动签发 HTTPS，要求 DNS 已解析到本机）"
  else
    LAN_IP="${LAN_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
    [[ -n "$LAN_IP" ]] || die "探测不到服务器 IP。请 --domain 指定域名，或设置 LAN_IP=http://x.x.x.x 的 x.x.x.x"
    set_kv PLATFORM_PUBLIC_URL "http://${LAN_IP}"
    set_kv LS_PUBLIC_URL "http://${LAN_IP}:${LS_PORT:-8100}"
    CADDY_TPL=Caddyfile.ip
    info "IP 模式：平台 http://${LAN_IP}，工作台 http://${LAN_IP}:${LS_PORT:-8100}"
  fi
else
  info ".env 已存在，沿用现有配置（要重来请删掉 .env）"
  DOMAIN="$(get_kv DOMAIN)"
  CADDY_TPL=Caddyfile.ip
  [[ -n "$DOMAIN" ]] && CADDY_TPL=Caddyfile.domain
fi

# ---- 管理员 ----------------------------------------------------------------
[[ -n "$ADMIN_PHONE" ]] || ADMIN_PHONE="$(get_kv ADMIN_PHONE)"
if [[ -z "$ADMIN_PHONE" ]]; then
  [[ $YES -eq 1 ]] && die "--yes 非交互模式必须给 --phone"
  read -r -p "平台管理员手机号（即登录名）: " ADMIN_PHONE
fi
[[ -n "$ADMIN_PHONE" ]] || die "手机号不能为空"
set_kv ADMIN_PHONE "$ADMIN_PHONE"
set_kv ADMIN_NAME "$ADMIN_NAME"
if [[ -z "$ADMIN_PASSWORD" ]]; then ADMIN_PASSWORD="$(get_kv ADMIN_PASSWORD)"; fi
if [[ -z "$ADMIN_PASSWORD" ]]; then ADMIN_PASSWORD="$(rand_hex)"; fi
set_kv ADMIN_PASSWORD "$ADMIN_PASSWORD"
chmod 600 .env

# ---- 域名模式的 ACME 联系人 ------------------------------------------------
# 用假邮箱也能签出证书，但到期/异常通知会全部丢掉。显式收一个真实邮箱；
# --yes 非交互模式不阻断（Caddyfile 模板有兜底），只留显著警告让人事后补。
if [[ -n "$DOMAIN" ]]; then
  ACME="$(get_kv ACME_EMAIL)"
  if [[ -z "$ACME" || "$ACME" == *example.cn || "$ACME" == *example.com ]]; then
    if [[ $YES -eq 1 ]]; then
      info "[warn] 未提供真实 ACME_EMAIL，证书通知将收不到；建议设 ACME_EMAIL 后重跑"
    else
      read -r -p "用于 HTTPS 证书通知的真实邮箱: " ACME
      [[ "$ACME" == *@* ]] || die "ACME_EMAIL 必须是真实邮箱"
      set_kv ACME_EMAIL "$ACME"
    fi
  fi
fi

# ---- 入口配置 --------------------------------------------------------------
info "入口模板：caddy/${CADDY_TPL} → caddy/Caddyfile"
cp "caddy/${CADDY_TPL}" caddy/Caddyfile

# ---- 构建启动 --------------------------------------------------------------
info "构建并启动（首次 5~15 分钟，取决于带宽）……"
docker compose up -d --build

# ---- 等待就绪（靠探测，不靠 sleep 猜）-------------------------------------
info "等待中间层就绪……"
for i in $(seq 1 60); do
  if docker compose exec -T orchestrator curl -fsS http://127.0.0.1:8300/healthz >/dev/null 2>&1; then
    ok "orchestrator /healthz"; break
  fi
  [[ $i -eq 60 ]] && die "orchestrator 三分钟没起来，看日志：docker compose logs orchestrator"
  sleep 3
done

LS_PORT_EFF="$(get_kv LS_PORT)"; LS_PORT_EFF="${LS_PORT_EFF:-8100}"
info "等待 Label Studio 就绪……"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${LS_PORT_EFF}/user/login/" >/dev/null 2>&1; then
    ok "Label Studio 登录页"; break
  fi
  [[ $i -eq 60 ]] && die "Label Studio 三分钟没起来，看日志：docker compose logs labelstudio"
  sleep 3
done

# ---- 建管理员（幂等；--force-admin 才重置已存在账号的密码）----------------
info "创建平台管理员 ${ADMIN_PHONE}……"
FORCE_FLAG=""
[[ $FORCE_ADMIN -eq 1 ]] && FORCE_FLAG="--force"
docker compose exec -T orchestrator python -m app.cli create-admin \
  --phone "$ADMIN_PHONE" --name "$ADMIN_NAME" --password "$ADMIN_PASSWORD" $FORCE_FLAG

# ---- LS API token（orchestrator 建项目/导入用；没有它每 5 分钟 401）--------
if [[ -z "$(get_kv LS_REFRESH_TOKEN)" ]]; then
  info "登录 LS 获取 API refresh token……"
  LS_EMAIL="$(get_kv LS_ADMIN_EMAIL)"
  LS_PW="$(get_kv LS_ADMIN_PASSWORD)"
  if REFRESH="$(python3 tools/fetch_ls_refresh.py \
        --ls-url "http://127.0.0.1:${LS_PORT_EFF}" \
        --email "$LS_EMAIL" --password "$LS_PW")"; then
    set_kv LS_REFRESH_TOKEN "$REFRESH"
    ok "LS refresh token 已写入 .env"
    # 环境变量变了必须重建容器才生效
    docker compose up -d orchestrator >/dev/null 2>&1
    sleep 2
  else
    die "自动获取 LS token 失败。看 README「手工获取 LS API token」一节，
       把值填进 .env 的 LS_REFRESH_TOKEN 后重跑 ./install.sh"
  fi
fi

# ---- 冒烟 ------------------------------------------------------------------
info "冒烟检查……"
bash ./smoke.sh || die "冒烟未全过，按上面标记 [X] 的项排查（日志：docker compose logs）"

echo
echo "=============================================================="
echo " 部署完成"
echo "   平台入口:     $(get_kv PLATFORM_PUBLIC_URL)"
echo "   标注工作台:   $(get_kv LS_PUBLIC_URL)"
echo "   （平台里点「进入工作台」会自动单点登录，无需二次输密码）"
echo ""
echo "   管理员手机号: ${ADMIN_PHONE}"
echo "   管理员密码:   ${ADMIN_PASSWORD}"
echo "   LS 管理员:    $(get_kv LS_ADMIN_EMAIL) / $(get_kv LS_ADMIN_PASSWORD)"
echo "=============================================================="
echo "以上密码只显示这一次（也存在 .env，权限 600），请立即妥善保存。"
echo "日常备份：./backup.sh    演练恢复：./restore.sh <备份目录>"

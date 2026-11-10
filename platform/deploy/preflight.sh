#!/usr/bin/env bash
# ============================================================================
# 智途数据生产平台 —— 服务器环境体检（install.sh 的第 0 步）
# ----------------------------------------------------------------------------
# 为什么单独做这一步：
#   install.sh 第 4 步是 `docker compose up -d --build`，会在服务器上现场构建
#   前端（Node）和标注引擎（pip 装 label-studio）。这一步要跑十几分钟，而它
#   的失败形态极难读：内存不够是 Node 被 OOM killer 杀掉、SELinux 挡挂载是
#   caddy 报 permission denied、端口被占是 bind: address already in use、
#   没外网是 registry 超时。全都要翻 docker 日志才看得出来。
#   这里把「反正后面一定会炸」的条件提前问一遍，每条给出可直接粘的命令。
#
# 判定分级：
#   [ok]  通过
#   [!]  警告 —— 能装，但有已知后果，自己决定
#   [X]  阻断 —— 不修就一定失败，脚本退出码 1
#
# 用法：
#   bash preflight.sh            # 独立体检，不改任何东西
#   ./install.sh                 # 会自动先跑一次，有 [X] 就停
#
# 这个脚本只读：不装包、不改内核参数、不开防火墙端口。所有修法都是打印出来
# 让你自己执行 —— 生产服务器上「悄悄改了系统配置」比装不上更糟。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")" 2>/dev/null || true

HARD=0
WARN=0
ok()   { echo "[ok] $*"; }
warn() { echo "[!]  $*"; WARN=$((WARN + 1)); }
bad()  { echo "[X]  $*"; HARD=$((HARD + 1)); }
tip()  { echo "       $*"; }

echo "=================================================================="
echo " 智途 · 服务器环境体检"
echo "=================================================================="

# ---------------------------------------------------------------- 1. 系统
echo
echo "---- 1/9 操作系统与架构 ----"
OS_ID=""; OS_LIKE=""; OS_NAME="unknown"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_ID="${ID:-}"; OS_LIKE="${ID_LIKE:-}"; OS_NAME="${PRETTY_NAME:-${NAME:-unknown}}"
  ok "系统：${OS_NAME}"
  case " ${OS_ID} ${OS_LIKE} " in
    *" debian "*|*" ubuntu "*) FAMILY="debian" ;;
    *" rhel "*|*" fedora "*|*" centos "*) FAMILY="rhel" ;;
    *kylin*|*uos*|*openkylin*|*deepin*) FAMILY="domestic" ;;
    *) FAMILY="other" ;;
  esac
  echo "       发行版族：${FAMILY}（只影响下面几条提示的措辞，不影响安装）"
else
  warn "读不到 /etc/os-release，无法识别发行版"
  FAMILY="other"
fi

ARCH="$(uname -m 2>/dev/null || echo unknown)"
case "$ARCH" in
  x86_64|amd64) ok "架构：${ARCH}" ;;
  aarch64|arm64)
    warn "架构：${ARCH}（ARM64）—— 官方镜像大多有 arm64 版，但 minio/mc 与"
    tip "label-studio 的构建耗时明显更长；如果构建报 exec format error，"
    tip "说明某个镜像只有 amd64 版，需要换镜像或改架构。"
    ;;
  *) warn "架构：${ARCH}（未验证过的架构，镜像可能拉不到）" ;;
esac

if [ -f /.dockerenv ] || grep -qa 'docker\|lxc' /proc/1/cgroup 2>/dev/null; then
  bad "当前环境看起来是容器内部 —— 不要在容器里套娃装平台"
  tip "平台自身就是一组容器，需要在宿主机（或云主机）上执行。"
fi

# ---------------------------------------------------------------- 2. 资源
echo
echo "---- 2/9 内存与磁盘 ----"
if [ -r /proc/meminfo ]; then
  mem_mb=$(awk '/^MemTotal:/{printf "%d", $2/1024}' /proc/meminfo)
  swap_mb=$(awk '/^SwapTotal:/{printf "%d", $2/1024}' /proc/meminfo)
  total_mb=$((mem_mb + swap_mb))
  echo "       物理内存 ${mem_mb} MB / 交换分区 ${swap_mb} MB（合计 ${total_mb} MB）"
  # 口径（别改成"看着差不多就行"）：
  #   运行期稳态：7 个容器合计约 1.2–1.6 GB（LS 最胖，PG×2 次之）
  #   构建期峰值：2.5–3 GB（Node 打包前端 + pip 装 label-studio 同时进行）
  # 所以判据用「内存 + swap 合计」，因为构建期是唯一会真炸的时刻。
  if [ "$total_mb" -lt 3000 ]; then
    bad "内存+swap 合计 ${total_mb} MB，低于构建峰值（约 3 GB）：第 4 步必然失败"
    tip "失败长这样：容器 exit code 137 / Killed —— 看到 137 就是 OOM，不是代码问题。"
    tip "先加 swap（磁盘够就行）："
    tip "  sudo fallocate -l 6G /swapfile && sudo chmod 600 /swapfile"
    tip "  sudo mkswap /swapfile && sudo swapon /swapfile"
    tip "  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab"
    tip "  echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl --system"
    tip "官方建议 4 核 8 GB（README）。2 核 1.7 GB + swap 能装、能演示，"
    tip "但多人同时标注会明显卡（LS 换页），长期别这么用。"
  elif [ "$total_mb" -lt 6000 ]; then
    warn "内存+swap ${total_mb} MB：能装，但接近下限（推荐 8 GB），构建要 30 分钟以上"
    if [ "$swap_mb" -eq 0 ]; then
      tip "建议加 4 GB swap 兜住构建峰值：sudo fallocate -l 4G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile"
    fi
  else
    ok "内存充足（含 swap 共 ${total_mb} MB）"
  fi
  if [ "$mem_mb" -lt 4096 ]; then
    warn "物理内存 ${mem_mb} MB 偏小：运行期 PG×2 + 标注引擎会长期占满，容易触发 OOM"
    tip "可选缓解：把两个库的 shared_buffers 调小（orchdb/lsdb 各 64MB），或把 LS 的库并到同一个实例。"
  fi
fi

DISK_DIR=/var/lib/docker
[ -d "$DISK_DIR" ] || DISK_DIR=/
avail_mb=$(df -Pk "$DISK_DIR" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024}')
if [ -n "${avail_mb:-}" ]; then
  echo "       ${DISK_DIR} 可用 ${avail_mb} MB"
  if [ "$avail_mb" -lt 5120 ]; then
    bad "${DISK_DIR} 可用空间不足 5 GB：镜像 + 数据卷放不下"
    tip "清一下：docker system prune -af，或把 docker 数据目录迁到大分区。"
  elif [ "$avail_mb" -lt 10240 ]; then
    warn "${DISK_DIR} 可用空间不足 10 GB：装完能用，但数据一多就会满"
  else
    ok "磁盘空间充足"
  fi
fi

# ---------------------------------------------------------------- 3. Docker
echo
echo "---- 3/9 Docker / Compose ----"
if ! command -v docker >/dev/null 2>&1; then
  bad "没找到 docker"
  tip "装：curl -fsSL https://get.docker.com | sh"
  tip "国内机器如果拉不动，换源或用云厂商的镜像安装文档。"
else
  ok "docker：$(docker --version 2>/dev/null | head -1)"
  if docker info >/dev/null 2>&1; then
    ok "docker 守护进程可连"
  else
    bad "docker 命令在，但连不上守护进程"
    tip "sudo systemctl start docker；如果报权限拒绝，把当前用户加进 docker 组："
    tip "sudo usermod -aG docker \$USER && newgrp docker"
  fi
  if docker compose version >/dev/null 2>&1; then
    ok "compose：$(docker compose version --short 2>/dev/null | head -1)"
  else
    bad "需要 Docker Compose V2（docker compose 子命令）"
    tip "老版 docker-compose（V1）不支持 service_completed_successfully，"
    tip "迁移容器不会被执行，结果是表没建、全站 500。"
  fi
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled docker >/dev/null 2>&1; then
      ok "docker 开机自启：已启用"
    else
      warn "docker 没设开机自启 —— 服务器重启后整站起不来（容器不会自己回来）"
      tip "sudo systemctl enable docker"
    fi
  fi
fi

# ---------------------------------------------------------------- 3b. 网段
# ★ 腾讯云 / 阿里云的 VPC 常用 172.16.0.0/12 这段，而 Docker 的默认地址池也
#   正好在这段里（docker0 固定 172.17.0.1/16，自建网络从 172.18~172.31 取）。
#   一旦重叠，容器访问内网其它机器会走错路由，症状是「平台能开、但调不通内网服务」，
#   而且不报任何错。这一步就是提前把这个雷标出来。
echo
echo "---- 3b/9 Docker 网段与内网网段是否重叠 ----"
if command -v ip >/dev/null 2>&1; then
  HOST_CIDRS="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}')"
  echo "       本机网段：$(echo "$HOST_CIDRS" | tr '\n' ' ')"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$HOST_CIDRS" <<'PY'
import ipaddress, subprocess, sys

host = []
for line in sys.argv[1].split("\n"):
    line = line.strip()
    if not line:
        continue
    try:
        host.append(ipaddress.ip_network(line, strict=False))
    except ValueError:
        pass

# Docker 默认池：docker0 = 172.17.0.0/16；用户自建网络 172.18~172.31
pools = [ipaddress.ip_network(f"172.{i}.0.0/16") for i in range(17, 32)]
clash = [(h, p) for h in host for p in pools if h.overlaps(p)]

if not clash:
    print("[ok] 本机网段与 Docker 默认地址池不重叠")
    raise SystemExit(0)

for h, p in clash:
    print(f"[!]  本机网段 {h} 与 Docker 默认地址池 {p} 重叠")
print("       症状：容器起得来，但访问内网其它机器走错路由，且不报错。")
print("       修法：第一次装之前先钉死 Docker 网段（三行，粘完重启 docker）：")
print('         sudo mkdir -p /etc/docker && sudo tee /etc/docker/daemon.json >/dev/null <<\'JSON\'')
print('         { "bip": "10.211.0.1/24",')
print('           "default-address-pools": [ { "base": "10.212.0.0/16", "size": 24 } ] }')
print('         JSON')
print("         sudo systemctl restart docker")
print("       为什么选 10.x：VPC 已经占了 172.16/12 的一大段，10.x 与它天然不重叠。")
print("       如果你 VPC 自己就用 10.x，把上面两个地址换成 192.168.211.0/24 这类没被占的段。")

# 已经存在的 docker 网络是否真的撞上了（撞上就不是建议，是故障）
try:
    names = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"],
                           capture_output=True, text=True, timeout=10).stdout.split()
except Exception:
    names = []
live = []
for n in names:
    try:
        out = subprocess.run(["docker", "network", "inspect", n, "-f",
                              "{{range .IPAM.Config}}{{.Subnet}} {{end}}"],
                             capture_output=True, text=True, timeout=10).stdout.split()
    except Exception:
        continue
    for s in out:
        try:
            net = ipaddress.ip_network(s, strict=False)
        except ValueError:
            continue
        if any(net.overlaps(h) for h in host):
            live.append((n, net))
if live:
    for n, net in live:
        print(f"[X]  已有 docker 网络 {n} 的 {net} 与本机网段重叠 —— 现在就是故障状态")
    raise SystemExit(1)
PY
    [ $? -eq 1 ] && HARD=$((HARD + 1))
  else
    # 没有 python3 就退回朴素判断：只看是不是落在 172.17~172.31
    if echo "$HOST_CIDRS" | grep -qE '^172\.(1[7-9]|2[0-9]|3[01])\.'; then
      warn "本机网段落在 Docker 默认地址池（172.17~172.31）里，容器访问内网可能走错路由"
      tip "没有 python3 无法精确判定，建议直接钉死：bip=10.211.0.1/24、default-address-pools=10.212.0.0/16"
    else
      ok "本机网段不在 Docker 默认地址池里"
    fi
  fi
else
  warn "没有 ip 命令，跳过网段重叠检查"
fi

# ---------------------------------------------------------------- 4. python3
echo
echo "---- 4/9 宿主机依赖 ----"
if command -v python3 >/dev/null 2>&1; then
  ok "python3：$(python3 -V 2>&1 | head -1)（第 6 步换 LS 长期凭证要用）"
else
  bad "缺少 python3（install.sh 第 6 步依赖它换取标注引擎长期凭证）"
  tip "Ubuntu/Debian：sudo apt install -y python3"
  tip "RHEL 系：sudo dnf install -y python3"
fi
if command -v curl >/dev/null 2>&1; then
  ok "curl 可用"
else
  warn "没有 curl —— 安装脚本最后一步的连通性检查会跳过"
fi

# ---------------------------------------------------------------- 5. SELinux
echo
echo "---- 5/9 SELinux（RHEL 系专有，其余系统跳过）----"
if command -v getenforce >/dev/null 2>&1; then
  SE="$(getenforce 2>/dev/null || echo Unknown)"
  if [ "$SE" = "Enforcing" ]; then
    warn "SELinux = Enforcing：docker 挂载宿主机文件会被拦"
    tip "本项目的 compose 已对 Caddyfile 挂载加了 :z 标签，正常情况下可用。"
    tip "如果 caddy 容器起不来、日志是 permission denied，临时排查："
    tip "sudo setenforce 0（仅排查用，别长期关）"
  else
    ok "SELinux = ${SE}"
  fi
else
  ok "没有 SELinux（Debian/Ubuntu 系正常）"
fi

# ---------------------------------------------------------------- 6. 防火墙
echo
echo "---- 6/9 防火墙与云安全组 ----"
FW_HINT=0
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
  warn "firewalld 正在运行"
  FW_HINT=1
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  warn "ufw 正在运行"
  FW_HINT=1
else
  ok "本机防火墙未启用 / 未安装"
fi
# 云主机最常见的问题是安全组只开了 22，本机防火墙反而是次要的 —— 这条必须说
echo "       提醒：阿里云/腾讯云/华为云的安全组是独立于本机防火墙的，"
echo "       必须放行 80、443、8100（8100 是标注引擎，标注页跳转会用到）。"
if [ "$FW_HINT" = "1" ]; then
  tip "firewalld：sudo firewall-cmd --permanent --add-service=http --add-service=https &&"
  tip "          sudo firewall-cmd --permanent --add-port=8100/tcp && sudo firewall-cmd --reload"
  tip "ufw：     sudo ufw allow 80,443,8100/tcp"
fi

# ---------------------------------------------------------------- 7. 端口
echo
echo "---- 7/9 端口占用 ----"
# 端口值优先取 .env（第二次安装时可能改过），否则用默认
get_env_port() {
  local key="$1" def="$2" val=""
  if [ -f .env ]; then val="$(sed -n "s|^${key}=||p" .env | head -1)"; fi
  [ -n "$val" ] && echo "$val" || echo "$def"
}
P_HTTP="$(get_env_port HTTP_PORT 80)"
P_HTTPS="$(get_env_port HTTPS_PORT 443)"
P_LS="$(get_env_port LS_PORT 8100)"
PORT_TOOL=""
command -v ss >/dev/null 2>&1 && PORT_TOOL="ss -ltnH"
if [ -z "$PORT_TOOL" ] && command -v netstat >/dev/null 2>&1; then PORT_TOOL="netstat -ltn"; fi
for pair in "HTTP:${P_HTTP}" "HTTPS:${P_HTTPS}" "标注引擎:${P_LS}"; do
  label="${pair%%:*}"; want="${pair#*:}"
  if [ -z "$want" ]; then
    warn "${label} 端口未配置，跳过"
    continue
  fi
  if [ -z "$PORT_TOOL" ]; then
    warn "没有 ss/netstat，跳过端口 ${want} 检查"
    continue
  fi
  if $PORT_TOOL 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${want}\$"; then
    if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":${want}->"; then
      ok "端口 ${want}（${label}）已被本平台的容器占用 —— 重装/升级场景正常"
    else
      bad "端口 ${want}（${label}）已被别的程序占用"
      tip "查是谁：ss -ltnp | grep ':${want}'"
      tip "或改端口：在 .env 里设 HTTP_PORT / HTTPS_PORT / LS_PORT。"
    fi
  else
    ok "端口 ${want}（${label}）空闲"
  fi
done

# ---------------------------------------------------------------- 8. 出网
echo
echo "---- 8/9 出网（构建要拉基础镜像和依赖）----"
if ! command -v curl >/dev/null 2>&1; then
  warn "没有 curl，跳过出网检查（装的时候大概率没问题）"
else
  check_url() {
    local name="$1" url="$2"
    # docker hub 的 /v2/ 正常回 401，说明通了；这里只要有 HTTP 响应就算通。
    # ★ 别写 `... -w '%{http_code}' || echo 000`：curl 失败时自己已经打印了
    #   "000"，再 echo 一次会拼成 "000000"，判空就永远不成立（试跑踩过）。
    local code
    code="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$url" 2>/dev/null)"
    code="${code:-000}"
    case "$code" in ''|*[!0-9]*) code="000" ;; esac
    if [ "$code" != "000" ]; then
      ok "${name} 可达（HTTP ${code}）"
    else
      warn "${name} 不可达 —— 可能需要配代理或换镜像源"
      return 1
    fi
  }
  FAILED=0
  check_url "Docker Hub" "https://registry-1.docker.io/v2/" || FAILED=1
  check_url "npm 镜像" "https://registry.npmmirror.com" || FAILED=1
  check_url "pip 镜像" "https://mirrors.aliyun.com/pypi/simple/" || FAILED=1
  if [ "$FAILED" = "1" ]; then
    bad "出网不全：install.sh 第 4 步要现场构建镜像，拉不到就只能做离线包"
    tip "离线方案：在能出网的机器上 docker save 全部镜像，拷过来 docker load。"
    tip "或者先配好代理：export HTTPS_PROXY=http://...; 再跑 ./install.sh"
  fi
fi

# ---------------------------------------------------------------- 9. 部署包
echo
echo "---- 9/9 部署包完整性 ----"
for f in docker-compose.yml install.sh docker/web.Dockerfile \
         docker/orchestrator.Dockerfile docker/labelstudio.Dockerfile; do
  if [ -f "$f" ]; then ok "存在 ${f}"; else bad "缺少 ${f}（部署包不完整，重新解压）"; fi
done
if [ -d migrations ] && ls migrations/*.sql >/dev/null 2>&1; then
  ok "迁移脚本 $(ls migrations/*.sql | wc -l | tr -d ' ') 个"
else
  bad "migrations/ 下没有 .sql（迁移会建不出表）"
fi

# ---------------------------------------------------------------- 汇总
echo
echo "=================================================================="
if [ "$HARD" -gt 0 ]; then
  echo " 体检结果：${HARD} 项阻断、${WARN} 项警告 —— 先修完阻断项再装"
  echo " 上面每条 [X] 下面都给了可直接粘的命令。"
  echo "=================================================================="
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo " 体检结果：0 项阻断、${WARN} 项警告 —— 可以继续安装"
  echo " 警告项不拦人，但请先看一眼它们会不会影响你（尤其是安全组和开机自启）。"
  echo "=================================================================="
  exit 0
else
  echo " 体检结果：全部通过 —— 直接 ./install.sh"
  echo "=================================================================="
  exit 0
fi
